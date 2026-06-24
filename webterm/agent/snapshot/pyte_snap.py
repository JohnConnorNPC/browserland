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


def render(ring_bytes: bytes, cols: int, rows: int) -> bytes:
    if pyte is None:
        raise RuntimeError(
            "--snapshot-mode pyte requires the 'pyte' package "
            "(pip install pyte, or install webterm[pyte])")
    screen = pyte.Screen(cols, rows)
    stream = pyte.ByteStream(screen)
    stream.feed(ring_bytes)

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
