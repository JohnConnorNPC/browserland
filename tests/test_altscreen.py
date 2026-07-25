"""Live DEC private-mode tracking off the PTY stream (#21 alt_screen, #23 DECCKM,
#138 bracketed paste, #154 mouse reporting)."""

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


# --------------------------------------------------------------------------- #
# mouse reporting (#154 track 1)
# --------------------------------------------------------------------------- #

def test_mouse_off_by_default():
    s = _feed(b"plain output")
    assert s.mouse_tracking == 0 and s.mouse_encoding == 0


def test_mouse_tracking_and_encoding_set_together():
    # The canonical modern TUI opener: button-drag tracking + SGR encoding in
    # one combined sequence. They are independent concepts and both must stick.
    s = _feed(b"\x1b[?1002;1006h")
    assert s.mouse_tracking == 1002
    assert s.mouse_encoding == 1006


def test_mouse_tracking_reset_leaves_encoding():
    # Clearing the tracking level says nothing about how a report would be
    # spelled — 1006 stays on until the app resets it.
    s = _feed(b"\x1b[?1002;1006h", b"\x1b[?1002l")
    assert s.mouse_tracking == 0
    assert s.mouse_encoding == 1006


def test_mouse_tracking_last_toggle_wins_across_levels():
    # 1000/1002/1003 are one mutually-exclusive concept: the newest wins, and a
    # reset of ANY of them clears the group (matching xterm.js's activeProtocol).
    assert _feed(b"\x1b[?1000h", b"\x1b[?1003h").mouse_tracking == 1003
    assert _feed(b"\x1b[?1003h", b"\x1b[?1000h").mouse_tracking == 1000
    assert _feed(b"\x1b[?1002h", b"\x1b[?1000l").mouse_tracking == 0


def test_mouse_combined_params_apply_in_order():
    # Within ONE sequence a real parser applies params left to right, so the
    # rightmost member of a group is the one that sticks.
    assert _feed(b"\x1b[?1000;1003h").mouse_tracking == 1003
    assert _feed(b"\x1b[?1003;1000h").mouse_tracking == 1000


def test_mouse_x10_and_sgr_pixels():
    assert _feed(b"\x1b[?9h").mouse_tracking == 9
    assert _feed(b"\x1b[?1016h").mouse_encoding == 1016
    # 1016 and 1006 are the same concept (extended encoding) — newest wins.
    assert _feed(b"\x1b[?1006h", b"\x1b[?1016h").mouse_encoding == 1016


def test_mouse_unsupported_encodings_ignored():
    # 1005/1015 are logged as unsupported by the vendored xterm.js; tracking
    # them would let the postamble promise an encoding the browser can't emit.
    assert _feed(b"\x1b[?1005h").mouse_encoding == 0
    assert _feed(b"\x1b[?1015h").mouse_encoding == 0


def test_mouse_split_across_chunks():
    assert _feed(b"\x1b[?10", b"02h").mouse_tracking == 1002
    assert _feed(b"\x1b[?1006", b"h").mouse_encoding == 1006


def test_mouse_substring_not_false_positive():
    # ?10062h / ?11002h contain the mode digits as a substring but aren't them.
    assert _feed(b"\x1b[?10062h").mouse_encoding == 0
    assert _feed(b"\x1b[?11002h").mouse_tracking == 0
    assert _feed(b"\x1b[?90h").mouse_tracking == 0


def test_mouse_survives_later_output():
    # The whole point: a repainting TUI streams for minutes after its DECSETs,
    # scrolling them out of the ring. Sniffer state must outlive that.
    s = _feed(b"\x1b[?1002;1006h")
    for _ in range(50):
        s.feed(b"\x1b[2Jrepaint " * 100)
    assert s.mouse_tracking == 1002 and s.mouse_encoding == 1006


def test_ris_clears_every_tracked_mode():
    # `reset` / `tput reset` emits RIS (ESC c). In the vendored xterm.js that
    # runs Terminal.reset() -> coreService.reset() + coreMouseService.reset(),
    # clearing everything tracked here. Missing it would let the next snapshot
    # postamble resurrect a mouse capture the user just reset away (#154).
    s = _feed(b"\x1b[?1049;1;2004;1002;1006h")
    s.feed(b"\x1bc")
    assert s.alt_screen is False and s.app_cursor is False
    assert s.bracketed_paste is False
    assert s.mouse_tracking == 0 and s.mouse_encoding == 0


def test_ris_respects_stream_order():
    # A RIS is a point in the stream, not a verdict on the whole chunk: modes
    # set AFTER it must survive, and modes set before it must not.
    s = _feed(b"\x1b[?1002h\x1bc\x1b[?1003;1006h")
    assert s.mouse_tracking == 1003 and s.mouse_encoding == 1006
    s2 = _feed(b"\x1b[?1003;1006h\x1bc\x1b[?1002h")
    assert s2.mouse_tracking == 1002 and s2.mouse_encoding == 0


def test_ris_split_across_chunks():
    s = _feed(b"\x1b[?1002h", b"\x1b", b"c")
    assert s.mouse_tracking == 0


def test_decstr_is_not_treated_as_a_reset():
    # xterm.js's softReset never touches the mouse service, so honouring
    # ESC[!p here would model a clear that does not actually happen.
    s = _feed(b"\x1b[?1002;1006h", b"\x1b[!p")
    assert s.mouse_tracking == 1002 and s.mouse_encoding == 1006


def test_esc_c_not_confused_with_csi_or_text():
    # Only a bare ESC c is RIS. A literal 'c' in output, or a CSI whose final
    # byte is 'c' (DA — ESC[c), must not clear state.
    s = _feed(b"\x1b[?1002h", b"cccc plain c text")
    assert s.mouse_tracking == 1002
    s.feed(b"\x1b[c")                 # Primary DA request, not RIS
    assert s.mouse_tracking == 1002


def test_chunk_splitting_never_changes_the_outcome():
    # The invariant the _tail carry exists to uphold: feeding a stream in ANY
    # split must land on the same state as feeding it whole. This got sharper
    # with #154 — RIS made the scan ORDER-sensitive, and the carry deliberately
    # re-scans an already-complete trailing sequence, so "replay is idempotent"
    # is now load-bearing rather than incidental.
    import random
    tokens = [b"\x1b[?1002h", b"\x1b[?1006h", b"\x1b[?1003h", b"\x1b[?1002l",
              b"\x1bc", b"plain", b"\x1b[?1049h", b"\x1b[?2004h", b"\x1b[?1h"]

    def state(s):
        return (s.alt_screen, s.app_cursor, s.bracketed_paste,
                s.mouse_tracking, s.mouse_encoding)

    rng = random.Random(1234)          # seeded: deterministic in CI
    seen_ris = 0
    for _ in range(2000):
        stream = b"".join(rng.choice(tokens)
                          for _ in range(rng.randint(1, 6)))
        seen_ris += b"\x1bc" in stream
        whole = DecModeSniffer()
        whole.feed(stream)
        cuts = sorted(rng.sample(range(len(stream) + 1), min(len(stream), 4)))
        split, prev = DecModeSniffer(), 0
        for c in list(cuts) + [len(stream)]:
            split.feed(stream[prev:c])
            prev = c
        assert state(whole) == state(split), (stream, cuts)
    assert seen_ris > 100        # the RIS path really was exercised


def test_mouse_independent_of_other_modes():
    s = _feed(b"\x1b[?1049;1;2004;1002;1006h")
    assert (s.alt_screen, s.app_cursor, s.bracketed_paste) == (True, True, True)
    assert s.mouse_tracking == 1002 and s.mouse_encoding == 1006
    s.feed(b"\x1b[?1002l")
    assert s.bracketed_paste is True and s.app_cursor is True
