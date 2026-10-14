"""#252 phase 2, atom 3: the socket routes, end to end.

A real broker A (LiveApp) relays to :class:`FakeUpstreamWs` (B). The page is
a ``websockets`` client: it mints a ticket with ``POST
/peer/<id>/ws-ticket`` (both tokens and the pairing header), then opens
``/peer/<id>/ws`` or ``/peer/<id>/control`` with ``?ticket=`` and nothing
else. Covered: the mint's gate and body, single use and binding at the
upgrade, the upstream handshake, frames both ways, the socket pool, revoke
and the config sweep, the close codes through the route, shutdown, a dead
peer, and the log.
"""

from __future__ import annotations

import asyncio
import logging
import re
import socket
import sys

import httpx
import pytest
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed

from webterm.broker import peer_sockets as ps
from webterm.broker import peers
from webterm.broker.app import create_app

from .auth_helpers import TEST_TOKEN
from .peer_upstream import Answer, FakeUpstream, LiveApp
from .peer_upstream_ws import BINARY, RESIZED, FakeUpstreamWs

B_TOKEN = "b-browser-token-secret"
HANDSHAKE_NAMES = {"host", "upgrade", "connection", "sec-websocket-key",
                   "sec-websocket-version", "authorization"}
_seq = 0


@pytest.fixture(scope="module")
def fake():
    srv = FakeUpstreamWs().start()
    yield srv
    srv.stop()


@pytest.fixture(scope="module")
def http_b():
    srv = FakeUpstream().start()
    yield srv
    srv.stop()


def _dead_origin():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return "http://127.0.0.1:%d" % sock.getsockname()[1]


def _make_app(tmp_path, peer_map):
    global _seq
    _seq += 1
    return create_app({"state_path": str(tmp_path / "webterm_state.json"),
                       "auth_token": TEST_TOKEN, "serve_ui": False,
                       "peers": peer_map},
                      port=4999, name=f"webterm-peer-ws-{_seq}")


@pytest.fixture
def live(tmp_path, fake):
    app = _make_app(tmp_path, {"b": {"origin": fake.origin},
                               "c": {"origin": fake.origin}})
    srv = LiveApp(app).start()
    yield srv
    srv.stop()


def _on_loop(live, fn, *args):
    """Run ``fn(*args)`` on A's loop (the registry's futures live there)."""
    done = __import__("threading").Event()

    def call():
        try:
            fn(*args)
        finally:
            done.set()
    live._loop.call_soon_threadsafe(call)
    assert done.wait(5)


def headers(origin, *, a=TEST_TOKEN, b=B_TOKEN):
    h = {}
    if a is not None:
        h["Authorization"] = "Bearer " + a
    if b is not None:
        h[peers.PEER_TOKEN_HEADER] = b
    if origin is not None:
        h[peers.PEER_ORIGIN_HEADER] = origin
    return h


async def mint(live, peer, origin, route="ws", cid="c1", *, h=None,
               body=None):
    async with httpx.AsyncClient(timeout=10, trust_env=False) as c:
        kw = ({"content": body} if body is not None
              else {"json": {"route": route, "clientId": cid}})
        return await c.post(live.url + "/peer/%s/ws-ticket" % peer,
                            headers=h if h is not None else headers(origin),
                            **kw)


async def ticket(live, peer, origin, route="ws", cid="c1"):
    r = await mint(live, peer, origin, route, cid)
    assert r.status_code == 200, r.text
    return r.json()["ticket"]


async def page(live, peer, route, qs, t):
    url = "ws://127.0.0.1:%d/peer/%s/%s?%s%sticket=%s" % (
        live.port, peer, route, qs, "&" if qs else "", t)
    # The page's own client logs under a name of its own, so a log scan
    # sees only what the broker under test wrote.
    return await connect(url, proxy=None, open_timeout=5,
                         compression=None, max_size=None,
                         logger=logging.getLogger("test.page"))


async def opened(fake, live, peer, route, qs, t):
    """The page's socket and the fake's, once A has dialed it."""
    n = len(fake.conns)
    ws = await page(live, peer, route, qs, t)
    return ws, fake.wait_conn(n + 1)


async def closed_with(ws, timeout=3.0):
    async def drain():
        try:
            while True:
                await ws.recv()
        except ConnectionClosed:
            pass
    await asyncio.wait_for(drain(), timeout)
    rc = ws.protocol.close_rcvd
    return (rc.code, rc.reason) if rc else (None, "")


async def recv_n(ws, n, timeout=3.0):
    return [await asyncio.wait_for(ws.recv(), timeout) for _ in range(n)]


async def wait_for(pred, timeout=3.0):
    for _ in range(int(timeout / 0.02)):
        if pred():
            return True
        await asyncio.sleep(0.02)
    return pred()


def assert_log_clean(caplog, *secrets):
    """No record names a token, a ticket, an address of B's, or the text
    of an exception: a dial failure is logged by its class alone."""
    text = "\n".join(r.getMessage() for r in caplog.records
                     if not r.name.startswith("test.page"))
    for needle in ("token=", "ticket=", TEST_TOKEN, B_TOKEN) + secrets:
        assert needle not in text, needle
    failed = [r.getMessage() for r in caplog.records
              if " socket for " in r.getMessage()
              and " failed (" in r.getMessage()]
    assert failed, "no dial-failure line was logged"
    for line in failed:
        assert re.search(r" failed \([A-Za-z_][A-Za-z0-9_]*\)$", line), line


def conn_closed(conn, timeout=3.0):
    assert conn.done.wait(timeout), "the fake's socket did not close"
    return conn.closed


# ---- the mint -----------------------------------------------------------------

async def test_the_mint_runs_the_browser_gate(live, fake):
    o = fake.origin
    for h, want in ((headers(o, a=None), (401, "auth_required")),
                    (headers(o, a="wrong"), (401, "auth_required")),
                    (headers(o, b=None), (401, "auth_required")),
                    (headers(None), (409, "peer_origin_changed")),
                    (headers("http://elsewhere:1"),
                     (409, "peer_origin_changed"))):
        r = await mint(live, "b", o, h=h)
        assert (r.status_code, r.json()["error"]) == want
        assert "ticket" not in r.json()
        assert r.headers[peers.PEER_REFUSAL_HEADER] == want[1]
    r = await mint(live, "zz", o)
    assert (r.status_code, r.json()["error"]) == (404, "unknown_peer")
    r = await mint(live, "b", o)
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True and body["ttl"] == ps.PEER_TICKET_TTL_S
    assert len(body["ticket"]) == 43
    assert r.headers["Cache-Control"] == "no-store"
    assert len(live.app.ctx.peer_tickets) == 1
    assert fake.handshakes == [] or all(
        hs.query.find(body["ticket"]) < 0 for hs in fake.handshakes)


@pytest.mark.parametrize("missing", ["httpx", "websockets-12"])
async def test_the_mint_is_503_without_the_proxy_extra(tmp_path, fake,
                                                       monkeypatch, missing):
    import types
    with monkeypatch.context() as m:
        if missing == "httpx":
            m.setitem(sys.modules, "httpx", None)
        else:
            old = types.ModuleType("websockets")
            old.__version__ = "12.0"
            m.setitem(sys.modules, "websockets", old)
        app = _make_app(tmp_path, {"b": {"origin": fake.origin}})
    srv = LiveApp(app).start()
    try:
        r = await mint(srv, "b", fake.origin)
        assert (r.status_code, r.json()["error"]) == (503,
                                                      "proxy_unavailable")
        assert len(app.ctx.peer_tickets) == 0
    finally:
        srv.stop()


@pytest.mark.parametrize("body", [
    b'{"route": "browserland", "clientId": "c1"}',
    b'{"route": "ws"}',
    b'{"route": "ws", "clientId": "  "}',
    b'{"route": "ws", "clientId": 7}',
    # Review round 1 (Q18): refused, never normalised.
    b'{"route": "ws", "clientId": " c1"}',
    b'{"route": "ws", "clientId": "c1 "}',
    b'{"route": "ws", "clientId": "' + b"c" * 129 + b'"}',
    b'["ws", "c1"]',
    b"not json",
    b'{"route": "ws", "clientId": "c1", "pad": "' + b"x" * 5000 + b'"}',
])
async def test_a_bad_mint_body_is_400(live, fake, body):
    r = await mint(live, "b", fake.origin, body=body)
    assert (r.status_code, r.json()["error"]) == (400, "bad_ticket")
    assert len(live.app.ctx.peer_tickets) == 0


async def test_non_upgrade_requests_never_dial(live, fake):
    n = len(fake.handshakes)
    async with httpx.AsyncClient(timeout=10, trust_env=False) as c:
        r = await c.get(live.url + "/peer/b/ws-ticket",
                        headers=headers(fake.origin))
        assert r.status_code == 405
        for route in ("ws", "control"):
            r = await c.get(live.url + "/peer/b/%s?ticket=x" % route,
                            headers=headers(fake.origin))
            # Sanic's own answer to a plain GET on a socket route.
            assert r.status_code == 426, (route, r.status_code)
    assert len(fake.handshakes) == n
    assert len(live.app.ctx.peer_tickets) == 0


# ---- redemption and the upstream handshake ------------------------------------

async def test_a_ticket_opens_the_peer_socket(live, fake):
    n = len(fake.handshakes)
    t = await ticket(live, "b", fake.origin)
    ws = await page(live, "b", "ws", "session=5&clientId=c1", t)
    try:
        got = await recv_n(ws, 2)
        assert got == [RESIZED, BINARY]
        hs = fake.handshakes[n]
        assert (hs.path, hs.query) == ("/ws", "session=5&clientId=c1")
        assert hs.names == HANDSHAKE_NAMES
        assert hs.all("Authorization") == ["Bearer " + B_TOKEN]
        crossed = repr(hs.headers) + hs.query
        assert TEST_TOKEN not in crossed and t not in crossed
        assert len(live.app.ctx.peer_sockets) == 1
    finally:
        await ws.close()


async def test_a_ticket_works_once(live, fake):
    t = await ticket(live, "b", fake.origin)
    n = len(fake.handshakes)
    ws1 = await page(live, "b", "ws", "clientId=c1", t)
    await recv_n(ws1, 2)
    ws2 = await page(live, "b", "ws", "clientId=c1", t)
    assert await closed_with(ws2, 1.0) == (4403, "ticket")
    assert len(fake.handshakes) == n + 1
    await ws1.close()


@pytest.mark.parametrize("peer, route, qs", [
    ("b", "ws", "clientId=c1&fake=never"),        # minted for control
    ("b", "control", "clientId=c2"),               # another client
    ("c", "control", "clientId=c1"),               # another peer
    ("b", "control", ""),                          # no clientId
    ("b", "control", "clientId=%20c1"),            # padded: no strip (Q18)
])
async def test_a_mis_bound_ticket_is_4403_and_burned(live, fake, peer, route,
                                                     qs):
    t = await ticket(live, "b", fake.origin, route="control", cid="c1")
    n = len(fake.handshakes)
    ws = await page(live, peer, route, qs, t)
    assert await closed_with(ws) == (4403, "ticket")
    # Burned: the right presentation fails too.
    ws = await page(live, "b", "control", "clientId=c1", t)
    assert await closed_with(ws) == (4403, "ticket")
    assert len(fake.handshakes) == n
    assert live.app.ctx.peer_sock_cap._busy == {}


async def test_an_expired_ticket_is_4403(live, fake, monkeypatch):
    monkeypatch.setattr(ps, "PEER_TICKET_TTL_S", 0.2)
    t = await ticket(live, "b", fake.origin)
    await asyncio.sleep(0.4)
    n = len(fake.handshakes)
    ws = await page(live, "b", "ws", "clientId=c1", t)
    assert await closed_with(ws) == (4403, "ticket")
    assert len(fake.handshakes) == n


async def test_frames_cross_the_route_verbatim(live, fake):
    t = await ticket(live, "b", fake.origin)
    n = len(fake.conns)
    ws = await page(live, "b", "ws", "clientId=c1", t)
    conn = fake.wait_conn(n + 1)
    try:
        got = await recv_n(ws, 2)
        assert got == [RESIZED, BINARY]
        assert type(got[0]) is str and type(got[1]) is bytes
        await ws.send('{"type":"input","data":"echo hi\\r"}')
        await ws.send(b"\x00\x01\xfe\xff")
        back = await recv_n(ws, 2)
        assert back == ['{"type":"input","data":"echo hi\\r"}',
                        b"\x00\x01\xfe\xff"]
        assert conn.frames_in == back
    finally:
        await ws.close()


# ---- pools --------------------------------------------------------------------

async def test_the_socket_pool_is_its_own(tmp_path, fake, http_b):
    hold = __import__("threading").Event()
    http_b.routes["/info"] = lambda seen: Answer(
        200, [("Content-Type", "application/json")], b"{}", hold=hold)
    app = _make_app(tmp_path, {"b": {"origin": fake.origin},
                               "h": {"origin": http_b.origin}})
    srv = LiveApp(app).start()
    socks = []
    try:
        for i in range(ps.PEER_MAX_SOCKETS_PER_PEER):
            t = await ticket(srv, "b", fake.origin, cid="c%d" % i)
            ws = await page(srv, "b", "ws", "clientId=c%d" % i, t)
            await recv_n(ws, 2)
            socks.append(ws)
        r = await mint(srv, "b", fake.origin)
        assert (r.status_code, r.json()["error"]) == (503, "peer_busy")
        # The HTTP dial pool is untouched by 32 sockets to b...
        async with httpx.AsyncClient(timeout=10, trust_env=False) as c:
            r = await c.get(srv.url + "/peer/b/info",
                            headers=headers(fake.origin))
        assert r.status_code != 503 or r.json().get("error") != "peer_busy"
        # ...and 8 held HTTP dials to h leave h's mint free.
        async with httpx.AsyncClient(timeout=10, trust_env=False,
                                     limits=httpx.Limits(
                                         max_connections=20)) as c:
            held = [asyncio.ensure_future(c.get(
                srv.url + "/peer/h/info", headers=headers(http_b.origin)))
                for _ in range(peers.PEER_MAX_DIALS_PER_PEER)]
            assert await wait_for(
                lambda: app.ctx.peer_cap._busy.get("h") ==
                peers.PEER_MAX_DIALS_PER_PEER)
            r = await mint(srv, "h", http_b.origin)
            assert r.status_code == 200
            hold.set()
            await asyncio.gather(*held)
    finally:
        hold.set()
        for ws in socks:
            await ws.close()
        srv.stop()


# ---- revocation ---------------------------------------------------------------

async def test_revoke_closes_one_peer_and_voids_its_tickets(live, fake):
    tb = await ticket(live, "b", fake.origin, cid="c1")
    wb, cb = await opened(fake, live, "b", "ws", "clientId=c1&fake=never", tb)
    tc = await ticket(live, "c", fake.origin, cid="c1")
    wc, cc = await opened(fake, live, "c", "ws", "clientId=c1&fake=never", tc)
    spare = await ticket(live, "b", fake.origin, cid="c9")
    n = len(fake.handshakes)
    _on_loop(live, live.app.ctx.peer_sockets.revoke, "b")
    # Q14: B's leg 1000, the page 4403 -- never the shutdown 1001.
    assert await closed_with(wb) == (4403, "revoked")
    assert conn_closed(cb) == (1000, "")
    ws = await page(live, "b", "ws", "clientId=c9", spare)
    assert await closed_with(ws) == (4403, "ticket")
    assert len(fake.handshakes) == n
    # c's pair is still open.
    await wc.send("still here")
    assert await wait_for(lambda: cc.frames_in == ["still here"])
    _on_loop(live, live.app.ctx.peer_sockets.revoke)
    assert await closed_with(wc) == (4403, "revoked")
    assert await wait_for(lambda: len(live.app.ctx.peer_sockets) == 0)
    assert await wait_for(lambda: live.app.ctx.peer_sock_cap._busy == {})


async def test_a_changed_origin_is_swept_at_the_next_mint(live, fake):
    tb = await ticket(live, "b", fake.origin)
    wb, cb = await opened(fake, live, "b", "ws", "clientId=c1&fake=never", tb)
    spare = await ticket(live, "b", fake.origin, cid="c9")
    tc = await ticket(live, "c", fake.origin)
    wc = await page(live, "c", "ws", "clientId=c1&fake=never", tc)
    moved = "http://127.0.0.2:%d" % fake.port
    _on_loop(live, live.app.ctx.peers["b"].__setitem__, "origin", moved)
    await asyncio.sleep(0.3)
    assert wb.protocol.close_rcvd is None     # nothing until the next event
    await ticket(live, "c", fake.origin, cid="other")
    assert await closed_with(wb) == (4403, "revoked")
    assert conn_closed(cb) == (1000, "")
    ws = await page(live, "b", "ws", "clientId=c9", spare)
    assert await closed_with(ws) == (4403, "ticket")
    assert wc.protocol.close_rcvd is None
    await wc.close()


async def test_a_changed_token_is_swept_at_the_next_redemption(live, fake):
    tb = await ticket(live, "b", fake.origin)
    wb = await page(live, "b", "ws", "clientId=c1&fake=never", tb)
    tc = await ticket(live, "c", fake.origin)
    _on_loop(live, setattr, live.app.ctx, "auth_token", "rotated-token")
    # The redemption of c's ticket sweeps first: both go.
    wc = await page(live, "c", "ws", "clientId=c1", tc)
    assert await closed_with(wb) == (4403, "revoked")
    assert await closed_with(wc) == (4403, "ticket")


async def test_apply_change_sweeps_without_a_mint(live, fake):
    tb = await ticket(live, "b", fake.origin)
    wb, _ = await opened(fake, live, "b", "ws", "clientId=c1&fake=never", tb)
    assert await wait_for(lambda: len(live.app.ctx.peer_sockets) == 1)
    _on_loop(live, live.app.ctx.peers.pop, "b")
    await asyncio.sleep(0.3)
    assert wb.protocol.close_rcvd is None
    _on_loop(live, peers.apply_change, live.app)
    assert await closed_with(wb) == (4403, "revoked")


async def test_a_change_during_the_dial_is_caught(live, fake, monkeypatch):
    # Codex MUST 2: the sweep runs while the upstream handshake is still in
    # flight, so it sees neither the ticket nor a pair.
    gate = __import__("threading").Event()
    real = ps.dial_upstream

    async def slow_dial(url, token):
        while not gate.is_set():
            await asyncio.sleep(0.02)
        return await real(url, token)
    monkeypatch.setattr(ps, "dial_upstream", slow_dial)
    t = await ticket(live, "b", fake.origin)
    n = len(fake.conns)
    ws = await page(live, "b", "ws", "clientId=c1&fake=never", t)
    await asyncio.sleep(0.2)
    moved = "http://127.0.0.2:%d" % fake.port
    _on_loop(live, live.app.ctx.peers["b"].__setitem__, "origin", moved)
    _on_loop(live, peers.apply_change, live.app)
    gate.set()
    assert await closed_with(ws) == (4403, "revoked")
    assert conn_closed(fake.wait_conn(n + 1)) == (1000, "")
    assert await wait_for(lambda: live.app.ctx.peer_sock_cap._busy == {})
    assert len(live.app.ctx.peer_sockets) == 0


async def test_a_repeated_client_id_is_refused(live, fake):
    # Codex SHOULD 7: one clientId, the one the peer will read.
    t = await ticket(live, "b", fake.origin, cid="c1")
    n = len(fake.handshakes)
    ws = await page(live, "b", "ws", "clientId=c1&clientId=c2", t)
    assert await closed_with(ws) == (4403, "ticket")
    assert len(fake.handshakes) == n


async def test_an_abandoned_ticket_expires_on_its_own(live, fake,
                                                      monkeypatch):
    # Codex SHOULD 5: no later mint is needed to drop it (and B's token).
    monkeypatch.setattr(ps, "PEER_TICKET_TTL_S", 0.2)
    await ticket(live, "b", fake.origin)
    assert len(live.app.ctx.peer_tickets) == 1
    assert await wait_for(lambda: len(live.app.ctx.peer_tickets) == 0, 2.0)
    assert live.app.ctx.peer_sock_cap._busy == {}


# ---- closes through the route ---------------------------------------------------

async def test_a_page_close_closes_the_peer_1000(live, fake):
    t = await ticket(live, "b", fake.origin)
    ws, conn = await opened(fake, live, "b", "ws", "clientId=c1&fake=never", t)
    await ws.close()
    assert conn_closed(conn) == (1000, "")
    assert await wait_for(lambda: live.app.ctx.peer_sock_cap._busy == {})


@pytest.mark.parametrize("script, want", [
    ("close-4409", (4409, "deactivated")),
    # Review round 1b: 4401 on these routes is only ever the peer's own.
    ("close-4401", (4401, "auth required")),
    ("close-2000", (1012, "")),
    ("close-empty", (1012, "")),
])
async def test_the_peer_close_reaches_the_page(live, fake, script, want):
    t = await ticket(live, "b", fake.origin)
    ws = await page(live, "b", "ws", "clientId=c1&fake=" + script, t)
    assert await closed_with(ws) == want


async def test_an_oversized_page_message_is_no_shutdown(tmp_path, fake):
    # Codex MUST 3 through real Sanic: Sanic closes the page 1009 itself,
    # which is not this broker stopping, so the peer hears 1000.
    app = _make_app(tmp_path, {"b": {"origin": fake.origin}})
    app.config.WEBSOCKET_MAX_SIZE = 1024
    srv = LiveApp(app).start()
    try:
        t = await ticket(srv, "b", fake.origin)
        ws, conn = await opened(fake, srv, "b", "ws",
                                "clientId=c1&fake=never", t)
        await ws.send("x" * 4096)
        assert (await closed_with(ws))[0] == 1009
        assert conn_closed(conn) == (1000, "")
    finally:
        srv.stop()


async def test_a_killed_peer_is_1012(live, fake):
    t = await ticket(live, "b", fake.origin)
    ws, _ = await opened(fake, live, "b", "ws", "clientId=c1&fake=never", t)
    fake.close_all()
    assert await closed_with(ws) == (1012, "")


def _fast_pings(app, monkeypatch, period=0.2):
    # Both legs ping every `period` and give up after as long: the page's
    # leg by Sanic (read per connection), the peer's by A's client.
    app.config.WEBSOCKET_PING_INTERVAL = period
    app.config.WEBSOCKET_PING_TIMEOUT = period
    kw = dict(ps.UPSTREAM_WS_KWARGS, ping_interval=period,
              ping_timeout=period)
    monkeypatch.setattr(ps, "UPSTREAM_WS_KWARGS", kw)


async def test_a_quiet_pair_whose_legs_answer_pings_stays_open(
        tmp_path, fake, monkeypatch):
    # Review round 1b (P2-R3 amended): no idle ceiling. Silent for far more
    # than many ping periods, both legs answering: nothing closes.
    app = _make_app(tmp_path, {"b": {"origin": fake.origin}})
    _fast_pings(app, monkeypatch)
    srv = LiveApp(app).start()
    try:
        t = await ticket(srv, "b", fake.origin)
        ws, conn = await opened(fake, srv, "b", "ws",
                                "clientId=c1&fake=never", t)
        await asyncio.sleep(2.0)
        assert ws.protocol.close_rcvd is None and not conn.done.is_set()
        await ws.send("still here")
        assert await wait_for(lambda: conn.frames_in == ["still here"])
        await ws.close()
        assert conn_closed(conn) == (1000, "")
    finally:
        srv.stop()


async def test_a_page_that_stops_answering_pings_ends_both_legs(
        tmp_path, fake, monkeypatch):
    app = _make_app(tmp_path, {"b": {"origin": fake.origin}})
    _fast_pings(app, monkeypatch)
    srv = LiveApp(app).start()
    try:
        t = await ticket(srv, "b", fake.origin)
        ws, conn = await opened(fake, srv, "b", "ws",
                                "clientId=c1&fake=never", t)
        ws.transport.pause_reading()          # the page goes deaf
        assert conn.done.wait(3.0), "the peer's leg stayed open"
        assert conn.closed == (1000, "")
        assert await wait_for(lambda: app.ctx.peer_sock_cap._busy == {})
        ws.transport.abort()
    finally:
        srv.stop()


async def test_a_stopping_broker_closes_the_peer_1001(tmp_path, fake):
    app = _make_app(tmp_path, {"b": {"origin": fake.origin}})
    srv = LiveApp(app).start()
    t = await ticket(srv, "b", fake.origin)
    ws, conn = await opened(fake, srv, "b", "ws", "clientId=c1&fake=never", t)
    await asyncio.get_running_loop().run_in_executor(None, srv.stop)
    assert conn_closed(conn) == (1001, "")
    # The page's leg: LiveApp halts its loop mid-shutdown, so what the page
    # hears here is the harness's; relay_sockets' 1001 to it is pinned in
    # test_peer_ws_relay (a cancelled handler).
    ws.transport.abort()


async def test_a_dead_peer_is_1011_and_frees_its_slot(tmp_path, fake,
                                                     caplog):
    caplog.set_level(logging.DEBUG)
    dead = _dead_origin()
    app = _make_app(tmp_path, {"d": {"origin": dead}})
    srv = LiveApp(app).start()
    try:
        t = await ticket(srv, "d", dead)
        ws = await page(srv, "d", "ws", "clientId=c1", t)
        assert await closed_with(ws, 6.0) == (1011, "peer_unreachable")
        assert await wait_for(lambda: app.ctx.peer_sock_cap._busy == {})
        r = await mint(srv, "d", dead)
        assert r.status_code == 200
    finally:
        srv.stop()
    # Review round 1: the failure path is scanned too.
    assert_log_clean(caplog, t, dead.split("//")[1], dead.rsplit(":", 1)[1])


async def test_a_redirecting_peer_is_1011_and_followed_nowhere(live, fake,
                                                              http_b,
                                                              caplog):
    caplog.set_level(logging.DEBUG)
    seen = len(http_b.seen)
    fake.redirect_to = "ws://127.0.0.1:%d/elsewhere" % http_b.port
    try:
        t = await ticket(live, "b", fake.origin)
        ws = await page(live, "b", "ws", "clientId=c1", t)
        assert await closed_with(ws, 6.0) == (1011, "peer_unreachable")
    finally:
        fake.redirect_to = None
    assert len(http_b.seen) == seen
    assert await wait_for(lambda: live.app.ctx.peer_sock_cap._busy == {})
    assert_log_clean(caplog, t, str(fake.port), str(http_b.port),
                     "elsewhere")


# ---- the log ------------------------------------------------------------------

async def test_nothing_secret_is_logged(live, fake, caplog):
    caplog.set_level(logging.DEBUG)
    t = await ticket(live, "b", fake.origin, cid="c1-secretish")
    ws = await page(live, "b", "ws", "session=5&clientId=c1-secretish", t)
    await recv_n(ws, 2)
    await ws.close()
    conn = fake.conns[-1]
    conn_closed(conn)
    reused = await page(live, "b", "ws", "clientId=c1-secretish", t)
    await closed_with(reused)
    await asyncio.sleep(0.2)
    text = "\n".join(r.getMessage() for r in caplog.records
                     if not r.name.startswith("test.page"))
    assert "socket for c1-secre opened" in text
    for needle in ("token", B_TOKEN, TEST_TOKEN, t, str(fake.port),
                   "c1-secretish"):
        assert needle not in text, needle
