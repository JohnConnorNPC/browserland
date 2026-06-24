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


def _newlines_to_enter(data: str) -> str:
    r"""Map logical newlines in tool input to a carriage return — the byte a
    real Enter key sends.

    PowerShell/PSReadLine submits a command on CR (``\r``); a line-feed
    (``\n``) is taken as a *soft line-continuation* and parks the line under a
    ``>>`` prompt, so a naive ``"cmd\n"`` never runs (issue #13). CR also
    submits on a default cooked-mode Unix shell (the line discipline maps it to
    NL), so sending ``\r`` works for both. ``\r\n`` collapses to a single
    ``\r`` (one Enter) and an explicit ``\r`` is left untouched. Control and
    escape bytes (Ctrl-C ``0x03``, ESC sequences) are not newlines and pass
    through unchanged.

    This is **MCP-tool policy**, not a transport change: the raw
    ``POST /mcp/input`` endpoint and :meth:`BrowserlandClient.send_input` stay
    byte-for-byte verbatim, so a caller needing a literal LF or raw-mode bytes
    drives the endpoint directly."""
    return data.replace("\r\n", "\r").replace("\n", "\r")


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
    r"""Type text into a terminal. The target window must be in 'readwrite' mode.

    Newlines in `data` are sent as Enter (carriage return) so commands actually
    run — including on PowerShell, where a line-feed is only a soft
    continuation. To send raw bytes verbatim (a literal `\n`, or hand-crafted
    control/escape sequences), drive the broker's `POST /mcp/input` endpoint
    directly."""
    return get_client().send_input(id, _newlines_to_enter(data))


@mcp.tool()
def launch_terminal(profile: Optional[str] = None, cols: int = 80,
                    rows: int = 24, title: Optional[str] = None,
                    cwd: Optional[str] = None) -> Dict[str, Any]:
    """Spawn a new terminal from a profile. The broker must have 'allow_launch' enabled."""
    return get_client().launch_terminal(
        profile=profile, cols=cols, rows=rows, title=title, cwd=cwd)
