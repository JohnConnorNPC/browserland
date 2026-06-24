"""FastMCP server exposing the Browserland broker's ``/mcp/*`` interface as tools.

Each tool delegates to a module-level :class:`BrowserlandClient` and returns the
broker's dict/list verbatim — FastMCP turns the return value into structured
tool output, and the type hints + docstrings drive the tool schema.

The client is built **lazily** from config handed in by ``__main__`` via
:func:`configure`, so importing this module has no side effects and stdio
startup never blocks on a network probe. The first tool call surfaces a clean
:class:`BrowserlandError` if the broker URL or token is wrong.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from mcp.server.fastmcp import FastMCP

from .client import BrowserlandClient

mcp = FastMCP("browserland")

# Resolved config + lazily-built client. Set by configure(); the client is not
# constructed until the first tool call.
_base: str = "http://127.0.0.1:4445"
_token: str = ""
_client: Optional[BrowserlandClient] = None


def configure(base: str, token: str) -> None:
    """Set the broker URL + MCP token for the lazily-built client."""
    global _base, _token, _client
    _base = base
    _token = token
    if _client is not None:  # close any prior client so its socket isn't leaked
        _client.close()
        _client = None


def get_client() -> BrowserlandClient:
    """Return the shared client, building it on first use."""
    global _client
    if _client is None:
        _client = BrowserlandClient(_base, _token)
    return _client


@mcp.tool()
def mcp_info() -> Dict[str, Any]:
    """Get Browserland broker MCP feature flags (allow_launch, default_mode)."""
    return get_client().info()


@mcp.tool()
def list_terminals() -> List[Dict[str, Any]]:
    """List the Browserland terminals visible to MCP (windows in 'off' mode are hidden)."""
    return get_client().list_terminals()


@mcp.tool()
def list_profiles() -> Dict[str, Any]:
    """List the launchable terminal profile names and the broker default."""
    return get_client().list_profiles()


@mcp.tool()
def read_screen(id: int) -> Dict[str, Any]:
    """Render a terminal's current screen as plain text. Pass a window id from list_terminals."""
    return get_client().read_screen(id)


@mcp.tool()
def send_input(id: int, data: str) -> Dict[str, Any]:
    """Type text into a terminal. The target window must be in 'readwrite' mode."""
    return get_client().send_input(id, data)


@mcp.tool()
def launch_terminal(profile: Optional[str] = None, cols: int = 80,
                    rows: int = 24, title: Optional[str] = None,
                    cwd: Optional[str] = None) -> Dict[str, Any]:
    """Spawn a new terminal from a profile. The broker must have 'allow_launch' enabled."""
    return get_client().launch_terminal(
        profile=profile, cols=cols, rows=rows, title=title, cwd=cwd)
