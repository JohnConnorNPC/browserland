"""The "from server" peer proxy (#252, L1): config, policy and the relay.

A broker (A) can relay an allowlisted set of another broker's (B's) HTTP routes
under ``/peer/<id>/...``, so a page or phone that can reach only A still
reaches B. A stores NO credential of B's: the browser sends A's token as the
bearer plus B's token in :data:`PEER_TOKEN_HEADER`, and A swaps them on the way
out. The operator names each peer in A's config::

    "peers": {"b": {"origin": "http://100.64.0.2:4445", "mcp": false}}

The client only ever names a peer **id**; the origin lives in A's config and is
read at dial time. :func:`parse_peers` is the one validation, run by
``__main__`` before the one-shots are decided (a refusal is one stderr line and
exit 2) and again by ``create_app`` as the second line of defence.

``httpx`` is NOT a core dependency. It ships as the optional ``proxy`` extra
and is imported lazily, inside functions only, so a broker without it still
starts and answers ``/peer/*`` with ``503 proxy_unavailable``.
"""

from __future__ import annotations

import asyncio
import http.cookiejar
import logging
import re
import urllib.parse
from typing import Any, Dict, Iterable, Optional, Tuple

# The peer id, the pairing header and the origin's canonical form are shared
# with the mcptool client (#252 phase 3, #261), which must not import the
# broker.
from ..protocol import (MCP_ADMIN_HEADER, ORIGIN_DEFAULT_PORTS as
                        _DEFAULT_PORTS, ORIGIN_RE as _ORIGIN_RE,
                        PEER_ID_RE, PEER_ORIGIN_HEADER, SCOPE_HEADER,
                        normalize_origin)

#: The browser gate's second credential: B's browser token, carried beside A's
#: own bearer. A never forwards it under this name (it becomes the upstream
#: ``Authorization``) and never stores it.
PEER_TOKEN_HEADER = "X-Browserland-Peer-Token"

#: The pairing header (``PEER_ORIGIN_HEADER``) lives in webterm/protocol.py
#: and is imported above: the origin the page paired that token with. A
#: compares it to the configured origin BEFORE any dial and answers 409
#: ``peer_origin_changed`` on a mismatch (a missing header is a mismatch on
#: the browser gate), so a page that has not seen a re-pointed origin cannot
#: send B's token to it. On the MCP gate it is checked only when sent.

#: Set on every refusal THIS broker writes for a /peer/* request, naming
#: the error. It is not on the response allowlist, so a peer can never send
#: it: the page acts on a pairing refusal only when this header says so, not
#: on a body the peer could imitate.
PEER_REFUSAL_HEADER = "X-Browserland-Peer-Refusal"

#: Upstream connect timeout. Below the page's 3 s ``FETCH_TIMEOUT_MS`` so a
#: down peer frees its dial slot before the page's own deadline fires.
PEER_CONNECT_TIMEOUT_S = 2.5

#: Upstream read timeout: the gap allowed between two chunks of B's answer.
#: It must outlast the longest hold a peer makes on purpose -- a waiting
#: ``/mcp/read`` (``MAX_MCP_WAIT_MS``, 15 s) and the desktop relay
#: (``DESKTOP_RPC_TIMEOUT_S``, 10 s) -- or a proxied wait reads as an error.
PEER_READ_TIMEOUT_S = 30.0

#: Concurrent upstream dials, all peers together. Over it: 503 ``peer_busy``,
#: never a queue (the page already retries on its own clock).
PEER_MAX_DIALS = 32

#: Concurrent upstream dials to ONE peer, inside :data:`PEER_MAX_DIALS`, so a
#: peer that is down (every dial waits out the connect timeout) cannot take
#: every slot from the peers that are up.
PEER_MAX_DIALS_PER_PEER = 8

#: The MCP gate's OWN dial pool. A cannot check B's MCP token, so anyone who
#: can reach A can dial through this gate: it gets a small pool of its own and
#: can never take a slot from the browser gate (whose callers hold A's token).
PEER_MCP_MAX_DIALS = 8
PEER_MCP_MAX_DIALS_PER_PEER = 2

#: Every MCP route takes a small JSON body, so the MCP gate reads it WHOLE
#: before it takes a slot: at most this many bytes (413 over it) within this
#: deadline (408 past it). A caller who stalls mid-body holds nothing.
PEER_MCP_MAX_BODY = 1 * 2**20
PEER_MCP_BODY_DEADLINE_S = 5.0

#: The ceiling on one whole dial, headers and body together. A peer that
#: drips a byte at a time keeps refreshing the read gap; this does not move.
#: Past it the upstream is closed and the downstream ended (504 if nothing
#: was sent yet, a dropped connection if something was).
PEER_DIAL_MAX_S = 600.0

#: The ceiling for ``GET /recording`` alone: a recording download, up to
#: ``MAX_RECORDING_BYTES`` (256 MiB) on the wire. 3600 s keeps that pull
#: alive down to about 0.6 Mbit/s, where :data:`PEER_DIAL_MAX_S` would need
#: 3.6 Mbit/s. Stalls are still bounded by the read gap; this only lets a
#: slow, steady download finish. Every other route -- ``GET /recordings``,
#: the list, included -- keeps :data:`PEER_DIAL_MAX_S`.
PEER_RECORDING_DIAL_MAX_S = 3600.0


def dial_ceiling(method: str, path: str) -> float:
    """The whole-dial ceiling for one table route (both read at call time,
    so a test can lower them)."""
    if (method, path) == ("GET", "/recording"):
        return PEER_RECORDING_DIAL_MAX_S
    return PEER_DIAL_MAX_S


#: The keys a peer entry may carry. Anything else is refused, so a typo
#: (``"orgin"``) is an error instead of a peer with no origin.
_PEER_KEYS = frozenset({"origin", "mcp"})

#: Host names that reach this machine whatever the bind address says.
_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "0.0.0.0",
                             "::"})


# normalize_origin (and the origin shape it checks) lives in
# webterm/protocol.py, imported above.


def _is_self(origin: str, self_port: int, self_hosts: Iterable[str]) -> bool:
    """Does ``origin`` dial this broker? Loopback names and the bind host, on
    this broker's own port. (A self-dial is one hop, never a loop -- the hop
    refusal stops the recursion -- but it is always a configuration mistake.)
    """
    parts = urllib.parse.urlsplit(origin)
    port = parts.port or _DEFAULT_PORTS[parts.scheme]
    hosts = set(_LOOPBACK_HOSTS)
    hosts.update(h.strip("[]").lower() for h in self_hosts if h)
    return port == self_port and (parts.hostname or "") in hosts


def parse_peers(raw: Any, self_port: int,
                self_hosts: Iterable[str] = ()) -> Tuple[Dict[str, dict],
                                                         Optional[str]]:
    """``({id: {"origin", "mcp"}}, None)``, or ``({}, one-line reason)``.

    ``raw`` is the config's ``peers`` value: absent (``None``) is no peers.
    Every entry is ``{"origin": str, "mcp": bool}`` with ``mcp`` optional and
    false by default (the MCP gate on the prefix is a per-peer opt-in).
    """
    if raw is None:
        return {}, None
    if not isinstance(raw, dict):
        return {}, "error: peers must be an object of {id: {origin, mcp}}"
    out: Dict[str, dict] = {}
    for pid, entry in raw.items():
        if not isinstance(pid, str) or not PEER_ID_RE.fullmatch(pid):
            return {}, (f"error: peer id {pid!r} must be 1-64 letters, "
                        f"digits, '_' or '-'")
        if not isinstance(entry, dict):
            return {}, f"error: peer {pid!r} must be an object {{origin, mcp}}"
        unknown = sorted(set(entry) - _PEER_KEYS)
        if unknown:
            return {}, f"error: peer {pid!r} has unknown key {unknown[0]!r}"
        origin, why = normalize_origin(entry.get("origin"))
        if why:
            return {}, f"error: peer {pid!r}: {why}"
        if _is_self(origin, self_port, self_hosts):
            return {}, (f"error: peer {pid!r}: origin {origin} is this "
                        f"broker")
        mcp = entry.get("mcp", False)
        if not isinstance(mcp, bool):
            return {}, f"error: peer {pid!r}: mcp must be true or false"
        out[pid] = {"origin": origin, "mcp": mcp}
    return out, None


#: The socket half (phase 2) needs websockets' asyncio client and
#: ``connect(proxy=)``: websockets 15, which the ``proxy`` extra pins. Core
#: needs only websockets 12.
PEER_WS_MIN_MAJOR = 15


def proxy_missing() -> Optional[str]:
    """What the ``proxy`` extra lacks here, in words for the boot line, or
    ``None``: httpx, and websockets at least :data:`PEER_WS_MIN_MAJOR` with
    its asyncio client. ONE gate for the whole proxy, HTTP and sockets.
    Lazy on purpose: core never imports either client, and a test simulates
    their absence through ``sys.modules``."""
    try:
        import httpx  # noqa: F401 -- the probe IS the import
    except ImportError:
        return "httpx is not installed"
    import websockets
    version = str(getattr(websockets, "__version__", "0"))
    try:
        major = int(version.split(".")[0])
    except ValueError:
        major = 0
    if major < PEER_WS_MIN_MAJOR:
        return "websockets %s < %d" % (version, PEER_WS_MIN_MAJOR)
    try:
        import websockets.asyncio.client  # noqa: F401
    except ImportError:
        return "websockets.asyncio.client is missing"
    return None


def proxy_available() -> bool:
    """Is the ``proxy`` extra complete (:func:`proxy_missing`)?"""
    return proxy_missing() is None


class NoCookieJar(http.cookiejar.CookieJar):
    """The upstream client's cookie jar, which never holds anything. One
    client serves every user of this broker: a jar that kept a peer's
    ``Set-Cookie`` would replay it as ``Cookie`` on the NEXT user's request.
    A ``CookieJar`` subclass rather than an ``httpx.Cookies`` one, because
    httpx copies a ``Cookies`` it is given into a plain jar but keeps a
    ``CookieJar`` as it is. Two chokepoints, each closed on its own: every
    stored cookie goes through ``set_cookie``, and every sent one through
    ``add_cookie_header``."""

    def set_cookie(self, cookie):
        return None

    def add_cookie_header(self, request):
        return None


#: The headers httpx adds to every request on its own (Accept, Accept-Encoding,
#: Connection, User-Agent). make_client removes them all, so what reaches the
#: peer is exactly what upstream_headers built (plus the transport's Host and
#: body framing).
_HTTPX_DEFAULT_HEADERS = ("Accept", "Accept-Encoding", "Connection",
                          "User-Agent")


def make_client():
    """The one upstream HTTP client, and its only constructor. No redirects (a
    redirect would chain a hop A never checked), no environment proxies, no
    cookies (:class:`NoCookieJar`), none of httpx's default headers, explicit
    timeouts, and a pool no bigger than the dial cap."""
    import httpx
    # httpx logs every request at INFO as "HTTP Request: GET <full URL>", and
    # httpcore logs connection detail at DEBUG: the upstream URL, its query,
    # and (phase 2) a WebSocket leg's ?token=. The broker's own lines name a
    # peer id instead, so both loggers stay at WARNING in this process.
    for name in ("httpx", "httpcore"):
        logging.getLogger(name).setLevel(logging.WARNING)
    client = httpx.AsyncClient(
        follow_redirects=False,
        trust_env=False,
        cookies=NoCookieJar(),
        headers={},
        timeout=httpx.Timeout(connect=PEER_CONNECT_TIMEOUT_S,
                              read=PEER_READ_TIMEOUT_S,
                              write=PEER_READ_TIMEOUT_S,
                              pool=PEER_CONNECT_TIMEOUT_S),
        limits=httpx.Limits(max_connections=PEER_MAX_DIALS,
                            max_keepalive_connections=PEER_MAX_DIALS))
    # headers={} does not do it: httpx merges its defaults in underneath.
    for name in _HTTPX_DEFAULT_HEADERS:
        if name in client.headers:
            del client.headers[name]
    return client


# ---- what may be relayed ----------------------------------------------------
# Explicit (method, path) tables, research #252 S7 minus GET/POST /mcp/config
# (#260). Anything absent is denied: no prefix matching, so `/mods/` cannot
# admit `/mods/install`. Never here, by design: GET /, /help-corpus.json,
# /vendor/*, /mods/<id>/<gen>/*, /mods/install|uninstall|rescan|installed,
# POST /restart, WS /browserland, /state (GET/PUT), /status/fetch, OPTIONS,
# GET/POST /mcp/config, and (until phase 2) /ws, /control.

#: The browser gate: A's browser token AND B's token in PEER_TOKEN_HEADER.
BROWSER_ALLOW = frozenset(
    [("GET", p) for p in (
        "/info", "/sessions", "/profiles", "/profiles/config",
        "/profiles/detect", "/recordings", "/recording", "/recording/notes",
        "/update/check")]
    + [("POST", p) for p in (
        "/launch", "/file/list", "/file/read", "/file/read_chunk",
        "/file/hash", "/file/write", "/file/upload", "/file/upload_begin",
        "/file/upload_chunk", "/file/upload_commit", "/file/upload_abort",
        "/file/paste_image", "/file/delete", "/file/mkdir", "/file/copy",
        "/file/move", "/file/zip", "/file/unzip", "/file/stat",
        "/file/setattr", "/session/procs", "/session/kill", "/session/git",
        "/session/mcp", "/session/control", "/recording/begin",
        "/recording/chunk", "/recording/commit", "/recording/abort",
        "/recording/delete", "/recording/notes", "/update/apply",
        "/update/policy", "/mods/policy", "/profiles/config")])

#: ``/mod-store/<modId>`` (GET, PUT) on the browser gate. The id shape is
#: app.py's ``_MODSTORE_ID_RE`` (pinned equal by test), so an id can never
#: carry a separator.
MOD_STORE_RE = re.compile(r"/mod-store/([a-z0-9][a-z0-9-]{0,63})")
MOD_STORE_METHODS = frozenset({"GET", "PUT"})

#: The MCP gate: B's MCP token alone, and only for a peer with ``mcp: true``.
#: ``/mcp/config`` is NOT here on purpose: on B it is a browser-realm route
#: (B's browser token, never the MCP one), so offering it on this gate would
#: send the MCP gate's bearer to a route that is not MCP-gated. It is not in
#: BROWSER_ALLOW either, so it answers 404 ``not_relayed``. (The prefix
#: ``mcp/`` picks the MCP gate, and ``_peer_relay`` refuses a table entry
#: that sits on the other gate, so listing it in BROWSER_ALLOW would not move
#: it either.)
MCP_ALLOW = frozenset(
    [("GET", p) for p in (
        "/mcp/info", "/mcp/terminals", "/mcp/profiles", "/mcp/desktop")]
    + [("POST", p) for p in (
        "/mcp/read", "/mcp/input", "/mcp/reset", "/mcp/flush", "/mcp/pace",
        "/mcp/launch", "/mcp/window", "/mcp/desktop", "/mcp/knobs")])

GATE_BROWSER = "browser"
GATE_MCP = "mcp"


def classify(method: str, rest: str) -> Tuple[Optional[str], str]:
    """``(gate, canonical path)`` for a relayable request, else
    ``(None, error)``.

    ``rest`` is what follows ``/peer/<id>/``, still percent-encoded (Sanic
    hands path parameters over undecoded). It is decoded ONCE here, the hop
    refusal runs first on the decoded segments, and the path that is dialed
    is the canonical table string, never the client's bytes.
    """
    path = "/" + urllib.parse.unquote(rest, errors="replace")
    # Hop limit 1: nothing that names another /peer/ hop is ever relayed,
    # whatever the table says. It is checked first so that it stays a rule
    # of its own, not a side effect of the table.
    if "peer" in path.split("/"):
        return None, "peer_hop_refused"
    if (method, path) in MCP_ALLOW:
        return GATE_MCP, path
    if (method, path) in BROWSER_ALLOW:
        return GATE_BROWSER, path
    if method in MOD_STORE_METHODS and MOD_STORE_RE.fullmatch(path):
        return GATE_BROWSER, path
    return None, "not_relayed"


def is_mcp_path(rest: str) -> bool:
    """Does this prefix path aim at the MCP gate? Decided on the decoded
    first segment, so the gate cannot be dodged by encoding ``mcp``."""
    return urllib.parse.unquote(rest, errors="replace").split("/")[0] == "mcp"


# ---- header policy ------------------------------------------------------------
# Allowlists both ways: a header not named here does not cross. That drops, by
# construction, A's own Authorization, the peer token and pairing headers,
# Cookie, Host, Origin, Referer, X-Forwarded-*, and every hop-by-hop header
# (Connection, Keep-Alive, Proxy-Authenticate, Proxy-Authorization, TE,
# Trailer, Transfer-Encoding, Upgrade) in both directions.

#: Request headers forwarded as the client sent them. Content-Length is NOT
#: here: it describes a body, so the route sets it only when a body is sent
#: (a bodiless GET that declared one would otherwise break the dial).
_UP_PLAIN = ("Content-Type", "Content-Encoding", "Accept")

#: Response headers forwarded as B sent them. Content-Type is never in this
#: list: it is forced (see :func:`forced_content_type`). Neither is any cache
#: header: a peer must not pin its answers in the browser under this origin,
#: so every /peer/* response is ``Cache-Control: no-store`` and B's
#: ``Cache-Control``, ``ETag``, ``Last-Modified``, ``Expires`` and ``Age``
#: stay behind.
_DOWN_PLAIN = ("Content-Length", "Content-Encoding", "Content-Disposition")

#: The enforced CSP of every /peer/* response: a relayed body is never a
#: document that can run script on A's origin.
SANDBOX_CSP = "sandbox; default-src 'none'; frame-ancestors 'none'"


class PolicyError(Exception):
    """A request A refuses locally, before any dial (``status``, ``error``)."""

    def __init__(self, status: int, error: str) -> None:
        super().__init__(error)
        self.status = status
        self.error = error


def upstream_headers(headers, *, gate: str, bearer: str,
                     admin_header: str) -> list:
    """The header list A sends to B. ``headers`` is the incoming multi-dict
    (``getall``); ``bearer`` is the credential for this gate (B's browser
    token, or B's MCP token on the MCP gate); ``admin_header`` is #191's
    ``X-Webterm-Admin``, forwarded only when the caller supplied it, and only
    on the browser gate.

    Exactly one ``X-Browserland-Scope`` crosses: a repeat is refused here
    (B would 400 it too), never collapsed to one copy A picked."""
    out = []
    for name in _UP_PLAIN:
        val = headers.get(name)
        if val is not None:
            out.append((name, val))
    scopes = headers.getall(SCOPE_HEADER, None) or []
    if len(scopes) > 1:
        raise PolicyError(400, "bad_scope")
    if scopes:
        out.append((SCOPE_HEADER, scopes[0]))
    if gate == GATE_BROWSER:
        # #191's admin header is a browser-realm credential: it never rides
        # the MCP gate, whose only credentials are B's MCP ones.
        for val in headers.getall(admin_header, None) or []:
            out.append((admin_header, val))
    else:
        # B's MCP admin header is B's credential, sent by the mcptool client
        # beside B's MCP token; on the browser gate it has no business.
        for val in headers.getall(MCP_ADMIN_HEADER, None) or []:
            out.append((MCP_ADMIN_HEADER, val))
    # The relay passes bytes through untouched, so ask for them untouched.
    out.append(("Accept-Encoding", "identity"))
    out.append(("Authorization", "Bearer " + bearer))
    return out


def upstream_query(raw_qs: str) -> str:
    """The query string A forwards: the client's, minus ``token`` and
    ``auth`` in any letter case. B reads a query token BEFORE the bearer
    (auth.provided_token), so a forwarded ``?token=`` would both leak A's
    token and override the credential A set; B reads only the lower-case
    names, but a ``Token=`` that crossed would still carry A's token to B."""
    pairs = urllib.parse.parse_qsl(raw_qs or "", keep_blank_values=True)
    return urllib.parse.urlencode(
        [(k, v) for k, v in pairs if k.lower() not in ("token", "auth")])


def forced_content_type(upstream_type: Optional[str]) -> str:
    """JSON stays JSON; everything else -- ``text/html`` above all -- is
    ``application/octet-stream``, which no browser renders as a page."""
    media = (upstream_type or "").split(";", 1)[0].strip().lower()
    if media == "application/json":
        return "application/json"
    return "application/octet-stream"


def downstream_headers(up_headers) -> dict:
    """The headers A sends back: B's allowlisted ones, plus the hardening.
    ``Set-Cookie``, ``Location`` and every ``Access-Control-*`` are not on
    the list, so they never cross."""
    out = {}
    for name in _DOWN_PLAIN:
        val = up_headers.get(name)
        if val is not None:
            out[name] = val
    out["X-Content-Type-Options"] = "nosniff"
    out["Content-Security-Policy"] = SANDBOX_CSP
    out["Cache-Control"] = "no-store"
    return out


# ---- the dial cap -----------------------------------------------------------------

class DialCap:
    """Concurrent upstream dials: at most ``per_peer`` to one peer, inside
    ``total`` overall. Non-blocking: a dial over either limit is refused (503
    ``peer_busy``), never queued -- a queue would be a second retry layer
    under the page's own. One event loop, so plain counters are enough. The
    route keeps two: the browser gate's (:data:`PEER_MAX_DIALS_PER_PEER` /
    :data:`PEER_MAX_DIALS`) and the MCP gate's
    (:data:`PEER_MCP_MAX_DIALS_PER_PEER` / :data:`PEER_MCP_MAX_DIALS`)."""

    def __init__(self, total: int = PEER_MAX_DIALS,
                 per_peer: int = PEER_MAX_DIALS_PER_PEER) -> None:
        self.total = total
        self.per_peer = per_peer
        self._busy: Dict[str, int] = {}

    def acquire(self, peer_id: str) -> bool:
        if (sum(self._busy.values()) >= self.total
                or self._busy.get(peer_id, 0) >= self.per_peer):
            return False
        self._busy[peer_id] = self._busy.get(peer_id, 0) + 1
        return True

    def release(self, peer_id: str) -> None:
        left = self._busy.get(peer_id, 0) - 1
        if left > 0:
            self._busy[peer_id] = left
        else:
            self._busy.pop(peer_id, None)


def apply_change(app) -> None:
    """The event for a changed ``app.ctx.peers`` or ``app.ctx.auth_token``.
    Nothing changes either at runtime today (a changed config takes effect
    on restart); a future editor calls this after it writes. It closes the
    relayed sockets and voids the tickets the change unbinds -- the same
    sweep every ticket mint and redemption runs first."""
    from .peer_sockets import token_id
    app.ctx.peer_sockets.sweep(app.ctx.peers, token_id(app.ctx.auth_token))


# ---- the relay ------------------------------------------------------------------

async def relay(client, *, peer_id: str, origin: str, method: str, path: str,
                query: str, headers: list, body, respond, abort, log):
    """:func:`_relay` under the route's dial ceiling (:func:`dial_ceiling`,
    read at call time). Past it the dial is cancelled -- which closes the
    upstream -- and the downstream gets a 504 if nothing was sent yet, or a
    dropped connection if B's answer had started (it is never ended as if
    whole)."""
    started = []

    async def _respond(**kwargs):
        started.append(True)
        return await respond(**kwargs)

    try:
        return await asyncio.wait_for(
            _relay(client, peer_id=peer_id, origin=origin, method=method,
                   path=path, query=query, headers=headers, body=body,
                   respond=_respond, abort=abort, log=log),
            dial_ceiling(method, path))
    except asyncio.TimeoutError as exc:
        log("peer %s: %s %s hit the dial ceiling (%s)", peer_id, method,
            path, type(exc).__name__)
        if started:
            abort()
            return None
        return 504, "peer_timeout"


async def _relay(client, *, peer_id: str, origin: str, method: str, path: str,
                 query: str, headers: list, body, respond, abort, log):
    """Dial ``origin + path`` and stream B's answer to ``respond``.

    Returns ``None`` once B's answer has been streamed (or the downstream was
    dropped mid-body), or ``(status, error)`` for a failure BEFORE anything
    was sent, which the caller answers as JSON. ``respond(status=, headers=,
    content_type=)`` is Sanic's ``request.respond``; ``abort()`` drops the
    downstream connection, so a body that dies half-way is never delivered
    as if it were complete. Redirects are not followed (the client is built
    with ``follow_redirects=False``): a 3xx is relayed with its status and
    without ``Location``.

    ``log`` gets the peer id, the method, the canonical path and the
    exception CLASS -- never the upstream URL or its query (an httpx
    exception's text embeds the URL)."""
    import httpx
    url = origin + path + ("?" + query if query else "")
    try:
        req = client.build_request(method, url, headers=headers,
                                   content=body, cookies=None)
        up = await client.send(req, stream=True)
    except httpx.TimeoutException as exc:
        log("peer %s: %s %s timed out (%s)", peer_id, method, path,
            type(exc).__name__)
        return 504, "peer_timeout"
    except Exception as exc:  # noqa: BLE001 -- any failure is the peer's
        # Not only httpx's own errors: h11 or a transport can raise others,
        # and an uncaught one is a 500 with a traceback in this broker's log.
        log("peer %s: %s %s failed (%s)", peer_id, method, path,
            type(exc).__name__)
        return 502, "peer_unreachable"
    try:
        response = await respond(
            status=up.status_code, headers=downstream_headers(up.headers),
            content_type=forced_content_type(up.headers.get("content-type")))
        try:
            # One chunk at a time: Sanic's send waits for the transport to
            # drain, so a slow client slows the upstream read, and nothing
            # (not even a 256 MiB recording) is held whole in memory.
            async for chunk in up.aiter_raw():
                await response.send(chunk)
        except Exception as exc:  # noqa: BLE001 -- see the dial above
            log("peer %s: %s %s broke mid-body (%s)", peer_id, method, path,
                type(exc).__name__)
            abort()
            return None
        await response.eof()
        return None
    finally:
        await up.aclose()
