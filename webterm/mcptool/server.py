"""FastMCP server exposing the Browserland broker's ``/mcp/*`` interface as tools.

Each tool delegates to a module-level :class:`BrowserlandClient` and returns the
broker's dict/list verbatim — FastMCP turns the return value into structured
tool output, and the type hints + docstrings drive the tool schema.

The client is built **lazily** from config handed in by ``__main__`` via
:func:`configure`, so importing this module has no side effects. The one
deliberate exception to "never probe at startup" (#245, owner decision): the
END of ``configure()`` probes every admin-token host's ``/mcp/info``
concurrently under a single bounded deadline, because admin-only tools must be
hidden unless a host confirms admin — that costs at most one timeout at stdio
startup, then never blocks again. The first tool call surfaces a clean
:class:`BrowserlandError` if the broker URL or token is wrong.
"""

from __future__ import annotations

import threading
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, List, NamedTuple, Optional, Tuple

from mcp.server.fastmcp import FastMCP

from .client import BrowserlandClient, BrowserlandError

mcp = FastMCP("browserland")


class HostConfig(NamedTuple):
    """One configured host (#24 multi-host, #245 admin credential): the base
    URL, the MCP token, the optional declared scope and the optional per-host
    admin token. This NamedTuple replaces the positional tuples configure()
    used to store, so a growing host map stays self-describing. Plain tuples
    ``(name, base, token[, scope[, admin_token]])`` are still accepted at the
    configure() seam and normalized (missing fields default to None), which
    keeps every existing caller and test injection working."""

    name: str
    base: str
    token: str
    scope: Optional[str] = None
    admin_token: Optional[str] = None


def _as_host_config(name: str, raw: Any) -> HostConfig:
    """Normalize one ``_host_configs`` value to a :class:`HostConfig`,
    tolerating the legacy positional tuples tests inject directly."""
    if isinstance(raw, HostConfig):
        return raw
    scope = raw[2] if len(raw) > 2 else None
    admin = raw[3] if len(raw) > 3 else None
    return HostConfig(name, raw[0], raw[1], scope, admin)


# Multi-host config (#24). configure() stores an ordered name -> HostConfig
# map; the per-host BrowserlandClient is built lazily on first use, so
# importing this module has no side effects and stdio startup never blocks
# on a probe (a scoped host's /mcp/info check, #232, runs on its first call).
# Window ids in every tool are namespaced ``"<host>:<int>"`` and
# :func:`_route` splits one back to a ``(client, int_id)`` pair. A plain
# ``--broker-url``/``--token`` config is just a single host named ``"default"``.
# name -> HostConfig, ordered
_host_configs: Dict[str, HostConfig] = {}
_clients: Dict[str, BrowserlandClient] = {}       # name -> client (lazy)
# Guards the lazy build in _get_client: FastMCP dispatches sync tools on a thread
# pool, so two concurrent first-calls for one host could otherwise each construct
# (and leak) an httpx.Client.
_clients_lock = threading.Lock()

# #243: launch handles this process holds, keyed by the namespaced window id.
# Bounded as an LRU (_HANDLES_MAX, oldest evicted): a long-lived server forgets
# the OLDEST handle, never the newest. NEVER pruned by visibility-filtered
# listings — a listing that hides an off/out-of-scope window hides exactly the
# window whose handle is the recovery path. Cleared wholesale by configure()
# (host names are reusable; a stale "host:id" handle must not survive a
# re-pointed host).
_handles: OrderedDict = OrderedDict()             # namespaced id -> handle
_handles_lock = threading.Lock()
_HANDLES_MAX = 256

# #245: the hosts whose admin credential the startup probe confirmed (filled
# by configure()'s probe; empty until then and whenever no host qualified).
# Admin-path tools check membership and refuse locally for every other host.
_admin_hosts: set = set()
# The admin tools are registered at most ONCE per process: FastMCP cannot
# unregister, and configure() is once-per-process in production (__main__
# calls it a single time). A later re-configure() re-probes and updates
# _admin_hosts (the per-host gate), but an earlier registration stays listed
# — documented as a limitation; the tests pin the refused-locally ladder.
_admin_tools_registered = False

# One overall deadline for the startup admin probe (#245): the browser's
# client default timeout, so the worst-case stdio startup delay is one
# timeout no matter how many admin hosts are configured.
_ADMIN_PROBE_TIMEOUT_S = 10.0


def _handle_remember(nid: str, handle: str) -> None:
    """Store a launch handle under its namespaced window id, LRU-bounded."""
    with _handles_lock:
        _handles[nid] = handle
        _handles.move_to_end(nid)
        while len(_handles) > _HANDLES_MAX:
            _handles.popitem(last=False)


def _handle_for(nid: str) -> Optional[str]:
    """The handle this process holds for ``nid``, or None. A hit counts as
    use (recently-used handles survive eviction)."""
    with _handles_lock:
        handle = _handles.get(nid)
        if handle is not None:
            _handles.move_to_end(nid)
        return handle


def configure(hosts) -> None:
    """Install the host map from an ordered iterable of descriptors —
    :class:`HostConfig` or a positional ``(name, base, token[, scope[,
    admin_token]])`` tuple (scope/admin_token None = unset). Closes any
    previously-built clients so their sockets aren't leaked; the new clients
    are built lazily on first use. Also clears, atomically with the rebuild,
    every cross-config state: the launch-handle store and the admin-host set
    (host names are reusable; a stale ``host:id`` handle or admin grant must
    not survive a re-pointed host) — the per-client capability caches die
    with the closed clients.

    After the rebuild, the one-shot admin probe (#245) runs: every host
    carrying an admin token is probed concurrently (one overall deadline);
    the hosts whose ``/mcp/info`` answers ``admin: true`` land in
    ``_admin_hosts``, and the admin tools are registered once any host
    qualifies."""
    global _host_configs, _clients
    with _clients_lock:
        for client in _clients.values():
            try:
                client.close()
            except Exception:  # a flaky close must not abort reconfiguration
                pass
        _clients = {}
        with _handles_lock:
            _handles.clear()
        _admin_hosts.clear()
        new_configs: Dict[str, HostConfig] = {}
        for h in hosts:
            cfg = h if isinstance(h, HostConfig) else HostConfig(*h)
            new_configs[cfg.name] = cfg
        _host_configs = new_configs
    _probe_admin_hosts()


def _probe_admin_hosts() -> None:
    """#245: the one-shot startup admin probe. For every host carrying an
    admin token, GET ``/mcp/info`` (the client sends the admin header); the
    host is admin-capable iff the body is a dict with ``admin: true`` — a
    pre-#245 broker answers without the key, and an unreachable host or a
    wrong token raises: every failure reads as NOT admin (fail closed). The
    probes run concurrently under one overall deadline
    (``_ADMIN_PROBE_TIMEOUT_S``, the client default), so the worst-case
    startup delay is one timeout, not timeout × hosts. When at least one
    host qualifies, the admin tools are registered exactly once for the
    process lifetime."""
    global _admin_tools_registered
    names = [name for name, cfg in _host_configs.items()
             if _as_host_config(name, cfg).admin_token]
    if not names:
        return

    def probe(name: str) -> bool:
        try:
            info = _get_client(name)._send(
                "GET", "/mcp/info", timeout=_ADMIN_PROBE_TIMEOUT_S)
        except Exception:
            return False
        return isinstance(info, dict) and info.get("admin") is True

    with ThreadPoolExecutor(max_workers=len(names)) as pool:
        qualified = list(pool.map(probe, names))
    _admin_hosts.update(
        name for name, ok in zip(names, qualified) if ok)
    if _admin_hosts and not _admin_tools_registered:
        mcp.tool(name="set_mcp_knobs")(set_mcp_knobs)
        mcp.tool(name="workspaces")(workspaces)
        _admin_tools_registered = True


def _get_client(name: str) -> BrowserlandClient:
    """Return the (lazily-built) client for an already-validated host name.
    Double-checked locking keeps concurrent first-calls from leaking a client.
    The client carries the host's scope (its header and its fail-closed
    probe, #232), its per-host admin token (header, #245) and its name for
    error messages."""
    client = _clients.get(name)
    if client is not None:
        return client
    with _clients_lock:
        client = _clients.get(name)
        if client is None:
            cfg = _as_host_config(name, _host_configs[name])
            client = BrowserlandClient(base=cfg.base, token=cfg.token,
                                       scope=cfg.scope,
                                       admin_token=cfg.admin_token, name=name)
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
             "NUL, M-x for Alt-x), an S-<key> Shift chord for Tab or a cursor "
             "key (S-Tab for back-tab, S-Up), or a single literal character")

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


def _shift_seq(rest: str, tok: str) -> str:
    r"""The byte sequence for a Shift chord ``S-<key>``. Only keys with a
    distinct shifted terminal encoding are supported: a cursor key folds to the
    CSI modifier-parameter form ``ESC [ 1;2 <final>`` (used regardless of DECCKM
    — a modified cursor key always takes the CSI form, never SS3), and Tab
    becomes back-tab ``ESC [ Z``. Any other key has no standard shifted encoding,
    so it raises rather than silently sending the unshifted bytes."""
    cursor = _CURSOR_KEYS.get(rest.lower())
    if cursor is not None:
        return "\x1b[1;2" + cursor
    if rest.lower() == "tab":
        return "\x1b[Z"
    raise ValueError(
        f"no shifted form for {tok!r}; a Shift chord covers only Tab "
        "(S-Tab -> back-tab) and the cursor keys (S-Up/Down/Left/Right, "
        "S-Home, S-End)")


def _keys_have_cursor(keys: List[str]) -> bool:
    """True if any token is a cursor key (so send_keys must learn DECCKM).
    Tolerates a non-list/None ``keys`` (returns False; _keys_to_text validates)."""
    if not isinstance(keys, list):
        return False
    return any(isinstance(t, str) and t.lower() in _CURSOR_KEYS for t in keys)


def _terminal_meta(client, id: int) -> Dict[str, Any]:
    """The terminal's registry metadata dict (from list_terminals — no screen
    render): the matching terminal, or ``{}`` on any failure / a producer that
    doesn't report it. ONE cheap fetch backs BOTH the cached DECCKM (``app_cursor``,
    #23 — CSI vs SS3 arrows) and the per-terminal default pacing (``pace_ms``,
    #133), so send_keys never pays two round-trips. Best-effort: a missing field
    reads as its neutral default (app_cursor=False -> CSI, pace_ms=0 -> burst)."""
    try:
        for t in client.list_terminals():
            if t.get("id") == id:
                return t
    except Exception:
        pass
    return {}


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
    if len(tok) >= 2 and tok[1] == "-" and tok[0] in "cCmMsS":
        kind, rest = tok[0].lower(), tok[2:]
        if kind == "c":  # Ctrl chord
            if rest.lower() == "space":
                return "\x00"
            if len(rest) == 1:
                return _ctrl_byte(rest)
            raise ValueError(f"a Ctrl chord takes one character: {tok!r}")
        if kind == "s":  # Shift chord (Tab / cursor keys only)
            return _shift_seq(rest, tok)
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
    """Get Browserland broker MCP feature flags (allow_launch, default_mode),
    plus `scope`: the scope this server declared to that host, as the broker
    saw it (null when unscoped).

    With `host` set, returns that one host's flags. Omit `host` (or pass "") to
    get a dict keyed by host name — each value is that host's flags, or
    `{"ok": false, "error": ...}` if the host is unreachable (or, for a scoped
    host, did not confirm the scope)."""
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
    not suppress the other hosts' terminals.

    Each terminal's `scope` is its tag (null when untagged). When this server
    declares a scope for a host (--scope, BROWSERLAND_MCP_SCOPE, or that host's
    own "scope"), it sees ONLY the windows tagged with that scope there:
    windows started by hand stay invisible until someone tags them. A scoped
    host that does not confirm the scope lists nothing and reports
    `scope_unsupported` in `errors` instead."""
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
                    # #243: mark the windows this process holds launch
                    # handles for — the ones set_window can still manage
                    # (an owner that restarted this server lost them).
                    if _handle_for(t["id"]) is not None:
                        t["owned"] = True
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
                attrs: bool = False, wait_for_idle: int = 0) -> Dict[str, Any]:
    """Render a terminal's current screen as plain text. Pass a namespaced window
    id ("<host>:<int>") from list_terminals.

    The result includes `content_hash` (a stable digest of the screen text),
    `stable_hash` (the same digest with the hardware-cursor cell masked, so a
    cursor BLINK in place doesn't change it while a cursor MOVE does — see
    `wait_for_idle`), `alt_screen` (true for a full-screen TUI like mc/btop/vim —
    the grid is the whole story, so scrollback is meaningless) and `cursor`
    {row, col}. Note
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
    raw non-grid fallback). It self-heals — read again, or trigger any repaint
    from the app — after which `partial` is absent.

    `idle_ms` is ms since the terminal last emitted output — best-effort, and
    absent from older agents. It is UNRELIABLE for a perpetually-animating app
    (Dwarf Fortress paints every frame, so its `idle_ms` never grows): for those,
    pacing/flush and a semantic screen check (`wait_for_text`/`wait_for_regex`)
    are the real settle signals, not `idle_ms`.

    COLOR / SELECTION — the default text mode drops cell attributes, so a menu row
    marked by color, reverse-video, bold, or underline ALONE (its text identical
    to the others — e.g. a Dwarf Fortress menu) is invisible here. Pass
    `attrs=true` to also get
    `attr_runs`: the styled cell runs [{row, col, len, fg, bg, reverse, bold,
    underscore}, ...] (0-based; `len` is a cell count) — the selected row shows up
    as a run whose `reverse`/`bold`/`underscore` is true or whose `fg`/`bg` differ
    from the rest, so you can tell
    which row is highlighted before pressing an activate key. `attr_runs` is the
    full current list (never a delta) and rides the high-fidelity renderer; it is
    absent on the rare `degraded` raw read. `content_hash`/`wait_for_change`
    track text only, so a style-only selection MOVE (same text) won't trip them —
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
    across two rows won't match.
    - wait for the screen to SETTLE: pass `wait_for_idle=<ms>` to block until the
      screen has stopped changing for that many ms (measured on `stable_hash`, so
      a bare cursor blink doesn't reset it), then return with `matched`: true (or
      false if it never settled before `timeout_ms`). Pass a `timeout_ms`
      comfortably LARGER than `wait_for_idle` (e.g. `wait_for_idle=300`,
      `timeout_ms=5000`) — with no `timeout_ms` (or one <= `wait_for_idle`) the
      settle window can't fit before the deadline, so it returns `matched: false`
      at once. Use it after firing an action to wait for the app to finish
      redrawing. CAVEAT: it only settles a
      CALMER TUI — a fully-animating app (Dwarf Fortress animates creatures,
      water and the *PAUSED* marquee off the cursor every frame) never reaches
      output-idle, so `wait_for_idle`/`stable_hash` never fire there; for those,
      use input pacing/flush (`set_pace`/`flush_input`) plus a semantic
      `wait_for_text`/`wait_for_regex` on the menu text, not idle.
    The wait modes are mutually exclusive (one signal decides `matched`) — pass
    only one of wait_for_change / wait_for_text / wait_for_regex / wait_for_idle.
    Omit all wait_* params for an immediate read.

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
                              attrs=attrs,
                              wait_for_idle=wait_for_idle)


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
    reach the app, not Browserland's renderer. To redraw a live app you must
    trigger the app's own repaint (there's no app-agnostic repaint key — `C-l`
    is only a readline/shell convention and a live keybinding in some full-screen
    TUIs); or `reset_terminal(id)` to wipe Browserland's screen buffer regardless
    of the app."""
    client, int_id = _route(id)
    return client.send_input(int_id, _newlines_to_enter(data))


@mcp.tool()
def send_keys(id: str, keys: List[str],
              delay_ms: Optional[int] = None) -> Dict[str, Any]:
    r"""Send terminal KEY SEQUENCES (control/escape keys) to a terminal. The
    window must be in 'readwrite' mode.

    Use this for keys that aren't plain text — Ctrl-C, Esc, arrows, function
    keys — which `send_input` can't express. `keys` is a list of tokens, each
    one of:
      - a named key: Enter, LF, Tab, Esc, Space, Backspace, Delete, Up, Down,
        Left, Right, Home, End, PageUp, PageDown, Insert, F1-F12;
      - a Ctrl chord `C-<char>` (e.g. `C-c` -> 0x03 / Ctrl-C, `C-d`, `C-[`,
        `C-Space` -> NUL, `C-h` -> 0x08), or an Alt chord `M-<char>` (ESC + char);
      - a Shift chord `S-<key>` for the keys with a distinct shifted encoding:
        `S-Tab` (back-tab, `ESC [ Z`) and the cursor keys (`S-Up`/`Down`/`Left`/
        `Right`, `S-Home`, `S-End` -> `ESC [ 1;2 <final>`, always the CSI form).
        A key with no standard shifted form (Enter, Esc, a letter, ...) raises;
      - a single literal character (sent as its UTF-8 bytes).
    e.g. `["C-c"]` to interrupt, `["Esc"]`, `["Up","Up","Enter"]`, `["S-Tab"]`.

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
    is still responding, the app itself has to repaint — that fresh output is
    what the renderer reads. There is no app-agnostic repaint key: `C-l` (Ctrl-L)
    is only a readline/shell redraw convention and is a live keybinding in many
    full-screen TUIs (Dwarf Fortress fires a game action on Ctrl-L, not a
    redraw), so prefer the app's own refresh key. A raw reset sequence sent as
    input is just keystrokes to the app and does NOT reset Browserland's own
    screen render; to wipe a corrupted buffer regardless of the app, use
    `reset_terminal` (it clears Browserland's side but won't make the app
    redraw). None of these un-freeze a hung app — kill it.

    Pacing (#129): by default the whole token list is written in ONE burst. A
    frame-polling raw-input TUI — one that reads input once per render frame,
    like Dwarf Fortress — drops keys that arrive faster than it polls, so a
    burst of arrows or spaces can advance only partially. Pass `delay_ms` (per
    token, capped 1000) to write each token in its own POST with that pause
    between them, so every keypress lands on a separate frame; omitting it keeps
    the single-burst write (back-compat). Pacing only kicks in with more than one
    token, and a bad token still raises before any byte is sent.

    Per-terminal default (#133): instead of threading `delay_ms` through every
    call, set a per-terminal default once with `set_pace(id, pace_ms)` — then a
    multi-token send that OMITS `delay_ms` (leaves it null) auto-paces at that
    default. Passing `delay_ms` explicitly overrides the default for that one
    call: a positive value is that pace, and `0` forces a single burst even on a
    paced terminal. A single-token send never paces, and with no per-terminal
    default set, omitting `delay_ms` behaves exactly like #129's burst.

    Pass a namespaced window id ("<host>:<int>")."""
    # Validate + translate up front (atomic, CSI form): a bad token raises here,
    # before any routing, metadata lookup or send.
    text = _keys_to_text(keys)
    client, int_id = _route(id)
    # ONE list_terminals fetch (#133) backs BOTH the DECCKM re-encode and the
    # per-terminal default pace, so neither costs a separate round-trip. Fetch
    # only when it can matter: a cursor token needs DECCKM, and a no-delay_ms
    # multi-key send needs the terminal's pace_ms (a single-key send has nothing
    # to pace, and an explicit delay_ms already decides the pace itself).
    multi = len(keys) > 1
    want_cursor = _keys_have_cursor(keys)
    # Fetch the terminal metadata ONLY when it can matter: a cursor token needs
    # DECCKM, and an UNSPECIFIED delay_ms (None) on a multi-key send needs the
    # terminal's pace_ms default. An explicit delay_ms (incl. 0) decides the pace
    # itself, so it needs no lookup — a caller can pass 0 to force a single burst
    # (and skip the fetch) even on a paced terminal.
    want_pace = delay_ms is None and multi
    meta = _terminal_meta(client, int_id) if (want_cursor or want_pace) else {}
    app_cursor = want_cursor and bool(meta.get("app_cursor"))
    if app_cursor:
        text = _keys_to_text(keys, app_cursor=True)   # re-encode arrows as SS3
    # #133: an unspecified delay_ms (None) uses the terminal's configured pace_ms
    # default (itself 0 = single-burst until set_pace sets it). An explicit
    # delay_ms overrides for this call: >0 is that pace, 0 (or a stray negative)
    # forces a single burst even on a paced terminal. All clamped to [0, cap].
    if delay_ms is None:
        try:
            resolved = int(meta.get("pace_ms", 0) or 0)
        except (TypeError, ValueError):
            resolved = 0
    else:
        try:
            resolved = int(delay_ms)
        except (TypeError, ValueError):
            resolved = 0
    pace = min(max(resolved, 0), _MAX_KEY_DELAY_MS)
    if pace and multi:
        # #129/#133: write one token per POST with a pause between, so a frame-
        # polling TUI sees each keypress on its own frame instead of dropping a
        # burst. Tokens are already validated above, so this can't half-type.
        result: Dict[str, Any] = {}
        for i, tok in enumerate(keys):
            if i:
                time.sleep(pace / 1000.0)
            result = client.send_input(int_id, _token_to_text(str(tok), app_cursor))
        return result
    return client.send_input(int_id, text)


@mcp.tool()
def set_pace(id: str, pace_ms: int) -> Dict[str, Any]:
    """Set a per-terminal DEFAULT inter-key pacing (ms) so subsequent `send_keys`
    calls that DON'T pass `delay_ms` auto-pace — for a frame-polling raw-input TUI
    like Dwarf Fortress that reads input once per render frame and drops a burst
    arriving faster than it polls. Set it once instead of threading `delay_ms`
    through every call: `send_keys` then writes each token in its own POST with
    this pause between them, so every keypress lands on a separate frame.

    `0` disables it (single-burst, the default); the value is capped at 1000 ms.
    An explicit `delay_ms` on a `send_keys` call still overrides this default for
    that one call. The default is PER-TERMINAL and EPHEMERAL — it lives on the
    live connection and resets if the agent reconnects, so re-set it after a
    relaunch. Requires the window be in 'readwrite' mode. Pass a namespaced window
    id ("<host>:<int>")."""
    client, int_id = _route(id)
    return client.set_pace(int_id, pace_ms)


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
    instead. After a reset the screen repopulates as the app emits output, so for
    a live app first trigger the app's own repaint (there's no app-agnostic
    repaint key — `C-l` is only a readline/shell convention and a live keybinding
    in some full-screen TUIs); reach for reset_terminal when the app won't redraw
    or the corruption is on Browserland's side."""
    client, int_id = _route(id)
    return client.reset_terminal(int_id)


@mcp.tool()
def flush_input(id: str) -> Dict[str, Any]:
    """Discard keystrokes queued to a terminal's app but NOT yet consumed, so the
    next read_screen reflects the settled state. Pass a namespaced window id
    ("<host>:<int>"); the window must be in 'readwrite' mode.

    Use this when you've sent input faster than the app drains it and a backlog
    is still being chewed through — e.g. a burst of send_keys on a frame-polling
    TUI like Dwarf Fortress, where queued keypresses keep advancing menus after
    you meant to stop. Flushing drops that unread backlog so the app stops
    reacting to stale keys and you can read a stable screen before the next move.

    It clears the INPUT queue only. It does NOT clear Browserland's screen buffer
    (that is reset_terminal) and does NOT touch the app's already-drawn screen or
    send it anything — already-consumed keystrokes have had their effect and
    can't be recalled. On a Windows/ConPTY agent it is a best-effort no-op (that
    backend exposes no input-queue flush)."""
    client, int_id = _route(id)
    return client.flush_input(int_id)


@mcp.tool()
def launch_terminal(profile: Optional[str] = None, cols: int = 80,
                    rows: int = 24, title: Optional[str] = None,
                    cwd: Optional[str] = None,
                    host: Optional[str] = None,
                    mode: Optional[str] = None) -> Dict[str, Any]:
    """Spawn a new terminal from a profile. The broker must have 'allow_launch'
    enabled. With multiple hosts configured, `host` is required to choose which
    broker; with a single host it's optional. The returned `id` is namespaced
    ("<host>:<int>") so it can be passed straight to the other tools.

    Every launch, scoped or not, comes up in `mode` ('off', 'read' or
    'readwrite'), or 'readwrite' when `mode` is omitted — a worker its
    launcher can't drive is useless (an invalid value is refused: 400
    bad_mode). When this server declares a scope for that host, the new
    window is also tagged with the scope before it starts, so you can see it
    at once. The result carries `scope` and `mode`, plus `handle` — a launch
    handle this server keeps in memory (keyed by the returned id) so
    `set_window` can retag this window later; the handle is lost when this
    process restarts (the admin credential or the browser can recover the
    window)."""
    client, name = _launch_target(host)
    result = client.launch_terminal(
        profile=profile, cols=cols, rows=rows, title=title, cwd=cwd,
        mode=mode or None)
    if isinstance(result, dict) and "id" in result:
        result = dict(result)
        result["id"] = f"{name}:{result['id']}"
        # #243: keep the launch handle the broker minted, keyed by the
        # namespaced id set_window takes. Lost on this process's restart —
        # the admin credential or the browser can recover the window.
        handle = result.get("handle")
        if isinstance(handle, str) and handle:
            _handle_remember(result["id"], handle)
    return result


@mcp.tool()
def set_window(id: str, scope: str = "", mode: str = "",
               clear_scope: bool = False,
               clear_mode: bool = False) -> Dict[str, Any]:
    """Set — or clear — the scope and/or MCP access mode of a window this
    server launched (#243). Pass the namespaced window id ("<host>:<int>")
    the launch answered.

    Semantics are keep|set|clear per field: a non-empty `scope`/`mode` SETS
    it, a `clear_scope`/`clear_mode` flag sends an explicit null (back to
    untagged / the broker default), and a field left empty with its flag
    false is left untouched (an explicit `clear_*` wins over a value).

    Two paths, picked locally: for a window this server holds a launch
    handle for, the handle rides the request (the owner may retag its own
    window, including to `off` or a foreign scope, and back); otherwise, on
    a host whose admin credential this server confirmed at startup, the
    admin path edits any window there. Anything else refuses locally and
    sends nothing. Both paths fail closed when the host's /mcp/info does not
    advertise `window_edit` (handle path) or `admin` (admin path): an older
    broker never sees the request."""
    client, int_id = _route(id)
    host = id.partition(":")[0]
    handle = _handle_for(id)
    if handle is not None:
        if not client.capabilities()["window_edit"]:
            raise BrowserlandError(
                0, "window_edit_unsupported",
                f"host {host!r} does not advertise window_edit on /mcp/info "
                "(an older broker); set_window is refused without sending.")
        body: Dict[str, Any] = {"id": int_id, "handle": handle}
    elif host in _admin_hosts:
        if not client.capabilities()["admin"]:
            raise BrowserlandError(
                0, "admin_unsupported",
                f"host {host!r} no longer confirms this server's admin "
                "credential on /mcp/info (rotated in the Control Panel?); "
                "set_window is refused without sending.")
        body = {"id": int_id}
    else:
        raise BrowserlandError(
            0, "no_handle",
            f"no launch handle for {id!r} (this server restarted, or the "
            "window was launched elsewhere) and its host has no confirmed "
            "admin credential: set_window refuses without sending.")
    # keep|set|clear: a non-empty value sets, a clear flag nulls, a key left
    # empty+unflagged stays out of the body entirely (the broker keeps the
    # field). Nothing in the body but id+handle is a local refusal, not a
    # round-trip the broker would reject.
    if clear_scope:
        body["scope"] = None
    elif scope:
        body["scope"] = scope
    if clear_mode:
        body["mode"] = None
    elif mode:
        body["mode"] = mode
    if "scope" not in body and "mode" not in body:
        raise BrowserlandError(
            0, "bad_args",
            "set_window: nothing to change — pass scope/mode to set, or "
            "clear_scope/clear_mode to clear (an omitted field is kept).")
    return client.set_window(body)


def set_mcp_knobs(host: str, default_mode: str = "",
                  allow_launch: Optional[bool] = None,
                  allow_desktop: Optional[bool] = None) -> Dict[str, Any]:
    """Change a broker's broker-wide MCP knobs (#245 — ADMIN ONLY): the
    default window mode (`default_mode`: off/read/readwrite), whether agents
    may launch terminals (`allow_launch`), and whether the desktop relay is
    on (`allow_desktop`). Pass a knob's value only to change it.

    Requires a host whose admin credential this server confirmed at startup
    (its --hosts entry carries "admin_token" and the startup probe saw
    admin: true); every other host refuses locally and nothing is sent. The
    MCP token, the admin token and the enabled switch stay Control-Panel
    only — an admin agent cannot rotate credentials or disable the surface
    it is using. A successful change re-reads the host's /mcp/info flags, so
    a knob that turns a capability on (allow_desktop) is visible to the next
    fail-closed tool call without restarting this server."""
    if host not in _admin_hosts:
        raise BrowserlandError(
            0, "admin_required",
            f"host {host!r} has no confirmed admin credential (its --hosts "
            'entry needs "admin_token", and the startup probe must see '
            "admin: true on /mcp/info); set_mcp_knobs refuses without "
            "sending.")
    for key, value in (("allow_launch", allow_launch),
                       ("allow_desktop", allow_desktop)):
        # Strict booleans, mirroring the broker's /mcp/knobs validation: a
        # string "false" is truthy junk and must never reach the wire.
        if value is not None and not isinstance(value, bool):
            raise BrowserlandError(
                0, "bad_args",
                f"set_mcp_knobs: {key} must be a real boolean (true/false), "
                f"got {value!r}.")
    body: Dict[str, Any] = {}
    if default_mode:
        body["default_mode"] = default_mode
    if allow_launch is not None:
        body["allow_launch"] = allow_launch
    if allow_desktop is not None:
        body["allow_desktop"] = allow_desktop
    if not body:
        raise BrowserlandError(
            0, "bad_args",
            "set_mcp_knobs: nothing to change — pass default_mode, "
            "allow_launch or allow_desktop.")
    client = _named_client(host)
    try:
        result = client.set_knobs(body)
    except BrowserlandError as exc:
        # [c10]: a broker-side admin_required on a host the probe cleared
        # means the credential rotated AFTER startup. Fail LOUD with the
        # remedy, never silently retry without the header.
        if exc.code == "admin_required":
            raise BrowserlandError(
                exc.status, exc.code,
                f"host {host!r}: {exc} — the admin token may have been "
                "rotated in the broker's Control Panel; refresh this "
                "server's config and restart it.") from exc
        raise
    # [c23]: knobs can flip capabilities (allow_desktop gates the desktop
    # tools); a successful change refreshes the host's cached flags.
    client.capabilities(refresh=True)
    return result


def _require_desktop(client: BrowserlandClient, host: str) -> None:
    """The #244 fail-closed gate, shared by every desktop tool: the relay is
    opt-in (``allow_desktop``), and a broker that does not advertise it on
    /mcp/info never sees a desktop request."""
    if not client.capabilities()["allow_desktop"]:
        raise BrowserlandError(
            0, "desktop_unsupported",
            f"host {host!r} does not advertise allow_desktop on /mcp/info "
            "(enable the desktop relay in Control Panel → MCP access); the "
            "desktop tools are refused without sending.")


@mcp.tool()
def get_desktop(host: str = "") -> Dict[str, Any]:
    """Read a browser's desktop (#244): every window with its layout
    (tiled column/row/width preset, or floating x/y/w/h), the viewport, the
    focused column, and the workspaces when the workspaces mod is on. The
    snapshot comes from the browser holding the control lease; `home` says
    whether that browser's home is this broker, and floating geometry is
    AS THAT BROWSER SEES IT (per-browser state, not stored on the broker).

    With `host` set, returns that one host's snapshot. Omit `host` (or pass
    "") for a dict keyed by host name — each value is that host's snapshot,
    or `{"ok": false, "error": ...}` when its relay is off or it has no
    active browser. Fails closed per host: a broker that does not advertise
    `allow_desktop` is never asked."""
    if host:
        client = _named_client(host)
        _require_desktop(client, host)
        return client.get_desktop()
    out: Dict[str, Any] = {}
    for name in _host_configs:
        try:
            client = _get_client(name)
            _require_desktop(client, name)
            out[name] = client.get_desktop()
        except Exception as exc:
            out[name] = {"ok": False, "error": str(exc)}
    return out


#: The mutating window ops arrange_window relays (the read side is
#: get_desktop's snapshot; `get` is not arrangeable).
_ARRANGE_OPS = ("focus", "move_column", "set_width", "tile", "float",
                "set_geometry", "tab_into", "split")


@mcp.tool()
def arrange_window(id: str, op: str, to: str = "", preset: str = "",
                   x: int = -1, y: int = -1, w: int = -1, h: int = -1,
                   target: str = "", axis: str = "") -> Dict[str, Any]:
    """Arrange a window on the lease-holding browser's desktop (#244). Pass
    the namespaced window id ("<host>:<int>") and one op:
      - `focus` — focus the window's column;
      - `move_column` `to`="left"/"right" or a column index;
      - `set_width` `preset`="1/3"/"1/2"/"2/3"/"max";
      - `tile` / `float`;
      - `set_geometry` `x`,`y`,`w`,`h` (floating only; the browser clamps to
        the viewport);
      - `tab_into` `target`=<namespaced id> — tab the window into the
        target's tile;
      - `split` `target`=<namespaced id> `axis`="row"/"column" — split after
        the target.
    `target` must be a window id on the SAME host as `id`. Ops apply on the
    browser holding the control lease (window placement is per-browser
    render, so any holder works) and are applied instantly. Fails closed
    when the host does not advertise `allow_desktop` on /mcp/info."""
    if op not in _ARRANGE_OPS:
        raise BrowserlandError(
            0, "bad_op",
            f"arrange_window: unknown op {op!r}; use one of: "
            + ", ".join(_ARRANGE_OPS) + ".")
    client, int_id = _route(id)
    host = id.partition(":")[0]
    args: Dict[str, Any] = {"id": int_id}

    def _target_int(what: str) -> int:
        if not target:
            raise BrowserlandError(
                0, "bad_args",
                f"arrange_window: {what} needs target (a namespaced window "
                "id on the same host).")
        tclient, t_id = _route(target)
        if tclient is not client:
            raise BrowserlandError(
                0, "bad_args",
                f"arrange_window: target {target!r} is on another host than "
                f"{id!r}.")
        return t_id

    if op == "move_column":
        if to in ("left", "right"):
            args["to"] = to
        else:
            try:
                args["to"] = int(to)
            except ValueError:
                raise BrowserlandError(
                    0, "bad_args",
                    "arrange_window: move_column's to must be 'left', "
                    "'right' or a column index.") from None
    elif op == "set_width":
        if not preset:
            raise BrowserlandError(
                0, "bad_args",
                "arrange_window: set_width needs preset ('1/3', '1/2', "
                "'2/3' or 'max').")
        args["preset"] = preset
    elif op == "set_geometry":
        if min(x, y, w, h) < 0:
            raise BrowserlandError(
                0, "bad_args",
                "arrange_window: set_geometry needs x, y, w and h (all >= "
                "0).")
        args.update(x=x, y=y, w=w, h=h)
    elif op == "tab_into":
        args["target"] = _target_int("tab_into")
    elif op == "split":
        if axis not in ("row", "column"):
            raise BrowserlandError(
                0, "bad_args",
                "arrange_window: split needs axis 'row' or 'column'.")
        args["target"] = _target_int("split")
        args["axis"] = axis
    _require_desktop(client, host)
    return client.desktop_op(op, args)


#: The workspace ops the `workspaces` tool relays (prefixed
#: "workspaces:<op>" on the wire, the broker's allowlist).
_WORKSPACE_OPS = ("list", "add", "rename", "remove", "switch", "send-window")


def workspaces(host: str, op: str, name: str = "", index: int = -1,
               target: int = -1, win: str = "",
               force: bool = False) -> Dict[str, Any]:
    """Manage the workspaces of a browser's desktop (#244 — ADMIN ONLY;
    workspace data spans every window, whatever its scope). Ops:
      - `list` — the workspaces and the active index;
      - `add` `name?`;
      - `rename` `index` `name`;
      - `remove` `index` — a NON-EMPTY workspace answers needs_confirm
        unless `force=true` (its windows move to the adjacent workspace; no
        dialog is ever shown); the last workspace is always refused;
        `force` is remove-only — every other op rejects it locally;
      - `switch` `index`;
      - `send-window` `win`=<namespaced window id on this host>
        `target`=<workspace index>.
    Requires a host whose admin credential this server confirmed at
    startup, and whose relay advertises `allow_desktop`. The op runs on the
    browser holding the control lease, and workspace data belongs to a
    browser's HOME desktop: when that browser's home is another broker the
    op answers `not_home` — run workspace ops from the home broker's host.
    With the workspaces mod off the op answers
    `{"ok": false, "reason": "absent"}` (or "inactive") instead of a silent
    no-op."""
    if op not in _WORKSPACE_OPS:
        raise BrowserlandError(
            0, "bad_op",
            f"workspaces: unknown op {op!r}; use one of: "
            + ", ".join(_WORKSPACE_OPS) + ".")
    # force means "bypass remove's needs_confirm"; on any other op it is a
    # misunderstood call, and silently ignoring it hides the mistake. Refuse
    # locally — the broker never sees a frame.
    if force and op != "remove":
        raise BrowserlandError(
            0, "bad_args",
            "workspaces: force applies only to remove (it bypasses "
            "needs_confirm); refusing without sending.")
    if host not in _admin_hosts:
        raise BrowserlandError(
            0, "admin_required",
            f"host {host!r} has no confirmed admin credential (its --hosts "
            'entry needs "admin_token", and the startup probe must see '
            "admin: true on /mcp/info); workspaces refuses without "
            "sending.")
    client = _named_client(host)
    args: Dict[str, Any] = {}
    if op == "add":
        if name:
            args["name"] = name
    elif op == "rename":
        if not name or index < 0:
            raise BrowserlandError(
                0, "bad_args",
                "workspaces rename needs index and name.")
        args.update(index=index, name=name)
    elif op == "remove":
        if index < 0:
            raise BrowserlandError(
                0, "bad_args", "workspaces remove needs index.")
        args["index"] = index
        if force:
            args["force"] = True
    elif op == "switch":
        if index < 0:
            raise BrowserlandError(
                0, "bad_args", "workspaces switch needs index.")
        args["index"] = index
    elif op == "send-window":
        if not win or target < 0:
            raise BrowserlandError(
                0, "bad_args",
                "workspaces send-window needs win (a namespaced window id "
                "on this host) and target (a workspace index).")
        wclient, w_id = _route(win)
        if wclient is not client:
            raise BrowserlandError(
                0, "bad_args",
                f"workspaces: win {win!r} is on another host than {host!r}.")
        args.update(win=w_id, target=target)
    _require_desktop(client, host)
    return client.desktop_op(f"workspaces:{op}", args)
