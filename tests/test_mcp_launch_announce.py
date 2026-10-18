"""gh #262, broker track: after a successful ``POST /mcp/launch`` the broker
tells the lease-holding browser to open the new window
(``session_launched``), in a detached task the reply never waits for.

Two seams, both deterministic:

* the ROUTE, driven through ``app.asgi_client`` inside ``asyncio.run`` so the
  loop outlives the response: either ``app.ctx.announce_launch`` swapped for
  a recorder (did the route schedule exactly one announce, for which id?) or
  the real announce against the REAL ``/control`` handler of
  tests/test_control_lease_248.py's ``Ctl`` harness;
* the announce COROUTINE itself (``app.ctx.announce_launch``) awaited directly
  against fake holder sockets: one frame, failover, the generation re-check
  and the 1012 close of a stalled socket.
"""

from __future__ import annotations

import asyncio
import json
import time

from webterm.broker.launcher import LaunchError
from webterm.broker.registry import CONTROL_SEND_TIMEOUT
from tests.test_control_lease_248 import Ctl, CtlWS, _settle
from tests.test_mcp_window import (LAUNCHED, MCP_TOKEN, _FakeLauncher,
                                   _launch, _make_app)

_AUTH = {"Authorization": f"Bearer {MCP_TOKEN}"}


def _app(tmp_path, monkeypatch, desktop=True):
    return _make_app(tmp_path, monkeypatch, mcp_allow_desktop=desktop)


def _post_launch(app, scope=None):
    headers = dict(_AUTH)
    if scope is not None:
        headers["X-Browserland-Scope"] = scope
    return app.asgi_client.post("/mcp/launch", json={}, headers=headers)


def _record_announces(app):
    calls = []

    async def fake(wid):
        calls.append(wid)
    app.ctx.announce_launch = fake
    return calls


def _launched(ws):
    return [f for f in ws.sent if f.get("type") == "session_launched"]


# ---- the route schedules one announce per successful launch ------------------

def test_a_successful_launch_schedules_one_announce_for_its_id(tmp_path,
                                                               monkeypatch):
    app = _app(tmp_path, monkeypatch)
    _launch(app)
    calls = _record_announces(app)
    out = {}

    async def go():
        _, resp = await _post_launch(app)
        await _settle()
        out["resp"] = (resp.status, resp.json)
    asyncio.run(go())
    status, body = out["resp"]
    assert status == 200
    assert calls == [LAUNCHED]
    # The launch answer itself is unchanged.
    assert set(body) == {"ok", "id", "registered", "agent_pid", "scope",
                         "mode", "handle"}
    assert body["id"] == LAUNCHED


def test_a_202_launch_announces_too_and_a_scoped_launch_does(tmp_path,
                                                             monkeypatch):
    """A 202 (agent still starting) is a launch: the page parks the id and
    opens it when it registers. A scoped launcher's own window is announced
    as well (ruling Q7); scope still gates every later op on it."""
    app = _app(tmp_path, monkeypatch)

    class _Slow(_FakeLauncher):
        async def launch(self, profile, **kwargs):
            await kwargs["pre_spawn"](LAUNCHED)
            return 202, {"ok": True, "id": LAUNCHED, "registered": False,
                         "agent_pid": 1}
    app.ctx.launcher = _Slow(app)
    calls = _record_announces(app)
    out = {}

    async def go():
        _, resp = await _post_launch(app)
        out["s1"] = resp.status
        _launch(app, ids=iter([LAUNCHED + 1]))
        _, resp = await _post_launch(app, scope="team")
        out["s2"] = resp.status
        await _settle()
    asyncio.run(go())
    assert (out["s1"], out["s2"]) == (202, 200)
    assert calls == [LAUNCHED, LAUNCHED + 1]


def test_no_announce_with_the_desktop_relay_off(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch, desktop=False)
    _launch(app)
    calls = _record_announces(app)

    async def go():
        _, resp = await _post_launch(app)
        await _settle()
        assert resp.status == 200
    asyncio.run(go())
    assert calls == []


def test_a_failed_launch_announces_nothing(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)

    class _Fail(_FakeLauncher):
        async def launch(self, profile, **kwargs):
            raise LaunchError(500, "spawn_failed")
    app.ctx.launcher = _Fail(app)
    calls = _record_announces(app)

    async def go():
        _, resp = await _post_launch(app)
        await _settle()
        assert resp.status == 500
    asyncio.run(go())
    assert calls == []


# ---- end to end over the real /control handler ---------------------------------

def test_the_holder_gets_one_frame_and_nobody_else_does(tmp_path, monkeypatch):
    """Two sockets of the holder's client id (a page and its replacement, or
    two tabs of one profile) and one socket of another client: exactly ONE
    frame, on a holder socket, naming the new id, the live generation and
    this broker run."""
    app = _app(tmp_path, monkeypatch)
    _launch(app)
    out = {}

    async def go():
        c = Ctl(app)
        a1 = await c.connect("A")
        a2 = await c.connect("A")
        b = await c.connect("B")
        _, resp = await _post_launch(app)
        await _settle()
        out["status"] = resp.status
        out["a"] = _launched(a1) + _launched(a2)
        out["b"] = _launched(b)
        out["gen"] = app.ctx.lease_generation
        await c.close_all()
    asyncio.run(go())
    assert out["status"] == 200
    assert out["b"] == []
    assert out["a"] == [{"type": "session_launched", "id": LAUNCHED,
                         "gen": out["gen"], "bootId": app.ctx.boot_id}]


def test_a_stalled_holder_never_delays_the_reply(tmp_path, monkeypatch):
    """The reply returns while the announce is still blocked on the holder;
    the announce then gives up after CONTROL_SEND_TIMEOUT, closes that
    socket 1012 and leaves bg_tasks."""
    app = _app(tmp_path, monkeypatch)
    _launch(app)
    closed = []
    monkeypatch.setattr(app.ctx.registry, "close_soon",
                        lambda sock, code, reason="": closed.append(code))
    out = {}

    class _Stuck:
        async def send(self, data):
            await asyncio.Event().wait()

    async def go():
        app.ctx.control_clients["S"] = {_Stuck()}
        app.ctx.active_client_id = "S"
        t0 = time.monotonic()
        _, resp = await _post_launch(app)
        out["elapsed"] = time.monotonic() - t0
        out["status"] = resp.status
        out["pending_after_reply"] = len(app.ctx.bg_tasks)
        await asyncio.sleep(CONTROL_SEND_TIMEOUT + 0.4)
        out["pending_later"] = len(app.ctx.bg_tasks)
    asyncio.run(go())
    assert out["status"] == 200
    assert out["elapsed"] < CONTROL_SEND_TIMEOUT * 0.8, out["elapsed"]
    assert out["pending_after_reply"] == 1
    assert closed == [1012]
    assert out["pending_later"] == 0


# ---- the announce coroutine itself ------------------------------------------------

class _Sock:
    def __init__(self, fail=None, on_send=None):
        self.sent = []
        self.fail = fail
        self.on_send = on_send

    async def send(self, data):
        if self.on_send is not None:
            self.on_send()
        if self.fail is not None:
            raise self.fail
        self.sent.append(json.loads(data))


def _run_announce(app, holder, sockets, wid=4242):
    app.ctx.control_clients[holder] = (sockets if isinstance(sockets, list)
                                       else set(sockets))
    app.ctx.active_client_id = holder
    asyncio.run(app.ctx.announce_launch(wid))


def test_no_holder_sends_nothing_and_raises_nothing(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    app.ctx.active_client_id = None
    other = _Sock()
    app.ctx.control_clients["X"] = {other}
    asyncio.run(app.ctx.announce_launch(4242))
    assert other.sent == []


def test_one_frame_even_with_several_healthy_holder_sockets(tmp_path,
                                                           monkeypatch):
    app = _app(tmp_path, monkeypatch)
    first, second = _Sock(), _Sock()
    _run_announce(app, "H", _Ordered([first, second]))
    assert len(first.sent) == 1 and second.sent == []
    assert first.sent[0] == {"type": "session_launched", "id": 4242,
                             "gen": app.ctx.lease_generation,
                             "bootId": app.ctx.boot_id}


def test_an_instant_failure_fails_over_to_a_sibling(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    dead, ok = _Sock(fail=ConnectionError("gone")), _Sock()
    _run_announce(app, "H", _Ordered([dead, ok]))
    assert len(ok.sent) == 1 and ok.sent[0]["id"] == 4242


def test_the_frame_is_dropped_once_the_lease_moves_on(tmp_path, monkeypatch):
    """A takeover between the lease snapshot and a send: the sibling is
    never sent the old holding's frame."""
    app = _app(tmp_path, monkeypatch)

    def takeover():
        app.ctx.lease_generation += 1
    dead = _Sock(fail=ConnectionError("gone"), on_send=takeover)
    ok = _Sock()
    _run_announce(app, "H", _Ordered([dead, ok]))
    assert ok.sent == []


def test_a_stalled_socket_is_closed_1012_and_a_sibling_gets_the_frame(
        tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    closed = []
    monkeypatch.setattr(app.ctx.registry, "close_soon",
                        lambda sock, code, reason="": closed.append(code))

    class _Stuck:
        async def send(self, data):
            await asyncio.Event().wait()
    stuck, ok = _Stuck(), _Sock()
    # Order inside a set is arbitrary; force the stalled one first.
    app.ctx.control_clients["H"] = _Ordered([stuck, ok])
    app.ctx.active_client_id = "H"
    t0 = time.monotonic()
    asyncio.run(app.ctx.announce_launch(4242))
    elapsed = time.monotonic() - t0
    assert closed == [1012]
    assert len(ok.sent) == 1
    assert CONTROL_SEND_TIMEOUT * 0.9 <= elapsed < CONTROL_SEND_TIMEOUT + 1.0


class _Ordered(list):
    """A list standing in for the client's socket set, so a cell can fix the
    order the announce tries them in (``list(set)`` has none)."""
