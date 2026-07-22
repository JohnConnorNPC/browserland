"""Audit committed session recordings for secrets (#145).

A ``.blrec`` captures the terminal's output stream byte-for-byte, so anything
the terminal ECHOED is in it: a secret pasted onto a visible command line, an
API key printed by a config dump, or the broker's own token if someone ran
``--print-token`` while recording. Recordings are durable (nothing sweeps them)
and downloadable, so "is there a secret in this file?" needs an answer.

**The trap this module exists for.** Output payloads are base64, so the obvious
audit --

    grep "$(python -m webterm.broker --print-token)" webterm_recordings/*.blrec

-- finds nothing even when the token IS in the recording, and reports a
confident all-clear. Every search here decodes ``d`` first.

Format (newline-delimited JSON): line 1 is meta, then one event per line.
``{"t":ms,"k":"o","d":"<base64 output>"}`` carries content; ``k:"i"`` is an
input MARKER (a byte count, never the keystrokes), ``k:"r"`` a resize, ``k:"g"``
a connection gap.

Read-only by design. A recording is an archived artifact and this never rewrites
one -- it tells you which file to look at (or delete), and where in the
playback.

## What this can and cannot find

FINDS a secret that reaches the terminal as contiguous output bytes -- which is
the common case, because a program printing a token writes it in one go. A
line wrap does NOT break that: wrapping is something the terminal does when
RENDERING, so the byte stream still holds the secret unbroken.

MISSES a secret that is broken up in the byte stream itself: interleaved with
ANSI escapes (a shell redrawing a long line as you type), typed one character
at a time with cursor moves between, or split by the remote end. So a clean
result is evidence, NOT proof -- ``scan_file`` says so in its return value and
the CLI says so in its output.
"""

from __future__ import annotations

import base64
import binascii
import json
import os
from pathlib import Path
from typing import Dict, Iterable, Iterator, List, NamedTuple, Optional, Tuple


class Finding(NamedTuple):
    """One secret occurrence, located for a human."""
    path: Path
    label: str            # which secret matched ("auth_token", ...)
    event_index: int      # 0-based index among EVENTS (meta is not an event)
    t_ms: int             # offset into the recording, for scrubbing to it
    spans_events: bool    # the match straddled an event boundary

    @property
    def clock(self) -> str:
        """mm:ss into the recording -- what the player's scrubber shows."""
        s, ms = divmod(max(0, self.t_ms), 1000)
        return f"{s // 60}:{s % 60:02d}"


class _Window:
    """Byte window that can find a needle straddling two events.

    PTY output arrives in arbitrary chunks, so a secret CAN be split across two
    (or more) ``k:"o"`` events -- and a per-event scan would miss it and report
    a false all-clear, which is exactly the failure this module exists to
    prevent. So chunks are searched joined, with enough of the tail retained to
    cover any needle overlapping the seam.

    Invariant: after ``feed``, ``buf`` ends with the newest chunk and begins at
    most ``keep`` bytes earlier, where ``keep >= longest_needle - 1``. That is
    the minimum that cannot cut a needle in half: a needle overlapping the seam
    has at most ``len-1`` bytes on the old side. Matches are reported only when
    they END inside the new chunk, so each occurrence is reported exactly once
    however many chunks it spans.
    """

    def __init__(self, keep: int) -> None:
        self.keep = max(0, keep)
        self.buf = b""
        self.base = 0                                  # abs offset of buf[0]
        self.marks: List[Tuple[int, int, int]] = []    # (abs_start, ev_idx, t)

    def feed(self, data: bytes, ev_index: int, t_ms: int) -> int:
        """Append a chunk; returns the absolute offset where it starts."""
        start = self.base + len(self.buf)
        self.marks.append((start, ev_index, t_ms))
        self.buf += data
        return start

    def locate(self, abs_offset: int) -> Tuple[int, int]:
        """(event_index, t_ms) of the chunk containing an absolute offset."""
        found = self.marks[0]
        for m in self.marks:
            if m[0] <= abs_offset:
                found = m
            else:
                break
        return found[1], found[2]

    def trim(self) -> None:
        """Drop everything older than the retention window, and the marks that
        can no longer be asked about."""
        excess = len(self.buf) - self.keep
        if excess <= 0:
            return
        self.buf = self.buf[excess:]
        self.base += excess
        # Keep every mark at/after the new base, plus the one covering it (an
        # offset inside a partially-dropped chunk still belongs to that event).
        kept: List[Tuple[int, int, int]] = []
        covering = None
        for m in self.marks:
            if m[0] <= self.base:
                covering = m
            else:
                kept.append(m)
        self.marks = ([covering] if covering else []) + kept


def iter_events(path: Path) -> Iterator[Tuple[int, dict]]:
    """(event_index, event) for each parseable event line, streaming.

    Line 1 is meta and is NOT an event, matching the player's parseRecording.
    Unparseable lines are skipped rather than fatal -- a truncated recording
    should still be auditable, and the player skips them too."""
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        index = -1
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except (ValueError, UnicodeDecodeError):
                continue
            if not isinstance(obj, dict):
                continue
            index += 1
            if index == 0:
                continue                                # meta
            yield index - 1, obj


#: Meta is reported with this pseudo-index: it is line 1, not an event.
META_INDEX = -1


class ScanResult(NamedTuple):
    findings: List[Finding]
    errors: List[str]      # per-file problems; a non-empty list means the
                           # audit was INCOMPLETE, which must not read as clean


def scan_file(path: Path, secrets: Dict[str, str]) -> ScanResult:
    """Every occurrence of any secret in one recording.

    ``secrets`` maps a label ("auth_token") to the literal value. Values are
    compared as UTF-8 bytes against the DECODED output stream.

    Never raises on a damaged recording: durable archives accumulate partial
    writes, and one corrupt file must not abort the audit of the rest. Problems
    come back in ``errors`` so the caller can exit non-zero for "incomplete"
    rather than reporting a clean bill of health."""
    needles = {label: value.encode("utf-8", "replace")
               for label, value in secrets.items() if value}
    if not needles:
        return ScanResult([], [f"{path}: no secrets to search for"])
    longest = max(len(n) for n in needles.values())
    window = _Window(keep=longest - 1)
    findings: List[Finding] = []
    errors: List[str] = []
    seen: set = set()

    def _scan_plain(blob: bytes, ev_index: int, t_ms: int) -> None:
        """Literal search over text that is NOT the base64 output stream."""
        for label, needle in needles.items():
            if needle in blob:
                key = (label, "plain", ev_index)
                if key not in seen:
                    seen.add(key)
                    findings.append(Finding(path, label, ev_index, t_ms, False))

    # Meta (line 1) is scanned: `title` is the terminal title, which many
    # shells set to the running command line -- a plausible resting place for a
    # pasted secret.
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            _scan_plain(fh.readline().encode("utf-8", "replace"), META_INDEX, 0)
    except OSError as exc:
        return ScanResult(findings, [f"{path}: unreadable ({exc})"])

    bad_b64 = 0
    try:
        for ev_index, ev in iter_events(path):
            t_ms = _int(ev.get("t"))
            if ev.get("k") != "o" or not isinstance(ev.get("d"), str):
                # Not an output payload -- but scan any OTHER string field, so
                # a future event kind that carries text can't become a silent
                # blind spot. `d` is excluded here; it is decoded below.
                for key, val in ev.items():
                    if key != "d" and isinstance(val, str) and val:
                        _scan_plain(val.encode("utf-8", "replace"),
                                    ev_index, t_ms)
                continue
            try:
                chunk = base64.b64decode(ev["d"], validate=False)
            except (binascii.Error, ValueError):
                bad_b64 += 1
                continue
            if not chunk:
                continue
            chunk_start = window.feed(chunk, ev_index, t_ms)
            for label, needle in needles.items():
                pos = window.buf.find(needle)
                while pos != -1:
                    abs_start = window.base + pos
                    # Report only matches ENDING in the chunk just fed, so an
                    # occurrence still sitting in the window isn't re-counted.
                    if abs_start + len(needle) > chunk_start:
                        ev_idx, at = window.locate(abs_start)
                        key = (label, abs_start)
                        if key not in seen:
                            seen.add(key)
                            findings.append(Finding(
                                path, label, ev_idx, at,
                                spans_events=abs_start < chunk_start))
                    pos = window.buf.find(needle, pos + 1)
            window.trim()
    except OSError as exc:
        errors.append(f"{path}: read failed partway ({exc})")
    if bad_b64:
        errors.append(f"{path}: {bad_b64} output event(s) had undecodable "
                      f"base64 and could NOT be scanned")
    return ScanResult(findings, errors)


def scan_dir(directory: Path, secrets: Dict[str, str]) -> ScanResult:
    """Scan every .blrec in a directory. Ids sort by date, so name order is
    chronological."""
    findings: List[Finding] = []
    errors: List[str] = []
    if not directory.is_dir():
        return ScanResult(findings, [f"{directory}: not a directory"])
    for path in sorted(directory.glob("*.blrec")):
        res = scan_file(path, secrets)
        findings.extend(res.findings)
        errors.extend(res.errors)
    return ScanResult(findings, errors)


def _int(value, default: int = 0) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) \
        else default
