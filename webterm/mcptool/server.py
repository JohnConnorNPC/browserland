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

import threading
import time
from typing import Any, Dict, List, Optional, Tuple

from mcp.server.fastmcp import FastMCP

from .client import BrowserlandClient, BrowserlandError

mcp = FastMCP("browserland")

# Multi-host config (#24). configure() stores an ordered ``name -> (base, token)``
# map; the per-host BrowserlandClient is built lazily on first use, so importing
# this module has no side effects and stdio startup never blocks on a probe.
# Window ids in every tool are namespaced ``"<host>:<int>"`` and :func:`_route`
# splits one back to a ``(client, int_id)`` pair. A plain ``--broker-url``/
# ``--token`` config is just a single host named ``"default"``.
_host_configs: Dict[str, Tuple[str, str]] = {}   # name -> (base, token), ordered
_clients: Dict[str, BrowserlandClient] = {}       # name -> client (lazy)
# Guards the lazy build in _get_client: FastMCP dispatches sync tools on a thread
# pool, so two concurrent first-calls for one host could otherwise each construct
# (and leak) an httpx.Client.
_clients_lock = threading.Lock()


def configure(hosts) -> None:
    """Install the host map from an ordered iterable of ``(name, base, token)``
    descriptors. Closes any previously-built clients so their sockets aren't
    leaked; the new clients are built lazily on first use."""
    global _host_configs, _clients
    with _clients_lock:
        for client in _clients.values():
            try:
                client.close()
            except Exception:  # a flaky close must not abort reconfiguration
                pass
        _clients = {}
        _host_configs = {name: (base, token) for name, base, token in hosts}


def _get_client(name: str) -> BrowserlandClient:
    """Return the (lazily-built) client for an already-validated host name.
    Double-checked locking keeps concurrent first-calls from leaking a client."""
    client = _clients.get(name)
    if client is not None:
        return client
    with _clients_lock:
        client = _clients.get(name)
        if client is None:
            base, token = _host_configs[name]
            client = BrowserlandClient(base, token)
            _clients[name] = client
        return client


def _named_client(name: str) -> BrowserlandClient:
    """The client for an explicitly-named host, or a :class:`BrowserlandError`
    if the name isn't configured (never a bare ``KeyError``)."""
    if name not in _host_configs:
        raise BrowserlandError(
            0, "unknown_host",
            f"unknown host {name!r}; configured hosts: {sorted(_host_configs)}")
    return _get_client(name)


def _route(id: str) -> Tuple[BrowserlandClient, int]:
    """Split a namespaced ``"<host>:<int>"`` window id into its routed
    ``(client, int_id)``. Raises a clear :class:`BrowserlandError` (never a bare
    ``KeyError``/``ValueError``) on a non-string id, a malformed id, or an
    unknown host."""
    if not isinstance(id, str):
        raise BrowserlandError(
            0, "malformed_id",
            f"id must be a '<host>:<int>' string, got {id!r}")
    host, sep, rest = id.partition(":")
    # `rest` must be plain ASCII decimal digits: this rejects a sign, surrounding
    # whitespace, underscores ("1_000"), and Unicode digits — all of which int()
    # would otherwise silently accept and forward as a bogus window id.
    if not sep or not host or not (rest.isascii() and rest.isdigit()):
        raise BrowserlandError(
            0, "malformed_id",
            f"id {id!r} must be '<host>:<int>' (e.g. 'default:12345'); "
            "get one from list_terminals")
    return _named_client(host), int(rest)


def _aggregate(method_name: str) -> Dict[str, Any]:
    """Call the no-arg client ``method_name`` on every configured host and return
    a dict keyed by host name. A host that fails contributes
    ``{"ok": False, "error": msg}`` instead of sinking the whole call — used by
    the host-less mcp_info / list_profiles forms."""
    out: Dict[str, Any] = {}
    for name in _host_configs:
        # Catch broadly: a host returning HTTP 200 with malformed JSON raises
        # ValueError (not BrowserlandError) — it must still not sink the others.
        # The error value carries `ok: False` so a caller can tell it apart from
        # a real broker reply (the broker's own info() always reports ok: True).
        try:
            out[name] = getattr(_get_client(name), method_name)()
        except Exception as exc:
            out[name] = {"ok": False, "error": str(exc)}
    return out


def _launch_target(host: Optional[str]) -> Tuple[BrowserlandClient, str]:
    """Resolve which host ``launch_terminal`` targets, returning ``(client,
    name)``. An explicit ``host`` wins; with exactly one configured host it
    defaults to that host; otherwise a clear :class:`BrowserlandError` asks the
    caller to name one. An empty string is treated as *absent* (MCP clients
    often fill an optional string param with "" rather than null)."""
    if host:
        return _named_client(host), host
    if len(_host_configs) == 1:
        name = next(iter(_host_configs))
        return _get_client(name), name
    raise BrowserlandError(
        0, "host_required",
        f"host=... is required to choose a broker; configured hosts: "
        f"{sorted(_host_configs)}")


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


# Named keys -> the byte sequence a terminal sends. The cursor keys (arrows,
# Home, End) are mode-dependent and live in _CURSOR_KEYS instead: their form
# depends on DECCKM (see send_keys), which a stateless map can't encode.
_NAMED_KEYS = {
    "enter": "\r", "return": "\r",
    # LF (0x0A): a "logical Enter" for raw-mode ncurses/PDCurses TUIs that read
    # the keypad directly and act on line-feed, ignoring the CR that
    # "enter"/"return" send (e.g. Dwarf Fortress's Labor screen, #127). Same
    # byte as the ``C-j`` chord, but discoverable by name.
    "lf": "\n", "linefeed": "\n",
    "tab": "\t",
    "esc": "\x1b", "escape": "\x1b",
    "space": " ",
    # Backspace is DEL (0x7f), the common default; use ``C-h`` for BS (0x08).
    "backspace": "\x7f", "bs": "\x7f",
    "delete": "\x1b[3~", "del": "\x1b[3~",
    "pageup": "\x1b[5~", "pgup": "\x1b[5~",
    "pagedown": "\x1b[6~", "pgdn": "\x1b[6~",
    "insert": "\x1b[2~", "ins": "\x1b[2~",
    "f1": "\x1bOP", "f2": "\x1bOQ", "f3": "\x1bOR", "f4": "\x1bOS",
    "f5": "\x1b[15~", "f6": "\x1b[17~", "f7": "\x1b[18~", "f8": "\x1b[19~",
    "f9": "\x1b[20~", "f10": "\x1b[21~", "f11": "\x1b[23~", "f12": "\x1b[24~",
}

# Cursor keys: the final byte after the CSI/SS3 introducer. In NORMAL cursor
# mode they go out as CSI (``ESC [ x``); under DECCKM (application cursor keys,
# set by mc/vim/less) as SS3 (``ESC O x``) — send_keys picks the form from the
# terminal's live DECCKM state (#23).
_CURSOR_KEYS = {"up": "A", "down": "B", "right": "C", "left": "D",
                "home": "H", "end": "F"}

# C-<symbol> beyond the letters: ``ord & 0x1f`` folds @ [ \ ] ^ _ to 0x00 and
# 0x1b-0x1f. ``?`` is the lone exception (DEL, 0x7f).
_CTRL_SYMBOLS = "@[\\]^_"

_KEY_HELP = ("use a named key (Enter, LF, Tab, Esc, Space, Backspace, Delete, "
             "Up/Down/Left/Right, Home, End, PageUp, PageDown, Insert, F1-F12), "
             "a C-<char> or M-<char> chord (e.g. C-c for Ctrl-C, C-Space for "
             "NUL, M-x for Alt-x), or a single literal character")

# send_keys inter-key pacing cap (#129): the largest PER-TOKEN pause `delay_ms`
# may request, so any one inter-key pause is bounded. It caps per token, not the
# whole call (a long token list can still pace for a while — total ~= cap x
# tokens). A negative/zero delay disables pacing entirely (single-burst).
_MAX_KEY_DELAY_MS = 1000


def _ctrl_byte(ch: str) -> str:
    r"""The control byte (as a 1-char str) for ``C-<ch>``. ASCII letters and
    ``@ [ \ ] ^ _`` fold via ``ord & 0x1f`` (C-a=0x01 .. C-z=0x1a, C-@=0x00,
    C-[=ESC, C-_=0x1f); ``?`` is DEL (0x7f)."""
    if ch == "?":
        return "\x7f"
    if ch.isascii() and (ch.isalpha() or ch in _CTRL_SYMBOLS):
        return chr(ord(ch.upper()) & 0x1F)
    raise ValueError(f"unsupported Ctrl chord 'C-{ch}'")


def _keys_have_cursor(keys: List[str]) -> bool:
    """True if any token is a cursor key (so send_keys must learn DECCKM).
    Tolerates a non-list/None ``keys`` (returns False; _keys_to_text validates)."""
    if not isinstance(keys, list):
        return False
    return any(isinstance(t, str) and t.lower() in _CURSOR_KEYS for t in keys)


def _terminal_app_cursor(client, id: int) -> bool:
    """The terminal's cached DECCKM (application cursor keys), read cheaply from
    list_terminals (registry metadata — no screen render). Best-effort: any
    failure, or a producer that doesn't report it, reads as False (CSI form)."""
    try:
        for t in client.list_terminals():
            if t.get("id") == id:
                return bool(t.get("app_cursor"))
    except Exception:
        pass
    return False


def _token_to_text(tok: str, app_cursor: bool = False) -> str:
    r"""Translate ONE key token to the bytes (as a str; encoded UTF-8 on the
    wire) a terminal sends for it. Cursor keys use SS3 (``ESC O x``) when
    ``app_cursor`` (DECCKM) is set, else CSI (``ESC [ x``). Raises ``ValueError``
    on an unrecognized token so the caller learns rather than silently typing
    it as text."""
    final = _CURSOR_KEYS.get(tok.lower())
    if final is not None:
        return ("\x1bO" if app_cursor else "\x1b[") + final
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


def _keys_to_text(keys: List[str], app_cursor: bool = False) -> str:
    """Translate a list of key tokens into one string of terminal input bytes.
    **Atomic**: an unrecognized token raises before any byte is sent, so a bad
    token never leaves a half-typed line behind."""
    if not isinstance(keys, list) or not keys:
        raise ValueError("keys must be a non-empty list of key tokens")
    return "".join(_token_to_text(str(t), app_cursor) for t in keys)


@mcp.tool()
def mcp_info(host: Optional[str] = None) -> Dict[str, Any]:
    """Get Browserland broker MCP feature flags (allow_launch, default_mode).

    With `host` set, returns that one host's flags. Omit `host` (or pass "") to
    get a dict keyed by host name — each value is that host's flags, or
    `{"ok": false, "error": ...}` if the host is unreachable."""
    if host:
        return _named_client(host).info()
    return _aggregate("info")


@mcp.tool()
def list_terminals() -> Dict[str, Any]:
    """List Browserland terminals across all configured hosts (windows in 'off'
    mode are hidden).

    Returns `{"terminals": [...], "errors": {host: message}}`. Each terminal's
    `host` field is set to the configured MCP host name and its `id` is rewritten
    to the namespaced `"<host>:<int>"` form the other tools expect; the broker's
    own per-terminal `host` (the producer's machine hostname) is preserved under
    `machine_host`. A host that can't be reached is reported in `errors` and does
    not suppress the other hosts' terminals."""
    terminals: List[Dict[str, Any]] = []
    errors: Dict[str, str] = {}
    for name in _host_configs:
        # Build this host's slice in a local list so a failure partway through
        # (e.g. malformed JSON -> ValueError, not just BrowserlandError) reports
        # the host in `errors` without leaving half its terminals merged in.
        try:
            host_terms = []
            for t in _get_client(name).list_terminals():
                t = dict(t)
                # The broker already sends `host` = the producer's machine
                # hostname; the spec wants `host` to be the configured MCP host
                # name, so move the machine hostname aside rather than lose it.
                if "host" in t:
                    t.setdefault("machine_host", t["host"])
                t["host"] = name
                if "id" in t:
                    t["id"] = f"{name}:{t['id']}"
                host_terms.append(t)
            terminals.extend(host_terms)
        except Exception as exc:
            errors[name] = str(exc)
    return {"terminals": terminals, "errors": errors}


@mcp.tool()
def list_profiles(host: Optional[str] = None) -> Dict[str, Any]:
    """List the launchable terminal profile names and the broker default.

    With `host` set, returns that one host's profiles. Omit `host` (or pass "")
    to get a dict keyed by host name — each value is that host's profiles, or
    `{"ok": false, "error": ...}` if the host is unreachable."""
    if host:
        return _named_client(host).list_profiles()
    return _aggregate("list_profiles")


@mcp.tool()
def read_screen(id: str, view: str = "screen", lines: int = 0,
                wait_for_change: str = "", timeout_ms: int = 0,
                wait_for_text: str = "", wait_for_regex: str = "",
                wait_absent: bool = False, since: str = "",
                attrs: bool = False) -> Dict[str, Any]:
    """Render a terminal's current screen as plain text. Pass a namespaced window
    id ("<host>:<int>") from list_terminals.

    The result includes `content_hash` (a stable digest of the screen text),
    `alt_screen` (true for a full-screen TUI like mc/btop/vim — the grid is the
    whole story, so scrollback is meaningless) and `cursor` {row, col}. Note
    `cursor` is the terminal's HARDWARE cursor, not the highlighted menu row — a
    full-screen menu often parks it in a corner unrelated to the selection. For a
    shell, pass `view="scrollback"` with `lines=N` to get up to N lines of
    history above the current grid (`history_lines` reports how many were
    included; ignored when `alt_screen` is true).

    `partial` (present and true only when it applies) flags a valid but possibly
    INCOMPLETE grid: a long-running alt-screen TUI painted its frame once and
    only streams diffs, and so much output has scrolled by that the original
    full-frame paint was lost before it could be captured, so some
    statically-painted panels may be missing. It's distinct from `degraded` (a
    raw non-grid fallback). It self-heals — read again, or force a repaint with
    `send_keys(id, ["C-l"])` — after which `partial` is absent.

    COLOR / SELECTION — the default text mode drops cell color, so a menu row
    marked by color or reverse-video ALONE (its text identical to the others —
    e.g. a Dwarf Fortress menu) is invisible here. Pass `attrs=true` to also get
    `attr_runs`: the styled cell runs [{row, col, len, fg, bg, reverse}, ...]
    (0-based; `len` is a cell count) — the selected row shows up as a run whose
    `reverse` is true or whose `fg`/`bg` differ from the rest, so you can tell
    which row is highlighted before pressing an activate key. `attr_runs` is the
    full current list (never a delta) and rides the high-fidelity renderer; it is
    absent on the rare `degraded` raw read. `content_hash`/`wait_for_change`
    track text only, so a color-only selection MOVE (same text) won't trip them —
    read with `attrs=true` after a cursor keypress rather than waiting on it.

    WAITING (one call, no polling) — all bounded by `timeout_ms` (capped 15000):
    - wait for ANY change: pass the previous read's `content_hash` as
      `wait_for_change`. Blocks until the screen differs, else returns the
      current screen at timeout. Best for a mostly-static shell.
    - wait for SPECIFIC content: pass `wait_for_text` (substring) or
      `wait_for_regex` (regex) to block until that appears — or set
      `wait_absent=true` to block until it DISAPPEARS. The result adds
      `matched`: true if the text/regex condition was met, false if it timed
      out. Prefer this on a busy TUI where every frame changes the hash (a
      clock, a spinner), so `wait_for_change` would wake on noise. Typical use:
        send_keys(id, ["Enter"])
        read_screen(id, wait_for_text="Ready", timeout_ms=5000)   # -> matched
    Note the search surface is the newline-joined grid, so a value wrapped
    across two rows won't match. Omit all wait_* params for an immediate read.

    DELTA (less to read) — to avoid re-reading the whole grid every call when
    driving a TUI, pass the previous read's `content_hash` as `since`: if the
    agent still holds that frame the result drops `text` and instead returns
    `delta=true` + `changed_rows` ([{row, text}, ...] — only the rows that
    differ), which you apply to your own copy of the screen. On a miss (frame
    evicted, resized, or mostly changed) it returns the full grid with
    `delta=false`, so always check `delta`. `content_hash` is always the full
    screen's hash — feed it back as the next `since`. Combine with a wait mode
    to wake on an event AND get only the delta in one call."""
    client, int_id = _route(id)
    return client.read_screen(int_id, view=view, lines=lines,
                              wait_for_change=wait_for_change or None,
                              timeout_ms=timeout_ms,
                              wait_for_text=wait_for_text or None,
                              wait_for_regex=wait_for_regex or None,
                              wait_absent=wait_absent,
                              since=since or None,
                              attrs=attrs)


@mcp.tool()
def send_input(id: str, data: str) -> Dict[str, Any]:
    r"""Type text into a terminal. Pass a namespaced window id ("<host>:<int>");
    the target window must be in 'readwrite' mode.

    For control/escape keys — Esc, Ctrl-C, arrows, Enter, function keys — use
    `send_keys` (e.g. `send_keys(id, ["Esc"])`); don't hand-assemble escape
    bytes or POST to the HTTP endpoint yourself.

    Newlines in `data` are sent as Enter (carriage return) so commands actually
    run — including on PowerShell, where a line-feed is only a soft
    continuation. Any control/escape bytes already in `data` pass through
    unchanged. The single thing this tool can't express is a literal line-feed
    byte (`\n` is remapped to Enter); for that rare case, drive the broker's
    `POST /mcp/input` endpoint directly.

    If read_screen looks corrupted, this tool can't fix it — bytes in `data`
    reach the app, not Browserland's renderer. Send `send_keys(id, ["C-l"])` to
    redraw a live app, or `reset_terminal(id)` to wipe the screen buffer."""
    client, int_id = _route(id)
    return client.send_input(int_id, _newlines_to_enter(data))


@mcp.tool()
def send_keys(id: str, keys: List[str], delay_ms: int = 0) -> Dict[str, Any]:
    r"""Send terminal KEY SEQUENCES (control/escape keys) to a terminal. The
    window must be in 'readwrite' mode.

    Use this for keys that aren't plain text — Ctrl-C, Esc, arrows, function
    keys — which `send_input` can't express. `keys` is a list of tokens, each
    one of:
      - a named key: Enter, LF, Tab, Esc, Space, Backspace, Delete, Up, Down,
        Left, Right, Home, End, PageUp, PageDown, Insert, F1-F12;
      - a Ctrl chord `C-<char>` (e.g. `C-c` -> 0x03 / Ctrl-C, `C-d`, `C-[`,
        `C-Space` -> NUL, `C-h` -> 0x08), or an Alt chord `M-<char>` (ESC + char);
      - a single literal character (sent as its UTF-8 bytes).
    e.g. `["C-c"]` to interrupt, `["Esc"]`, `["Up","Up","Enter"]`.

    Enter emits a carriage return (CR, 0x0D) — what a real Enter key sends and
    what cooked-mode shells expect. A raw-mode ncurses/PDCurses TUI that reads
    the keypad directly may ignore CR and act only on a line-feed (LF, 0x0A);
    for those send `LF` (identical to the `C-j` chord) instead of `Enter` — e.g.
    Dwarf Fortress's per-dwarf Labor screen (#127).

    This **emits the byte sequences** a keyboard would send (Ctrl-C -> 0x03); it
    does not synthesize OS key events. Whether 0x03 actually interrupts depends
    on the target's PTY/mode (Browserland's headless agents use a backend where
    it does). Arrows/Home/End are sent as SS3 (`ESC O x`) when the terminal has
    DECCKM / application-cursor-key mode on (mc, vim, less), else CSI (`ESC [ x`)
    — best-effort from the agent's cached DECCKM, so arrows work in those TUIs
    without hand-assembling escapes (#23); it falls back to CSI for a non-agent
    producer or if the state can't be read. Tokens are sent verbatim (no
    newline->Enter rewrite); use `send_input` for ordinary text.

    Recovery: if read_screen shows ghost text or a corrupted screen but the app
    is still responding, send `["C-l"]` (Ctrl-L) to make the app repaint — that
    fresh output is what the renderer reads. A raw reset sequence sent as input
    is just keystrokes to the app and does NOT reset Browserland's own screen
    render; to wipe a corrupted buffer regardless of the app, use
    `reset_terminal`. (Neither un-freezes a hung app — kill it.)

    Pacing (#129): by default the whole token list is written in ONE burst. A
    frame-polling raw-input TUI — one that reads input once per render frame,
    like Dwarf Fortress — drops keys that arrive faster than it polls, so a
    burst of arrows or spaces can advance only partially. Pass `delay_ms` (per
    token, capped 1000) to write each token in its own POST with that pause
    between them, so every keypress lands on a separate frame; the default `0`
    keeps the single-burst write (back-compat). Pacing only kicks in with more
    than one token, and a bad token still raises before any byte is sent.

    Pass a namespaced window id ("<host>:<int>")."""
    # Validate + translate up front (atomic, CSI form): a bad token raises here,
    # before any routing, DECCKM lookup or send.
    text = _keys_to_text(keys)
    client, int_id = _route(id)
    app_cursor = _keys_have_cursor(keys) and _terminal_app_cursor(client, int_id)
    if app_cursor:
        text = _keys_to_text(keys, app_cursor=True)   # re-encode arrows as SS3
    pace = min(max(int(delay_ms), 0), _MAX_KEY_DELAY_MS)
    if pace and len(keys) > 1:
        # #129: write one token per POST with a pause between, so a frame-polling
        # TUI sees each keypress on its own frame instead of dropping a burst.
        # Tokens are already validated above, so this can't half-type a line.
        result: Dict[str, Any] = {}
        for i, tok in enumerate(keys):
            if i:
                time.sleep(pace / 1000.0)
            result = client.send_input(int_id, _token_to_text(str(tok), app_cursor))
        return result
    return client.send_input(int_id, text)


@mcp.tool()
def reset_terminal(id: str) -> Dict[str, Any]:
    """Wipe Browserland's screen buffer for a terminal so read_screen renders
    from a clean slate. Pass a namespaced window id ("<host>:<int>"); the window
    must be in 'readwrite' mode.

    Use this when read_screen shows accumulated ghost text / corruption that a
    redraw won't clear: it empties the agent's PTY-output ring — the buffer the
    screen renderer actually reads — so the NEXT read_screen starts blank,
    regardless of what the app does. It does NOT touch the running app (it sends
    nothing to the app's stdin) and can't un-freeze a hung process — kill that
    instead. After a reset the screen repopulates as the app emits output, so
    for a live app prefer `send_keys(id, ["C-l"])` first to force a redraw;
    reach for reset_terminal when even that won't clear the corruption."""
    client, int_id = _route(id)
    return client.reset_terminal(int_id)


@mcp.tool()
def launch_terminal(profile: Optional[str] = None, cols: int = 80,
                    rows: int = 24, title: Optional[str] = None,
                    cwd: Optional[str] = None,
                    host: Optional[str] = None) -> Dict[str, Any]:
    """Spawn a new terminal from a profile. The broker must have 'allow_launch'
    enabled. With multiple hosts configured, `host` is required to choose which
    broker; with a single host it's optional. The returned `id` is namespaced
    ("<host>:<int>") so it can be passed straight to the other tools."""
    client, name = _launch_target(host)
    result = client.launch_terminal(
        profile=profile, cols=cols, rows=rows, title=title, cwd=cwd)
    if isinstance(result, dict) and "id" in result:
        result = dict(result)
        result["id"] = f"{name}:{result['id']}"
    return result
