"""gh #250, broker track: the desktop relay's rpc frame and send step.

In-process cells drive the REAL ``_control_ws`` handler through the ``Ctl``
harness of tests/test_control_lease_248.py and send a real ``/mcp/desktop``
request through ``app.asgi_client`` on the same loop, so the relay, the lease
bump and the holder's socket all interleave exactly as in the broker. The
e2e cell reuses the ``broker`` subprocess fixture of tests/test_mcp_desktop.py.
"""

from __future__ import annotations

import asyncio
import json
from concurrent.futures import ThreadPoolExecutor

import websockets

from webterm import protocol
from tests.test_control_lease_248 import Ctl, CtlWS, _settle
from tests.test_mcp_desktop import (  # noqa: F401  (broker is a fixture)
    ADMIN_AUTH, MCP_TOKEN, _desktop_app, _post, broker)
from tests.test_terminal_control import _until

from .auth_helpers import TEST_TOKEN

_AUTH = {"Authorization": f"Bearer {MCP_TOKEN}"}


class HolderWS(CtlWS):
    """A /control socket that answers every desktop_rpc it is sent (like a
    page), and records the live lease generation at each write."""

    def __init__(self, ctx, answer=True):
        super().__init__()
        self.ctx = ctx
        self.answer = answer
        self.log = []

    async def send(self, data):
        await super().send(data)
        frame = json.loads(data)
        self.log.append((frame.get("type"), self.ctx.lease_generation))
        if self.answer and frame.get("type") == "desktop_rpc":
            self.push({"type": "desktop_result", "rid": frame["rid"],
                       "ok": True, "value": {"viewport": {}, "windows": []}})


def _rpcs(ws):
    return [f for f in ws.sent if f.get("type") == "desktop_rpc"]


def _get_desktop(app):
    return app.asgi_client.get("/mcp/desktop", headers=_AUTH)


# ---- A1: gen + bootId in the frame -----------------------------------------

def test_rpc_frame_with_gen_and_boot_exact():
    """T5: the frame carries gen and bootId when given (the no-gen shape is
    pinned unchanged by tests/test_mcp_desktop.py)."""
    frame = json.loads(protocol.desktop_rpc_frame(
        9, "focus", {"id": 7, "visible": None}, gen=7, boot_id="b"))
    assert frame == {"type": "desktop_rpc", "rid": 9, "op": "focus",
                     "args": {"id": 7, "visible": None},
                     "gen": 7, "bootId": "b"}


def test_relayed_frame_names_the_call_generation_and_run(tmp_path,
                                                         monkeypatch):
    """T1: over a real /control holder, the rpc's gen is the live lease
    generation the call was registered under and bootId the broker run."""
    app = _desktop_app(tmp_path, monkeypatch)
    seen = {}

    async def go():
        c = Ctl(app)
        a = await c.attach("A", HolderWS(app.ctx))
        seen["gen"] = app.ctx.lease_generation
        _, resp = await _get_desktop(app)
        await _settle()
        seen["http"] = resp.status
        seen["rpcs"] = _rpcs(a)
        await c.close_all()
    asyncio.run(go())
    assert seen["http"] == 200
    assert len(seen["rpcs"]) == 1
    rpc = seen["rpcs"][0]
    assert seen["gen"] >= 1
    assert rpc["gen"] == seen["gen"]
    assert rpc["bootId"] == app.ctx.boot_id


def test_e2e_rpc_gen_matches_the_holders_last_status(broker):  # noqa: F811
    """E1: on a real broker, the frame's gen/bootId equal the ones in the
    newest status the holder was sent."""
    port, base, _ = broker

    async def script():
        ws = await websockets.connect(
            f"ws://127.0.0.1:{port}/control?clientId=e1-holder"
            f"&token={TEST_TOKEN}")
        try:
            last = json.loads(await asyncio.wait_for(ws.recv(), 5))
            if not last["active"]:
                await ws.send(json.dumps({"type": "become_active"}))
                last = await _until(ws, lambda f: f.get("type") == "status"
                                    and f["active"] is True)
            with ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(_post, f"{base}/mcp/desktop",
                                     {"op": "get", "args": {}}, ADMIN_AUTH)
                frame = await _until(ws, lambda f: f.get("type")
                                     == "desktop_rpc")
                await ws.send(json.dumps({
                    "type": "desktop_result", "rid": frame["rid"],
                    "ok": True, "value": {"viewport": {}, "windows": []}}))
                return last, frame, future.result(timeout=15)
        finally:
            await ws.close()

    last, frame, (status, _) = asyncio.run(script())
    assert status == 200
    assert isinstance(last["gen"], int) and isinstance(last["bootId"], str)
    assert (frame["gen"], frame["bootId"]) == (last["gen"], last["bootId"])


# ---- A2: the send task re-checks the generation ------------------------------

def _takeover_at_registration(app, monkeypatch):
    """Run one GET /mcp/desktop while holder A is taken over by B at the
    exact point the issue names: B's become_active is queued from inside
    desktop_rpc_frame, which the relay calls synchronously just before it
    registers the call, so B's handler wakes BEFORE the send task that
    wait_for schedules. Returns what happened, plus every asyncio exception
    the loop reported."""
    app.ctx.desktop_rpc_timeout_s = 1.0
    orig = protocol.desktop_rpc_frame
    out = {"loop_errors": []}

    async def go():
        loop = asyncio.get_running_loop()
        loop.set_exception_handler(
            lambda _l, ctx: out["loop_errors"].append(ctx.get("message")))
        c = Ctl(app)
        a = await c.attach("A", HolderWS(app.ctx))
        b = await c.connect("B")
        out["gen0"] = app.ctx.lease_generation

        def hooked(*args, **kw):
            b.push({"type": "become_active"})       # the takeover, NOW
            return orig(*args, **kw)
        monkeypatch.setattr(protocol, "desktop_rpc_frame", hooked)
        try:
            _, resp = await _get_desktop(app)
        finally:
            monkeypatch.setattr(protocol, "desktop_rpc_frame", orig)
        await _settle()
        import gc
        gc.collect()
        await _settle()
        out["http"] = (resp.status, resp.json)
        out["holder"] = app.ctx.active_client_id
        out["gen1"] = app.ctx.lease_generation
        out["a_rpcs"] = _rpcs(a)
        out["a_log"] = list(a.log)
        await c.close_all()
    asyncio.run(go())
    return out


def test_a_takeover_before_the_send_writes_nothing_to_the_deposed_holder(
        tmp_path, monkeypatch):
    """T2 (the #250 P2 probe as a test): the caller gets 409 lost_lease and
    the deposed holder's socket was never sent the desktop_rpc."""
    out = _takeover_at_registration(_desktop_app(tmp_path, monkeypatch),
                                    monkeypatch)
    assert out["holder"] == "B" and out["gen1"] > out["gen0"]
    assert out["http"][0] == 409
    assert out["http"][1]["error"] == "lost_lease"
    assert out["a_rpcs"] == [], out["a_log"]


def test_the_failed_future_is_retrieved(tmp_path, monkeypatch):
    """T3: the bump failed the call's future before the send step refused;
    the relay retrieves it, so asyncio reports nothing."""
    out = _takeover_at_registration(_desktop_app(tmp_path, monkeypatch),
                                    monkeypatch)
    assert out["http"][0] == 409
    assert out["loop_errors"] == []


# ---- A3: a random rid start per run ---------------------------------------

def test_each_run_numbers_its_rids_from_a_random_start(tmp_path, monkeypatch):
    """T6: two fresh broker apps (two runs) do not start at rid 1, nor at
    the same rid; every rid stays a positive int under 2**53. (Each start
    is random over 2**40, so a false failure is ~2**-40.)"""
    firsts = []
    for n in range(2):
        sub = tmp_path / f"run{n}"
        sub.mkdir()
        app = _desktop_app(sub, monkeypatch)
        firsts.append(next(app.ctx.desktop_rid))
    assert all(isinstance(r, int) and 1 < r < 2 ** 53 for r in firsts), firsts
    assert firsts[0] != firsts[1], firsts
