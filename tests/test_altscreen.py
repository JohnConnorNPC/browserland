"""Live DEC private-mode tracking off the PTY stream (#21 alt_screen, #23 DECCKM)."""

from __future__ import annotations

from webterm.agent.altscreen import DecModeSniffer


def _feed(*chunks):
    s = DecModeSniffer()
    for c in chunks:
        s.feed(c)
    return s


def test_alt_enter_and_exit():
    s = DecModeSniffer()
    s.feed(b"plain output")
    assert s.alt_screen is False
    s.feed(b"\x1b[?1049h")
    assert s.alt_screen is True
    s.feed(b"\x1b[?1049l")
    assert s.alt_screen is False


def test_alt_1047_and_47_variants():
    assert _feed(b"\x1b[?1047h").alt_screen is True
    assert _feed(b"\x1b[?47h").alt_screen is True
    assert _feed(b"\x1b[?47h", b"\x1b[?47l").alt_screen is False


def test_app_cursor_decckm():
    s = DecModeSniffer()
    assert s.app_cursor is False
    s.feed(b"\x1b[?1h")                 # DECCKM set
    assert s.app_cursor is True
    s.feed(b"\x1b[?1l")                 # DECCKM reset
    assert s.app_cursor is False


def test_bracketed_paste_set_and_reset():
    # #138: DECSET 2004 — snapshots re-assert it so a reloaded xterm recovers
    # the app's bracketed-paste request.
    s = DecModeSniffer()
    assert s.bracketed_paste is False
    s.feed(b"\x1b[?2004h")
    assert s.bracketed_paste is True
    s.feed(b"\x1b[?2004l")
    assert s.bracketed_paste is False


def test_bracketed_paste_combined_with_alt():
    # Shells commonly bundle modes: ?1049;2004h sets both in one sequence.
    s = _feed(b"\x1b[?1049;2004h")
    assert s.alt_screen is True and s.bracketed_paste is True
    s.feed(b"\x1b[?1049;2004l")
    assert s.alt_screen is False and s.bracketed_paste is False


def test_bracketed_paste_split_across_chunks():
    assert _feed(b"\x1b[?20", b"04h").bracketed_paste is True
    assert _feed(b"\x1b[?2004", b"h").bracketed_paste is True


def test_bracketed_paste_substring_not_false_positive():
    # 12004 / 20040 contain "2004" as a substring but are different modes.
    assert _feed(b"\x1b[?12004h").bracketed_paste is False
    assert _feed(b"\x1b[?20040h").bracketed_paste is False


def test_bracketed_paste_survives_later_output():
    s = _feed(b"\x1b[?2004h")
    for _ in range(50):
        s.feed(b"prompt output " * 100)
    assert s.bracketed_paste is True


def test_alt_and_app_cursor_independent():
    # A TUI commonly toggles both in one combined sequence.
    s = _feed(b"\x1b[?1049;1h")
    assert s.alt_screen is True and s.app_cursor is True
    s.feed(b"\x1b[?1049;1l")
    assert s.alt_screen is False and s.app_cursor is False


def test_split_across_chunks():
    assert _feed(b"\x1b[?10", b"49h").alt_screen is True
    assert _feed(b"\x1b[?", b"1h").app_cursor is True


def test_state_survives_later_output():
    # Once set, a mode stays set through later output (its set-sequence would
    # have scrolled out of a re-scanned ring).
    s = _feed(b"\x1b[?1049h")
    for _ in range(50):
        s.feed(b"\xe2\x94\x80" * 200)
    assert s.alt_screen is True


def test_last_toggle_wins_within_chunk():
    assert _feed(b"\x1b[?1049h..\x1b[?1049l").alt_screen is False
    assert _feed(b"\x1b[?1049l..\x1b[?1049h").alt_screen is True


def test_alt_last_toggle_wins_across_modes():
    # 1049h then 47l: alt-screen is ONE concept; the latest toggle of any alt
    # mode wins, so this is not-alt (not any()-stuck-true — #23 review).
    assert _feed(b"\x1b[?1049h", b"\x1b[?47l").alt_screen is False
    assert _feed(b"\x1b[?47h", b"\x1b[?1049l").alt_screen is False


def test_app_cursor_leading_zeros():
    # ?01h / ?0001h are numerically DECCKM (mode 1).
    assert _feed(b"\x1b[?01h").app_cursor is True
    assert _feed(b"\x1b[?0001h").app_cursor is True


def test_altscreen_alias_back_compat():
    # #21's AltScreenSniffer name still imports; its feed() returns the alt flag.
    from webterm.agent.altscreen import AltScreenSniffer
    s = AltScreenSniffer()
    assert s.feed(b"\x1b[?1049h") is True
    assert s.feed(b"plain") is True
    assert s.feed(b"\x1b[?1049l") is False


def test_non_alt_mode_leaves_state_untouched():
    s = _feed(b"\x1b[?1049h")
    s.feed(b"\x1b[?25l")               # cursor visibility — not alt, not DECCKM
    assert s.alt_screen is True and s.app_cursor is False


def test_substring_mode_is_not_a_false_positive():
    # "470"/"11049"/"10" contain mode digits as a substring but aren't the mode.
    assert _feed(b"\x1b[?470h").alt_screen is False
    assert _feed(b"\x1b[?11049h").alt_screen is False
    assert _feed(b"\x1b[?10h").app_cursor is False   # mode 10, not DECCKM 1


def test_empty_chunk_keeps_state():
    s = _feed(b"\x1b[?1049h")
    s.feed(b"")
    assert s.alt_screen is True
