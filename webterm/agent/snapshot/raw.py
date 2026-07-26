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

Only the RAW mode needs this. The tier-2 pyte renderer feeds the ring to a
screen model and emits the settled grid, so an OSC 52 there is consumed by the
model and can never be re-emitted (pinned by a test).
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
# The payload class excludes the two bytes that ABORT an OSC string in xterm's
# parser — CAN (0x18) and SUB (0x1a) — plus the bytes that END one. Excluding
# the aborters matters: a sequence containing one can never reach the clipboard
# (``end()`` is called with success=False), so failing to match it is CORRECT
# rather than a miss. Newlines are allowed through, so a payload from
# ``base64``'s 76-column line wrapping is removed whole instead of leaving its
# tail as visible junk.
#
# ``\xc2`` is excluded too, and that exclusion is a RUNTIME bound, not a
# grammar detail: every C1 introducer starts with ``\xc2``, so a ring packed
# with back-to-back ``\xc2\x9d52;`` introducers cannot make each match attempt
# scan to the end of the buffer. (The ``ESC ]`` form is already self-limiting,
# because ESC is excluded.) The cost, stated rather than hidden: a NON-ASCII
# byte planted inside ``Pc`` stops the body class and the sequence survives the
# strip. Nothing legal is lost — Pc is drawn from ``c s p q 0-7`` and the
# payload is base64 — and the browser rejects a malformed Pc outright, so what
# survives here is a sequence the browser will refuse anyway.
#
# THE TERMINATOR SET IS WIDER THAN ST. Read out of the vendored parser table
# rather than assumed: ``addMany([156,27,24,26,7], 8, 6, 0)`` routes BEL, ESC,
# CAN, SUB and C1-ST out of OSC_STRING, and the OSC_END action is
# ``end(24!==r && 26!==r)`` — so a BARE ESC ends the string with **success**
# and the registered handler RUNS. An OSC 52 followed by any escape sequence is
# therefore a completed clipboard write, not the harmless truncation it looks
# like. It is matched with a zero-width lookahead, never by consuming the ESC:
# the same byte re-enters the parser as the start of the next sequence
# (``27===r && (n|=1)``), so eating it would destroy real output.
#
# ``\Z`` closes the last case — a sequence cut off at the newest end of the
# ring, where the payload simply runs out of buffer.
#
# The bound is set from what the BROWSER will accept (1 MiB decoded, ~1.34 MiB
# of base64), not from the ring size, which is a CLI knob (``--ring-bytes``)
# and can be raised past any constant written here. Anything longer than this
# is left in the replay and then refused browser-side as over-cap, so the two
# ends agree without this file having to know the ring's size.
_OSC52_MAX_PAYLOAD = 2 * 1024 * 1024
_OSC52_RE = re.compile(
    rb"(?:\x1b\]|\xc2\x9d)0*52;"
    rb"[^\x1b\x07\x18\x1a\xc2]{0,%d}"
    rb"(?:\x07|\x1b\\|\xc2\x9c|(?=\x1b)|\Z)" % _OSC52_MAX_PAYLOAD
)

#: Deleting a span SPLICES its neighbours together, and the join can spell a
#: sequence that was not there before — ``ESC ] 5`` + <removed OSC 52> +
#: ``2;c;…BEL`` becomes a brand-new ``ESC ] 5 2 ; c ; … BEL``. re.sub is a
#: single left-to-right pass over the ORIGINAL bytes and never revisits a join,
#: and the two patterns can feed each other in either direction (removing a DA
#: query can assemble an OSC 52; removing an OSC 52 can assemble a DA query).
#: So both run to a FIXPOINT instead of once. Each round strictly shortens the
#: data, so this terminates; the cap is a backstop whose only failure mode is
#: the accepted one — a sequence left in the replay for the browser gates.
_STRIP_ROUNDS = 8


def render(ring_bytes: bytes, evicted: bool = True) -> bytes:
    body = _trim(ring_bytes, evicted)
    for _ in range(_STRIP_ROUNDS):
        stripped = _OSC52_RE.sub(b"", _QUERY_RE.sub(b"", body))
        if stripped == body:
            break
        body = stripped
    return PREAMBLE + body


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
