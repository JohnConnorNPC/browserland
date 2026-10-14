"""#252 phase 1, atom 2: the relay core, before any route exists.

``peers.classify`` (the explicit allowlist, the hop refusal, the decode rule),
the header policy both ways, the forced content type, and ``peers.relay``
itself dialing a recording fake upstream (tests/peer_upstream.py) with the
real httpx client from ``peers.make_client``. The downstream is a sink that
stands in for Sanic's ``request.respond`` and records every chunk it is sent.
"""

from __future__ import annotations

import asyncio
import json
import threading

import httpx
import pytest
from sanic.compat import Header

from webterm.broker import app as broker_app
from webterm.broker import peers
from webterm.protocol import MCP_ADMIN_HEADER, SCOPE_HEADER

from .peer_upstream import Answer, FakeUpstream, json_answer

ADMIN = broker_app.ADMIN_HEADER
A_TOKEN = "a-token-AAAA"
B_TOKEN = "b-token-BBBB"

# ---- the tables ---------------------------------------------------------------
# Research #252 S7, typed out again on purpose: the code's tables must equal
# the design's, entry for entry.
S7_BROWSER = (
    [("GET", p) for p in "/info /sessions /profiles /profiles/config "
     "/profiles/detect /recordings /recording /recording/notes "
     "/update/check".split()]
    + [("POST", p) for p in "/launch /file/list /file/read /file/read_chunk "
       "/file/hash /file/write /file/upload /file/upload_begin "
       "/file/upload_chunk /file/upload_commit /file/upload_abort "
       "/file/paste_image /file/delete /file/mkdir /file/copy /file/move "
       "/file/zip /file/unzip /file/stat /file/setattr /session/procs "
       "/session/kill /session/git /session/mcp /session/control "
       "/recording/begin /recording/chunk /recording/commit /recording/abort "
       "/recording/delete /recording/notes /update/apply /update/policy "
       "/mods/policy /profiles/config".split()])
S7_MCP = (
    [("GET", p) for p in "/mcp/info /mcp/terminals /mcp/profiles "
     "/mcp/desktop /mcp/config".split()]
    + [("POST", p) for p in "/mcp/read /mcp/input /mcp/reset /mcp/flush "
       "/mcp/pace /mcp/launch /mcp/window /mcp/desktop /mcp/config "
       "/mcp/knobs".split()])


def test_tables_are_the_design_verbatim():
    assert peers.BROWSER_ALLOW == frozenset(S7_BROWSER)
    assert len(peers.BROWSER_ALLOW) == 9 + 35
    assert peers.MCP_ALLOW == frozenset(S7_MCP)
    assert len(peers.MCP_ALLOW) == 15


def test_mod_store_id_shape_is_the_brokers():
    assert peers.MOD_STORE_RE.pattern == (
        "/mod-store/(" + broker_app._MODSTORE_ID_RE.pattern[2:-2] + ")")


@pytest.mark.parametrize("method,rest,gate,path", [
    ("GET", "info", "browser", "/info"),
    ("GET", "inf%6F", "browser", "/info"),          # S4: decoded once
    ("POST", "file/list", "browser", "/file/list"),
    ("PUT", "mod-store/clock", "browser", "/mod-store/clock"),
    ("GET", "mod-store/clock", "browser", "/mod-store/clock"),
    ("GET", "mcp/terminals", "mcp", "/mcp/terminals"),
    ("POST", "mcp/read", "mcp", "/mcp/read"),
])
def test_classify_accepts(method, rest, gate, path):
    assert peers.classify(method, rest) == (gate, path)


@pytest.mark.parametrize("method,rest", [
    ("GET", ""), ("GET", "help-corpus.json"), ("GET", "vendor/xterm.js"),
    ("GET", "mods/clock/1/clock.js"), ("POST", "mods/install"),
    ("POST", "mods/uninstall"), ("POST", "mods/rescan"),
    ("GET", "mods/installed"), ("POST", "restart"), ("GET", "browserland"),
    ("GET", "state"), ("PUT", "state"), ("GET", "status/fetch"),
    ("GET", "ws"), ("GET", "control"), ("OPTIONS", "info"),
    ("POST", "info"),                       # right path, wrong method
    ("DELETE", "mod-store/clock"), ("GET", "mod-store/a%2Fb"),
    ("GET", "mod-store/Clock"), ("GET", "info/"), ("GET", "/info"),
    ("GET", "file/../info"), ("GET", "mcp/info/x"),
])
def test_classify_denies(method, rest):
    assert peers.classify(method, rest) == (None, "not_relayed")


@pytest.mark.parametrize("rest", [
    "peer/b/info", "x/peer/y", "mod-store/a%2Fpeer%2Fb", "%70eer/b/info",
    "mod-store/peer",
])
def test_hop_is_refused_before_the_table(rest):
    assert peers.classify("GET", rest) == (None, "peer_hop_refused")


def test_mcp_gate_is_decided_on_the_decoded_segment():
    assert peers.is_mcp_path("mcp/info")
    assert peers.is_mcp_path("%6Dcp/info")
    assert not peers.is_mcp_path("info")
    assert not peers.is_mcp_path("mcpx/info")


# ---- header policy -------------------------------------------------------------

def _incoming(*extra):
    return Header([
        ("Authorization", "Bearer " + A_TOKEN),
        (peers.PEER_TOKEN_HEADER, B_TOKEN),
        (peers.PEER_ORIGIN_HEADER, "http://b:1"),
        ("Cookie", "sid=1"), ("Origin", "http://a:1"), ("Host", "a:1"),
        ("Referer", "http://a:1/"), ("X-Forwarded-For", "10.9.9.9"),
        ("Connection", "keep-alive, X-Sentinel"), ("Keep-Alive", "timeout=99"),
        ("TE", "trailers"), ("Trailer", "X-Sentinel"), ("Upgrade", "h2c"),
        ("Proxy-Authorization", "Basic c2VjcmV0"),
        ("Transfer-Encoding", "chunked"),
        ("Content-Type", "application/json"), ("Accept", "*/*"),
        *extra])


def _names(pairs):
    return [k.lower() for k, _ in pairs]


def test_browser_gate_headers():
    out = peers.upstream_headers(
        _incoming((SCOPE_HEADER, "s1"), (ADMIN, "adm"),
                  (MCP_ADMIN_HEADER, "madm"), ("Content-Encoding", "gzip")),
        gate=peers.GATE_BROWSER, bearer=B_TOKEN, admin_header=ADMIN)
    assert sorted(_names(out)) == sorted(
        ["content-type", "content-encoding", "accept", SCOPE_HEADER.lower(),
         ADMIN.lower(), "accept-encoding", "authorization"])
    assert ("Authorization", "Bearer " + B_TOKEN) in out
    assert not [v for _, v in out if A_TOKEN in v]
    assert (ADMIN, "adm") in out


def test_mcp_gate_forwards_the_mcp_admin_header():
    out = peers.upstream_headers(
        _incoming((MCP_ADMIN_HEADER, "madm"), (ADMIN, "adm")),
        gate=peers.GATE_MCP, bearer=B_TOKEN, admin_header=ADMIN)
    assert (MCP_ADMIN_HEADER, "madm") in out
    # #191's browser-realm admin credential never rides the MCP gate.
    assert ADMIN.lower() not in _names(out)
    assert SCOPE_HEADER.lower() not in _names(out)


def test_a_repeated_scope_is_refused_locally():
    with pytest.raises(peers.PolicyError) as exc:
        peers.upstream_headers(
            _incoming((SCOPE_HEADER, "s1"), (SCOPE_HEADER, "s2")),
            gate=peers.GATE_MCP, bearer=B_TOKEN, admin_header=ADMIN)
    assert (exc.value.status, exc.value.error) == (400, "bad_scope")


def test_query_loses_token_and_auth_only():
    assert peers.upstream_query(
        "token=AAA&path=a%20b&auth=AAA&x=&id=7") == "path=a+b&x=&id=7"
    assert peers.upstream_query("") == ""


@pytest.mark.parametrize("ctype,forced", [
    ("application/json", "application/json"),
    ("Application/JSON; charset=utf-8", "application/json"),
    ("text/html", "application/octet-stream"),
    ("text/html; charset=utf-8", "application/octet-stream"),
    ("application/json+evil", "application/octet-stream"),
    ("image/svg+xml", "application/octet-stream"),
    (None, "application/octet-stream"),
])
def test_forced_content_type(ctype, forced):
    assert peers.forced_content_type(ctype) == forced


def test_downstream_headers_drop_everything_unlisted():
    up = Header([("Set-Cookie", "a=1"), ("Location", "http://evil/"),
                 ("Access-Control-Allow-Origin", "*"),
                 ("Access-Control-Allow-Credentials", "true"),
                 ("Connection", "close"), ("Keep-Alive", "timeout=5"),
                 ("Transfer-Encoding", "chunked"),
                 ("Content-Security-Policy", "script-src *"),
                 ("Content-Type", "text/html"), ("ETag", '"e1"'),
                 ("Last-Modified", "Tue, 13 Oct 2026 00:00:00 GMT"),
                 ("Expires", "Wed, 14 Oct 2026 00:00:00 GMT"), ("Age", "5"),
                 ("Cache-Control", "public, max-age=3600"),
                 ("Content-Length", "3"), ("Content-Encoding", "gzip"),
                 ("Content-Disposition", "attachment; filename=r.jsonl")])
    out = peers.downstream_headers(up)
    # Round 1b: no cache header of B's crosses; every answer is no-store.
    assert out == {"Content-Length": "3", "Content-Encoding": "gzip",
                   "Content-Disposition": "attachment; filename=r.jsonl",
                   "Cache-Control": "no-store",
                   "X-Content-Type-Options": "nosniff",
                   "Content-Security-Policy": peers.SANDBOX_CSP}


# ---- the relay against a fake upstream --------------------------------------------

class Sink:
    """Stands in for ``request.respond``: records status, headers and every
    chunk, and can set an event when the first byte arrives."""

    def __init__(self, first_byte: threading.Event = None) -> None:
        self.status = self.headers = self.content_type = None
        self.chunks = []
        self.ended = self.aborted = False
        self._first = first_byte

    async def respond(self, status, headers, content_type):
        self.status, self.headers, self.content_type = (
            status, headers, content_type)
        return self

    async def send(self, data):
        self.chunks.append(bytes(data))
        if self._first is not None:
            self._first.set()

    async def eof(self):
        self.ended = True

    def abort(self):
        self.aborted = True

    @property
    def body(self) -> bytes:
        return b"".join(self.chunks)


@pytest.fixture
def upstream():
    up = FakeUpstream().start()
    yield up
    up.stop()


async def _relay(up, path, *, method="GET", query="", headers=None,
                 body=None, sink=None, client=None, logs=None):
    sink = sink or Sink()
    own = client is None
    client = client or peers.make_client()
    try:
        err = await peers.relay(
            client, peer_id="b", origin=up.origin, method=method, path=path,
            query=query,
            headers=headers if headers is not None else peers.upstream_headers(
                _incoming(), gate=peers.GATE_BROWSER, bearer=B_TOKEN,
                admin_header=ADMIN),
            body=body, respond=sink.respond, abort=sink.abort,
            log=(lambda *a: logs.append(a[0] % a[1:])) if logs is not None
            else (lambda *a: None))
    finally:
        if own:
            await client.aclose()
    return err, sink


async def test_relay_crosses_only_the_allowed_headers(upstream):
    err, sink = await _relay(upstream, "/info", query="path=x")
    assert err is None and sink.status == 200 and sink.ended
    assert json.loads(sink.body)["broker_id"] == "fake-b"
    seen = upstream.seen[-1]
    assert (seen.method, seen.target) == ("GET", "/info?path=x")
    assert seen.one("Authorization") == "Bearer " + B_TOKEN
    flat = " ".join(f"{k}: {v}" for k, v in seen.headers)
    for gone in (A_TOKEN, peers.PEER_TOKEN_HEADER, peers.PEER_ORIGIN_HEADER,
                 "sid=1", "X-Sentinel", "timeout=99", "h2c", "c2VjcmV0",
                 "10.9.9.9", "Origin", "Referer", "Cookie"):
        assert gone not in flat, gone
    assert seen.one("TE") is None and seen.one("Upgrade") is None
    assert seen.one("Host") == f"127.0.0.1:{upstream.port}"


async def test_relay_forwards_a_body_with_its_length(upstream):
    payload = json.dumps({"path": "C:/x"}).encode()

    async def body():
        yield payload[:5]
        yield payload[5:]

    hdrs = peers.upstream_headers(
        Header([("Content-Type", "application/json"),
                ("Content-Length", str(len(payload)))]),
        gate=peers.GATE_BROWSER, bearer=B_TOKEN, admin_header=ADMIN)
    # Content-Length describes a body, so the policy never forwards it;
    # the route adds it only when it sends one (as here).
    assert "content-length" not in _names(hdrs)
    hdrs.append(("Content-Length", str(len(payload))))
    err, sink = await _relay(upstream, "/file/list", method="POST",
                             headers=hdrs, body=body())
    assert err is None and sink.status == 200
    seen = upstream.seen[-1]
    assert seen.body == payload
    assert seen.one("Content-Length") == str(len(payload))
    assert seen.one("Transfer-Encoding") is None


async def test_html_upstream_is_never_html_downstream(upstream):
    upstream.routes["/info"] = lambda s: Answer(
        200, [("Content-Type", "text/html; charset=utf-8")],
        b"<script>alert(localStorage)</script>")
    err, sink = await _relay(upstream, "/info")
    assert err is None
    assert sink.content_type == "application/octet-stream"
    assert sink.headers["X-Content-Type-Options"] == "nosniff"
    assert sink.headers["Content-Security-Policy"].startswith("sandbox")


async def test_redirect_is_relayed_not_followed(upstream):
    upstream.routes["/info"] = lambda s: Answer(302, [
        ("Location", upstream.origin + "/redirect-target"),
        ("Set-Cookie", "sid=evil; Path=/"),
        ("Access-Control-Allow-Origin", "*"),
        ("Connection", "X-Up-Sentinel"), ("Keep-Alive", "timeout=7")])
    err, sink = await _relay(upstream, "/info")
    assert err is None and sink.status == 302
    assert upstream.hits("/redirect-target") == 0
    lowered = {k.lower() for k in sink.headers}
    for gone in ("location", "set-cookie", "access-control-allow-origin",
                 "connection", "keep-alive"):
        assert gone not in lowered


async def test_twenty_mib_stream_is_relayed_chunk_by_chunk(upstream):
    """The upstream sends 64 KiB, then WAITS until the downstream has
    received a byte before it sends the rest. A relay that buffered the
    whole body would never deliver that first byte in time."""
    size = 20 * 2**20
    blob = bytes(range(256)) * (size // 256)
    first = threading.Event()
    upstream.routes["/recording"] = lambda s: Answer(
        200, [("Content-Type", "application/octet-stream")], blob,
        gate=first, gate_timeout=10)
    err, sink = await _relay(upstream, "/recording", query="id=r1",
                             sink=Sink(first))
    assert err is None and sink.ended
    assert upstream.gate_timed_out is False
    assert len(sink.chunks) > 1
    assert sink.body == blob


async def test_down_peer_is_502_and_the_log_names_no_url(upstream):
    port = upstream.port
    upstream.stop()
    logs = []
    sink = Sink()
    client = peers.make_client()
    try:
        err = await peers.relay(
            client, peer_id="b", origin=f"http://127.0.0.1:{port}",
            method="GET", path="/recording", query="id=secret-q&x=1",
            headers=[("Authorization", "Bearer " + B_TOKEN)], body=None,
            respond=sink.respond, abort=sink.abort,
            log=lambda *a: logs.append(a[0] % a[1:]))
    finally:
        await client.aclose()
    assert err == (502, "peer_unreachable")
    assert sink.status is None
    assert len(logs) == 1 and logs[0].startswith("peer b: GET /recording ")
    for secret in ("http", "127.0.0.1", str(port), "secret-q", B_TOKEN):
        assert secret not in logs[0], secret


async def test_slow_peer_is_504(upstream):
    hold = threading.Event()
    upstream.routes["/info"] = lambda s: Answer(hold=hold)
    client = httpx.AsyncClient(timeout=httpx.Timeout(0.5),
                               follow_redirects=False, trust_env=False)
    logs = []
    try:
        err, sink = await _relay(upstream, "/info", client=client, logs=logs)
    finally:
        hold.set()
        await client.aclose()
    assert err == (504, "peer_timeout")
    assert logs and "timed out (ReadTimeout)" in logs[0]


async def test_a_body_that_dies_midway_aborts_the_downstream(upstream):
    blob = b"x" * (4 * 2**20)
    gate = threading.Event()

    def answer(_seen):
        # Claims 4 MiB, sends 64 KiB, then the gate never opens and the
        # writer gives up after 1 s and closes: a short body.
        return Answer(200, [("Content-Type", "application/octet-stream"),
                            ("Content-Length", str(len(blob) * 2))],
                      blob, gate=gate, gate_timeout=1)

    upstream.routes["/recording"] = answer
    logs = []
    err, sink = await _relay(upstream, "/recording", logs=logs)
    assert err is None
    assert sink.aborted is True and sink.ended is False
    assert logs and "broke mid-body" in logs[0]


async def test_an_informational_status_is_never_relayed(upstream):
    """A 101 to a request that asked for no upgrade is not a final answer:
    nothing is relayed, and the dial reads as a broken peer."""
    upstream.routes["/info"] = lambda s: Answer(
        101, [("Upgrade", "evil"), ("Connection", "Upgrade")], b"post-upgrade")
    err, sink = await _relay(upstream, "/info")
    assert err == (502, "peer_unreachable")
    assert sink.status is None and sink.chunks == []


def test_a_trailing_dot_names_the_same_host():
    assert peers.normalize_origin("http://LocalHost.:4445") == (
        "http://localhost:4445", None)
    out, why = peers.parse_peers({"b": {"origin": "http://localhost.:4445"}},
                                 4445)
    assert why == ("error: peer 'b': origin http://localhost:4445 is this "
                   "broker")



# ---- round 1: the upstream header set is EXACTLY the allowlist -------------------

#: What the transport itself must add: the request target's host.
TRANSPORT = {"host"}


async def test_browser_gate_sends_exactly_the_allowlist(upstream):
    body = b'{"path": "C:/x"}'
    incoming = _incoming(
        (SCOPE_HEADER, "s1"), (ADMIN, "adm"), (MCP_ADMIN_HEADER, "madm"),
        ("Content-Encoding", "identity"), ("Content-Length", str(len(body))),
        ("User-Agent", "Mozilla/5.0"), ("Accept-Encoding", "gzip, br"),
        ("X-Anything", "1"))
    hdrs = peers.upstream_headers(incoming, gate=peers.GATE_BROWSER,
                                  bearer=B_TOKEN, admin_header=ADMIN)
    hdrs.append(("Content-Length", str(len(body))))     # as the route does

    async def gen():
        yield body

    err, _ = await _relay(upstream, "/file/list", method="POST",
                          headers=hdrs, body=gen())
    assert err is None
    seen = upstream.seen[-1]
    expected = ({n.lower() for n in peers._UP_PLAIN}
                | {SCOPE_HEADER.lower(), ADMIN.lower(), "accept-encoding",
                   "authorization", "content-length"} | TRANSPORT)
    assert {k.lower() for k, _ in seen.headers} == expected
    assert seen.all("Accept-Encoding") == ["identity"]
    for never in ("user-agent", "cookie", "connection", "x-anything",
                  MCP_ADMIN_HEADER.lower()):
        assert never not in expected


async def test_mcp_gate_sends_exactly_the_allowlist(upstream):
    incoming = _incoming((SCOPE_HEADER, "s1"), (ADMIN, "adm"),
                         (MCP_ADMIN_HEADER, "madm"),
                         ("User-Agent", "agent/1"))
    hdrs = peers.upstream_headers(incoming, gate=peers.GATE_MCP,
                                  bearer=B_TOKEN, admin_header=ADMIN)
    err, _ = await _relay(upstream, "/mcp/terminals", headers=hdrs)
    assert err is None
    seen = upstream.seen[-1]
    # GET with no body: Content-Type/Accept are the client's, no length.
    expected = ({"content-type", "accept", SCOPE_HEADER.lower(),
                 MCP_ADMIN_HEADER.lower(), "accept-encoding",
                 "authorization"} | TRANSPORT)
    assert {k.lower() for k, _ in seen.headers} == expected
    assert seen.all("Accept-Encoding") == ["identity"]



# ---- round 1b ----------------------------------------------------------------------

class _ExplodingClient:
    """A client whose dial raises something that is NOT an httpx error (as
    h11 does for a Content-Length the body does not match)."""

    def build_request(self, *a, **k):
        return None

    async def send(self, *a, **k):
        raise RuntimeError("h11 said no: http://secret-origin/?token=x")


async def test_any_dial_failure_is_a_502_with_the_class_only():
    logs = []
    sink = Sink()
    err = await peers.relay(
        _ExplodingClient(), peer_id="b", origin="http://secret-origin",
        method="GET", path="/info", query="token=x", headers=[], body=None,
        respond=sink.respond, abort=sink.abort,
        log=lambda *a: logs.append(a[0] % a[1:]))
    assert err == (502, "peer_unreachable")
    assert logs == ["peer b: GET /info failed (RuntimeError)"]


async def test_the_dial_ceiling_before_headers_is_504(upstream, monkeypatch):
    monkeypatch.setattr(peers, "PEER_DIAL_MAX_S", 0.6)
    upstream.routes["/info"] = lambda s: Answer(drip_every=0.2, drip_head=15)
    logs = []
    err, sink = await _relay(upstream, "/info", logs=logs)
    assert err == (504, "peer_timeout")
    assert sink.status is None
    assert logs == ["peer b: GET /info hit the dial ceiling (TimeoutError)"]


async def test_the_dial_ceiling_mid_body_drops_the_downstream(upstream,
                                                              monkeypatch):
    monkeypatch.setattr(peers, "PEER_DIAL_MAX_S", 0.6)
    upstream.routes["/recording"] = lambda s: Answer(drip_every=0.2,
                                                     drip_body=15)
    logs = []
    err, sink = await _relay(upstream, "/recording", logs=logs)
    assert err is None
    assert sink.status == 200 and sink.aborted is True and sink.ended is False
    assert logs and "hit the dial ceiling" in logs[0]
