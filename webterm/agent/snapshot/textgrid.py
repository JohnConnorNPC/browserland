"""Dependency-free terminal grid renderer (fallback for the MCP screen read).

When pyte is installed the agent renders the screen through it (full grid + SGR
fidelity). When it is **not**, this module replays the PTY ring through a small
in-house terminal emulator and returns the settled ``rows``x``cols`` character
grid — so a full-screen TUI (btop/htop/vim/less) reads as a clean, **bounded**
grid with box-drawing and braille glyphs intact, instead of the unbounded
raw-ANSI dump the old fallback produced (issue #15).

Scope: enough of the VT/ECMA-48 model to lay cursor-addressed output onto a grid
— CUP/CUU/CUD/CUF/CUB/CHA/VPA cursor moves, ED/EL erase, autowrap, the
CR/LF/BS/TAB controls, RIS/RI, cursor save/restore, and alternate-screen entry
(clears, like a real fresh alt buffer). SGR (color/style), other DEC private
modes (synchronized update, cursor visibility), and OSC/DCS/SOS/PM/APC control
strings are parsed and **discarded** — they change *how* a cell looks or carry
out-of-band payload, not *which* character settles into a cell. Anything
unrecognized is swallowed, never emitted, so the result is always clean text
bounded by the window size. ``render`` **never raises** and is bounded in both
output size and (via dimension caps + replay trimming) compute.

Known limits vs pyte (cost fidelity, never boundedness/safety — the primary
pyte path covers these, and this is a best-effort fallback, not a second full
emulator): no scroll regions (DECSTBM), no IL/DL/ICH/DCH, no separate
wrap-pending cell, and wide (CJK)/combining glyphs counted as one column.
"""

from __future__ import annotations

from collections import deque
from typing import Any, Dict, List, Tuple

_PRINTABLE_MIN = 0x20
_DEL = 0x7F
_C1_LO, _C1_HI = 0x80, 0x9F  # C1 controls (UTF-8 encoded) — not printable
_TAB_WIDTH = 8
# CSI bytes that precede the final byte: parameters (0x30-0x3F incl. ? < = > : ;)
# and intermediates (0x20-0x2F, e.g. the SP in `CSI SP q`).
_CSI_PARAM = set("0123456789;:<=>?")
_CSI_INTERMEDIATE = set(" !\"#$%&'()*+,-./")
_CSI_PREFINAL = _CSI_PARAM | _CSI_INTERMEDIATE
# Defensive dimension cap: a real terminal is well under this; the ceiling only
# stops a malicious/buggy resize from allocating an enormous grid.
_MAX_DIM = 1000
# Replay-trim markers: everything before the last full clear / alt-screen entry
# is invisible, so rendering can start there (bounds compute on repaint-heavy
# TUIs). Mirrors snapshot/raw.py's restart-marker heuristic.
_RESTART_MARKERS = (b"\x1b[2J", b"\x1b[?1049h", b"\x1b[?1047h", b"\x1b[47h")
# Alt-screen DEC private modes, matched as ;-separated param TOKENS (not a
# substring — so "47" isn't seen inside "470", and a combined toggle like
# "?1049;25h" is recognised; consistent with the live AltScreenSniffer — #21).
_ALT_MODES = frozenset(("1049", "1047", "47"))
# Scrollback bounds (#21): cap retained primary-scroll history both in line
# count and total cells, so a scrollback read can't blow the token budget.
_MAX_HISTORY_LINES = 1000
_MAX_HISTORY_CELLS = 100_000


def _params_have_alt_mode(params: str) -> bool:
    """True if a CSI private param string (e.g. ``"?1049;25"``) toggles an
    alt-screen mode, by ;-token membership (not substring)."""
    return bool(_ALT_MODES.intersection(params.lstrip("?").split(";")))


def _ints(params: str) -> List[int]:
    """Parse a CSI parameter string into ints. Each ``;``-separated field
    yields its leading run of digits (0 when empty or non-numeric, e.g. a
    private ``?`` prefix), so callers can apply VT's "0 means default" rule.

    Digit runs are capped at 6 chars before ``int()``: real terminal params are
    tiny, an absurdly long run only clamps to a grid edge anyway, and an
    untrimmed run past ~4300 digits would trip CPython's int-from-string limit
    (``ValueError``) — which must never escape ``render``."""
    out: List[int] = []
    for part in params.split(";"):
        digits = "".join(c for c in part if c.isdigit())
        out.append(int(digits[:6]) if digits else 0)
    return out


def _erase_display(grid: List[List[str]], x: int, y: int,
                   cols: int, rows: int, mode: int) -> None:
    if mode == 0:  # cursor -> end of screen
        for c in range(min(x, cols), cols):
            grid[y][c] = " "
        for r in range(y + 1, rows):
            grid[r] = [" "] * cols
    elif mode == 1:  # start of screen -> cursor
        for r in range(0, y):
            grid[r] = [" "] * cols
        for c in range(0, min(x + 1, cols)):
            grid[y][c] = " "
    else:  # 2 / 3: whole screen
        for r in range(rows):
            grid[r] = [" "] * cols


def _erase_line(row: List[str], x: int, cols: int, mode: int) -> None:
    if mode == 0:  # cursor -> end of line
        for c in range(min(x, cols), cols):
            row[c] = " "
    elif mode == 1:  # start of line -> cursor
        for c in range(0, min(x + 1, cols)):
            row[c] = " "
    else:  # 2: whole line
        for c in range(cols):
            row[c] = " "


def _skip_string(text: str, k: int, n: int) -> int:
    """Skip an OSC/DCS/SOS/PM/APC control string body starting at ``k`` (just
    past the introducer). Terminates on BEL (0x07) or ST (ESC \\). Returns the
    index just past the terminator (or ``n`` if unterminated)."""
    while k < n:
        ch = text[k]
        if ch == "\x07":
            return k + 1
        if ch == "\x1b" and k + 1 < n and text[k + 1] == "\\":
            return k + 2
        k += 1
    return n


def _trim_to_last_restart(data: bytes) -> bytes:
    """Drop everything before the last full clear / alt-screen entry — it is
    not visible on the settled screen — so a 50-frame TUI capture renders only
    its final frame. No marker → unchanged."""
    best = max(data.rfind(m) for m in _RESTART_MARKERS)
    return data[best:] if best > 0 else data


def _replay(data: bytes, cols: int, rows: int, trim: bool = True):
    """Replay ``data`` (raw PTY bytes) onto a fresh ``rows``x``cols`` grid.
    Returns ``(grid, cursor_x, cursor_y, history)`` where ``history`` is the
    lines that scrolled off the top of the PRIMARY screen (bounded; never
    captured while an alternate-screen buffer is active, so a TUI's scrolling
    doesn't pollute shell scrollback — #21). Never raises.

    ``trim`` drops everything before the last clear/alt-enter (bounds compute for
    a screen-only render). Scrollback passes ``trim=False`` so real history that
    preceded the last clear is preserved — that trim is right for the visible
    grid but would silently discard legitimate scrollback (#21 review)."""
    cols = min(max(1, int(cols)), _MAX_DIM)
    rows = min(max(1, int(rows)), _MAX_DIM)
    grid: List[List[str]] = [[" "] * cols for _ in range(rows)]
    # Bound retained history by BOTH lines and total cells (a wide grid would
    # otherwise keep 1000 full rows = far more than the cell budget).
    hist_cap = min(_MAX_HISTORY_LINES, max(1, _MAX_HISTORY_CELLS // cols))
    history: "deque[str]" = deque(maxlen=hist_cap)
    x = y = 0
    saved: Tuple[int, int] = (0, 0)
    in_alt = False
    text = (_trim_to_last_restart(data) if trim else data).decode(
        "utf-8", "replace")
    n = len(text)
    i = 0

    def newline() -> None:
        nonlocal y
        y += 1
        if y > rows - 1:
            if not in_alt:                       # primary scroll = real history
                history.append("".join(grid[0]))
            grid.pop(0)
            grid.append([" "] * cols)
            y = rows - 1

    def clear() -> None:
        for r in range(rows):
            grid[r] = [" "] * cols

    while i < n:
        ch = text[i]

        if ch == "\x1b":
            j = i + 1
            if j >= n:
                break
            nxt = text[j]
            if nxt == "[":  # CSI
                k = j + 1
                while k < n and text[k] in _CSI_PARAM:
                    k += 1
                while k < n and text[k] in _CSI_INTERMEDIATE:
                    k += 1
                if k >= n:  # truncated sequence at the ring head/tail
                    break
                final = text[k]
                params = text[j + 1:k].rstrip("".join(_CSI_INTERMEDIATE))
                priv = params.startswith("?")
                nums = _ints(params)
                if priv and final in "hl":  # DEC private set/reset
                    if _params_have_alt_mode(params):
                        in_alt = final == "h"   # enter (h) vs leave (l) alt buffer
                        clear()                 # best-effort: no primary restore
                        x = y = 0
                    # other private modes (sync update, cursor vis): no cell change
                elif final in "Hf":  # CUP
                    row = nums[0] if nums and nums[0] > 0 else 1
                    col = nums[1] if len(nums) > 1 and nums[1] > 0 else 1
                    y = min(max(row - 1, 0), rows - 1)
                    x = min(max(col - 1, 0), cols - 1)
                elif final == "A":  # CUU
                    y = max(y - (nums[0] or 1), 0)
                elif final == "B":  # CUD
                    y = min(y + (nums[0] or 1), rows - 1)
                elif final == "C":  # CUF
                    x = min(x + (nums[0] or 1), cols - 1)
                elif final == "D":  # CUB
                    x = max(x - (nums[0] or 1), 0)
                elif final == "G":  # CHA: absolute column
                    x = min(max((nums[0] or 1) - 1, 0), cols - 1)
                elif final == "d":  # VPA: absolute row
                    y = min(max((nums[0] or 1) - 1, 0), rows - 1)
                elif final == "E":  # CNL
                    y = min(y + (nums[0] or 1), rows - 1)
                    x = 0
                elif final == "F":  # CPL
                    y = max(y - (nums[0] or 1), 0)
                    x = 0
                elif final == "J" and not priv:  # ED
                    _erase_display(grid, x, y, cols, rows, nums[0] if nums else 0)
                elif final == "K" and not priv:  # EL
                    _erase_line(grid[y], min(x, cols - 1), cols,
                                nums[0] if nums else 0)
                # else: SGR ('m'), and anything else we don't model — discarded.
                i = k + 1
                continue
            if nxt in "]PX^_":  # OSC / DCS / SOS / PM / APC control strings
                i = _skip_string(text, j + 1, n)
                continue
            if nxt in "()*+":  # charset designation: skip the selector char too
                i = j + 2
                continue
            if nxt == "c":  # RIS: full reset
                clear()
                x = y = 0
                saved = (0, 0)
                i = j + 1
                continue
            if nxt == "7":  # DECSC: save cursor
                saved = (x, y)
                i = j + 1
                continue
            if nxt == "8":  # DECRC: restore cursor
                x = min(max(saved[0], 0), cols - 1)
                y = min(max(saved[1], 0), rows - 1)
                i = j + 1
                continue
            if nxt == "M":  # RI: reverse index (up, scrolling down at the top)
                if y > 0:
                    y -= 1
                else:
                    grid.pop()
                    grid.insert(0, [" "] * cols)
                i = j + 1
                continue
            # Other two-byte escapes (= > D E ...): swallow the pair.
            i = j + 1
            continue

        if ch == "\n":
            newline()
            i += 1
            continue
        if ch == "\r":
            x = 0
            i += 1
            continue
        if ch == "\b":
            if x > 0:
                x -= 1
            i += 1
            continue
        if ch == "\t":
            x = min(cols - 1, (x // _TAB_WIDTH + 1) * _TAB_WIDTH)
            i += 1
            continue

        code = ord(ch)
        if code < _PRINTABLE_MIN or code == _DEL or _C1_LO <= code <= _C1_HI:
            i += 1  # C0/C1 control chars: ignore
            continue

        # Printable: deferred autowrap (writing past the last column wraps).
        if x > cols - 1:
            x = 0
            newline()
        grid[y][x] = ch
        x += 1
        i += 1

    return grid, x, y, history


def render(data: bytes, cols: int, rows: int) -> str:
    """The settled ``rows``x``cols`` grid as newline-joined, space-padded lines.
    The original screen-only contract; never raises. (For scrollback/cursor use
    :func:`render_screen`.)"""
    grid, _x, _y, _hist = _replay(data, cols, rows)
    return "\n".join("".join(r) for r in grid)


def render_screen(data: bytes, cols: int, rows: int,
                  view: str = "screen", lines: int = 0) -> Dict[str, Any]:
    """Structured render for the MCP read (#21): ``{text, cursor, history_lines}``.

    ``view="screen"`` (default) returns just the current grid. ``view=
    "scrollback"`` with ``lines>0`` prepends up to that many lines of primary
    scrollback above the grid (bounded by :data:`_MAX_HISTORY_LINES` and
    :data:`_MAX_HISTORY_CELLS`); ``history_lines`` is how many were actually
    included. ``cursor`` is ``{row, col}`` 0-based within the current grid (it
    stays screen-relative even when scrollback is prepended). Never raises."""
    cols_c = min(max(1, int(cols)), _MAX_DIM)
    scrollback = view == "scrollback"
    # Scrollback replays the FULL ring (trim=False) so history before the last
    # clear/alt-enter survives; screen-only keeps the bounding trim.
    grid, x, y, history = _replay(data, cols, rows, trim=not scrollback)
    screen = "\n".join("".join(r) for r in grid)
    cursor = {"row": min(max(int(y), 0), len(grid) - 1),
              "col": min(max(int(x), 0), cols_c - 1)}
    if scrollback and int(lines) > 0 and history:
        budget = max(1, _MAX_HISTORY_CELLS // cols_c)
        eff = min(int(lines), _MAX_HISTORY_LINES, budget)
        hist = list(history)[-eff:]
        text = "\n".join(hist) + ("\n" + screen if screen else "")
        return {"text": text, "cursor": cursor, "history_lines": len(hist)}
    return {"text": screen, "cursor": cursor, "history_lines": 0}
