"""Tier-2 snapshot: replay the ring through pyte, render the settled grid.

Replay-at-snapshot strategy: a fresh ``pyte.Screen`` is built per
``snapshot_please`` and fed the ring bytes, then the grid is rendered as one
idempotent full redraw. This fixes tier 1's duplicated-scrollback cosmetic
issue at zero hot-path cost (continuously feeding pyte would pin a core on
output floods).

The renderer shares the raw-snapshot preamble, then emits per-row 1-based
cursor moves, SGR-diffed cells, and a postamble restoring the cursor.
Known limits: no scrollback replay, no alt-screen modeling, minor SGR
fidelity loss.
"""

from __future__ import annotations

from typing import Optional

try:
    import pyte  # type: ignore
except ImportError:  # pragma: no cover - exercised only without the extra
    pyte = None

PREAMBLE = b"\x1b[0m\x1b[2J\x1b[H"

_NAMED = {
    "black": 0, "red": 1, "green": 2, "yellow": 3,
    "blue": 4, "magenta": 5, "cyan": 6, "white": 7,
    "brightblack": 8, "brightred": 9, "brightgreen": 10,
    "brightyellow": 11, "brightblue": 12, "brightmagenta": 13,
    "brightcyan": 14, "brightwhite": 15,
}


def available() -> bool:
    return pyte is not None


def _color_params(color: str, is_bg: bool) -> list:
    """pyte colors are 'default', a named color, or a 6-hex-digit string."""
    if not color or color == "default":
        return [49 if is_bg else 39]
    idx = _NAMED.get(color)
    if idx is not None:
        if idx < 8:
            return [(40 if is_bg else 30) + idx]
        return [(100 if is_bg else 90) + (idx - 8)]
    if len(color) == 6:
        try:
            r = int(color[0:2], 16)
            g = int(color[2:4], 16)
            b = int(color[4:6], 16)
        except ValueError:
            return [49 if is_bg else 39]
        return [48 if is_bg else 38, 2, r, g, b]
    return [49 if is_bg else 39]


class _Sgr:
    __slots__ = ("fg", "bg", "bold", "italics", "underscore", "reverse",
                 "strikethrough", "blink")

    def __init__(self, fg="default", bg="default", bold=False, italics=False,
                 underscore=False, reverse=False, strikethrough=False,
                 blink=False):
        self.fg = fg
        self.bg = bg
        self.bold = bold
        self.italics = italics
        self.underscore = underscore
        self.reverse = reverse
        self.strikethrough = strikethrough
        self.blink = blink

    @classmethod
    def from_char(cls, ch) -> "_Sgr":
        return cls(
            fg=ch.fg, bg=ch.bg, bold=ch.bold, italics=ch.italics,
            underscore=ch.underscore, reverse=ch.reverse,
            strikethrough=ch.strikethrough,
            blink=getattr(ch, "blink", False),
        )

    _FLAG_CODES = (("bold", 1), ("blink", 5), ("italics", 3),
                   ("underscore", 4), ("reverse", 7), ("strikethrough", 9))

    def diff_into(self, prev: "_Sgr", out: bytearray) -> None:
        params: list = []
        # Any flag turning off forces a full reset + re-emit (no reliable
        # per-flag "off" codes across emulators) — same rule as snapshot.rs.
        needs_reset = any(
            getattr(prev, name) and not getattr(self, name)
            for name, _ in self._FLAG_CODES
        )
        if needs_reset:
            params.append(0)
            for name, code in self._FLAG_CODES:
                if getattr(self, name):
                    params.append(code)
            params.extend(_color_params(self.fg, False))
            params.extend(_color_params(self.bg, True))
        else:
            for name, code in self._FLAG_CODES:
                if getattr(self, name) and not getattr(prev, name):
                    params.append(code)
            if self.fg != prev.fg:
                params.extend(_color_params(self.fg, False))
            if self.bg != prev.bg:
                params.extend(_color_params(self.bg, True))
        if params:
            out += b"\x1b[" + ";".join(str(p) for p in params).encode() + b"m"


def emit_screen(screen, cols: int, rows: int) -> bytes:
    """Re-emit a fed :class:`pyte.Screen`'s settled grid as one idempotent,
    self-contained full redraw: the shared PREAMBLE, then per-row 1-based cursor
    moves + SGR-diffed cells, and a postamble restoring the cursor.

    Factored out of :func:`render` so the same materializer backs the #130
    keyframe: feeding these bytes into a fresh screen reproduces the grid +
    cursor, so a statically-painted alt-screen frame can be captured as an
    immutable byte keyframe and later prepended to the surviving ring tail to
    survive >256 KiB eviction of the original full-frame paint."""
    out = bytearray(PREAMBLE)
    state = _Sgr()
    for r in range(rows):
        out += b"\x1b[%d;1H" % (r + 1)
        row = screen.buffer[r]
        for c in range(cols):
            ch = row[c]
            target = _Sgr.from_char(ch)
            target.diff_into(state, out)
            state = target
            # Wide-char spacers hold empty data; the lead cell already
            # advanced the cursor by two columns.
            if ch.data:
                out += ch.data.encode("utf-8", errors="replace")

    cur_row = min(max(screen.cursor.y, 0), rows - 1) + 1
    cur_col = min(max(screen.cursor.x, 0), cols - 1) + 1
    out += b"\x1b[0m\x1b[%d;%dH" % (cur_row, cur_col)
    return bytes(out)


def render(ring_bytes: bytes, cols: int, rows: int) -> bytes:
    if pyte is None:
        raise RuntimeError(
            "--snapshot-mode pyte requires the 'pyte' package "
            "(pip install pyte, or install webterm[pyte])")
    screen = pyte.Screen(cols, rows)
    stream = pyte.ByteStream(screen)
    stream.feed(ring_bytes)
    return emit_screen(screen, cols, rows)


# read_screen attrs (#128): cap the styled-run list so an opt-in attribute read
# can't blow the MCP token budget on a pathologically multi-colored grid. A real
# menu/TUI yields far fewer runs than this; rows are walked top-to-bottom, so a
# truncated list keeps the upper screen.
_MAX_ATTR_RUNS = 2000


def attr_runs(screen, cols: int, rows: int, cap: int = _MAX_ATTR_RUNS) -> list:
    """The screen's styled horizontal runs — maximal same-style cell spans whose
    fg/bg/reverse/bold/underscore differs from the terminal default — as
    ``{row, col, len, fg, bg, reverse, bold, underscore}`` dicts (0-based
    ``row``/``col``; ``len`` is a CELL count).

    This surfaces the color / reverse-video / bold / underline signal that the
    plain-text render drops, so an agent can tell which menu row is selected when
    the selection is marked by an attribute alone — identical row text otherwise
    (#128, #136). ``screen`` is a fed :class:`pyte.Screen`; the "unstyled"
    baseline is read from its own ``default_char`` (not a hardcoded ``"default"``)
    so it tracks pyte — including its default bold/underscore. Style now includes
    the bold/underscore flags alongside fg/bg/reverse — enough to locate a bold-
    or underline-marked selection, not a full SGR model — so a run splits on the
    finer style; the list is still bounded to ``cap`` runs."""
    dc = screen.default_char
    base = (dc.fg, dc.bg, bool(dc.reverse), bool(dc.bold), bool(dc.underscore))
    runs: list = []
    for r in range(rows):
        row = screen.buffer[r]        # a defaultdict: a gap yields default_char
        c = 0
        while c < cols:
            ch = row[c]
            style = (ch.fg, ch.bg, bool(ch.reverse),
                     bool(ch.bold), bool(ch.underscore))
            if style == base:                        # unstyled cell: skip
                c += 1
                continue
            start = c
            c += 1
            while c < cols:                          # extend the same-style run
                nxt = row[c]
                if (nxt.fg, nxt.bg, bool(nxt.reverse),
                        bool(nxt.bold), bool(nxt.underscore)) != style:
                    break
                c += 1
            runs.append({"row": r, "col": start, "len": c - start,
                         "fg": style[0], "bg": style[1], "reverse": style[2],
                         "bold": style[3], "underscore": style[4]})
            if len(runs) >= cap:
                return runs
    return runs
