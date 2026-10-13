"""gh #248, broker track: the /control lease channel.

In-process cells drive the REAL ``_control_ws`` handler (the route's
callable, ``route.handler.args[0]``) with a fake request and a fake socket,
so they can read ``ctx.lease_generation`` and stall one socket's sends. The
cells that need the real websocket stack run a broker subprocess (the
``broker`` fixture and helpers from tests/test_terminal_control.py, which
follow tests/test_broker_e2e.py's ``broker_proc``).
"""

from __future__ import annotations

import asyncio
import json

import webterm.broker.app as app_mod
from webterm import protocol

from .auth_helpers import TEST_TOKEN

_app_seq = 0


def _app(tmp_path, monkeypatch):
    global _app_seq
    _app_seq += 1
    monkeypatch.delenv("WEB_TERMINAL_TOKEN", raising=False)
    monkeypatch.delenv("WEB_TERMINAL_MCP_TOKEN", raising=False)
    cfg = {"state_path": str(tmp_path / "webterm_state.json"),
           "mcp_state_path": str(tmp_path / "webterm_mcp.json"),
           "auth_token": TEST_TOKEN}
    return app_mod.create_app(cfg, name=f"webterm-lease-248-{_app_seq}")


def _control_handler(app):
    """The undecorated /control handler (sanic wraps it in a partial)."""
    for route in app.router.routes:
        if route.path == "control":
            return route.handler.args[0]
    raise AssertionError("no /control route")


class CtlReq:
    def __init__(self, client_id, token=TEST_TOKEN):
        self.args = {"clientId": client_id, "token": token}
        self.headers = {}
        self.ip = "127.0.0.1"


class CtlWS:
    """A /control socket: ``push`` a frame in, read ``sent`` out. ``stall``
    (an Event) holds every send until it is set."""

    def __init__(self, stall=None):
        self.inbox = asyncio.Queue()
        self.sent = []
        self.stall = stall
        self.closed = None

    async def send(self, data):
        if self.stall is not None:
            await self.stall.wait()
        self.sent.append(json.loads(data))

    async def close(self, code=1000, reason=""):
        self.closed = code
        self.inbox.put_nowait(None)

    def push(self, frame):
        self.inbox.put_nowait(frame if isinstance(frame, str)
                              else json.dumps(frame))

    def hang_up(self):
        self.inbox.put_nowait(None)

    def __aiter__(self):
        return self

    async def __anext__(self):
        message = await self.inbox.get()
        if message is None:
            raise StopAsyncIteration
        return message


async def _settle(n=10):
    for _ in range(n):
        await asyncio.sleep(0.005)


class Ctl:
    """One app, many /control connections."""

    def __init__(self, app):
        self.app = app
        self.ctx = app.ctx
        self.handler = _control_handler(app)
        self.tasks = []
        self.wss = []

    async def connect(self, client_id, stall=None):
        return await self.attach(client_id, CtlWS(stall))

    async def attach(self, client_id, ws):
        self.wss.append(ws)
        self.tasks.append(asyncio.get_running_loop().create_task(
            self.handler(CtlReq(client_id), ws)))
        await _settle()
        return ws

    async def close_all(self):
        # Hang every socket up first so each handler ends its own loop: a
        # bare cancel can be swallowed by Python 3.10's wait_for when it
        # lands just as a bounded send completes (bpo-42130).
        for ws in self.wss:
            ws.hang_up()
        await _settle()
        rest = list(self.app.ctx.bg_tasks)        # e.g. a hung 4409 cut
        for t in self.tasks + rest:
            t.cancel()
        await asyncio.gather(*self.tasks, *rest, return_exceptions=True)


def _statuses(ws):
    return [f for f in ws.sent if f.get("type") == "status"]


# ---- atom 1: status carries gen + bootId ----------------------------------

def test_status_frame_exact():
    assert json.loads(protocol.control_status_frame(True, "A", 7, "b00t")) == {
        "type": "status", "active": True, "activeClientId": "A",
        "gen": 7, "bootId": "b00t"}
    assert json.loads(protocol.control_status_frame(False, None, 0, "b")) == {
        "type": "status", "active": False, "activeClientId": None,
        "gen": 0, "bootId": "b"}


def test_status_names_the_live_generation_and_boot(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)

    async def go():
        c = Ctl(app)
        a = await c.connect("A")
        b = await c.connect("B")
        g = c.ctx.lease_generation
        boot = c.ctx.boot_id
        assert _statuses(a) == [{"type": "status", "active": True,
                                 "activeClientId": "A", "gen": g,
                                 "bootId": boot}]
        assert _statuses(b) == [{"type": "status", "active": False,
                                 "activeClientId": "A", "gen": g,
                                 "bootId": boot}]
        b.push({"type": "become_active"})
        await _settle()
        assert c.ctx.lease_generation == g + 1
        assert _statuses(a)[-1]["gen"] == g + 1
        assert _statuses(b)[-1] == {"type": "status", "active": True,
                                    "activeClientId": "B", "gen": g + 1,
                                    "bootId": boot}
        await c.close_all()
    asyncio.run(go())


# ---- atom 2: ping -> pong (nonce + live lease) ----------------------------

import websockets  # noqa: E402

from tests.test_terminal_control import (  # noqa: E402,F401
    TOKEN, FakeWS, _hello, _lease, _until, broker)


async def _ctl(port, client_id):
    """A raw /control socket and its first status."""
    ws = await websockets.connect(
        f"ws://127.0.0.1:{port}/control?clientId={client_id}&token={TOKEN}")
    return ws, json.loads(await asyncio.wait_for(ws.recv(), 5))


async def _next(ws, timeout=5.0):
    return json.loads(await asyncio.wait_for(ws.recv(), timeout))


def _as_pong(status, n):
    return dict(status, type="pong", n=n)


def test_protocol_pong_frame_exact():
    assert json.loads(protocol.control_pong_frame(3, False, "A", 9, "b")) == {
        "type": "pong", "n": 3, "active": False, "activeClientId": "A",
        "gen": 9, "bootId": "b"}


def test_e2e_ping_pong_echoes_the_nonce_and_the_live_lease(broker):
    port, _ = broker

    async def scenario():
        a = await _lease(port, "PONG-A")
        b, sb = await _ctl(port, "PONG-B")
        assert sb["active"] is False and sb["activeClientId"] == "PONG-A"
        # The holder's pong: its nonce, and the lease exactly as a status
        # would say it right now.
        await a.send(json.dumps({"type": "ping", "n": 41}))
        assert await _next(a) == _as_pong(dict(sb, active=True), 41)
        # A non-holder's ping without a nonce: n null, its own view.
        await b.send(json.dumps({"type": "ping"}))
        assert await _next(b) == _as_pong(sb, None)
        # An unknown frame is still ignored: the next frame b reads is the
        # pong to the ping sent after it, nothing in between.
        await b.send(json.dumps({"type": "bogus", "n": 1}))
        await b.send(json.dumps({"type": "ping", "n": 7}))
        assert await _next(b) == _as_pong(sb, 7)
        # become_active still works after pings, and the next pong says so.
        for i in range(3):
            await b.send(json.dumps({"type": "ping", "n": 100 + i}))
            assert (await _next(b))["n"] == 100 + i
        await b.send(json.dumps({"type": "become_active"}))
        won = await _until(b, lambda f: f.get("type") == "status")
        assert won == dict(sb, active=True, activeClientId="PONG-B",
                           gen=sb["gen"] + 1)
        lost = await _until(a, lambda f: f.get("type") == "status")
        assert lost == dict(won, active=False)
        await a.send(json.dumps({"type": "ping", "n": 5}))
        assert await _next(a) == _as_pong(lost, 5)
        await a.close()
        await b.close()
    asyncio.run(scenario())


def test_a_stalled_pong_send_closes_the_socket_1012(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)

    async def go():
        c = Ctl(app)
        gate = asyncio.Event()
        gate.set()
        ws = await c.connect("STALL", stall=gate)
        assert _statuses(ws)[0]["active"] is True
        gate.clear()                       # every later send hangs
        loop = asyncio.get_running_loop()
        t0 = loop.time()
        ws.push({"type": "ping", "n": 1})
        while ws.closed is None and loop.time() - t0 < 3:
            await asyncio.sleep(0.02)
        assert ws.closed == 1012
        assert loop.time() - t0 < 1.5
        await c.close_all()
    asyncio.run(go())


# ---- atom 3: become_active from the current holder is idempotent ----------

_wid_seq = [248100]


async def _terminals(app, *client_ids):
    """One attached terminal /ws per clientId: {clientId: socket}."""
    _wid_seq[0] += 1
    entry = await app.ctx.registry.register(FakeWS(), _hello(_wid_seq[0]))
    socks = {}
    for cid in client_ids:
        socks[cid] = FakeWS()
        entry.add_subscriber(socks[cid], cid)
    return socks


def _cuts(socks):
    return [(cid, s.closed) for cid, s in socks.items() if s.closed]


def test_holder_become_active_twice_changes_nothing(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)

    async def go():
        c = Ctl(app)
        a1 = await c.connect("A")
        a2 = await c.connect("A")           # the holder's second socket
        b = await c.connect("B")
        socks = await _terminals(app, "A", "B")
        g = c.ctx.lease_generation
        before = {id(w): len(w.sent) for w in (a1, a2, b)}
        for _ in range(2):
            a1.push({"type": "become_active"})
            await _settle()
        assert c.ctx.lease_generation == g
        assert c.ctx.active_client_id == "A"
        assert _cuts(socks) == []
        # One status per request, to the requesting socket only.
        assert a1.sent[before[id(a1)]:] == [
            {"type": "status", "active": True, "activeClientId": "A",
             "gen": g, "bootId": c.ctx.boot_id}] * 2
        assert len(a2.sent) == before[id(a2)]
        assert len(b.sent) == before[id(b)]
        await c.close_all()
    asyncio.run(go())


def test_non_holder_become_active_still_flips(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)

    async def go():
        c = Ctl(app)
        a = await c.connect("A")
        b = await c.connect("B")
        socks = await _terminals(app, "A", "B")
        g = c.ctx.lease_generation
        b.push({"type": "become_active"})
        await _settle()
        assert c.ctx.lease_generation == g + 1
        assert c.ctx.active_client_id == "B"
        assert _cuts(socks) == [("A", 4409)]
        assert _statuses(a)[-1]["active"] is False
        assert _statuses(a)[-1]["activeClientId"] == "B"
        assert _statuses(b)[-1]["active"] is True
        await c.close_all()
    asyncio.run(go())


# ---- atom 4 (S7): every status send is bounded; one stall delays nobody ---

async def _wait(pred, timeout=3.0):
    loop = asyncio.get_running_loop()
    t0 = loop.time()
    while not pred():
        if loop.time() - t0 > timeout:
            return None
        await asyncio.sleep(0.01)
    return loop.time() - t0


async def _stalled_loser(c, client_id):
    """A connected socket that takes its first status, then stalls."""
    gate = asyncio.Event()
    gate.set()
    ws = await c.connect(client_id, stall=gate)
    gate.clear()
    return ws


class _HangingClose(FakeWS):
    """A terminal /ws whose close never completes."""

    async def close(self, code=1000, reason=""):
        await asyncio.Event().wait()


def test_a_stalled_loser_does_not_delay_the_takeover_statuses(
        tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)

    async def go():
        c = Ctl(app)
        await c.connect("A")                      # the holder
        # The holder's attached terminal: cutting it (4409) hangs, which must
        # not hold back any page's status either.
        entry = await app.ctx.registry.register(FakeWS(), _hello(248001))
        entry.add_subscriber(_HangingClose(), "A")
        stalled = await _stalled_loser(c, "L")    # ahead of H in dict order
        h = await c.connect("H")
        w = await c.connect("W")
        nh, nw = len(h.sent), len(w.sent)
        w.push({"type": "become_active"})
        # Faster than ONE send timeout: the others are not queued behind it.
        dt = await _wait(lambda: len(h.sent) > nh and len(w.sent) > nw)
        assert dt is not None and dt < 0.3, dt
        assert h.sent[-1]["activeClientId"] == "W"
        assert w.sent[-1]["active"] is True
        # The stalled one is dropped so its page re-dials for a fresh status.
        assert await _wait(lambda: stalled.closed == 1012) is not None
        # The hung 4409 cut never wedges the winner's socket: it still
        # answers a ping (a page's pong deadline would otherwise drop it).
        w.push({"type": "ping", "n": 9})
        assert await _wait(lambda: w.sent[-1].get("type") == "pong")             is not None
        assert w.sent[-1]["n"] == 9 and w.sent[-1]["active"] is True
        await c.close_all()
    asyncio.run(go())


def test_a_stalled_peer_does_not_delay_the_release_statuses(
        tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)

    async def go():
        c = Ctl(app)
        a = await c.connect("A")                  # the holder
        stalled = await _stalled_loser(c, "L")
        h = await c.connect("H")
        nh = len(h.sent)
        a.hang_up()                               # release on disconnect
        dt = await _wait(lambda: len(h.sent) > nh)
        assert dt is not None and dt < 0.3, dt
        assert h.sent[-1]["activeClientId"] is None
        assert await _wait(lambda: stalled.closed == 1012) is not None
        await c.close_all()
    asyncio.run(go())


# ---- atom 5 (A6): one log line per holder change, short ids only ----------

import logging  # noqa: E402

LONG_A = "aaaaaaaa-1111-2222-3333-444444444444"
LONG_B = "bbbbbbbb-5555-6666-7777-888888888888"


def test_one_lease_log_line_per_holder_change(tmp_path, monkeypatch, caplog):
    app = _app(tmp_path, monkeypatch)
    caplog.set_level(logging.INFO, logger="webterm.broker.app")

    async def go():
        c = Ctl(app)
        a = await c.connect(LONG_A)              # claim
        b = await c.connect(LONG_B)              # no change
        b.push({"type": "become_active"})        # take over
        await _settle()
        b.push({"type": "become_active"})        # repeat: no change
        await _settle()
        a.hang_up()                              # a non-holder leaves
        await _settle()
        b.hang_up()                              # release
        await _settle()
        await c.close_all()
        return c.ctx.lease_generation
    gen = asyncio.run(go())
    lines = [r.getMessage() for r in caplog.records
             if r.name == "webterm.broker.app"
             and r.getMessage().startswith("lease: ")]
    assert lines == [
        f"lease: aaaaaaaa claimed a free lease (gen {gen - 2})",
        f"lease: bbbbbbbb took over from aaaaaaaa (gen {gen - 1})",
        f"lease: bbbbbbbb released it on disconnect (gen {gen})",
    ]
    text = caplog.text
    assert LONG_A not in text and LONG_B not in text
    assert LONG_A[:9] not in text and LONG_B[:9] not in text


# ---- atom 6 (A5): a lease-refused drive frame gets ONE not_active hint ----

import pytest  # noqa: E402

from webterm.broker.registry import BrokerRegistry  # noqa: E402
from tests.test_terminal_control import (  # noqa: E402
    FakeBrowser, _attach, _producer_frames, _registered)
from tests.test_terminal_control import _settle as _relay_settle  # noqa: E402


class LeaseCtx:
    def __init__(self, active, gen=1):
        self.active_client_id = active
        self.lease_generation = gen


def _errors(ws):
    out = []
    for raw in ws.sent:
        if isinstance(raw, str):
            f = json.loads(raw)
            if f.get("type") == "error":
                out.append(f)
    return out


def _hint(entry):
    return {"type": "error", "reason": "not_active", "session": entry.id}


def test_protocol_not_active_frame_exact():
    # The #248 wire contract's key is `session` (error_frame's is
    # `session_id`).
    assert json.loads(protocol.not_active_frame(12)) == {
        "type": "error", "reason": "not_active", "session": 12}


DRIVE_FRAMES = [{"type": "input", "data": "x"}, {"type": "paste", "data": "x"},
                b"x-binary", {"type": "resize", "cols": 40, "rows": 10}]


@pytest.mark.parametrize("frame", DRIVE_FRAMES,
                         ids=["input", "paste", "binary", "resize"])
def test_a_non_holder_gets_one_hint_per_generation(frame):
    async def scenario():
        reg = BrokerRegistry()
        ctx = LeaseCtx("phone", gen=5)
        entry = await _registered(reg)
        desk = FakeBrowser()
        task = _attach(reg, ctx, entry, desk, "desktop")
        await _relay_settle()
        desk.push(frame)
        await _relay_settle()
        assert _errors(desk) == [_hint(entry)]
        assert _producer_frames(entry) == []
        # Same generation: dropped again, no second hint.
        for f in DRIVE_FRAMES:
            desk.push(f)
        await _relay_settle()
        assert _errors(desk) == [_hint(entry)]
        # A new generation (another holder change) re-arms it once.
        ctx.lease_generation = 6
        ctx.active_client_id = "tablet"
        desk.push(frame)
        desk.push(frame)
        await _relay_settle()
        assert _errors(desk) == [_hint(entry)] * 2
        assert _producer_frames(entry) == []
        desk.hang_up()
        await task
    asyncio.run(scenario())


def test_the_holder_gets_no_hint():
    async def scenario():
        reg = BrokerRegistry()
        ctx = LeaseCtx("desktop")
        entry = await _registered(reg)
        desk = FakeBrowser()
        task = _attach(reg, ctx, entry, desk, "desktop")
        await _relay_settle()
        for f in DRIVE_FRAMES:
            desk.push(f)
        await _relay_settle()
        assert _errors(desk) == []
        assert len(_producer_frames(entry)) == 4
        desk.hang_up()
        await task
    asyncio.run(scenario())


def test_controller_gated_and_revoked_sockets_get_no_hint():
    async def scenario():
        reg = BrokerRegistry()
        ctx = LeaseCtx("desktop")
        entry = await _registered(reg)
        desk, ctl = FakeBrowser(), FakeBrowser()
        tasks = [_attach(reg, ctx, entry, desk, "desktop"),
                 _attach(reg, ctx, entry, ctl, "ext")]
        await _relay_settle()
        # A #238 controller holds the terminal: the lease holder's frames
        # are dropped for the CONTROLLER's sake -- no not_active.
        ctl.push({"type": "control", "op": "claim", "label": "phone"})
        await _relay_settle()
        for f in DRIVE_FRAMES:
            desk.push(f)
        await _relay_settle()
        assert _errors(desk) == []
        # Revoked: the controller lost the terminal (a POST release); its
        # socket watches until it claims again -- no not_active either, even
        # though its clientId does not hold the lease.
        assert reg.release_by_gen(entry, entry.control_gen) is None
        for f in DRIVE_FRAMES:
            ctl.push(f)
        await _relay_settle()
        assert [e["reason"] for e in _errors(ctl)] == []
        assert _producer_frames(entry) == []
        for ws in (desk, ctl):
            ws.hang_up()
        await asyncio.gather(*tasks)
        await reg.drain_control()
    asyncio.run(scenario())


def test_a_refusal_under_the_send_lock_hints_too():
    # Admitted on arrival (it held the lease), dropped at the second gate
    # because the lease moved while it waited on the send lock: the hint
    # describes THAT refusal.
    async def scenario():
        reg = BrokerRegistry()
        ctx = LeaseCtx("desktop", gen=3)
        entry = await _registered(reg)
        desk = FakeBrowser()
        task = _attach(reg, ctx, entry, desk, "desktop")
        await _relay_settle()
        await entry._send_lock.acquire()      # a snapshot/RPC in flight
        desk.push({"type": "input", "data": "late"})
        await _relay_settle()                 # admitted, waiting on the lock
        ctx.active_client_id, ctx.lease_generation = "phone", 4
        entry._send_lock.release()
        await _relay_settle()
        assert _producer_frames(entry) == []
        assert _errors(desk) == [_hint(entry)]
        desk.hang_up()
        await task
    asyncio.run(scenario())


def test_e2e_non_holder_input_gets_one_not_active(broker):
    from tests.test_terminal_control import _browser, _next_drive, _producer
    port, base = broker

    async def scenario():
        wid = 248600
        prod = await _producer(port, base, wid)
        holder = await _lease(port, "HINT-B")
        desk, _ = await _browser(port, wid, "HINT-A")
        for i in range(3):
            await desk.send(json.dumps({"type": "input", "data": f"A{i}"}))
        await desk.send(json.dumps({"type": "resize", "cols": 9, "rows": 9}))
        # Barrier (tests/test_terminal_control.py _processed), keeping every
        # frame it reads: once not_controller arrives, the relay has handled
        # all of desk's frames above.
        await desk.send(json.dumps({"type": "control", "op": "release"}))
        frames = []
        while not frames or frames[-1].get("reason") != "not_controller":
            raw = await asyncio.wait_for(desk.recv(), 5)
            if isinstance(raw, str):
                frames.append(json.loads(raw))
        mine, _ = await _browser(port, wid, "HINT-B")
        await mine.send(json.dumps({"type": "input", "data": "B-marker"}))
        assert await _next_drive(prod) == {"type": "input",
                                           "data": "B-marker"}
        hints = [f for f in frames if f.get("type") == "error"
                 and f.get("reason") == "not_active"]
        assert hints == [{"type": "error", "reason": "not_active",
                          "session": wid}]
        for ws in (desk, mine, holder, prod):
            await ws.close()
    asyncio.run(scenario())


# ---- atom 7 (S8): the desktop relay slices its deadline per socket --------

import time  # noqa: E402

from tests.test_mcp_desktop import (  # noqa: E402
    _desktop_app, _FakeBrowserWS, _mcp, _reply_ok)


class _StalledWS:
    def __init__(self):
        self.frames = []

    async def send(self, frame):
        self.frames.append(json.loads(frame))
        await asyncio.sleep(10)


def test_a_stalled_first_socket_leaves_time_for_a_healthy_one(
        tmp_path, monkeypatch):
    app = _desktop_app(tmp_path, monkeypatch)
    app.ctx.desktop_rpc_timeout_s = 0.6
    snap = {"active_workspace": 0,
            "workspaces": [{"index": 0, "id": "L1", "name": None}]}
    stalled, healthy = _StalledWS(), _FakeBrowserWS(app, _reply_ok(snap))
    # A list, not the real set, so the stalled socket is FIRST: the order a
    # set happens to yield is exactly what this must not depend on.
    app.ctx.control_clients["two"] = [stalled, healthy]
    app.ctx.active_client_id = "two"
    t0 = time.monotonic()
    _, resp = _mcp(app, "/mcp/desktop", method="GET")
    elapsed = time.monotonic() - t0
    assert resp.status == 200, resp.json
    assert resp.json["active_workspace"] == 0
    assert len(stalled.frames) == 1 and len(healthy.frames) == 1
    assert elapsed < 0.6 + 0.3, elapsed


# ---- codex review round 1 --------------------------------------------------

def test_a_takeover_cuts_only_the_terminals_its_losers_held_at_the_flip(
        tmp_path, monkeypatch):
    # B takes over from A; while B's statuses are held up by a stalled
    # socket, C takes over and attaches a terminal as the holder. B's cut
    # must have taken exactly the sockets its losers held at B's flip --
    # never C's new one, whatever B does after its fan-out.
    app = _app(tmp_path, monkeypatch)

    async def go():
        c = Ctl(app)
        await c.connect("A")
        await _stalled_loser(c, "L")
        b = await c.connect("B")
        cc = await c.connect("C")
        entry = await app.ctx.registry.register(FakeWS(), _hello(248002))
        term_a, term_c_old = FakeWS(), FakeWS()
        entry.add_subscriber(term_a, "A")
        entry.add_subscriber(term_c_old, "C")
        b.push({"type": "become_active"})
        await _settle(2)                   # B flipped; its fan-out is held
        cc.push({"type": "become_active"})
        await _settle(2)
        assert c.ctx.active_client_id == "C"
        term_c_new = FakeWS()              # C's terminal, opened as holder
        entry.add_subscriber(term_c_new, "C")
        await asyncio.sleep(0.8)           # B's fan-out times out
        assert term_a.closed == 4409
        assert term_c_old.closed == 4409   # C lost at B's flip
        assert term_c_new.closed is None
        await c.close_all()
    asyncio.run(go())


def test_a_socket_whose_handler_is_gone_is_not_written(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)

    async def go():
        c = Ctl(app)
        a = await c.connect("A")
        b = await c.connect("B")
        # B's handler is gone (its lock popped) but a fan-out still names
        # the socket: it is skipped, not given a fresh unshared lock.
        app.ctx.control_send_locks.pop(b)
        n = len(b.sent)
        a.hang_up()                        # release -> fan-out to b
        await _settle()
        assert len(b.sent) == n
        await c.close_all()
    asyncio.run(go())


def test_the_send_bound_includes_waiting_for_the_socket_lock(
        tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)

    async def go():
        c = Ctl(app)
        a = await c.connect("A")
        b = await c.connect("B")
        # Something holds a's send lock and never lets go: a status queued
        # behind it must still give up within the bound and drop the socket.
        await app.ctx.control_send_locks[a].acquire()
        b.push({"type": "become_active"})
        assert await _wait(lambda: a.closed == 1012, 2.0) is not None
        await c.close_all()
    asyncio.run(go())


class _CancelOnPong(CtlWS):
    """Cancels the handler task the instant its pong send completes -- the
    moment Python 3.10's wait_for loses a cancellation (bpo-42130)."""

    task = None

    async def send(self, data):
        await super().send(data)
        if self.sent[-1].get("type") == "pong":
            asyncio.get_running_loop().call_soon(self.task.cancel)


def test_a_cancel_landing_as_a_send_completes_is_not_lost(
        tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)

    async def go():
        c = Ctl(app)
        a = await c.attach("A", _CancelOnPong())
        a.task = c.tasks[-1]
        a.push({"type": "ping", "n": 1})
        assert await _wait(lambda: a.task.done(), 2.0) is not None
        assert "A" not in app.ctx.control_clients   # the finally ran
        assert app.ctx.active_client_id is None
        await c.close_all()
    asyncio.run(go())


class _HeldPong(CtlWS):
    """Holds only the send of the pong with n == 1 until ``release`` is set;
    every other frame goes straight through (sends are NOT serialized here,
    unlike most transports, so only the broker can keep them in order)."""

    def __init__(self):
        super().__init__()
        self.release = asyncio.Event()

    async def send(self, data):
        f = json.loads(data)
        if f.get("type") == "pong" and f.get("n") == 1:
            await self.release.wait()
        self.sent.append(f)


def test_lease_frames_on_one_socket_land_in_lease_order(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)

    async def go():
        c = Ctl(app)
        a = await c.attach("A", _HeldPong())
        b = await c.connect("B")
        a.push({"type": "ping", "n": 1})   # built at gen G, held in send
        await _settle(2)
        b.push({"type": "become_active"})  # gen G+1 status for a
        await _settle(4)
        a.release.set()
        await _settle()
        gens = [(f["type"], f["gen"]) for f in a.sent]
        assert [g for _, g in gens] == sorted(g for _, g in gens), gens
        assert a.sent[-1]["type"] == "status"
        assert a.sent[-1]["active"] is False
        await c.close_all()
    asyncio.run(go())


class _HintGate(FakeBrowser):
    """Fails the first not_active send, stalls the second until ``go``."""

    def __init__(self):
        super().__init__()
        self.hint_sends = 0
        self.go = asyncio.Event()

    async def send(self, data):
        if isinstance(data, str) and '"not_active"' in data:
            self.hint_sends += 1
            if self.hint_sends == 1:
                raise ConnectionError("first hint send fails")
            if self.hint_sends == 2:
                await self.go.wait()
        await super().send(data)


def test_a_hint_is_retried_after_a_failed_send_and_never_wedges_the_loop():
    async def scenario():
        reg = BrokerRegistry()
        ctx = LeaseCtx("phone", gen=2)
        entry = await _registered(reg)
        desk = _HintGate()
        task = _attach(reg, ctx, entry, desk, "desktop")
        await _relay_settle()
        desk.push({"type": "input", "data": "1"})   # hint send raises
        await _relay_settle()
        assert desk.hint_sends == 1 and _errors(desk) == []
        desk.push({"type": "input", "data": "2"})   # retried: it stalls
        await asyncio.sleep(0.8)                    # past the send bound
        assert desk.hint_sends == 2
        # The read loop moved on: the desktop now holds the lease and its
        # next input reaches the producer.
        ctx.active_client_id, ctx.lease_generation = "desktop", 3
        desk.push({"type": "input", "data": "mine"})
        await _relay_settle()
        assert _producer_frames(entry) == [{"type": "input", "data": "mine"}]
        desk.go.set()
        desk.hang_up()
        await task
    asyncio.run(scenario())


def test_a_timed_out_relay_send_closes_that_socket(tmp_path, monkeypatch):
    class _Stalled(_StalledWS):
        closed = None

        async def close(self, code=1000, reason=""):
            self.closed = code

    app = _desktop_app(tmp_path, monkeypatch)
    app.ctx.desktop_rpc_timeout_s = 0.6
    snap = {"active_workspace": 0,
            "workspaces": [{"index": 0, "id": "L1", "name": None}]}
    stalled, healthy = _Stalled(), _FakeBrowserWS(app, _reply_ok(snap))
    app.ctx.control_clients["two"] = [stalled, healthy]
    app.ctx.active_client_id = "two"
    _, resp = _mcp(app, "/mcp/desktop", method="GET")
    assert resp.status == 200, resp.json
    assert stalled.closed == 1012


def test_log_ids_carry_no_control_characters(tmp_path, monkeypatch, caplog):
    app = _app(tmp_path, monkeypatch)
    caplog.set_level(logging.INFO, logger="webterm.broker.app")

    async def go():
        c = Ctl(app)
        await c.connect("ev\nil\r\x1b-0000000")
        await c.close_all()
    asyncio.run(go())
    lines = [r.getMessage() for r in caplog.records
             if r.getMessage().startswith("lease: ")]
    assert lines[0].startswith("lease: ev?il??- claimed a free lease")
    assert all("\n" not in m and "\r" not in m and "\x1b" not in m
               for m in lines)
