"""Live alternate-screen tracking off the PTY stream (issue #21)."""

from __future__ import annotations

from webterm.agent.altscreen import AltScreenSniffer


def test_enter_and_exit():
    s = AltScreenSniffer()
    assert s.feed(b"plain output") is False
    assert s.feed(b"\x1b[?1049h") is True       # enter alt
    assert s.alt_screen is True
    assert s.feed(b"\x1b[?1049l") is False      # leave alt
    assert s.alt_screen is False


def test_1047_and_47_variants():
    assert AltScreenSniffer().feed(b"\x1b[?1047h") is True
    assert AltScreenSniffer().feed(b"\x1b[?47h") is True
    s = AltScreenSniffer()
    s.feed(b"\x1b[?47h")
    assert s.feed(b"\x1b[?47l") is False


def test_split_across_chunks():
    s = AltScreenSniffer()
    assert s.feed(b"\x1b[?10") is False         # marker split mid-sequence
    assert s.feed(b"49h") is True               # completes -> enter alt


def test_state_survives_later_output():
    # The whole point of live tracking: once in alt, it stays alt through later
    # output (the enter would have scrolled out of a re-scanned ring).
    s = AltScreenSniffer()
    s.feed(b"\x1b[?1049h")
    for _ in range(50):
        assert s.feed(b"\xe2\x94\x80" * 200) is True   # box-drawing churn
    assert s.alt_screen is True


def test_last_toggle_wins_within_chunk():
    s = AltScreenSniffer()
    # enter then exit in one chunk -> ends not-alt
    assert s.feed(b"\x1b[?1049h...stuff...\x1b[?1049l") is False
    # exit then enter -> ends alt
    assert s.feed(b"\x1b[?1049l...\x1b[?1049h") is True


def test_empty_chunk_keeps_state():
    s = AltScreenSniffer()
    s.feed(b"\x1b[?1049h")
    assert s.feed(b"") is True


def test_multi_param_dec_sequence():
    # combined toggle (alt-buffer + cursor) must still register as alt entry
    s = AltScreenSniffer()
    assert s.feed(b"\x1b[?1049;25h") is True
    # a non-alt private mode leaves alt state untouched
    assert s.feed(b"\x1b[?25l") is True
    assert s.feed(b"\x1b[?2004h") is True
    # combined alt exit
    assert s.feed(b"\x1b[?1049;25l") is False


def test_substring_mode_is_not_a_false_positive():
    # "470" / "11049" contain alt-mode digits as a substring but are NOT alt.
    s = AltScreenSniffer()
    assert s.feed(b"\x1b[?470h") is False
    assert s.feed(b"\x1b[?11049h") is False
