"""Wire-protocol frame builders and parsers.

This is the ONLY module that knows the JSON shapes. Both the agent and the
broker import from here. These shapes are Browserland's own web-terminal
producer protocol; the relay/registry framing was adapted from xterm-py
(``browser/broker.py``), a separate codebase at
https://github.com/JohnConnorNPC/xterm-py.

Producer -> broker (text JSON):
    {"type": "hello",   "window_id": <int>, "pid": <int>, "title": "...",
     "cols": N, "rows": M}    # + optional "host", "kind", "agent", "cwd", "version"
    {"type": "title",   "data": "..."}
    {"type": "agent",   "data": "claude"|"grok"|"codex"|"opencode"|""}  # foreground agent
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
    {"type": "agent",   "data": "claude"|"grok"|"codex"|"opencode"|""}  # foreground agent
    {"type": "cwd",     "data": "C:\\path\\to\\dir"}  # live working dir push
    {"type": "exit",    "code": <int>}    # session's child exited — tear down now
    {"type": "error", "reason": "unknown_session", "session_id": <int>}
Broker -> browser (binary): producer bytes, verbatim.

Browser -> broker (text JSON): input / paste / resize (mouse ignored).
"""

from __future__ import annotations

import json
from typing import Any, Dict, Optional


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
    version: Optional[str] = None,
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
    # "version" carries the producer's build id (webterm.build_version()) so the
    # broker can surface it and flag stale agents (issue #22).
    if version:
        frame["version"] = str(version)
    return json.dumps(frame)


def title_frame(title: str) -> str:
    return json.dumps({"type": "title", "data": str(title)})


def agent_frame(name: str) -> str:
    """Foreground-agent push: which of claude/grok/codex/opencode is running, or
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
                             timeout_ms: int = 0) -> str:
    """Broker -> producer: render the live screen and reply with plain text.
    Backs the MCP /mcp/read endpoint; only agents answer it (non-agent
    producers have no handler, so the request times out -> 502). ``view``
    (``"screen"`` default / ``"scrollback"``) + ``lines`` request scrollback
    history above the current grid (#21); older agents ignore the extra keys.

    ``wait_for_change`` (a prior ``content_hash``) + ``timeout_ms`` make the
    AGENT hold the reply until the freshly-rendered screen hash differs from
    that baseline, or the timeout elapses — a single round-trip instead of a
    busy-poll (#26). Older agents ignore both and reply immediately."""
    return json.dumps({"type": "screen_text_please", "req": int(req),
                       "view": str(view), "lines": int(lines),
                       "wait_for_change": wait_for_change,
                       "timeout_ms": int(timeout_ms)})


def kill_frame(req: int, pid: int) -> str:
    return json.dumps({"type": "kill", "req": int(req), "pid": int(pid)})


def git_status_please_frame(req: int) -> str:
    return json.dumps({"type": "git_status_please", "req": int(req)})


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


def git_status_frame(req: int, status: Dict[str, Any]) -> str:
    frame: Dict[str, Any] = {"type": "git_status", "req": int(req)}
    frame.update(status or {})
    return json.dumps(frame)


def screen_text_frame(req: int, text: str, cols: int, rows: int,
                      degraded: bool = False, alt_screen: bool = False,
                      cursor: Optional[Dict[str, int]] = None,
                      view: str = "screen", history_lines: int = 0,
                      app_cursor: bool = False, content_hash: str = "") -> str:
    """Producer -> broker: the rendered plain-text screen for a screen_text
    request. ``text`` is a bounded ``rows``x``cols`` grid (plus ``history_lines``
    of scrollback above it when ``view="scrollback"``), rendered via pyte or the
    dependency-free in-house emulator. ``alt_screen`` (#21) is whether the
    terminal is showing a full-screen alternate buffer — when true, scrollback
    is meaningless and ``view`` comes back ``"screen"``. ``app_cursor`` (#23) is
    DECCKM (application cursor keys) — when true, send_keys must emit SS3 arrows.
    ``cursor`` is ``{row, col}`` 0-based within the grid (``None`` when degraded).
    ``content_hash`` (#26) is a stable digest of ``text`` so a caller can detect
    change across reads (the empty string means the agent didn't compute one).
    ``degraded`` is the rare last-ditch raw decode (``view="raw"``), so the
    caller knows the text is not a clean grid render."""
    frame: Dict[str, Any] = {
        "type": "screen_text",
        "req": int(req),
        "text": str(text),
        "cols": int(cols),
        "rows": int(rows),
        "degraded": bool(degraded),
        "alt_screen": bool(alt_screen),
        "app_cursor": bool(app_cursor),
        "view": str(view),
        "history_lines": int(history_lines),
        "content_hash": str(content_hash),
    }
    # cursor is null on the degraded raw path (no grid to locate it in) — the
    # helper enforces the invariant regardless of what the caller passed.
    frame["cursor"] = (None if bool(degraded) or not isinstance(cursor, dict)
                       else {"row": int(cursor.get("row", 0)),
                             "col": int(cursor.get("col", 0))})
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


# ---------------------------------------------------------------------------
# Control channel (/control) — single-active-browser lease
#
# Each browser carries a stable ``clientId``; the broker tracks the one
# ``active`` client per broker and tears down the others' terminal views.
#   broker -> client: {"type": "status", "active": <bool>,
#                      "activeClientId": <id|null>}
#   client -> broker: {"type": "become_active"}
# ---------------------------------------------------------------------------

def control_status_frame(active: bool,
                         active_client_id: Optional[str]) -> str:
    """Broker -> client on /control: whether THIS client holds the lease, plus
    who currently does (``None`` when nobody is active)."""
    return json.dumps({
        "type": "status",
        "active": bool(active),
        "activeClientId": active_client_id,
    })


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
