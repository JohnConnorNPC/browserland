"""The broker's outbound HTTP, pinned by what the source does (#252).

Until #252 this was a sentence ("the broker's ONLY outbound HTTP is
/status/fetch") that was already wrong once /update/check landed. It is now an
executable inventory over ``webterm/broker``: every call or import that can
open an outbound connection is listed, and the list must be exactly

* ``/status/fetch``: app.py's ``_STATUS_OPENER`` (stdlib, no redirects, no
  env proxies), opened in ``_fetch_status_blocking``;
* ``/update/check``: update.py's ``_OPENER``, the same construction;
* the configured peers (#252): ``httpx``, imported ONLY inside peers.py
  functions (core never imports it; it is the optional ``proxy`` extra), and
  one ``AsyncClient`` built in ``make_client`` with no redirects and no env
  proxies.

``vendor_codemirror.py`` is a dev-only vendoring script that nothing in the
broker imports; it is excluded and that exclusion is itself asserted.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

from webterm.broker import peers

REPO = Path(__file__).resolve().parents[1]
BROKER = REPO / "webterm" / "broker"

#: Callables that open (or build something that opens) an outbound socket.
NET_CALLS = {"urlopen", "build_opener", "urlretrieve", "open_connection",
             "create_connection", "AsyncClient", "Client", "HTTPConnection",
             "HTTPSConnection", "connect"}
#: Modules whose import is itself an outbound-capable surface.
NET_MODULES = {"httpx", "requests", "aiohttp", "urllib3", "http.client",
               "websockets"}
#: The two stdlib openers; their `.open(` call sites are egress too.
OPENER_NAMES = {"_STATUS_OPENER", "_OPENER"}

EXPECTED = {
    ("app.py", "call urllib.request.build_opener", "<module>"),
    ("app.py", "call _STATUS_OPENER.open", "_fetch_status_blocking"),
    ("update.py", "call urllib.request.build_opener", "<module>"),
    ("update.py", "call _OPENER.open", "fetch_json"),
    ("peers.py", "import httpx", "proxy_available"),
    ("peers.py", "import httpx", "make_client"),
    ("peers.py", "import httpx", "relay"),
    ("peers.py", "call httpx.AsyncClient", "make_client"),
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
                elif isinstance(child, ast.Import):
                    for alias in child.names:
                        if alias.name in NET_MODULES:
                            found.add((rel, "import " + alias.name, scope))
                elif isinstance(child, ast.ImportFrom):
                    if (child.module or "") in NET_MODULES:
                        found.add((rel, "import " + child.module, scope))
                visit(child, inner)

        visit(tree, "<module>")
    return found


def test_the_broker_egress_inventory_is_exact():
    assert _inventory() == EXPECTED


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
    finally:
        await client.aclose()


def test_core_dependencies_stay_websockets_and_sanic():
    text = (REPO / "pyproject.toml").read_text(encoding="utf-8")
    deps = re.search(r"^dependencies = \[(.*?)\]", text, re.M | re.S).group(1)
    assert re.findall(r'"([a-z0-9_-]+)', deps) == ["websockets", "sanic"]
    assert re.search(r'^proxy = \["httpx>=[0-9.]+"\]$', text, re.M)
