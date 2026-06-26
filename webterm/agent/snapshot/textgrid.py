"""Dependency-free terminal grid renderer (fallback for the MCP screen read).

When pyte is installed the agent renders the screen through it (full grid + SGR
fidelity). When it is **not**, this module replays the PTY ring through a small
in-house terminal emulator and returns the settled ``rows``x``cols`` character
grid — so a full-screen TUI (btop/htop/vim/less) reads as a clean, **bounded**
grid with box-drawing and braille glyphs intact, instead of the unbounded
raw-ANSI dump the old fallback produced (issue #15).

Scope: enough of the VT/ECMA-48 model to lay cursor-addressed output onto a grid
— CUP/CUU/CUD/CUF/CUB/CHA/VPA cursor moves, ED/EL erase (incl. private DECSED/
DECSEL ``?J``/``?K``), ICH/DCH/ECH char insert/delete/erase, IL/DL line
insert/delete, SU/SD and DECSTBM scroll-region scrolling, autowrap, the
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
emulator): no DEC origin mode (DECOM ``?6h``, so CUP stays screen-absolute even
under a scroll region), no separate wrap-pending cell, and wide (CJK)/combining
glyphs counted as one column. Scroll regions (DECSTBM), IL/DL, ICH/DCH, ECH and
SU/SD ARE modeled (#28) so a TUI's menu-teardown ops don't leave ghost text.
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


def _trim_for_screen(data: bytes, evicted: bool = False) -> bytes:
    """Trim the ring for a current-screen render. Drop everything before the
    last full clear / alt-screen entry — invisible on the settled screen — so a
    50-frame TUI capture renders only its final frame. With no such marker, an
    **evicted** ring head may begin mid-escape-sequence, so resync to the first
    LF or ESC; otherwise the head is the true start and is kept. Mirrors
    :func:`webterm.agent.snapshot.raw._trim` so the MCP screen render can't
    mis-decode a truncated leading sequence into ghost glyphs near top-left
    (#28). Dropping a partial leading line is the accepted tradeoff (that
    fragment would render mis-positioned anyway)."""
    best = max(data.rfind(m) for m in _RESTART_MARKERS)
    if best > 0:
        return data[best:]
    if best == 0 or not evicted:
        return data
    candidates = [i for i in (data.find(b"\n"), data.find(b"\x1b")) if i >= 0]
    return data[min(candidates):] if candidates else data


def _trim_to_last_restart(data: bytes) -> bytes:
    """Marker-only trim (no eviction resync) — equivalent to
    ``_trim_for_screen(data, evicted=False)``. Kept for callers that don't track
    ring-eviction state."""
    return _trim_for_screen(data)


def _replay(data: bytes, cols: int, rows: int, trim: bool = True,
            evicted: bool = False):
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
    # DECSTBM scroll region [top, bot], inclusive, 0-based (default: whole
    # screen). Scrolling, IL/DL and SU/SD act within it; rows outside stay put.
    top, bot = 0, rows - 1
    text = (_trim_for_screen(data, evicted) if trim else data).decode(
        "utf-8", "replace")
    n = len(text)
    i = 0

    def scroll_up(count: int = 1) -> None:
        # Scroll the [top, bot] region up by `count`: a row leaves at the top of
        # the region, a blank appears at the bottom; rows outside are untouched.
        # pop(top)+insert(bot, blank) keeps the row count and the outside rows
        # fixed — correct ONLY because `bot` is the post-pop insertion index.
        for _ in range(max(1, count)):
            if top == 0 and not in_alt:          # full-screen scroll = history
                history.append("".join(grid[top]))
            grid.pop(top)
            grid.insert(bot, [" "] * cols)

    def scroll_down(count: int = 1) -> None:
        # Scroll the [top, bot] region down by `count`: a blank at the top, a row
        # leaves at the bottom; rows outside the region are untouched.
        for _ in range(max(1, count)):
            grid.insert(top, [" "] * cols)
            grid.pop(bot + 1)

    def newline() -> None:
        nonlocal y
        if y == bot:                             # at bottom margin -> scroll
            scroll_up(1)
        elif y < rows - 1:                       # else move down (clamp at edge)
            y += 1

    def clear() -> None:
        nonlocal top, bot
        for r in range(rows):
            grid[r] = [" "] * cols
        top, bot = 0, rows - 1                   # screen switch / RIS resets it

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
                elif final == "J":  # ED (and DECSED ?J: same erase — textgrid
                    # models no selective-erase protection, so ?J behaves as J)
                    _erase_display(grid, x, y, cols, rows, nums[0] if nums else 0)
                elif final == "K":  # EL (and DECSEL ?K, same rationale)
                    _erase_line(grid[y], min(x, cols - 1), cols,
                                nums[0] if nums else 0)
                elif final == "r" and not priv:  # DECSTBM: set scroll region
                    if not params:               # CSI r -> reset to full screen
                        top, bot = 0, rows - 1    # (no cursor move, matches pyte)
                    else:
                        t0 = (nums[0] or 1) - 1
                        b0 = (nums[1] if len(nums) > 1 and nums[1] > 0
                              else rows) - 1
                        if 0 <= t0 < b0 <= rows - 1:   # valid -> set + home
                            top, bot = t0, b0
                            x = y = 0
                        # invalid (top>=bot): region & cursor unchanged (pyte)
                elif final == "L" and top <= y <= bot:  # IL: insert blank lines
                    for _ in range(min(nums[0] or 1, bot - y + 1)):
                        grid.pop(bot)
                        grid.insert(y, [" "] * cols)
                    x = 0
                elif final == "M" and top <= y <= bot:  # DL: delete lines
                    for _ in range(min(nums[0] or 1, bot - y + 1)):
                        grid.pop(y)
                        grid.insert(bot, [" "] * cols)
                    x = 0
                elif final == "@":  # ICH: insert blanks, shift right (clip edge)
                    cnt = min(nums[0] or 1, cols - x)
                    grid[y][x:cols] = ([" "] * cnt + grid[y][x:cols])[:cols - x]
                elif final == "P":  # DCH: delete chars, shift left, pad right
                    cnt = min(nums[0] or 1, cols - x)
                    grid[y][x:cols] = grid[y][x + cnt:cols] + [" "] * cnt
                elif final == "X":  # ECH: erase n chars in place (no shift)
                    cnt = min(nums[0] or 1, cols - x)
                    for c in range(x, x + cnt):
                        grid[y][c] = " "
                elif final == "S":  # SU: scroll region up
                    scroll_up(nums[0] or 1)
                elif final == "T":  # SD: scroll region down
                    scroll_down(nums[0] or 1)
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
            if nxt == "M":  # RI: reverse index — up, or scroll region down at top
                if y == top:
                    scroll_down(1)
                else:
                    y = max(y - 1, 0)
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
                  view: str = "screen", lines: int = 0,
                  evicted: bool = False) -> Dict[str, Any]:
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
    grid, x, y, history = _replay(data, cols, rows, trim=not scrollback,
                                  evicted=evicted)
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
