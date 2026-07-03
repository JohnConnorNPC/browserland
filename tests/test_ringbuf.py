from webterm.agent.ringbuf import ByteRing


def test_append_and_get():
    ring = ByteRing(100)
    ring.append(b"abc")
    ring.append(b"def")
    assert ring.get() == b"abcdef"
    assert len(ring) == 6
    assert ring.evicted is False


def test_chunk_granular_eviction():
    ring = ByteRing(10)
    ring.append(b"aaaa")   # 4
    ring.append(b"bbbb")   # 8
    ring.append(b"cccc")   # 12 -> evict "aaaa" whole, not byte-by-byte
    assert ring.get() == b"bbbbcccc"
    assert len(ring) == 8
    assert ring.evicted is True


def test_never_evicts_newest_chunk():
    ring = ByteRing(10)
    ring.append(b"x" * 50)  # single oversized chunk must survive
    assert ring.get() == b"x" * 50
    ring.append(b"y")       # now the oversized one can go
    assert ring.get() == b"y"


def test_empty_append_is_noop():
    ring = ByteRing(10)
    ring.append(b"")
    assert ring.get() == b""
    assert len(ring) == 0


def test_clear():
    ring = ByteRing(4)
    ring.append(b"aaaa")
    ring.append(b"bb")
    assert ring.evicted is True
    ring.clear()
    assert ring.get() == b""
    assert ring.evicted is False


# ---- #130: total_appended monotonic counter for keyframe reconstruction -----

def test_total_appended_monotonic_and_survives_eviction():
    ring = ByteRing(10)
    assert ring.total_appended == 0
    ring.append(b"aaaa")   # 4
    ring.append(b"bbbb")   # 8
    ring.append(b"cccc")   # 12 -> evicts "aaaa", but the counter keeps climbing
    assert ring.evicted is True
    assert ring.total_appended == 12          # NOT reduced by eviction
    assert len(ring) == 8                      # surviving bytes


def test_evicted_total_invariant_lands_on_chunk_boundary():
    # evicted_total = total_appended - len(ring) is exactly the count of bytes
    # dropped from the front, and it always equals a sum of whole evicted chunk
    # lengths (so a keyframe offset K - evicted_total lands on a chunk boundary).
    ring = ByteRing(10)
    for chunk in (b"aaaa", b"bbbb", b"cccc", b"dddd"):
        ring.append(chunk)
    evicted_total = ring.total_appended - len(ring)
    assert evicted_total == 8                  # "aaaa"+"bbbb" dropped whole
    assert ring.get() == b"ccccdddd"


def test_total_appended_resets_on_clear():
    ring = ByteRing(100)
    ring.append(b"hello")
    assert ring.total_appended == 5
    ring.clear()
    assert ring.total_appended == 0
    ring.append(b"x")
    assert ring.total_appended == 1


def test_empty_append_does_not_advance_counter():
    ring = ByteRing(10)
    ring.append(b"")
    assert ring.total_appended == 0
