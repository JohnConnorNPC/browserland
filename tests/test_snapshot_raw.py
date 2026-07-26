import pytest

from webterm.agent.snapshot import raw


def test_preamble_no_hard_reset():
    out = raw.render(b"hello", evicted=False)
    assert out.startswith(b"\x1b[0m\x1b[2J\x1b[H")
    assert b"\x1bc" not in out      # no RIS
    assert b"\x1b[3J" not in out    # no scrollback clear
    assert out.endswith(b"hello")


def test_not_evicted_keeps_everything():
    data = b"partial-line-without-newline"
    assert raw.render(data, evicted=False) == raw.PREAMBLE + data


def test_evicted_resyncs_at_first_escape():
    data = b"23;42Hgarbage\x1b[31mred"  # cut mid-CSI
    out = raw.render(data, evicted=True)
    assert out == raw.PREAMBLE + b"\x1b[31mred"


def test_evicted_resyncs_at_first_newline_when_earlier():
    data = b"tail of a line\nnext line"
    out = raw.render(data, evicted=True)
    assert out == raw.PREAMBLE + b"\nnext line"


def test_replays_from_last_clear_screen():
    data = b"old old old\x1b[2Jfresh screen"
    out = raw.render(data, evicted=True)
    assert out == raw.PREAMBLE + b"\x1b[2Jfresh screen"
    # Applies even without eviction — skips stale screens.
    assert raw.render(data, evicted=False) == raw.PREAMBLE + b"\x1b[2Jfresh screen"


def test_replays_from_alt_screen_entry():
    data = b"shell scrollback\x1b[?1049hvim screen"
    out = raw.render(data, evicted=True)
    assert out == raw.PREAMBLE + b"\x1b[?1049hvim screen"


def test_latest_restart_marker_wins():
    data = b"a\x1b[2Jb\x1b[?1049hc"
    assert raw.render(data, evicted=True) == raw.PREAMBLE + b"\x1b[?1049hc"
    data2 = b"a\x1b[?1049hb\x1b[2Jc"
    assert raw.render(data2, evicted=True) == raw.PREAMBLE + b"\x1b[2Jc"


def test_no_resync_candidates_keeps_data():
    data = b"no escapes or newlines here"
    assert raw.render(data, evicted=True) == raw.PREAMBLE + data


def test_terminal_queries_stripped_from_replay():
    # DA1, DA2, DSR and CPR requests must not be replayed — an attaching
    # xterm.js would answer them, typing junk into the shell.
    data = (b"\x1b[c\x1b[0c\x1b[>c\x1b[5n\x1b[6n\x1b[?6n"
            b"prompt\x1b[31m red\x1b[0m\x1b[2;1Hline2")
    out = raw.render(data, evicted=False)
    assert out == raw.PREAMBLE + b"prompt\x1b[31m red\x1b[0m\x1b[2;1Hline2"


def test_query_strip_keeps_sgr_and_cursor_sequences():
    data = b"\x1b[1;31mbold red\x1b[0m\x1b[10;20H\x1b[?25l\x1b[?1049h"
    out = raw.render(data, evicted=False)
    # 'm', 'H', 'l', 'h' finals are untouched; trim starts at ?1049h marker.
    assert out == raw.PREAMBLE + b"\x1b[?1049h"
    no_marker = b"\x1b[1;31mbold red\x1b[0m\x1b[10;20H\x1b[?25l"
    assert raw.render(no_marker, evicted=False) == raw.PREAMBLE + no_marker


# ---- OSC 52 clipboard strip (#153) -----------------------------------------
# The ring is replayed on every attach, to every attached browser. A clipboard
# request emitted once must not be re-delivered to browsers that were not there.


def test_osc52_stripped_from_replay():
    data = b"before\x1b]52;c;aGVsbG8=\x07after"
    assert raw.render(data, evicted=False) == raw.PREAMBLE + b"beforeafter"


def test_osc52_leading_zero_form_stripped():
    # Leading zeros are legal in an OSC parameter: 052 IS 52.
    data = b"a\x1b]052;c;aGVsbG8=\x07b"
    assert raw.render(data, evicted=False) == raw.PREAMBLE + b"ab"


def test_osc52_c1_introducer_stripped():
    # The C1 OSC as xterm.js actually sees it: UTF-8 encoded U+009D.
    data = b"a\xc2\x9d52;c;aGVsbG8=\x07b"
    assert raw.render(data, evicted=False) == raw.PREAMBLE + b"ab"


def test_osc52_st_terminators_stripped():
    # ESC \ (7-bit ST) and the C1 ST as UTF-8 (U+009C) both end the string.
    st7 = b"a\x1b]52;c;aGVsbG8=\x1b\\b"
    assert raw.render(st7, evicted=False) == raw.PREAMBLE + b"ab"
    st8 = b"a\x1b]52;c;aGVsbG8=\xc2\x9cb"
    assert raw.render(st8, evicted=False) == raw.PREAMBLE + b"ab"


def test_osc52_empty_pc_form_stripped():
    # What tmux actually emits on a copy-mode copy: Pc is EMPTY.
    data = b"x\x1b]52;;Q09QWQ==\x07y"
    assert raw.render(data, evicted=False) == raw.PREAMBLE + b"xy"


def test_osc52_wrapped_base64_payload_stripped_whole():
    # base64(1) wraps at 76 columns; a newline inside the payload must not
    # leave the tail behind as visible junk.
    data = b"x\x1b]52;c;QUFB\nQkJC\nQ0ND\x07y"
    assert raw.render(data, evicted=False) == raw.PREAMBLE + b"xy"


def test_osc52_unterminated_at_ring_tail_stripped():
    # Cut mid-write at the newest end of the ring: nothing follows it, so
    # removing it cannot eat anything.
    data = b"visible output\x1b]52;c;QUFBQQ"
    assert raw.render(data, evicted=False) == raw.PREAMBLE + b"visible output"


def test_osc52_unterminated_absorbs_trailing_plain_text():
    # Plain text after an unterminated sequence is still PAYLOAD: xterm.js is
    # inside the OSC string and swallows it, so it was never on screen live
    # either. Removing it reproduces the live rendering rather than changing it.
    data = b"\x1b]52;c;QUFBQQ" + b"later output"
    assert raw.render(data, evicted=False) == raw.PREAMBLE + b""


def test_osc52_unterminated_never_eats_a_following_escape_sequence():
    # The case the bound exists for. An ESC after a truncated sequence ABORTS
    # the OSC in xterm's parser, so everything from there really is on screen —
    # a greedy "terminator optional" match would have eaten it.
    data = b"\x1b]52;c;QUFBQQ" + b"\x1b[31mred\x1b[0m rest"
    out = raw.render(data, evicted=False)
    assert out == raw.PREAMBLE + data
    assert b"\x1b[31mred" in out and b"rest" in out


def test_osc52_aborted_sequence_is_not_matched():
    # CAN (0x18) aborts an OSC string, so this one can never reach a
    # clipboard. Leaving it is correct — and the ABORTED text is what a live
    # terminal renders, so the replay matches what the user saw.
    data = b"\x1b]52;c;QUFB\x18tail\x07"
    assert raw.render(data, evicted=False) == raw.PREAMBLE + data


def test_osc52_strip_leaves_ordinary_output_untouched():
    # The negative case: nothing resembling OSC 52 is disturbed. Includes an
    # OSC 0 title (which the title sniffer owns) and a literal "52;" in text.
    data = (b"\x1b]0;my title\x07"
            b"exit code 52; retrying\r\n"
            b"\x1b[1;32mok\x1b[0m\n"
            b"\x1b]8;;https://example.com/x\x07link\x1b]8;;\x07")
    assert raw.render(data, evicted=False) == raw.PREAMBLE + data


def test_osc52_strip_runs_after_trim_and_with_query_strip():
    # All three passes compose: trim to the last clear, drop the DA request,
    # drop the clipboard write.
    data = (b"stale\x1b[2Jfresh \x1b[c"
            b"\x1b]52;c;aGVsbG8=\x07"
            b"\x1b[31mred\x1b[0m")
    out = raw.render(data, evicted=True)
    assert out == raw.PREAMBLE + b"\x1b[2Jfresh \x1b[31mred\x1b[0m"


def test_pyte_snapshot_mode_needs_no_strip_of_its_own():
    """Only the RAW mode replays bytes, and it is the default. The tier-2 pyte
    renderer feeds the ring to a screen model and emits the settled GRID, so an
    OSC 52 in the ring is consumed by the model and can never be re-emitted --
    which is why the strip lives in raw.py alone rather than in both."""
    pyte = pytest.importorskip("pyte")
    from webterm.agent.snapshot import pyte_snap

    out = pyte_snap.render(b"hello\x1b]52;c;aGVsbG8=\x07world", 40, 4)
    assert b"]52;" not in out
    assert b"aGVsbG8=" not in out
    assert b"hello" in out and b"world" in out


def test_osc52_repeated_c1_introducers_do_not_blow_up():
    # Runtime bound, not correctness: a ring packed with C1 introducers must
    # not make the scan quadratic. 256 KiB is a full ring.
    data = b"\xc2\x9d52;" * 40000
    out = raw.render(data, evicted=False)
    # Every one of them is unterminated and followed by another introducer,
    # so only the last (which runs to the end) is removable.
    assert out.startswith(raw.PREAMBLE)
    assert len(out) < len(raw.PREAMBLE) + len(data)
