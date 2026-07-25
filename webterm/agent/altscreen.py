"""Live DEC private-mode tracker for the PTY output stream (#21, #23, #138, #154).

read_screen / send_keys / snapshot need to know things about the terminal that
only the running output stream can tell them:

- **alt_screen** — is a full-screen alternate buffer showing (mc/btop/vim)?
  Then the grid is the whole story and scrollback is meaningless (#21).
- **app_cursor** (DECCKM) — is application-cursor-key mode on? Then arrow /
  Home / End keys must be sent as SS3 (``ESC O x``), not CSI (``ESC [ x``), or
  the TUI ignores them (#23).
- **bracketed_paste** (DECSET 2004) — did the app ask for ``ESC[200~``-framed
  pastes? Snapshots re-assert it so a reloaded xterm.js recovers the state and
  a multiline paste isn't submitted line-by-line (#138).
- **mouse_tracking** / **mouse_encoding** — did the app turn on mouse reporting,
  and in which wire encoding? Same reason as 2004: a snapshot replays only the
  grid, and a repainting TUI's ``ESC[2J`` reliably sits *after* the mouse
  DECSETs it emitted at startup, so the replayed slice never contains them. A
  reloaded xterm.js then has tracking off while lazygit/btop/mc still believe
  it is on — clicks and scroll go dead (#154).

All are DEC private modes toggled by ``CSI ? <modes> h`` (set) / ``l`` (reset),
so one sniffer tracks them. The authoritative signal is the live stream, NOT a
scan of the bounded ring: a long-running TUI's mode-set scrolls out of the ring,
so a re-scan would wrongly report the mode off. The Agent feeds every PTY chunk
here and keeps the latest state, which survives ring eviction.

RIS (``ESC c``) is honoured as a full reset (see ``_MODE_RE``). DECSTR
(``ESC[!p``) is not — see there for why.

Heuristic limits (it is a sniffer, not a full VT parser): a ``CSI ?...h/l`` or
``ESC c`` that appears INSIDE an OSC/DCS string payload would be counted (rare
in practice), and 8-bit CSI (0x9b) is not matched. Both are pre-existing and
apply equally to every tracked mode; #154 inherits them rather than adding
them."""

from __future__ import annotations

import re
from typing import Iterator

# Any DEC private mode set/reset: CSI ? <params> (h|l). We match the whole
# private sequence and test its ;-separated params numerically, so a combined
# ``CSI ?1049;1h`` sets both, leading zeros (``?01h`` == mode 1) are honoured,
# and ``?470h`` / ``?10h`` are NOT mistaken for 47 / 1 via substring.
#
# RIS (``ESC c``) is folded into the SAME scan so stream order is preserved: a
# RIS followed by ``?1002h`` must end up mouse-ON, not cleared. In the vendored
# xterm.js, RIS routes fullReset() -> Terminal.reset(), which calls BOTH
# coreService.reset() (DECCKM + bracketed paste) and coreMouseService.reset()
# (tracking + encoding) and swaps back to the normal buffer — i.e. it clears
# every mode tracked here. Modelling it matters because ``reset`` / ``tput
# reset`` is precisely what a user runs to unstick a terminal that a killed TUI
# left with mouse capture on; without this the next snapshot postamble would
# resurrect that capture on the following reload (#154).
#
# DECSTR (``ESC[!p``) is deliberately NOT treated as a reset: xterm.js's
# softReset clears DECCKM and bracketed paste but never touches the mouse
# service, so honouring it here would model a clear that does not happen.
_MODE_RE = re.compile(rb"\x1b\[\?([0-9;]*)([hl])|\x1bc")
_ALT_MODES = frozenset((47, 1047, 1049))   # alt-screen
_DECCKM = 1                                 # application cursor keys
_BRACKETED_PASTE = 2004                     # ESC[200~ / ESC[201~ paste framing
# Mouse reporting is TWO independent concepts, each internally exclusive:
# the tracking level (how much the app wants reported) and the wire encoding
# (how a report is spelled). Within each group the latest toggle wins, which is
# also how xterm.js models them -- it keeps one activeProtocol and one
# activeEncoding, and DECRST of ANY member drops the group back to its default.
MOUSE_TRACK_MODES = (9, 1000, 1002, 1003)  # X10 / VT200 / button-drag / any
MOUSE_ENCODINGS = (1006, 1016)             # SGR / SGR-pixels
_MOUSE_MODES = frozenset(MOUSE_TRACK_MODES + MOUSE_ENCODINGS)
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
    """Feed raw PTY bytes; query the latest alt-screen / DECCKM / bracketed-
    paste / mouse-reporting state. The last toggle of a mode wins (matching
    real terminal behaviour), tracked per concept so the modes stay
    independent. Never raises."""

    def __init__(self) -> None:
        self._alt = False
        self._app_cursor = False
        self._bracketed_paste = False
        self._mouse_track = 0
        self._mouse_encoding = 0
        self._tail = b""

    def _reset_all(self) -> None:
        """Back to power-on state (RIS).

        ``ESC c`` is a 2-byte pattern, so raw binary on the PTY (``cat`` of an
        executable) will trip it by chance far more often than the 9-byte mode
        sequences do. That is accepted, because the error is asymmetric: a
        wrongly-CLEARED mode costs one un-restored re-assert — exactly the
        pre-#154 behaviour — while a wrongly-KEPT mouse mode makes every click
        inject an escape sequence into a shell that never asked for one. It is
        also self-limiting in practice: you dump a binary at a shell prompt,
        where these modes are already off."""
        self._alt = False
        self._app_cursor = False
        self._bracketed_paste = False
        self._mouse_track = 0
        self._mouse_encoding = 0

    def feed(self, chunk: bytes) -> None:
        if not chunk:
            return
        buf = self._tail + chunk
        for m in _MODE_RE.finditer(buf):
            if m.group(2) is None:        # RIS (ESC c): power-on state
                self._reset_all()
                continue
            on = m.group(2) == b"h"
            params = list(_mode_ints(m.group(1)))
            modes = set(params)
            if modes & _ALT_MODES:
                self._alt = on            # last alt toggle wins (any alt mode)
            if _DECCKM in modes:
                self._app_cursor = on     # last DECCKM toggle wins
            if _BRACKETED_PASTE in modes:
                self._bracketed_paste = on  # last 2004 toggle wins
            if modes & _MOUSE_MODES:
                # Walk the params IN ORDER, not as a set: a combined
                # ``CSI ?1002;1003h`` is applied left to right by a real
                # parser, so the rightmost member of a group is the one that
                # sticks. Set stores the mode number, reset stores 0 -- any
                # member's reset clears its whole group, which is what both
                # xterm and xterm.js do.
                for mode in params:
                    if mode in MOUSE_TRACK_MODES:
                        self._mouse_track = mode if on else 0
                    elif mode in MOUSE_ENCODINGS:
                        self._mouse_encoding = mode if on else 0
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

    @property
    def bracketed_paste(self) -> bool:
        """DECSET 2004 — the app wants pastes framed in ESC[200~/201~ (#138)."""
        return self._bracketed_paste

    @property
    def mouse_tracking(self) -> int:
        """Active mouse tracking level as its mode number (9 / 1000 / 1002 /
        1003), or 0 when the app is not asking for mouse reports (#154)."""
        return self._mouse_track

    @property
    def mouse_encoding(self) -> int:
        """Active extended mouse-report encoding as its mode number (1006 SGR /
        1016 SGR-pixels), or 0 for the default X10-style encoding (#154).

        1005 and 1015 are deliberately absent: the vendored xterm.js logs both
        as unsupported, so re-asserting them would promise an encoding the
        browser cannot produce."""
        return self._mouse_encoding


class AltScreenSniffer(DecModeSniffer):
    """Back-compat alias for #21's name; ``feed`` returns the alt-screen flag."""

    def feed(self, chunk: bytes) -> bool:   # type: ignore[override]
        super().feed(chunk)
        return self._alt
