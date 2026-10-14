"""#252 phase 2, atom 2: the socket relay core, before any route.

``peer_sockets.upstream_ws_url``, ``dial_upstream`` (the handshake A sends B,
no redirect followed, no environment proxy) and ``relay_sockets`` between a
:class:`FakeDown` (Sanic's side, as the page sees it) and a real upstream
socket to :class:`FakeUpstreamWs` (B).
"""

from __future__ import annotations

import asyncio
import logging

import pytest

from webterm.broker import peer_sockets as ps
from webterm.broker import peers

from .peer_upstream import FakeUpstream
from .peer_upstream_ws import BINARY, RESIZED, FakeDown, FakeUpstreamWs

BT = "b-token-BBBB-secret"
HANDSHAKE_NAMES = {"host", "upgrade", "connection", "sec-websocket-key",
                   "sec-websocket-version", "authorization"}


@pytest.fixture(scope="module")
def fake():
    srv = FakeUpstreamWs().start()
    yield srv
    srv.stop()


@pytest.fixture(scope="module")
def target():
    srv = FakeUpstream().start()
    yield srv
    srv.stop()


def _url(fake, path, qs=""):
    return "ws://127.0.0.1:%d%s%s" % (fake.port, path, "?" + qs if qs else "")


async def _wait(pred, timeout=3.0):
    for _ in range(int(timeout / 0.02)):
        if pred():
            return True
        await asyncio.sleep(0.02)
    return pred()


class Counting:
    """The upstream socket, counting the frames the relay reads from it."""

    def __init__(self, up):
        self.up = up
        self.reads = 0

    def __getattr__(self, name):
        return getattr(self.up, name)

    def __aiter__(self):
        return self._gen()

    async def _gen(self):
        async for frame in self.up:
            self.reads += 1
            yield frame


async def _start(fake, path, idle_s=None, wrap=None, down=None,
                 stopping=False):
    n = len(fake.conns)
    up = await ps.dial_upstream(_url(fake, path), BT)
    conn = fake.wait_conn(n + 1)
    down = down or FakeDown()
    logged = []
    relay_up = wrap(up) if wrap else up
    task = asyncio.ensure_future(ps.relay_sockets(
        down, relay_up, peer_id="b", route="ws", idle_s=idle_s,
        stopping=lambda: stopping, log=lambda *a: logged.append(a)))
    return up, conn, down, task, logged, relay_up


# ---- the URL ------------------------------------------------------------------

def test_the_upstream_url_carries_no_credential():
    got = ps.upstream_ws_url(
        "https://b:1", "ws", "session=5&clientId=c&ticket=T&token=X&auth=Y")
    assert got == "wss://b:1/ws?session=5&clientId=c"
    assert ps.upstream_ws_url("http://127.0.0.1:4448", "control",
                              "clientId=c1&ticket=T") \
        == "ws://127.0.0.1:4448/control?clientId=c1"
    assert ps.upstream_ws_url("http://b:2", "ws", "") == "ws://b:2/ws"
    # Review round 1: any letter case.
    assert ps.upstream_ws_url(
        "http://b:2", "ws", "clientId=c&Token=X&AUTH=Y&Ticket=T&TICKET=U") \
        == "ws://b:2/ws?clientId=c"


# ---- the handshake ------------------------------------------------------------

async def test_the_handshake_is_exact_and_the_token_rides_the_header(fake):
    n = len(fake.handshakes)
    up = await ps.dial_upstream(_url(fake, "/never", "session=5"), BT)
    try:
        hs = fake.handshakes[n]
        assert hs.path == "/never" and hs.query == "session=5"
        assert hs.names == HANDSHAKE_NAMES
        assert hs.all("Authorization") == ["Bearer " + BT]
        assert "token" not in hs.query and BT not in hs.query
    finally:
        await up.close()


def test_the_handshake_keywords_are_pinned():
    kw = ps.UPSTREAM_WS_KWARGS
    assert kw["proxy"] is None
    assert kw["compression"] is None
    assert kw["user_agent_header"] is None
    assert kw["max_size"] == ps.PEER_WS_FRAME_MAX
    assert kw["max_queue"] == 2
    assert kw["open_timeout"] == peers.PEER_CONNECT_TIMEOUT_S
    assert kw["ping_interval"] == 20
    assert kw["ping_timeout"] == 20
    assert kw["close_timeout"] == peers.PEER_CONNECT_TIMEOUT_S


async def test_no_environment_proxy_is_used(fake, monkeypatch):
    dead = "http://127.0.0.1:9"
    for name in ("WSS_PROXY", "WS_PROXY", "HTTPS_PROXY", "HTTP_PROXY",
                 "ALL_PROXY", "wss_proxy", "https_proxy", "all_proxy"):
        monkeypatch.setenv(name, dead)
    monkeypatch.delenv("NO_PROXY", raising=False)
    monkeypatch.delenv("no_proxy", raising=False)
    n = len(fake.handshakes)
    up = await ps.dial_upstream(_url(fake, "/never"), BT)
    await up.close()
    assert len(fake.handshakes) == n + 1


async def test_a_redirect_is_never_followed(fake, target):
    fake.redirect_to = "ws://127.0.0.1:%d/elsewhere" % target.port
    try:
        with pytest.raises(Exception):
            await ps.dial_upstream(_url(fake, "/ws"), BT)
    finally:
        fake.redirect_to = None
    assert target.seen == []
    assert fake.handshakes[-1].all("Authorization") == ["Bearer " + BT]


# ---- frames -------------------------------------------------------------------

async def test_peer_frames_reach_the_page_verbatim(fake):
    up, conn, down, task, _, _ = await _start(fake, "/ws")
    assert await _wait(lambda: len(down.sent) >= 2)
    assert down.sent[:2] == [RESIZED, BINARY]
    assert type(down.sent[0]) is str and type(down.sent[1]) is bytes
    down.page_close()
    assert await task == "page"


async def test_page_frames_reach_the_peer_verbatim(fake):
    up, conn, down, task, _, _ = await _start(fake, "/never")
    down.push("hello {\"a\": 1}")
    down.push(b"\x00\x01bin\xff")
    assert await _wait(lambda: len(conn.frames_in) >= 2)
    assert conn.frames_in == ["hello {\"a\": 1}", b"\x00\x01bin\xff"]
    assert type(conn.frames_in[1]) is bytes
    down.page_close()
    assert await task == "page"


async def test_a_slow_page_slows_the_peer_read(fake):
    box = {}

    def wrap(up):
        box["c"] = Counting(up)
        return box["c"]

    # The page's drain is held from the start: the first send blocks.
    down = FakeDown()
    down.drain.clear()
    up, conn, down, task, _, counting = await _start(fake, "/burst",
                                                     wrap=wrap, down=down)
    await asyncio.sleep(0.5)
    assert counting.reads == 1
    assert down.sent == []
    down.drain.set()
    assert await _wait(lambda: len(down.sent) == 6)
    assert counting.reads == 6
    down.page_close()
    await task


# ---- closes -------------------------------------------------------------------

async def test_the_peer_close_code_reaches_the_page(fake):
    up, conn, down, task, _, _ = await _start(fake, "/close-4409")
    assert await asyncio.wait_for(task, 3) == "peer"
    assert (down.close_code, down.close_reason) == (4409, "deactivated")


async def test_a_vanished_peer_is_1012(fake):
    up, conn, down, task, _, _ = await _start(fake, "/never")
    fake.close_all()
    assert await asyncio.wait_for(task, 3) == "peer"
    assert down.close_code == 1012


async def test_an_unsendable_peer_code_is_1012_without_a_traceback(fake,
                                                                    caplog):
    caplog.set_level(logging.DEBUG)
    up, conn, down, task, logged, _ = await _start(fake, "/close-2000")
    assert await asyncio.wait_for(task, 3) == "peer"
    assert down.close_code == 1012
    assert logged == []
    assert not [r for r in caplog.records if r.exc_info
                or "Traceback" in r.getMessage()]


async def test_a_codeless_peer_close_is_1012(fake):
    up, conn, down, task, _, _ = await _start(fake, "/close-empty")
    assert await asyncio.wait_for(task, 3) == "peer"
    assert up.protocol.close_rcvd.code == 1005     # what the peer sent
    assert down.close_code == 1012


async def test_a_page_close_closes_the_peer_1000(fake):
    up, conn, down, task, _, _ = await _start(fake, "/never")
    await asyncio.sleep(0.1)
    down.page_close()
    assert await asyncio.wait_for(task, 3) == "page"
    assert await asyncio.get_running_loop().run_in_executor(
        None, conn.done.wait, 1.0)
    assert conn.closed[0] == 1000


async def test_server_shutdown_closes_the_peer_1001(fake):
    # Sanic's close_if_idle at a stop: the server sends the page 1001 first.
    up, conn, down, task, _, _ = await _start(fake, "/never", stopping=True)
    await asyncio.sleep(0.1)
    down.server_close(1001)
    assert await asyncio.wait_for(task, 3) == "shutdown"
    assert await asyncio.get_running_loop().run_in_executor(
        None, conn.done.wait, 1.0)
    assert conn.closed[0] == 1001


async def test_a_server_first_close_that_is_no_stop_is_1000(fake):
    # Codex MUST 3: Sanic also closes first for a 1009 (an oversized
    # message) or a protocol error. Only the stop signal means 1001.
    up, conn, down, task, _, _ = await _start(fake, "/never")
    await asyncio.sleep(0.1)
    down.server_close(1009)
    assert await asyncio.wait_for(task, 3) == "page"
    assert await asyncio.get_running_loop().run_in_executor(
        None, conn.done.wait, 1.0)
    assert conn.closed[0] == 1000


async def test_a_cancel_during_teardown_cannot_abandon_the_closes(fake):
    # Codex MUST 1: the peer closed, the relay is closing the page, and
    # Sanic cancels the handler right then: the close still completes.
    down = FakeDown()
    down.close_gate = asyncio.Event()
    up, conn, down, task, _, _ = await _start(fake, "/close-4409", down=down)
    await asyncio.sleep(0.5)                 # the teardown waits on the gate
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    down.close_gate.set()
    for _ in range(100):
        if down.close_code is not None:
            break
        await asyncio.sleep(0.02)
    assert (down.close_code, down.close_reason) == (4409, "deactivated")


async def test_a_cancelled_handler_closes_the_peer_1001(fake):
    # Sanic's _cancel_websocket_tasks at a stop, the page's socket open.
    up, conn, down, task, _, _ = await _start(fake, "/never", stopping=True)
    await asyncio.sleep(0.1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert await asyncio.get_running_loop().run_in_executor(
        None, conn.done.wait, 1.0)
    assert conn.closed[0] == 1001
    assert down.close_code == 1001


async def test_a_cancelled_handler_after_a_page_close_is_1000(fake):
    # Sanic also cancels the handler once the page's connection is gone.
    up, conn, down, task, _, _ = await _start(fake, "/never")
    await asyncio.sleep(0.1)
    down.ws_proto.close_rcvd = down.ws_proto.close_sent = None
    down.ws_proto.state = __import__("websockets").State.CLOSED
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert await asyncio.get_running_loop().run_in_executor(
        None, conn.done.wait, 1.0)
    assert conn.closed[0] == 1000


async def test_the_idle_ceiling_closes_both(fake):
    up, conn, down, task, _, _ = await _start(fake, "/never", idle_s=0.5)
    assert await asyncio.wait_for(task, 1.5) == "idle"
    assert (down.close_code, down.close_reason) == (1000, "idle")
    assert await asyncio.get_running_loop().run_in_executor(
        None, conn.done.wait, 1.0)
    assert conn.closed[0] == 1000


async def test_page_frames_alone_keep_a_socket_alive(fake):
    up, conn, down, task, _, _ = await _start(fake, "/never", idle_s=0.5)
    for i in range(4):
        await asyncio.sleep(0.3)
        down.push("k%d" % i)
    assert not task.done()
    down.page_close()
    assert await task == "page"


async def test_peer_frames_alone_keep_a_socket_alive(fake):
    up, conn, down, task, _, _ = await _start(fake, "/tick", idle_s=0.5)
    await asyncio.sleep(1.2)
    assert not task.done()
    assert len(down.sent) >= 3
    down.page_close()
    assert await task == "page"


# ---- the log ------------------------------------------------------------------

async def test_nothing_secret_is_logged(fake, caplog):
    # Undo any level an earlier dial set, so a missing setLevel shows.
    logging.getLogger("websockets").setLevel(logging.NOTSET)
    caplog.set_level(logging.DEBUG)
    up, conn, down, task, logged, _ = await _start(fake, "/ws")
    assert await _wait(lambda: len(down.sent) >= 2)
    down.page_close()
    await task
    text = "\n".join(r.getMessage() for r in caplog.records) + repr(logged)
    for needle in ("token", BT, "Bearer", str(fake.port)):
        assert needle not in text, needle
