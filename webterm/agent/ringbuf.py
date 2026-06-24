"""Byte-capped ring buffer of PTY output chunks.

Backs tier-1 snapshots: on ``snapshot_please`` the agent replays the ring
after a clear-screen preamble. Whole chunks are evicted from the front when
the cap is exceeded — chunk granularity keeps append O(1) and is fine for
snapshot purposes (the trim heuristic in snapshot/raw.py deals with a cut
landing mid-escape-sequence).
"""

from __future__ import annotations

from collections import deque


class ByteRing:
    def __init__(self, capacity: int = 256 * 1024):
        if capacity <= 0:
            raise ValueError("capacity must be positive")
        self.capacity = capacity
        self._chunks: deque = deque()
        self._size = 0
        # True once any chunk has been evicted — snapshot rendering uses this
        # to decide whether the front of the ring may start mid-sequence.
        self.evicted = False

    def append(self, chunk: bytes) -> None:
        if not chunk:
            return
        self._chunks.append(bytes(chunk))
        self._size += len(chunk)
        # Never evict the newest chunk, even if it alone exceeds capacity —
        # an empty ring would make snapshots blank, which is strictly worse
        # than a briefly-oversized ring.
        while self._size > self.capacity and len(self._chunks) > 1:
            old = self._chunks.popleft()
            self._size -= len(old)
            self.evicted = True

    def get(self) -> bytes:
        return b"".join(self._chunks)

    def clear(self) -> None:
        self._chunks.clear()
        self._size = 0
        self.evicted = False

    def __len__(self) -> int:
        return self._size
