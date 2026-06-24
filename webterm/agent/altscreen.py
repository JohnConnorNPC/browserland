"""Live alternate-screen tracker for the PTY output stream (issue #21).

read_screen needs to tell a caller whether the terminal is showing a
full-screen alternate buffer (mc/btop/vim — the grid is the whole story) or the
primary screen (where scrollback is real history). The authoritative signal is
the running stream of DEC private mode toggles, NOT a scan of the ring buffer:
a long-running TUI's alt-enter scrolls out of the bounded ring, so a ring scan
would wrongly report it as non-alt. So the Agent feeds every PTY chunk here and
keeps the latest state, which survives ring eviction.

Tracks the three alt-screen private modes — 1049 (the modern save-cursor +
alt-buffer + clear), 1047, and 47 — set (``h`` = enter) or reset (``l`` =
exit). The most recent toggle wins. A small carry buffer stitches a sequence
split across two chunks."""

from __future__ import annotations

import re

# Any DEC private mode set/reset: CSI ? <params> (h|l). We match the whole
# private sequence and test its ;-separated params, so a combined toggle like
# ``CSI ?1049;25h`` (alt-screen + cursor) is recognised — an exact 1049/1047/47
# match would miss it (#21 review).
_ALT_RE = re.compile(rb"\x1b\[\?([0-9;]*)([hl])")
_ALT_MODES = {b"1049", b"1047", b"47"}
# Stitch a sequence split across feeds by carrying any trailing partial escape
# (from the last ESC to the end), capped so a lone ESC can't grow the carry.
_MAX_CARRY = 64


class AltScreenSniffer:
    """Feed raw PTY bytes; ``alt_screen`` reflects the latest alt-enter/exit."""

    def __init__(self) -> None:
        self._alt = False
        self._tail = b""

    def feed(self, chunk: bytes) -> bool:
        """Update state from a PTY chunk; return the current alt-screen flag.
        The last alt toggle in the buffer wins; private modes that aren't an
        alt-screen mode leave the state untouched. Re-matching a carried,
        already-complete sequence is harmless (same h/l → same state)."""
        if not chunk:
            return self._alt
        buf = self._tail + chunk
        for m in _ALT_RE.finditer(buf):
            if _ALT_MODES.intersection(m.group(1).split(b";")):
                self._alt = m.group(2) == b"h"
        # Carry a trailing, possibly-incomplete escape so it completes next feed.
        esc = buf.rfind(b"\x1b")
        self._tail = (buf[esc:] if esc != -1 and len(buf) - esc <= _MAX_CARRY
                      else b"")
        return self._alt

    @property
    def alt_screen(self) -> bool:
        return self._alt
