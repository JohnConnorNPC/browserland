"""Tier-1 snapshot: clear-screen preamble + ring replay.

The preamble is Browserland's snapshot renderer: reset attributes, clear
screen, home cursor. Deliberately NOT a hard reset (``ESC c``) and NOT
``ESC [3J`` — snapshots are broadcast over the producer's single binary
channel, so already-attached browsers receive them too; a hard reset would
clobber their scrollback and modes.

If the ring has evicted chunks, its head may start mid-escape-sequence.
Heuristics, best first:
  1. replay from the last full clear (``ESC [2J``) or alt-screen entry
     (``ESC [?1049h``) — everything before it is invisible anyway;
  2. otherwise skip to the first LF or ESC, which at worst drops a partial
     line instead of feeding xterm.js a truncated sequence.

Terminal queries are stripped from the replay: a ring that contains the
child's startup DA (``ESC [c``) or DSR/CPR (``ESC [6n``) request would make
every newly-attaching xterm.js *answer* it — typing ``^[[?1;2c``-style junk
into the shell on each attach. Queries have no display effect, so removing
them never changes the rendered screen. Live output is not filtered.
"""

from __future__ import annotations

import re

PREAMBLE = b"\x1b[0m\x1b[2J\x1b[H"

_RESTART_MARKERS = (b"\x1b[2J", b"\x1b[?1049h")

# CSI sequences with final byte 'c' (DA1/DA2) or 'n' (DSR/CPR requests).
_QUERY_RE = re.compile(rb"\x1b\[[0-9;?>=]*[cn]")


def render(ring_bytes: bytes, evicted: bool = True) -> bytes:
    return PREAMBLE + _QUERY_RE.sub(b"", _trim(ring_bytes, evicted))


def _trim(data: bytes, evicted: bool) -> bytes:
    # Replaying from the last clear/alt-screen-entry is a win regardless of
    # eviction: it shrinks the snapshot and skips stale screens.
    best = max(data.rfind(m) for m in _RESTART_MARKERS)
    if best > 0:
        return data[best:]
    if best == 0 or not evicted:
        return data
    # Evicted head may be a cut sequence — resync at the first LF or ESC.
    candidates = [i for i in (data.find(b"\n"), data.find(b"\x1b")) if i >= 0]
    if candidates:
        return data[min(candidates):]
    return data
