"""Incremental OSC 0/2 title sniffer.

Watches the raw PTY output stream for ``ESC ] 0;payload BEL``,
``ESC ] 2;payload BEL`` and their ``ESC \\`` (ST) terminated forms, and
reports title changes. The parser is a byte-at-a-time state machine so every
state survives chunk boundaries — including a chunk that ends in a bare
``ESC`` inside the payload, which the next chunk may turn into ST (emit) or
anything else (abort).

Non-title OSC sequences (param != 0/2) are consumed to their terminator
without capture so their payloads can't be mis-scanned. Payloads are capped
at 4 KiB; on overflow the sequence is abandoned back to GROUND.
"""

from __future__ import annotations

from typing import Optional

_GROUND = 0
_ESC = 1          # saw ESC
_OSC_NUM = 2      # saw ESC ] — accumulating numeric param
_OSC_STRING = 3   # in payload (capturing or ignoring per self._capture)
_OSC_STRING_ESC = 4  # saw ESC inside payload — next byte decides ST vs abort

_BEL = 0x07
_ESC_B = 0x1B
_MAX_PAYLOAD = 4096
_MAX_PARAM_DIGITS = 16


class OscTitleSniffer:
    def __init__(self) -> None:
        self._state = _GROUND
        self._param = bytearray()
        self._payload = bytearray()
        self._capture = False
        self.title: Optional[str] = None  # last emitted title

    def feed(self, data: bytes) -> Optional[str]:
        """Feed a chunk; return the new title if it changed (the most recent
        change wins when one chunk carries several), else None."""
        changed: Optional[str] = None
        for b in data:
            emitted = self._feed_byte(b)
            if emitted is not None:
                changed = emitted
        return changed

    def _feed_byte(self, b: int) -> Optional[str]:
        state = self._state

        if state == _GROUND:
            if b == _ESC_B:
                self._state = _ESC
            return None

        if state == _ESC:
            if b == 0x5D:  # ']'
                self._state = _OSC_NUM
                self._param.clear()
            elif b == _ESC_B:
                pass  # ESC ESC: the second ESC is still a pending escape
            else:
                self._state = _GROUND
            return None

        if state == _OSC_NUM:
            if 0x30 <= b <= 0x39:  # digit
                if len(self._param) < _MAX_PARAM_DIGITS:
                    self._param.append(b)
                else:
                    self._state = _GROUND
                return None
            if b == 0x3B:  # ';' — param complete, payload starts
                self._capture = bytes(self._param) in (b"0", b"2")
                self._payload.clear()
                self._state = _OSC_STRING
                return None
            if b == _ESC_B:
                # Malformed OSC; the ESC may start a new sequence.
                self._state = _ESC
                return None
            # BEL terminates a payload-less OSC; anything else is a sequence
            # we don't model (e.g. ESC ] P palette) — bail to GROUND.
            self._state = _GROUND
            return None

        if state == _OSC_STRING:
            if b == _BEL:
                self._state = _GROUND
                return self._finish()
            if b == _ESC_B:
                self._state = _OSC_STRING_ESC
                return None
            # Ignored payloads accumulate too, purely to enforce the cap.
            if len(self._payload) >= _MAX_PAYLOAD:
                self._state = _GROUND  # runaway payload: abandon
            else:
                self._payload.append(b)
            return None

        if state == _OSC_STRING_ESC:
            if b == 0x5C:  # '\' — ESC \ is ST, the sequence terminator
                self._state = _GROUND
                return self._finish()
            # The ESC aborted the OSC and starts something new.
            if b == 0x5D:  # ']' — immediately a fresh OSC
                self._state = _OSC_NUM
                self._param.clear()
            elif b == _ESC_B:
                self._state = _ESC  # the new ESC is itself a pending escape
            else:
                self._state = _GROUND
            return None

        return None

    def _finish(self) -> Optional[str]:
        if not self._capture:
            return None
        new_title = self._payload.decode("utf-8", errors="replace")
        if new_title == self.title:
            return None
        self.title = new_title
        return new_title
