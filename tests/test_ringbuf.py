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
