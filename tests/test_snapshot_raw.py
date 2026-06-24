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
