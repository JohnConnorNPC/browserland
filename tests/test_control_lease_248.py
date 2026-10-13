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

    async def connect(self, client_id, stall=None):
        ws = CtlWS(stall)
        self.tasks.append(asyncio.get_running_loop().create_task(
            self.handler(CtlReq(client_id), ws)))
        await _settle()
        return ws

    async def close_all(self):
        for t in self.tasks:
            t.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)


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
    TOKEN, _lease, _until, broker)


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
