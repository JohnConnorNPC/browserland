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


# Named keys -> the byte sequence a NORMAL-cursor-mode xterm sends. Arrows and
# Home/End use the normal CSI form; an application-cursor-mode TUI may instead
# expect SS3 (``ESC O ...``) — a stateless translator can't know which, so this
# is documented as a known limit (works in shells; may differ inside some TUIs).
_NAMED_KEYS = {
    "enter": "\r", "return": "\r",
    "tab": "\t",
    "esc": "\x1b", "escape": "\x1b",
    "space": " ",
    # Backspace is DEL (0x7f), the common default; use ``C-h`` for BS (0x08).
    "backspace": "\x7f", "bs": "\x7f",
    "delete": "\x1b[3~", "del": "\x1b[3~",
    "up": "\x1b[A", "down": "\x1b[B", "right": "\x1b[C", "left": "\x1b[D",
    "home": "\x1b[H", "end": "\x1b[F",
    "pageup": "\x1b[5~", "pgup": "\x1b[5~",
    "pagedown": "\x1b[6~", "pgdn": "\x1b[6~",
    "insert": "\x1b[2~", "ins": "\x1b[2~",
    "f1": "\x1bOP", "f2": "\x1bOQ", "f3": "\x1bOR", "f4": "\x1bOS",
    "f5": "\x1b[15~", "f6": "\x1b[17~", "f7": "\x1b[18~", "f8": "\x1b[19~",
    "f9": "\x1b[20~", "f10": "\x1b[21~", "f11": "\x1b[23~", "f12": "\x1b[24~",
}

# C-<symbol> beyond the letters: ``ord & 0x1f`` folds @ [ \ ] ^ _ to 0x00 and
# 0x1b-0x1f. ``?`` is the lone exception (DEL, 0x7f).
_CTRL_SYMBOLS = "@[\\]^_"

_KEY_HELP = ("use a named key (Enter, Tab, Esc, Space, Backspace, Delete, "
             "Up/Down/Left/Right, Home, End, PageUp, PageDown, Insert, F1-F12), "
             "a C-<char> or M-<char> chord (e.g. C-c for Ctrl-C, C-Space for "
             "NUL, M-x for Alt-x), or a single literal character")


def _ctrl_byte(ch: str) -> str:
    r"""The control byte (as a 1-char str) for ``C-<ch>``. ASCII letters and
    ``@ [ \ ] ^ _`` fold via ``ord & 0x1f`` (C-a=0x01 .. C-z=0x1a, C-@=0x00,
    C-[=ESC, C-_=0x1f); ``?`` is DEL (0x7f)."""
    if ch == "?":
        return "\x7f"
    if ch.isascii() and (ch.isalpha() or ch in _CTRL_SYMBOLS):
        return chr(ord(ch.upper()) & 0x1F)
    raise ValueError(f"unsupported Ctrl chord 'C-{ch}'")


def _token_to_text(tok: str) -> str:
    r"""Translate ONE key token to the bytes (as a str; encoded UTF-8 on the
    wire) a terminal sends for it. Raises ``ValueError`` on an unrecognized
    token so the caller learns rather than silently typing it as text."""
    named = _NAMED_KEYS.get(tok.lower())
    if named is not None:
        return named
    if len(tok) >= 2 and tok[1] == "-" and tok[0] in "cCmM":
        kind, rest = tok[0].lower(), tok[2:]
        if kind == "c":  # Ctrl chord
            if rest.lower() == "space":
                return "\x00"
            if len(rest) == 1:
                return _ctrl_byte(rest)
            raise ValueError(f"a Ctrl chord takes one character: {tok!r}")
        # Meta/Alt chord -> ESC prefix + the (single) character's UTF-8.
        if len(rest) == 1:
            return "\x1b" + rest
        raise ValueError(f"a Meta chord takes one character: {tok!r}")
    if len(tok) == 1:
        return tok  # single literal character (sent as its UTF-8 bytes)
    raise ValueError(f"unrecognized key {tok!r}; {_KEY_HELP}")


def _keys_to_text(keys: List[str]) -> str:
    """Translate a list of key tokens into one string of terminal input bytes.
    **Atomic**: an unrecognized token raises before any byte is sent, so a bad
    token never leaves a half-typed line behind."""
    if not isinstance(keys, list) or not keys:
        raise ValueError("keys must be a non-empty list of key tokens")
    return "".join(_token_to_text(str(t)) for t in keys)


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
def read_screen(id: int, view: str = "screen", lines: int = 0) -> Dict[str, Any]:
    """Render a terminal's current screen as plain text. Pass a window id from
    list_terminals.

    The result includes `alt_screen` (true for a full-screen TUI like mc/btop/
    vim — the grid is the whole story, so scrollback is meaningless) and
    `cursor` {row, col}. For a shell, pass `view="scrollback"` with `lines=N` to
    get up to N lines of history above the current grid (`history_lines` reports
    how many were included; ignored when `alt_screen` is true)."""
    return get_client().read_screen(id, view=view, lines=lines)


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
def send_keys(id: int, keys: List[str]) -> Dict[str, Any]:
    r"""Send terminal KEY SEQUENCES (control/escape keys) to a terminal. The
    window must be in 'readwrite' mode.

    Use this for keys that aren't plain text — Ctrl-C, Esc, arrows, function
    keys — which `send_input` can't express. `keys` is a list of tokens, each
    one of:
      - a named key: Enter, Tab, Esc, Space, Backspace, Delete, Up, Down,
        Left, Right, Home, End, PageUp, PageDown, Insert, F1-F12;
      - a Ctrl chord `C-<char>` (e.g. `C-c` -> 0x03 / Ctrl-C, `C-d`, `C-[`,
        `C-Space` -> NUL, `C-h` -> 0x08), or an Alt chord `M-<char>` (ESC + char);
      - a single literal character (sent as its UTF-8 bytes).
    e.g. `["C-c"]` to interrupt, `["Esc"]`, `["Up","Up","Enter"]`.

    This **emits the byte sequences** a keyboard would send (Ctrl-C -> 0x03); it
    does not synthesize OS key events. Whether 0x03 actually interrupts depends
    on the target's PTY/mode (Browserland's headless agents use a backend where
    it does). Arrows/Home/End use normal-cursor-mode sequences and may differ
    inside an application-cursor-mode TUI. Tokens are sent verbatim (no
    newline->Enter rewrite); use `send_input` for ordinary text."""
    return get_client().send_input(id, _keys_to_text(keys))


@mcp.tool()
def launch_terminal(profile: Optional[str] = None, cols: int = 80,
                    rows: int = 24, title: Optional[str] = None,
                    cwd: Optional[str] = None) -> Dict[str, Any]:
    """Spawn a new terminal from a profile. The broker must have 'allow_launch' enabled."""
    return get_client().launch_terminal(
        profile=profile, cols=cols, rows=rows, title=title, cwd=cwd)
