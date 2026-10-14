"""The broker's outbound HTTP, pinned by what the source does (#252).

Until #252 this was a sentence ("the broker's ONLY outbound HTTP is
/status/fetch") that was already wrong once /update/check landed. It is now an
executable inventory over ``webterm/broker``: every call or import of the
network APIs this scan knows (the urllib openers, httpx, requests, aiohttp,
urllib3, http.client, websockets, and the raw connect calls) is listed, and
the list must be exactly the following. It pins those spellings; it cannot
see a library it does not name, so a new egress library belongs in its sets.

* ``/status/fetch``: app.py's ``_STATUS_OPENER`` (stdlib, no redirects, no
  env proxies), opened in ``_fetch_status_blocking``;
* ``/update/check``: update.py's ``_OPENER``, the same construction;
* the configured peers (#252): ``httpx``, imported ONLY inside peers.py
  functions (core never imports it; it is the optional ``proxy`` extra), and
  one ``AsyncClient`` built in ``make_client`` with no redirects and no env
  proxies;
* the configured peers' sockets (#252 phase 2): ``websockets``, imported by
  peer_sockets.py inside functions only (the client needs websockets 15, the
  ``proxy`` extra; core needs 12), and one ``PeerWsConnect`` (a ``connect``
  that follows no redirect) dialed in ``dial_upstream`` with no env proxy;
* not an HTTP library but still egress: the update apply's ``git fetch``
  child process (``_git_mutation(["fetch", url, branch])`` in
  ``perform_apply``), found by its argument list.

So: three HTTP-library egresses, the peers' sockets, and the update apply's
git fetch. A module counts by prefix: ``import websockets.asyncio.client``
and ``from http.client import x`` are ``websockets`` and ``http.client``.

``vendor_codemirror.py`` is a dev-only vendoring script that nothing in the
broker imports; it is excluded and that exclusion is itself asserted.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

from webterm.broker import peer_sockets, peers

REPO = Path(__file__).resolve().parents[1]
BROKER = REPO / "webterm" / "broker"

#: Callables that open (or build something that opens) an outbound socket.
NET_CALLS = {"urlopen", "build_opener", "urlretrieve", "open_connection",
             "create_connection", "AsyncClient", "Client", "HTTPConnection",
             "HTTPSConnection", "connect", "PeerWsConnect"}
#: Modules whose import is itself an outbound-capable surface.
NET_MODULES = {"httpx", "requests", "aiohttp", "urllib3", "http.client",
               "websockets"}
#: git verbs that reach the network; a call passing a list that starts with
#: one (``_git_mutation(["fetch", url, branch])``) is egress through a child
#: process the Python-API scan cannot see.
NET_GIT_VERBS = {"fetch", "pull", "clone", "ls-remote", "push"}
#: The two stdlib openers; their `.open(` call sites are egress too.
OPENER_NAMES = {"_STATUS_OPENER", "_OPENER"}


def _net_module(name):
    """Is ``name`` one of NET_MODULES or inside one (a prefix match on
    whole dotted parts: ``http.client`` yes, ``http.cookiejar`` no)?"""
    return any(name == n or name.startswith(n + ".") for n in NET_MODULES)

EXPECTED = {
    ("app.py", "call urllib.request.build_opener", "<module>"),
    ("app.py", "call _STATUS_OPENER.open", "_fetch_status_blocking"),
    ("update.py", "call urllib.request.build_opener", "<module>"),
    ("update.py", "call _OPENER.open", "fetch_json"),
    ("peers.py", "import httpx", "proxy_missing"),
    ("peers.py", "import websockets", "proxy_missing"),
    ("peers.py", "import websockets.asyncio.client", "proxy_missing"),
    ("peers.py", "import httpx", "make_client"),
    ("peers.py", "import httpx", "_relay"),
    # Not an HTTP library, still an egress: the update apply's git fetch.
    ("update.py", "git fetch", "perform_apply"),
    ("peers.py", "call httpx.AsyncClient", "make_client"),
    # #252 phase 2: the peers' sockets. Every websockets import sits inside
    # a function: the client needs websockets 15 (the proxy extra), core 12.
    ("peer_sockets.py", "import websockets.asyncio.client",
     "peer_ws_connect"),
    ("peer_sockets.py", "import websockets.exceptions", "_sendable"),
    ("peer_sockets.py", "import websockets.frames", "_sendable"),
    ("peer_sockets.py", "import websockets.protocol", "relay_sockets"),
    ("peer_sockets.py", "call PeerWsConnect", "dial_upstream"),
}


def _inventory():
    found = set()
    for path in sorted(BROKER.rglob("*.py")):
        if path.name == "vendor_codemirror.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        rel = path.relative_to(BROKER).as_posix()

        def visit(node, scope):
            for child in ast.iter_child_nodes(node):
                inner = scope
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    inner = child.name
                if isinstance(child, ast.Call):
                    func = child.func
                    name = (func.attr if isinstance(func, ast.Attribute)
                            else getattr(func, "id", None))
                    if name in NET_CALLS or (
                            name == "open" and isinstance(func, ast.Attribute)
                            and getattr(func.value, "id", None)
                            in OPENER_NAMES):
                        found.add((rel, "call " + ast.unparse(func), scope))
                    for arg in child.args:
                        if (isinstance(arg, ast.List) and arg.elts
                                and isinstance(arg.elts[0], ast.Constant)
                                and arg.elts[0].value in NET_GIT_VERBS):
                            found.add((rel, "git " + arg.elts[0].value,
                                       scope))
                elif isinstance(child, ast.Import):
                    for alias in child.names:
                        if _net_module(alias.name):
                            found.add((rel, "import " + alias.name, scope))
                elif isinstance(child, ast.ImportFrom):
                    if _net_module(child.module or ""):
                        found.add((rel, "import " + child.module, scope))
                visit(child, inner)

        visit(tree, "<module>")
    return found


def test_the_broker_egress_inventory_is_exact():
    assert _inventory() == EXPECTED


def test_no_websockets_import_at_a_module_top_of_the_broker():
    # Q11: app.py imports every broker module; websockets 12 (the core
    # floor) has no asyncio client, so a top-level import would break boot.
    assert not [row for row in _inventory()
                if row[1].startswith("import websockets")
                and row[2] == "<module>"]


def test_a_module_counts_by_prefix():
    for name in ("websockets", "websockets.asyncio.client", "http.client",
                 "httpx", "aiohttp.client"):
        assert _net_module(name), name
    for name in ("http", "http.cookiejar", "httpxy", "websocketsx",
                 "webterm.websockets", ""):
        assert not _net_module(name), name


def test_vendor_script_is_imported_by_nothing():
    pat = re.compile(r"^\s*(from\s+\S*vendor_codemirror\s+import|import\s+"
                     r"\S*vendor_codemirror|from\s+\.\s+import\s+.*"
                     r"\bvendor_codemirror\b)", re.M)
    hits = [p.name for p in BROKER.rglob("*.py")
            if pat.search(p.read_text(encoding="utf-8"))]
    assert hits == []


async def test_the_peer_client_follows_no_redirects_and_no_env_proxies():
    client = peers.make_client()
    try:
        assert client.follow_redirects is False
        assert client.trust_env is False
        assert client.timeout.connect == peers.PEER_CONNECT_TIMEOUT_S
        assert client.timeout.read == peers.PEER_READ_TIMEOUT_S
        # Round 1: one client serves every user, so it keeps no cookies...
        assert type(client.cookies.jar) is peers.NoCookieJar
        # ...and adds none of httpx's own headers (User-Agent, Accept, ...).
        assert dict(client.headers) == {}
    finally:
        await client.aclose()
    # Phase 2: the peers' sockets read no env proxy and follow no redirect.
    assert peer_sockets.UPSTREAM_WS_KWARGS["proxy"] is None
    exc = RuntimeError("302")
    assert peer_sockets.peer_ws_connect().process_redirect(None, exc) is exc


def test_core_dependencies_stay_websockets_and_sanic():
    text = (REPO / "pyproject.toml").read_text(encoding="utf-8")
    deps = re.search(r"^dependencies = \[(.*?)\]", text, re.M | re.S).group(1)
    assert re.findall(r'"([a-z0-9_-]+)', deps) == ["websockets", "sanic"]
    assert re.search(r'^proxy = \["httpx>=[0-9.]+", "websockets>=15"\]$',
                     text, re.M)
    # Q11: the core floor stays 12; the extra carries the client's 15.
    assert re.findall(r'"(websockets>=[0-9.]+)"', deps) == ["websockets>=12"]


def test_the_no_cookie_jar_stores_nothing():
    import httpx
    jar = httpx.Cookies(peers.NoCookieJar())
    resp = httpx.Response(200, headers={"Set-Cookie": "sid=x; Path=/"},
                          request=httpx.Request("GET", "http://b:1/info"))
    jar.extract_cookies(resp)
    assert len(jar.jar) == 0


def test_the_no_cookie_jar_sends_nothing_it_somehow_holds():
    import http.cookiejar
    import httpx
    def planted(jar):
        # Past the storing chokepoint, so only the sending one is tested.
        http.cookiejar.CookieJar.set_cookie(jar, http.cookiejar.Cookie(
            0, "sid", "x", None, False, "b.example", False, False, "/", True,
            False, None, False, None, None, {}))
        return jar

    # The same cookie in a PLAIN jar is sent, so the check below is live.
    req = httpx.Request("GET", "http://b.example/info")
    httpx.Cookies(planted(http.cookiejar.CookieJar())).set_cookie_header(req)
    assert req.headers.get("cookie") == "sid=x"
    raw = planted(peers.NoCookieJar())
    assert len(raw) == 1
    req = httpx.Request("GET", "http://b.example/info")
    httpx.Cookies(raw).set_cookie_header(req)
    assert "cookie" not in req.headers
