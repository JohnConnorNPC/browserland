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

import re
import urllib.parse
from typing import Any, Dict, Iterable, Optional, Tuple

#: The browser gate's second credential: B's browser token, carried beside A's
#: own bearer. A never forwards it under this name (it becomes the upstream
#: ``Authorization``) and never stores it.
PEER_TOKEN_HEADER = "X-Browserland-Peer-Token"

#: The pairing header: the origin the page paired that token with. A compares
#: it to the configured origin BEFORE any dial and answers 409
#: ``peer_origin_changed`` on a mismatch (a missing header is a mismatch), so a
#: page that has not seen a re-pointed origin cannot send B's token to it.
PEER_ORIGIN_HEADER = "X-Browserland-Peer-Origin"

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

#: A peer id: what the client names, and the path segment after ``/peer/``.
PEER_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,64}")

#: The keys a peer entry may carry. Anything else is refused, so a typo
#: (``"orgin"``) is an error instead of a peer with no origin.
_PEER_KEYS = frozenset({"origin", "mcp"})

#: Shape of a rebuilt origin (lower-case host chars or a bracketed IPv6
#: literal, an optional port) -- a last belt over the URL parser's output.
_ORIGIN_RE = re.compile(
    r"https?://(?:[a-z0-9.\-]+|\[[0-9a-f:.]+\])(?::[0-9]{1,5})?")

_DEFAULT_PORTS = {"http": 80, "https": 443}

#: Host names that reach this machine whatever the bind address says.
_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "0.0.0.0",
                             "::"})


def normalize_origin(raw: Any) -> Tuple[Optional[str], Optional[str]]:
    """``(origin, None)`` for an acceptable peer origin, else ``(None, why)``.

    The origin is ``http`` or ``https`` with a host and an optional port:
    no userinfo, no path (a lone trailing ``/`` is tolerated and dropped), no
    query, no fragment. It comes back rebuilt as ``scheme://host[:port]``,
    lower-case, with a default port dropped -- never the operator's bytes.
    """
    if not isinstance(raw, str) or not raw:
        return None, "origin must be a non-empty string"
    if any(ch <= " " or ch in "\\\x7f" for ch in raw):
        return None, "origin must not contain whitespace or backslashes"
    try:
        parts = urllib.parse.urlsplit(raw)
        port = parts.port
    except ValueError:
        return None, "origin has an invalid port"
    if parts.scheme not in _DEFAULT_PORTS:
        return None, "origin must be http or https"
    if "@" in parts.netloc or parts.username is not None:
        return None, "origin must not carry userinfo"
    if parts.path not in ("", "/"):
        return None, "origin must not carry a path"
    if parts.query or "?" in raw:
        return None, "origin must not carry a query"
    if parts.fragment or "#" in raw:
        return None, "origin must not carry a fragment"
    host = (parts.hostname or "").lower()
    if not host:
        return None, "origin must name a host"
    if port is not None and not 1 <= port <= 65535:
        return None, "origin has an invalid port"
    netloc = f"[{host}]" if ":" in host else host
    if port is not None and port != _DEFAULT_PORTS[parts.scheme]:
        netloc += f":{port}"
    origin = f"{parts.scheme}://{netloc}"
    if not _ORIGIN_RE.fullmatch(origin):
        return None, "origin has an invalid host"
    return origin, None


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


def proxy_available() -> bool:
    """Is the ``proxy`` extra (httpx) importable? Lazy on purpose: core never
    imports httpx, and a test simulates its absence through ``sys.modules``."""
    try:
        import httpx  # noqa: F401 -- the probe IS the import
    except ImportError:
        return False
    return True


def make_client():
    """The one upstream HTTP client. No redirects (a redirect would chain a
    hop A never checked), no environment proxies, explicit timeouts, and a
    pool no bigger than the dial cap."""
    import httpx
    return httpx.AsyncClient(
        follow_redirects=False,
        trust_env=False,
        timeout=httpx.Timeout(connect=PEER_CONNECT_TIMEOUT_S,
                              read=PEER_READ_TIMEOUT_S,
                              write=PEER_READ_TIMEOUT_S,
                              pool=PEER_CONNECT_TIMEOUT_S),
        limits=httpx.Limits(max_connections=PEER_MAX_DIALS,
                            max_keepalive_connections=PEER_MAX_DIALS))
