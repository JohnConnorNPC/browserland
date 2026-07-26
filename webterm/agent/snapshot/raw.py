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

OSC 52 (clipboard write) is stripped for the same *shape* of reason (#153).
The ring is replayed on every attach and reattach, broadcast to every
attached browser — so a clipboard request a program made once would be
re-delivered on each attach, to browsers that were not even connected when
it was emitted. It has no display effect either, so removing it never
changes the rendered screen.

This strip is **not load-bearing**. Its failure mode is "leaves an OSC 52 in
the replay", which the browser-side gates (per-host opt-in, focus, recent
input, front terminal, rate limit) then catch; an old agent paired with a new
browser gets no strip at all. It is defence in depth, deliberately kept to a
bounded regex — if it ever needs to grow past one, the right answer is to
exclude OSC 52 while *populating* the ring, not a bigger post-hoc pattern.
"""

from __future__ import annotations

import re

PREAMBLE = b"\x1b[0m\x1b[2J\x1b[H"

_RESTART_MARKERS = (b"\x1b[2J", b"\x1b[?1049h")

# CSI sequences with final byte 'c' (DA1/DA2) or 'n' (DSR/CPR requests).
_QUERY_RE = re.compile(rb"\x1b\[[0-9;?>=]*[cn]")

# OSC 52 (#153). Introducer: ``ESC ]`` or the C1 OSC as UTF-8 (``\xc2\x9d``) —
# a *bare* 0x9D is deliberately not matched, because xterm.js decodes UTF-8 and
# a lone 0x9D becomes U+FFFD, which can never introduce an OSC. Leading zeros
# are legal in the parameter, so ``052`` is the same sequence as ``52``.
#
# The payload class excludes every byte that ENDS an OSC string in xterm's
# parser — BEL, ESC (which begins ST) — and the two that ABORT it, CAN (0x18)
# and SUB (0x1a). Excluding the aborters matters: a sequence containing one can
# never reach the clipboard, so failing to match it is correct rather than a
# miss. Newlines are allowed through, so a payload from ``base64``'s 76-column
# line wrapping is removed whole instead of leaving its tail as visible junk.
#
# ``\xc2`` is excluded too, and that exclusion is a RUNTIME bound, not a
# grammar detail. An OSC 52 payload is ``Pc`` (from ``c s p q 0-7``) plus
# base64 — pure ASCII, so nothing legal is lost. What it buys: every C1
# introducer starts with ``\xc2``, so a ring packed with back-to-back
# ``\xc2\x9d52;`` introducers can no longer make each match attempt scan to the
# end of the buffer. (The ``ESC ]`` form is already self-limiting, because ESC
# is excluded.) Without it a hostile program could turn every attach into a
# quadratic scan of the whole ring.
#
# Two alternatives, both bounded (the ring is 256 KiB, so nothing it can
# physically hold is missed):
#   1. terminated — BEL, ``ESC \`` (ST), or the C1 ST as UTF-8;
#   2. unterminated, but only when it runs to the END of the ring. A truncated
#      sequence with output after it is LEFT ALONE: a greedy "terminator
#      optional" match would swallow that output. Leaving it costs nothing,
#      because xterm.js would swallow it at replay exactly as it did live —
#      the parser is still inside the OSC string, so the clipboard write never
#      completes either way.
_OSC52_MAX_PAYLOAD = 256 * 1024
_OSC52_BODY = rb"[^\x1b\x07\x18\x1a\xc2]{0,%d}" % _OSC52_MAX_PAYLOAD
_OSC52_RE = re.compile(
    rb"(?:\x1b\]|\xc2\x9d)0*52;"
    rb"(?:" + _OSC52_BODY + rb"(?:\x07|\x1b\\|\xc2\x9c)"
    rb"|" + _OSC52_BODY + rb"\Z)"
)


def render(ring_bytes: bytes, evicted: bool = True) -> bytes:
    body = _trim(ring_bytes, evicted)
    return PREAMBLE + _QUERY_RE.sub(b"", _OSC52_RE.sub(b"", body))


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
