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

_ORIG_WAIT_FOR = asyncio.wait_for


async def _hop_wait_for(aw, timeout):
    """asyncio.wait_for, except that the relay's ``_send_rpc`` starts in a
    task of its own, one loop step later -- what wait_for did on Python
    <=3.11. Since 3.12 wait_for awaits its coroutine inline, so the send's
    generation check ran before a become_active already queued; this forces
    the bump-first order on every Python. Every other wait_for passes
    through unchanged."""
    if getattr(aw, "__qualname__", "").endswith("._send_rpc"):
        aw = asyncio.ensure_future(aw)
    return await _ORIG_WAIT_FOR(aw, timeout)


def _takeover_at_registration(app, monkeypatch):
    """Run one GET /mcp/desktop while holder A is taken over by B at the
    exact point the issue names: B's become_active is queued from inside
    desktop_rpc_frame, which the relay calls synchronously just before it
    registers the call, and the send is started one loop step later
    (``_hop_wait_for``), so B's handler runs BEFORE the send's generation
    check. Returns what happened, plus every asyncio exception the loop
    reported."""
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
        monkeypatch.setattr(asyncio, "wait_for", _hop_wait_for)
        try:
            _, resp = await _get_desktop(app)
        finally:
            monkeypatch.setattr(protocol, "desktop_rpc_frame", orig)
            monkeypatch.setattr(asyncio, "wait_for", _ORIG_WAIT_FOR)
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
    """T6: two fresh broker apps (two runs) do not start at the same rid;
    every rid stays a positive int under 2**53. (Each start is random over
    2**40, so a false failure is ~2**-40; T6b pins the draw exactly.)"""
    firsts = []
    for n in range(2):
        sub = tmp_path / f"run{n}"
        sub.mkdir()
        app = _desktop_app(sub, monkeypatch)
        firsts.append(next(app.ctx.desktop_rid))
    assert all(isinstance(r, int) and 1 <= r < 2 ** 53 for r in firsts), firsts
    assert firsts[0] != firsts[1], firsts


# ---- A6: the lost_lease body says whether the op was dispatched ------------

def test_a_takeover_before_the_send_says_not_dispatched(tmp_path,
                                                        monkeypatch):
    """T2 body: no socket was ever handed the frame, so dispatched:false
    (the one lost_lease that promises nothing applied)."""
    out = _takeover_at_registration(_desktop_app(tmp_path, monkeypatch),
                                    monkeypatch)
    assert out["http"] == (409, {"error": "lost_lease",
                                 "dispatched": False})


class _TakenOverAtTheWriteWS(HolderWS):
    """A holder that never answers; at the instant its desktop_rpc is written
    it records (holder, live generation, the frame's gen) and then queues
    B's become_active -- a takeover right after the send started."""

    def __init__(self, ctx):
        super().__init__(ctx, answer=False)
        self.b = None
        self.witness = []

    async def send(self, data):
        frame = json.loads(data)
        if frame.get("type") == "desktop_rpc":
            self.witness.append((self.ctx.active_client_id,
                                 self.ctx.lease_generation, frame["gen"]))
            self.b.push({"type": "become_active"})
        await super().send(data)


def test_a_takeover_after_the_send_started_says_dispatched(tmp_path,
                                                           monkeypatch):
    """The send-first order (what 3.12+ wait_for gives with no hop): the
    write happened while A held the live generation, then B took over, so
    the caller's lost_lease says dispatched:true."""
    app = _desktop_app(tmp_path, monkeypatch)
    app.ctx.desktop_rpc_timeout_s = 2.0
    out = {}

    async def go():
        c = Ctl(app)
        a = await c.attach("A", _TakenOverAtTheWriteWS(app.ctx))
        a.b = await c.connect("B")
        out["gen0"] = app.ctx.lease_generation
        _, resp = await _get_desktop(app)
        await _settle()
        out["http"] = (resp.status, resp.json)
        out["holder"] = app.ctx.active_client_id
        out["witness"] = list(a.witness)
        await c.close_all()
    asyncio.run(go())
    g = out["gen0"]
    assert out["witness"] == [("A", g, g)]
    assert out["http"] == (409, {"error": "lost_lease", "dispatched": True})
    assert out["holder"] == "B"


class _StalledRpcWS(HolderWS):
    """A holder whose desktop_rpc send blocks (after the relay's re-check
    passed) until ``release`` is set; status frames pass straight through."""

    def __init__(self, ctx):
        super().__init__(ctx, answer=False)
        self.release = asyncio.Event()
        self.rpc_started = asyncio.Event()

    async def send(self, data):
        if json.loads(data).get("type") == "desktop_rpc":
            self.rpc_started.set()
            await self.release.wait()
        await super().send(data)


def test_a_takeover_during_the_send_says_dispatched(tmp_path, monkeypatch):
    """T7: the send started under the live generation, a takeover landed
    while it was blocked, then the frame went out: the deposed browser may
    apply it, so the caller's lost_lease says dispatched:true."""
    app = _desktop_app(tmp_path, monkeypatch)
    app.ctx.desktop_rpc_timeout_s = 2.0
    out = {}

    async def go():
        c = Ctl(app)
        a = await c.attach("A", _StalledRpcWS(app.ctx))
        b = await c.connect("B")
        call = asyncio.ensure_future(_get_desktop(app))
        await asyncio.wait_for(a.rpc_started.wait(), 2)
        b.push({"type": "become_active"})
        await _settle()
        out["holder"] = app.ctx.active_client_id
        a.release.set()
        _, resp = await call
        out["http"] = (resp.status, resp.json)
        out["a_rpcs"] = _rpcs(a)
        await c.close_all()
    asyncio.run(go())
    assert out["holder"] == "B"
    assert len(out["a_rpcs"]) == 1          # the frame did reach A
    assert out["http"] == (409, {"error": "lost_lease", "dispatched": True})


class _DeadWS:
    def __init__(self):
        self.tries = 0

    async def send(self, data):
        self.tries += 1
        raise ConnectionError("gone")


def test_every_send_failed_is_dispatched(tmp_path, monkeypatch):
    """T8: sends that raised were still STARTED; a raise does not prove
    nothing reached the browser, so the answer stays cautious."""
    app = _desktop_app(tmp_path, monkeypatch)
    app.ctx.desktop_rpc_timeout_s = 0.3
    socks = [_DeadWS(), _DeadWS()]
    app.ctx.control_clients["dead"] = set(socks)
    app.ctx.active_client_id = "dead"
    _, resp = app.test_client.get("/mcp/desktop", headers=_AUTH)
    assert [s.tries for s in socks] == [1, 1]
    assert (resp.status, resp.json) == (409, {"error": "lost_lease",
                                              "dispatched": True})


def _client_error(body):
    import httpx
    import pytest
    from tests.test_mcptool import _ToolBroker, _install, _reset_server
    from webterm.mcptool.client import BrowserlandError
    broker_ = _ToolBroker(responses={
        "/mcp/desktop": lambda req: httpx.Response(409, json=body)})
    server = _install({"h": broker_})
    try:
        with pytest.raises(BrowserlandError) as ei:
            server.arrange_window("h:7", "focus")
    finally:
        _reset_server()
    return ei.value


def test_client_text_for_a_dispatched_lost_lease():
    """A6 client: dispatched:true -- and an old broker's body without the
    key -- tell the agent to check with get_desktop before retrying."""
    for body in ({"error": "lost_lease", "dispatched": True},
                 {"error": "lost_lease"}):
        err = _client_error(body)
        assert err.code == "lost_lease"
        assert "get_desktop" in str(err) and "before" in str(err), str(err)
        assert "safe to retry" not in str(err)


def test_client_text_for_an_undispatched_lost_lease():
    """A6 client: only dispatched:false says nothing was applied."""
    err = _client_error({"error": "lost_lease", "dispatched": False})
    assert err.code == "lost_lease"
    assert "nothing was applied" in str(err) and "safe to retry" in str(err)
    assert "get_desktop" not in str(err)


class _BumpThenStallWS:
    """A holder socket: the FIRST socket the relay tries bumps the lease
    generation (a takeover) and then never finishes its send; any later
    socket would write normally."""

    def __init__(self, app, shared):
        self.app = app
        self.shared = shared
        self.frames = []
        self.closed = None

    async def send(self, data):
        if not self.shared["bumped"]:
            self.shared["bumped"] = True
            self.app.ctx.lease_generation += 1
            await asyncio.Event().wait()          # stalls until cancelled
        self.frames.append(json.loads(data))

    async def close(self, code=1000, reason=""):
        self.closed = code


def test_dispatched_sticks_across_a_failover(tmp_path, monkeypatch):
    """T9: the first socket's send started (then stalled past its slice and
    was cancelled); by the second socket the generation has moved, so its
    re-check refuses -- the answer must still say dispatched:true, because
    the first, cancelled send may have put the frame on the wire."""
    app = _desktop_app(tmp_path, monkeypatch)
    app.ctx.desktop_rpc_timeout_s = 0.4
    shared = {"bumped": False}
    socks = [_BumpThenStallWS(app, shared), _BumpThenStallWS(app, shared)]
    app.ctx.control_clients["h"] = set(socks)
    app.ctx.active_client_id = "h"
    _, resp = app.test_client.get("/mcp/desktop", headers=_AUTH)
    assert shared["bumped"] is True
    assert [s.frames for s in socks] == [[], []]       # never fully written
    assert (resp.status, resp.json) == (409, {"error": "lost_lease",
                                              "dispatched": True})


def test_the_rid_start_is_drawn_from_secrets(tmp_path, monkeypatch):
    """T6b (deterministic): the first rid is secrets.randbelow(2**40) + 1."""
    import webterm.broker.app as app_mod
    asked = []

    def fake(n):
        asked.append(n)
        return 41
    monkeypatch.setattr(app_mod.secrets, "randbelow", fake)
    app = _desktop_app(tmp_path, monkeypatch)
    assert 2 ** 40 in asked
    assert next(app.ctx.desktop_rid) == 42


def test_the_docs_say_a_started_send_is_dispatched():
    """TR states what the relay guarantees: it never STARTS a send once the
    lease has moved on, and a send already started reports dispatched:true."""
    from pathlib import Path
    tr = " ".join((Path(__file__).resolve().parents[1] / "wiki"
                   / "Technical-Reference.md").read_text(
                       encoding="utf-8").split())
    assert "never starts sending an op once the lease has moved on" in tr
    assert "the agent is then told `dispatched: true`" in tr
    assert "never sends an op once the lease" not in tr
