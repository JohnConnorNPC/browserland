"""#260: the class the /mcp/config relay bug belonged to.

``peers.MCP_ALLOW`` is the list of routes the hub sends a peer's MCP token to,
so every entry must be MCP-gated on the peer itself: the browser token alone
is refused (401), the MCP token is not. A browser-realm route listed there --
as ``GET``/``POST /mcp/config`` once were -- fails here: on the peer the
browser token passes and the MCP token is the one refused.

A file of its own on purpose: it drives a fresh app with Sanic's in-process
test client, which must not share a module with the live-server fixtures of
tests/test_peer_proxy.py (the test client re-finalizes the router state those
servers are running on).
"""

from __future__ import annotations

from webterm.broker import peers
from webterm.broker.app import create_app

from .auth_helpers import TEST_TOKEN

B_MCP = "b-mcp-token"


def test_every_mcp_allow_route_is_mcp_gated_on_the_peer(tmp_path, monkeypatch):
    for var in ("WEB_TERMINAL_TOKEN", "WEB_TERMINAL_MCP_TOKEN",
                "WEB_TERMINAL_MCP_ADMIN_TOKEN"):
        monkeypatch.delenv(var, raising=False)
    app = create_app(
        {"state_path": str(tmp_path / "webterm_state.json"),
         "auth_token": TEST_TOKEN, "serve_ui": False, "peers": {},
         "mcp_enabled": True, "mcp_token": B_MCP,
         "mcp_state_path": str(tmp_path / "webterm_mcp.json")},
        port=4999, name="webterm-mcp-allow-gate-class")
    assert peers.MCP_ALLOW
    for method, path in sorted(peers.MCP_ALLOW):
        kw = {"json": {}} if method == "POST" else {}
        call = getattr(app.test_client, method.lower())
        _, browser_only = call(
            path, headers=[("Authorization", "Bearer " + TEST_TOKEN)], **kw)
        assert browser_only.status == 401, (method, path, browser_only.status)
        _, with_mcp = call(
            path, headers=[("Authorization", "Bearer " + B_MCP)], **kw)
        assert with_mcp.status != 401, (method, path, with_mcp.status)
