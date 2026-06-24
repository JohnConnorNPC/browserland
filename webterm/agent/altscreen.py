"""Live DEC private-mode tracker for the PTY output stream (#21, #23).

read_screen / send_keys need to know two things about the terminal that only
the running output stream can tell them:

- **alt_screen** — is a full-screen alternate buffer showing (mc/btop/vim)?
  Then the grid is the whole story and scrollback is meaningless (#21).
- **app_cursor** (DECCKM) — is application-cursor-key mode on? Then arrow /
  Home / End keys must be sent as SS3 (``ESC O x``), not CSI (``ESC [ x``), or
  the TUI ignores them (#23).

Both are DEC private modes toggled by ``CSI ? <modes> h`` (set) / ``l`` (reset),
so one sniffer tracks them. The authoritative signal is the live stream, NOT a
scan of the bounded ring: a long-running TUI's mode-set scrolls out of the ring,
so a re-scan would wrongly report the mode off. The Agent feeds every PTY chunk
here and keeps the latest state, which survives ring eviction.

Heuristic limits (it is a sniffer, not a full VT parser): a ``CSI ?...h/l`` that
appears INSIDE an OSC/DCS string payload would be counted (rare in practice),
and 8-bit CSI (0x9b) is not matched."""

from __future__ import annotations

import re
from typing import Iterator

# Any DEC private mode set/reset: CSI ? <params> (h|l). We match the whole
# private sequence and test its ;-separated params numerically, so a combined
# ``CSI ?1049;1h`` sets both, leading zeros (``?01h`` == mode 1) are honoured,
# and ``?470h`` / ``?10h`` are NOT mistaken for 47 / 1 via substring.
_MODE_RE = re.compile(rb"\x1b\[\?([0-9;]*)([hl])")
_ALT_MODES = frozenset((47, 1047, 1049))   # alt-screen
_DECCKM = 1                                 # application cursor keys
# Stitch a sequence split across feeds by carrying any trailing partial escape
# (from the last ESC to the end), capped so a lone ESC can't grow the carry.
_MAX_CARRY = 64


def _mode_ints(params: bytes) -> Iterator[int]:
    """Yield int mode numbers from a ``;``-separated param byte-string; empty or
    non-numeric fields are skipped, leading zeros tolerated."""
    for tok in params.split(b";"):
        if tok.isdigit():
            yield int(tok)


class DecModeSniffer:
    """Feed raw PTY bytes; query the latest alt-screen / DECCKM state. The last
    toggle of a mode wins (matching real terminal behaviour), tracked per
    concept so alt-screen and DECCKM stay independent. Never raises."""

    def __init__(self) -> None:
        self._alt = False
        self._app_cursor = False
        self._tail = b""

    def feed(self, chunk: bytes) -> None:
        if not chunk:
            return
        buf = self._tail + chunk
        for m in _MODE_RE.finditer(buf):
            on = m.group(2) == b"h"
            modes = set(_mode_ints(m.group(1)))
            if modes & _ALT_MODES:
                self._alt = on            # last alt toggle wins (any alt mode)
            if _DECCKM in modes:
                self._app_cursor = on     # last DECCKM toggle wins
        # Carry a trailing, possibly-incomplete escape so it completes next feed.
        esc = buf.rfind(b"\x1b")
        self._tail = (buf[esc:] if esc != -1 and len(buf) - esc <= _MAX_CARRY
                      else b"")

    @property
    def alt_screen(self) -> bool:
        return self._alt

    @property
    def app_cursor(self) -> bool:
        """DECCKM (application cursor keys) — arrows go out as SS3 when set."""
        return self._app_cursor


class AltScreenSniffer(DecModeSniffer):
    """Back-compat alias for #21's name; ``feed`` returns the alt-screen flag."""

    def feed(self, chunk: bytes) -> bool:   # type: ignore[override]
        super().feed(chunk)
        return self._alt
