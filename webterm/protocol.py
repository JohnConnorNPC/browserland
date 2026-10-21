"""Wire-protocol frame builders and parsers.

This is the ONLY module that knows the JSON shapes. Both the agent and the
broker import from here. These shapes are Browserland's own web-terminal
producer protocol; the relay/registry framing was adapted from xterm-py
(``browser/broker.py``), a separate codebase at
https://github.com/JohnConnorNPC/xterm-py.

Producer -> broker (text JSON):
    {"type": "hello",   "window_id": <int>, "pid": <int>, "title": "...",
     "cols": N, "rows": M}    # + optional "host", "kind", "agent", "cwd", "version", "pyte"
    {"type": "title",   "data": "..."}
    {"type": "agent",   "data": "claude"|"grok"|"codex"|"opencode"|"hermes"|""}  # foreground agent
    {"type": "cwd",     "data": "C:\\path\\to\\dir"}  # live working dir of the shell
    {"type": "resized", "cols": <int>, "rows": <int>}
    {"type": "exit",    "code": <int>}    # child process exited (PTY EOF)
Producer -> broker (binary): raw PTY bytes AND snapshot bytes (no framing;
single-WS ordering is the only guarantee).

Broker -> producer (text JSON):
    {"type": "input",  "data": "<utf-8 text>"}
    {"type": "resize", "cols": N, "rows": M}
    {"type": "snapshot_please"}

Broker -> browser (text JSON):
    {"type": "resized", "cols": N, "rows": M}
    {"type": "title",   "data": "..."}        # live title push on change
    {"type": "agent",   "data": "claude"|"grok"|"codex"|"opencode"|"hermes"|""}  # foreground agent
    {"type": "cwd",     "data": "C:\\path\\to\\dir"}  # live working dir push
    {"type": "exit",    "code": <int>}    # session's child exited — tear down now
    {"type": "error", "reason": "unknown_session", "session_id": <int>}
    {"type": "control", "state": "free"|"controlled", "label", "since", "gen"}
    {"type": "control", "state": "revoked", "by": "browser"|"claim", "gen"}
    {"type": "error", "reason": "bad_label"|"not_controller"|"bad_op",
     "session_id": <int>}                # a refused control op; socket stays
Broker -> browser (binary): producer bytes, verbatim.

Browser -> broker (text JSON): input / paste / resize (mouse ignored), and
``control`` claim/release (#238, see "Terminal control" below).
``paste`` is a LEGACY ALIAS for ``input`` (#138): the UI now routes clipboard
text through xterm's paste() and sends plain ``input`` frames; the relay keeps
accepting ``paste`` verbatim only so tabs served before the change keep
working until they reload.

MCP client -> broker (HTTP, #237): the scope grammar and the header a client
declares its scope on (``SCOPE_RE``, ``SCOPE_HEADER`` below). They live here,
not in the broker, because this module imports nothing beyond the standard
library and the MCP server (``webterm.mcptool``) must not import
``webterm.broker``.
"""

from __future__ import annotations

import ipaddress
import json
import re
import unicodedata
import urllib.parse
from typing import Any, Dict, Optional, Tuple


# ---------------------------------------------------------------------------
# MCP client -> broker (HTTP): scopes (#237)
# ---------------------------------------------------------------------------

#: A scope name. Always ``fullmatch``: no ``:`` (MCP window ids are
#: ``host:int``), no whitespace, 1-64 characters. The broker validates the
#: header against it; the MCP server (webterm.mcptool) validates its own
#: configured scopes with it.
SCOPE_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")

#: The request header an MCP client declares its scope on (#230). The broker
#: reads it in exactly one place, app.py's ``_mcp_scope``.
SCOPE_HEADER = "X-Browserland-Scope"

#: The request header carrying the MCP ADMIN credential (#245) — a second
#: secret ADDITIONAL to the MCP token (never instead of it, and never the
#: query string; app.py's ``_mcp_auth_error`` refuses both abuses). Lives here
#: beside SCOPE_HEADER for the same reason: ``webterm.mcptool`` imports it
#: without pulling in the broker. Deliberately a different header from #191's
#: ``X-Webterm-Admin`` so the two credential classes never share one slot.
MCP_ADMIN_HEADER = "X-Browserland-Mcp-Admin"


# ---------------------------------------------------------------------------
# Peer proxy (#252): the peer id, the pairing header and an origin's
# canonical form
# ---------------------------------------------------------------------------
# Shared by the broker's /peer/ gates (webterm/broker/peers.py) and the
# mcptool client, which sends the pairing for a host configured with a
# ``peer_origin``: one spelling of the header, and one canonical form, so a
# correct config never reads as a re-pointed peer.

#: A peer id: what the client names, and the path segment after ``/peer/``.
#: Always ``fullmatch``. The mcptool reads it too (#261): the ids in a hosts
#: entry's ``peers`` and the ones a hub lists on ``/mcp/info``.
PEER_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,64}")

#: The most ids of one hub's ``mcp_peers`` the mcptool checks against its
#: config (#261 round 1): each unconfigured one is an ``errors`` row on every
#: ``list_terminals``, so a hub listing thousands cannot make that answer
#: grow without bound. Past it, one row on the hub says how many were left.
MAX_MCP_PEERS = 64

#: The pairing header: the origin the client paired the peer's token with.
#: The browser gate requires it; the MCP gate checks it when it is sent. A
#: mismatch is 409 ``peer_origin_changed``, before any dial.
PEER_ORIGIN_HEADER = "X-Browserland-Peer-Origin"

#: Shape of a rebuilt origin (lower-case host chars or a bracketed IPv6
#: literal, an optional port) -- a last belt over the URL parser's output.
ORIGIN_RE = re.compile(
    r"https?://(?:[a-z0-9.\-]+|\[[0-9a-f:.]+\])(?::[0-9]{1,5})?")

ORIGIN_DEFAULT_PORTS = {"http": 80, "https": 443}


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
    if parts.scheme not in ORIGIN_DEFAULT_PORTS:
        return None, "origin must be http or https"
    if "@" in parts.netloc or parts.username is not None:
        return None, "origin must not carry userinfo"
    if parts.path not in ("", "/"):
        return None, "origin must not carry a path"
    if parts.query or "?" in raw:
        return None, "origin must not carry a query"
    if parts.fragment or "#" in raw:
        return None, "origin must not carry a fragment"
    # A trailing dot is the same DNS name ("localhost." is localhost).
    host = (parts.hostname or "").lower().rstrip(".")
    if not host:
        return None, "origin must name a host"
    if ":" in host:
        # One spelling per IPv6 address (round 1b N8): `[0:0::1]` and
        # `[::1]` are the same peer, so both sides compare the compressed
        # form, and a config and an mcptool entry spelled differently agree.
        try:
            host = ipaddress.IPv6Address(host).compressed
        except ValueError:
            return None, "origin has an invalid host"
    if port is not None and not 1 <= port <= 65535:
        return None, "origin has an invalid port"
    netloc = f"[{host}]" if ":" in host else host
    if port is not None and port != ORIGIN_DEFAULT_PORTS[parts.scheme]:
        netloc += f":{port}"
    origin = f"{parts.scheme}://{netloc}"
    if not ORIGIN_RE.fullmatch(origin):
        return None, "origin has an invalid host"
    return origin, None


# ---------------------------------------------------------------------------
# Producer -> broker
# ---------------------------------------------------------------------------

def hello_frame(
    window_id: int,
    pid: int,
    title: str,
    cols: int,
    rows: int,
    host: Optional[str] = None,
    kind: Optional[str] = None,
    agent: Optional[str] = None,
    cwd: Optional[str] = None,
    profile: Optional[str] = None,
    version: Optional[str] = None,
    pyte: Optional[bool] = None,
) -> str:
    """First frame on /browserland. Ints MUST be JSON numbers (the broker calls
    int() on them; the picker compares ids numerically in JS, so ids must
    stay < 2**53)."""
    frame: Dict[str, Any] = {
        "type": "hello",
        "window_id": int(window_id),
        "pid": int(pid),
        "title": str(title),
        "cols": int(cols),
        "rows": int(rows),
    }
    # Additive fields: older parsers ignore unknown keys, so adding one is
    # backward-compatible. "host" predates the rest. "agent" carries the current
    # foreground agent so a re-hello after reconnect stays accurate; "cwd"
    # carries the shell's working dir (the AGENTS.md button targets it).
    if host:
        frame["host"] = str(host)
    if kind:
        frame["kind"] = str(kind)
    if agent:
        frame["agent"] = str(agent)
    if cwd:
        frame["cwd"] = str(cwd)
    # "profile" carries the launch-profile name (#115) so the broker can surface
    # it in /sessions and the UI can seed a per-profile default terminal color.
    # Additive/optional, like cwd — omitted by non-launcher producers.
    if profile:
        frame["profile"] = str(profile)
    # "version" carries the producer's build id (webterm.build_version()) so the
    # broker can surface it and flag stale agents (issue #22).
    if version:
        frame["version"] = str(version)
    # "pyte" (#134) reports whether the producer has pyte installed, so the broker
    # (and MCP list_terminals) can flag that a pyte-less agent's read_screen falls
    # back to the dependency-free textgrid renderer — no attr_runs (#128), no
    # keyframe repair (#130); a sparse alt-screen frame after ring eviction is
    # flagged `partial` only. Additive/optional like version, BUT tested with
    # ``is not None`` rather than truthiness: False (pyte absent) is the very value
    # worth reporting, so it MUST ride the frame — a truthy guard would drop it.
    if pyte is not None:
        frame["pyte"] = bool(pyte)
    return json.dumps(frame)


def title_frame(title: str) -> str:
    return json.dumps({"type": "title", "data": str(title)})


def agent_frame(name: str) -> str:
    """Foreground-agent push: which of claude/grok/codex/opencode/hermes is
    running, or
    "" for none. Sent by the agent on change and re-broadcast by the broker."""
    return json.dumps({"type": "agent", "data": str(name)})


def cwd_frame(path: str) -> str:
    """Live working-directory push: the shell's current dir, sent by the agent
    on change and re-broadcast by the broker so the AGENTS.md button tracks a
    ``cd`` without waiting for the next /sessions poll."""
    return json.dumps({"type": "cwd", "data": str(path)})


def mode_frame(app_cursor: bool) -> str:
    """Live DEC-mode push: DECCKM / application-cursor-key state, sent by the
    agent on change so the broker can cache it (cheaply readable by send_keys to
    pick CSI vs SS3 arrows — #23) without an on-demand screen render."""
    return json.dumps({"type": "mode", "app_cursor": bool(app_cursor)})


def resized_frame(cols: int, rows: int) -> str:
    return json.dumps({"type": "resized", "cols": int(cols), "rows": int(rows)})


def exit_frame(code: int) -> str:
    """Child-process exit (PTY EOF). The agent sends this ONCE on a real child
    exit — never on a transient broker-WS drop — and the broker re-broadcasts
    it to attached browsers so they tear the window down immediately instead of
    waiting out the /sessions poll grace cycle (~12 s). A transient producer
    disconnect carries no exit frame, so reconnect grace is unaffected."""
    return json.dumps({"type": "exit", "code": int(code)})


# ---------------------------------------------------------------------------
# Broker -> producer
# ---------------------------------------------------------------------------

def input_frame(data: str) -> str:
    return json.dumps({"type": "input", "data": data})


def resize_frame(cols: int, rows: int) -> str:
    return json.dumps({"type": "resize", "cols": int(cols), "rows": int(rows)})


def snapshot_please_frame() -> str:
    return json.dumps({"type": "snapshot_please"})


# ---------------------------------------------------------------------------
# Management RPCs (broker <-> producer round-trips, correlated by ``req``)
#
# Unlike snapshot_please (fire-and-forget), these carry a per-connection
# request id so the broker can match a reply to the request that asked for it.
# The broker allocates ``req`` on the specific producer WindowEntry; the agent
# echoes it back verbatim in the reply. See registry.WindowEntry's pending-RPC
# map for the correlation/cleanup rules.
# ---------------------------------------------------------------------------

# Broker -> producer: "list your process tree" / "kill this pid" / "git status"
# / "render your screen as plain text" (MCP read).
def procs_please_frame(req: int) -> str:
    return json.dumps({"type": "procs_please", "req": int(req)})


def screen_text_please_frame(req: int, view: str = "screen",
                             lines: int = 0,
                             wait_for_change: Optional[str] = None,
                             timeout_ms: int = 0,
                             wait_for_text: Optional[str] = None,
                             wait_for_regex: Optional[str] = None,
                             wait_absent: bool = False,
                             since: Optional[str] = None,
                             attrs: bool = False,
                             wait_for_idle: int = 0) -> str:
    """Broker -> producer: render the live screen and reply with plain text.
    Backs the MCP /mcp/read endpoint; only agents answer it (non-agent
    producers have no handler, so the request times out -> 502). ``view``
    (``"screen"`` default / ``"scrollback"``) + ``lines`` request scrollback
    history above the current grid (#21); older agents ignore the extra keys.

    ``wait_for_change`` (a prior ``content_hash``) + ``timeout_ms`` make the
    AGENT hold the reply until the freshly-rendered screen hash differs from
    that baseline, or the timeout elapses — a single round-trip instead of a
    busy-poll (#26). Older agents ignore both and reply immediately.

    ``wait_for_text`` (substring) / ``wait_for_regex`` (regex), with the same
    ``timeout_ms``, are a CONTENT predicate (#51): the agent holds the reply
    until the rendered screen contains (or, with ``wait_absent``, no longer
    contains) the match — waking once on the event the caller cares about
    instead of on every noisy frame. The reply carries ``matched`` (true if the
    predicate was satisfied, false on timeout). Older agents ignore these keys
    and reply immediately.

    ``since`` (a prior ``content_hash``, #52) asks for a DELTA: if the agent
    still holds that frame and this is a clean same-size current-screen render,
    the reply carries ``changed_rows`` (only the rows that differ) + ``delta``:
    true, instead of the full grid. Misses fall back to a full read.

    ``attrs`` (#128, #136) asks the agent to also return ``attr_runs`` — the
    styled fg/bg/reverse/bold/underscore cell runs — so a menu selection the plain
    text drops (marked by color, reverse-video, bold, or underline alone) is
    visible. Orthogonal to the wait/delta modes; older agents ignore the key.
    Always the full current run list (never deltaed).

    ``wait_for_idle`` (#135, ms; ``0`` = unset) is a settle wait: the agent holds
    the reply until the CURSOR-BLIND screen hash (``stable_hash`` — the text with
    the hardware-cursor cell masked, so a bare cursor blink doesn't count) has
    been unchanged for that many ms — i.e. output has gone quiet — or
    ``timeout_ms`` elapses. Like the content predicate the reply carries
    ``matched`` (true once it settled, false on timeout). It is a DISTINCT wait
    signal, so the broker rejects combining it with wait_for_change/text/regex;
    older agents ignore the key. It settles only CALMER TUIs — a fully-animating
    app (Dwarf Fortress paints creatures/water/the ``*PAUSED*`` marquee off the
    cursor every frame) never reaches output-idle."""
    return json.dumps({"type": "screen_text_please", "req": int(req),
                       "view": str(view), "lines": int(lines),
                       "wait_for_change": wait_for_change,
                       "timeout_ms": int(timeout_ms),
                       "wait_for_text": wait_for_text,
                       "wait_for_regex": wait_for_regex,
                       "wait_absent": bool(wait_absent),
                       "since": since, "attrs": bool(attrs),
                       "wait_for_idle": int(wait_for_idle)})


def kill_frame(req: int, pid: int) -> str:
    return json.dumps({"type": "kill", "req": int(req), "pid": int(pid)})


def git_status_please_frame(req: int) -> str:
    return json.dumps({"type": "git_status_please", "req": int(req)})


def reset_please_frame(req: int) -> str:
    """Broker -> producer: clear the PTY-output ring so the next read_screen /
    reconnect snapshot renders from a clean slate (#27). Only agents answer it
    (a non-agent producer has no handler -> the RPC times out -> 502)."""
    return json.dumps({"type": "reset_please", "req": int(req)})


def flush_input_please_frame(req: int) -> str:
    """Broker -> producer: discard keystrokes queued toward the app but not yet
    consumed — the INPUT-side mirror of reset_please (#133). Where reset clears
    the OUTPUT ring, this drops the pending input backlog. Only agents answer it
    (a non-agent producer has no handler -> the RPC times out -> 502)."""
    return json.dumps({"type": "flush_input_please", "req": int(req)})


# Producer -> broker: the matching replies.
def procs_frame(req: int, procs) -> str:
    return json.dumps({"type": "procs", "req": int(req),
                       "procs": list(procs or [])})


def killed_frame(req: int, ok: bool, error: Optional[str] = None,
                 pid: Optional[int] = None) -> str:
    frame: Dict[str, Any] = {"type": "killed", "req": int(req), "ok": bool(ok)}
    if error:
        frame["error"] = str(error)
    if pid is not None:
        frame["pid"] = int(pid)
    return json.dumps(frame)


def reset_done_frame(req: int, ok: bool, error: Optional[str] = None) -> str:
    """Producer -> broker: ack for reset_please (#27)."""
    frame: Dict[str, Any] = {"type": "reset_done", "req": int(req),
                             "ok": bool(ok)}
    if error:
        frame["error"] = str(error)
    return json.dumps(frame)


def flush_input_done_frame(req: int, ok: bool, error: Optional[str] = None) -> str:
    """Producer -> broker: ack for flush_input_please (#133)."""
    frame: Dict[str, Any] = {"type": "flush_input_done", "req": int(req),
                             "ok": bool(ok)}
    if error:
        frame["error"] = str(error)
    return json.dumps(frame)


def git_status_frame(req: int, status: Dict[str, Any]) -> str:
    frame: Dict[str, Any] = {"type": "git_status", "req": int(req)}
    frame.update(status or {})
    return json.dumps(frame)


def screen_text_frame(req: int, text: str, cols: int, rows: int,
                      degraded: bool = False, alt_screen: bool = False,
                      cursor: Optional[Dict[str, int]] = None,
                      view: str = "screen", history_lines: int = 0,
                      app_cursor: bool = False, content_hash: str = "",
                      matched: Optional[bool] = None,
                      delta: bool = False,
                      changed_rows: Optional[list] = None,
                      attr_runs: Optional[list] = None,
                      partial: bool = False,
                      idle_ms: int = 0,
                      stable_hash: str = "") -> str:
    """Producer -> broker: the rendered plain-text screen for a screen_text
    request. ``text`` is a bounded ``rows``x``cols`` grid (plus ``history_lines``
    of scrollback above it when ``view="scrollback"``), rendered via pyte or the
    dependency-free in-house emulator. ``alt_screen`` (#21) is whether the
    terminal is showing a full-screen alternate buffer — when true, scrollback
    is meaningless and ``view`` comes back ``"screen"``. ``app_cursor`` (#23) is
    DECCKM (application cursor keys) — when true, send_keys must emit SS3 arrows.
    ``cursor`` is ``{row, col}`` 0-based within the grid (``None`` when degraded).
    ``content_hash`` (#26) is a stable digest of ``text`` so a caller can detect
    change across reads (the empty string means the agent didn't compute one) —
    it stays text-only, so a color-only selection move (identical text) doesn't
    change it. ``stable_hash`` (#135) is the same digest with the HARDWARE-cursor
    cell masked to a sentinel, so a cursor BLINK in place doesn't change it while
    a cursor MOVE does — the settle signal ``wait_for_idle`` rides. Always present
    (empty string when the agent computed none, e.g. a degraded read); it equals
    ``content_hash`` when there is no cursor. ``attr_runs`` (#128, #136), present only
    when the read asked for
    ``attrs``, is the styled cell runs (``{row, col, len, fg, bg, reverse, bold,
    underscore}``) so a menu selection the plain text drops — marked by color,
    reverse-video, bold, or underline alone — is visible; it is always the full
    current list, alongside any text delta. ``degraded``
    is the rare last-ditch raw decode (``view="raw"``), so the caller knows the
    text is not a clean grid render. ``partial`` (#130) is distinct from
    ``degraded``: the grid + cursor ARE valid, but the read couldn't fully
    reconstruct a long-running alt-screen TUI after ring eviction lost its
    static full-frame paint, so some statically-painted panels may be missing —
    the caller should treat the frame as possibly incomplete (force a repaint)
    rather than trust it. It self-heals on the next in-window read or any app
    repaint. ``idle_ms`` (#133) is a best-effort count of milliseconds since the
    terminal last emitted PTY output, computed at reply time; it is additive (an
    older agent omits it) and unreliable for a perpetually-animating app that
    paints every frame."""
    frame: Dict[str, Any] = {
        "type": "screen_text",
        "req": int(req),
        "text": str(text),
        "cols": int(cols),
        "rows": int(rows),
        "degraded": bool(degraded),
        "partial": bool(partial),
        "alt_screen": bool(alt_screen),
        "app_cursor": bool(app_cursor),
        "view": str(view),
        "history_lines": int(history_lines),
        "content_hash": str(content_hash),
        "stable_hash": str(stable_hash),
        "idle_ms": int(idle_ms),
    }
    # cursor is null on the degraded raw path (no grid to locate it in) — the
    # helper enforces the invariant regardless of what the caller passed.
    frame["cursor"] = (None if bool(degraded) or not isinstance(cursor, dict)
                       else {"row": int(cursor.get("row", 0)),
                             "col": int(cursor.get("col", 0))})
    # matched (#51): present ONLY for a content-predicate (wait_for_text/regex)
    # read — true if the predicate was satisfied, false if it timed out first.
    # Omitted for immediate / wait_for_change reads so they stay unambiguous.
    if matched is not None:
        frame["matched"] = bool(matched)
    # delta (#52): when true, ``text`` is empty and ``changed_rows`` carries only
    # the rows that differ from the caller's ``since`` baseline ({row, text});
    # the caller patches its own grid model. ``delta`` is always present so the
    # caller knows which shape it got (a full read reports false).
    frame["delta"] = bool(delta)
    if changed_rows is not None:
        frame["changed_rows"] = [
            {"row": int(c.get("row", 0)), "text": str(c.get("text", ""))}
            for c in changed_rows]
    # attr_runs (#128/#136): present ONLY for an attrs read — the styled
    # fg/bg/reverse/bold/underscore cell runs, so a selection the plain text drops
    # (marked by color, reverse-video, bold, or underline alone) is visible. The
    # helper re-projects each run to its canonical shape (never trusts the caller
    # to have supplied every key / the right types); bold/underscore default to
    # False so an older agent's fg/bg/reverse-only run stays well-formed.
    if attr_runs is not None:
        frame["attr_runs"] = [
            {"row": int(a.get("row", 0)), "col": int(a.get("col", 0)),
             "len": int(a.get("len", 0)), "fg": str(a.get("fg", "default")),
             "bg": str(a.get("bg", "default")),
             "reverse": bool(a.get("reverse", False)),
             "bold": bool(a.get("bold", False)),
             "underscore": bool(a.get("underscore", False))}
            for a in attr_runs]
    return json.dumps(frame)


# ---------------------------------------------------------------------------
# Broker -> browser
# ---------------------------------------------------------------------------

def error_frame(reason: str, session_id: int) -> str:
    return json.dumps({
        "type": "error",
        "reason": reason,
        "session_id": session_id,
    })


def not_active_frame(session_id: int) -> str:
    """#248: broker -> browser on a terminal /ws, the advisory answer to an
    input/paste/binary/resize frame dropped because this browser does not
    hold the single-active-browser lease. The key is ``session`` (the #248
    wire contract), not error_frame's ``session_id``."""
    return json.dumps({
        "type": "error",
        "reason": "not_active",
        "session": session_id,
    })


def mcp_activity_frame(kind: str) -> str:
    """Transient per-window MCP-touch pulse (#33): the broker emits this on an
    MCP read/write so the browser can briefly flash that window's robot icon
    (cool/soft for a read, warm/sharp for a write). ``kind`` is ``"read"`` or
    ``"write"``. The window id is intentionally omitted — every browser WS is
    bound to one window via ``?session=<id>``, so a broadcast on that entry
    already targets exactly the right window. Fire-and-forget; no reply."""
    return json.dumps({"type": "mcp_activity", "kind": str(kind)})


# ---------------------------------------------------------------------------
# Control channel (/control) — single-active-browser lease
#
# Each browser carries a stable ``clientId``; the broker tracks the one
# ``active`` client per broker and tears down the others' terminal views.
#   broker -> client: {"type": "status", "active": <bool>,
#                      "activeClientId": <id|null>, "gen": <int>,
#                      "bootId": <str>}
#                     {"type": "terminal_control", ...}  (#238, below)
#                     {"type": "pong", "n": <int|null>, + the status fields}
#                     {"type": "session_launched", "id": <int>, "gen": <int>,
#                      "bootId": <str>}  (#262, lease holder only, below)
#   client -> broker: {"type": "become_active"}
#                     {"type": "ping", "n": <int>}  (#248)
# ---------------------------------------------------------------------------

def control_status_frame(active: bool,
                         active_client_id: Optional[str],
                         gen: int, boot_id: str) -> str:
    """Broker -> client on /control: whether THIS client holds the lease, plus
    who currently does (``None`` when nobody is active). #248: ``gen`` is the
    broker's lease generation (it bumps on every holder change) and
    ``bootId`` names the broker run, so a page can key "a NEW holding" on
    (bootId, gen) -- a generation alone restarts with the broker."""
    return json.dumps({
        "type": "status",
        "active": bool(active),
        "activeClientId": active_client_id,
        "gen": int(gen),
        "bootId": str(boot_id),
    })


def control_pong_frame(n: Optional[int], active: bool,
                       active_client_id: Optional[str],
                       gen: int, boot_id: str) -> str:
    """#248: broker -> client on /control, the answer to ``{type:'ping',
    n}`` on the same socket. Echoes the ping's nonce (``None`` when the ping
    carried no integer ``n``) and carries the live lease exactly as a status
    frame does, so a page whose status went missing re-learns its lease from
    any ping."""
    return json.dumps({
        "type": "pong",
        "n": n,
        "active": bool(active),
        "activeClientId": active_client_id,
        "gen": int(gen),
        "bootId": str(boot_id),
    })


def desktop_rpc_frame(rid: int, op: str,
                      args: Optional[Dict[str, Any]] = None,
                      gen: Optional[int] = None,
                      boot_id: Optional[str] = None) -> str:
    """Broker -> lease-holding browser on /control (#244): one desktop op to
    run against the layout. ``rid`` is unique per broker run; the browser
    applies it exactly once and answers exactly one ``desktop_result`` with
    the same rid (the frames' full contract lives in the broker's desktop
    relay: design/broker.md ## Wire contract). ``args.visible`` carries the
    caller's visible window ids (null for an admin) for the browser's own
    visibility refusal. #250: ``gen`` (the lease generation the call was
    made under) and ``bootId`` (the broker run that numbered ``rid``) are
    added only when given, so the browser can key its answered-rid cache on
    (host, run, rid) and refuse an rpc of a generation it has seen pass."""
    frame: Dict[str, Any] = {"type": "desktop_rpc", "rid": int(rid),
                             "op": str(op), "args": args or {}}
    if gen is not None:
        frame["gen"] = int(gen)
    if boot_id is not None:
        frame["bootId"] = str(boot_id)
    return json.dumps(frame)


def session_launched_frame(window_id: int, gen: int, boot_id: str) -> str:
    """Broker -> ONE /control socket of the lease holder (#262), after a
    successful ``POST /mcp/launch``: open window ``window_id`` on your
    desktop. Fire-and-forget (no rid, no answer). ``gen`` is the lease
    generation it was sent under and ``bootId`` the broker run, so a page
    that has since lost the lease, or talks to another run, drops it. A page
    that predates the frame ignores it (the window stays a taskbar chip,
    which any desktop op opens)."""
    return json.dumps({"type": "session_launched", "id": int(window_id),
                       "gen": int(gen), "bootId": str(boot_id)})


# ---------------------------------------------------------------------------
# Terminal control (#238) — one external controller per terminal
#
# A controller is any /ws subscriber that claims the terminal; while it holds
# it, only its socket's input and resize reach the producer. Every surface
# carries the same VIEW: {state: "free"|"controlled", label: str|None,
# since: epoch ms|None, gen: int} (label/since are None when free; gen:
# strictly increasing per terminal id within one broker run, across same-id
# replacement too; clients may order by it. Across a broker restart it is
# seeded from the wall clock in ms, which normally keeps it increasing but is
# not guaranteed if the clock steps back, so clients re-baseline then).
#   controller -> broker (/ws): {"type": "control", "op": "claim", "label": s}
#                               {"type": "control", "op": "release"}
#   broker -> /ws subscriber:   {"type": "control", **view}   (each change, and
#                               once right after `resized` on attach)
#   broker -> revoked ctrl:     {"type": "control", "state": "revoked",
#                                "by": "browser"|"claim", "gen": g}
#   broker -> /control socket:  {"type": "terminal_control", "id": n, **view}
# ---------------------------------------------------------------------------

#: The longest claim label, in Unicode code points (Python ``len``; a JS
#: string's ``length`` counts UTF-16 units and can read up to twice this).
CONTROL_LABEL_MAX = 64

# Format characters (Cf) a label may carry: ZWNJ and ZWJ, which emoji and
# several scripts need. Every other Cf is refused, which covers all of
# Unicode's Bidi_Control set (U+061C, U+200E-200F, U+202A-202E,
# U+2066-2069) as well as invisible ones like U+200B and U+FEFF.
_LABEL_CF_ALLOWED = frozenset("\u200c\u200d")
# Control, surrogate, unassigned, line/paragraph separator. Categories come
# from the RUNNING Python's Unicode database (unicodedata.unidata_version; 3.10
# on deploy), so a code point newer than it reads as unassigned and is refused.
_LABEL_REFUSED_CATEGORIES = frozenset(("Cc", "Cs", "Cn", "Zl", "Zp"))
# Letters, numbers, punctuation and symbols that still render as nothing
# (Hangul fillers, the braille blank, the Khmer inherent vowels): they do not
# count as the visible character a label must have.
_LABEL_FILLERS = frozenset(map(chr, (0x115F, 0x1160, 0x3164, 0xFFA0, 0x2800,
                                     0x17B4, 0x17B5)))


def control_label_ok(label: Any) -> bool:
    """Whether ``label`` is a valid claim label: a str of 1..CONTROL_LABEL_MAX
    code points with at least one visible character (a letter, number,
    punctuation or symbol that is not a filler; so not whitespace-only, and
    not only marks, joiners or variation selectors), and no control,
    surrogate, unassigned, line/paragraph-separator or format character
    (ZWNJ/ZWJ excepted). The label is shown to the owner verbatim, so it is
    refused, never repaired (no normalization, no stripping). The length cap
    also bounds stacked combining marks."""
    if not isinstance(label, str):
        return False
    if not 1 <= len(label) <= CONTROL_LABEL_MAX:
        return False
    visible = False
    for ch in label:
        cat = unicodedata.category(ch)
        if cat in _LABEL_REFUSED_CATEGORIES:
            return False
        if cat == "Cf" and ch not in _LABEL_CF_ALLOWED:
            return False
        if cat[0] in "LNPS" and ch not in _LABEL_FILLERS:
            visible = True
    return visible


def control_view(state: str, label: Optional[str], since: Optional[int],
                 gen: int) -> Dict[str, Any]:
    """The one control VIEW shape every surface carries (see above)."""
    controlled = state == "controlled"
    return {"state": "controlled" if controlled else "free",
            "label": str(label) if controlled else None,
            "since": int(since) if controlled else None,
            "gen": int(gen)}


def control_frame(view: Dict[str, Any]) -> str:
    """Broker -> every /ws subscriber of a terminal: its control view."""
    frame: Dict[str, Any] = {"type": "control"}
    frame.update(view)
    return json.dumps(frame)


def control_revoked_frame(by: str, gen: int) -> str:
    """Broker -> the controller that just lost the terminal, sent BEFORE the
    broadcast of the view that replaced it. ``by`` is ``"browser"`` (POST
    /session/control) or ``"claim"`` (another socket claimed). ``gen`` is the
    new view's gen. The socket stays open as a watcher."""
    return json.dumps({"type": "control", "state": "revoked",
                       "by": str(by), "gen": int(gen)})


def terminal_control_frame(window_id: int, view: Dict[str, Any]) -> str:
    """Broker -> every /control socket: terminal ``window_id``'s control view
    changed. A controlled desktop window has closed its /ws, so this is how it
    hears of a release it did not cause."""
    frame: Dict[str, Any] = {"type": "terminal_control", "id": int(window_id)}
    frame.update(view)
    return json.dumps(frame)


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

def parse(text: str) -> Optional[Dict[str, Any]]:
    """Parse one text frame. Returns None on malformed JSON or a non-object
    payload (callers treat that as an ignorable frame)."""
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    return data
