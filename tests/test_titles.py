import pytest

from webterm.agent.titles import OscTitleSniffer

BEL_SEQ = b"before\x1b]0;hello world\x07after"
ST_SEQ = b"pre\x1b]2;another title\x1b\\post"


@pytest.mark.parametrize("split", range(len(BEL_SEQ) + 1))
def test_bel_terminated_survives_any_split(split):
    sniffer = OscTitleSniffer()
    sniffer.feed(BEL_SEQ[:split])
    sniffer.feed(BEL_SEQ[split:])
    assert sniffer.title == "hello world"


@pytest.mark.parametrize("split", range(len(ST_SEQ) + 1))
def test_st_terminated_survives_any_split(split):
    """Covers the chunk ending in a bare ESC that the next chunk turns
    into ST."""
    sniffer = OscTitleSniffer()
    sniffer.feed(ST_SEQ[:split])
    sniffer.feed(ST_SEQ[split:])
    assert sniffer.title == "another title"


def test_byte_at_a_time():
    sniffer = OscTitleSniffer()
    for i in range(len(BEL_SEQ)):
        sniffer.feed(BEL_SEQ[i:i + 1])
    assert sniffer.title == "hello world"


def test_osc2_and_osc0_both_match():
    s0 = OscTitleSniffer()
    assert s0.feed(b"\x1b]0;zero\x07") == "zero"
    s2 = OscTitleSniffer()
    assert s2.feed(b"\x1b]2;two\x07") == "two"


def test_non_title_osc_payload_not_captured():
    sniffer = OscTitleSniffer()
    # OSC 52 (clipboard) contains arbitrary payload — must be consumed
    # without emitting, and without mis-scanning its payload.
    assert sniffer.feed(b"\x1b]52;c;aGVsbG8=\x07") is None
    assert sniffer.title is None
    # Sniffer still works afterwards.
    assert sniffer.feed(b"\x1b]0;next\x07") == "next"


def test_emit_only_on_change():
    sniffer = OscTitleSniffer()
    assert sniffer.feed(b"\x1b]0;same\x07") == "same"
    assert sniffer.feed(b"\x1b]0;same\x07") is None
    assert sniffer.feed(b"\x1b]0;diff\x07") == "diff"


def test_overflow_abandons_to_ground():
    sniffer = OscTitleSniffer()
    huge = b"\x1b]0;" + b"x" * 5000  # > 4 KiB cap, never terminated
    assert sniffer.feed(huge) is None
    assert sniffer.title is None
    # Ground state recovered: a later title still parses.
    assert sniffer.feed(b"\x07\x1b]0;ok\x07") == "ok"


def test_esc_abort_inside_payload():
    sniffer = OscTitleSniffer()
    # ESC followed by something that is not '\' aborts the OSC.
    assert sniffer.feed(b"\x1b]0;junk\x1bXrest") is None
    assert sniffer.title is None
    assert sniffer.feed(b"\x1b]0;good\x07") == "good"


def test_esc_abort_then_immediate_new_osc():
    sniffer = OscTitleSniffer()
    assert sniffer.feed(b"\x1b]0;junk\x1b]0;real\x07") == "real"


def test_double_esc_before_bracket():
    sniffer = OscTitleSniffer()
    assert sniffer.feed(b"\x1b\x1b]0;t\x07") == "t"


def test_utf8_payload_split_mid_codepoint():
    payload = "café ☃".encode("utf-8")
    seq = b"\x1b]0;" + payload + b"\x07"
    for split in range(len(seq) + 1):
        sniffer = OscTitleSniffer()
        sniffer.feed(seq[:split])
        sniffer.feed(seq[split:])
        assert sniffer.title == "café ☃", f"split={split}"


def test_csi_and_plain_text_ignored():
    sniffer = OscTitleSniffer()
    assert sniffer.feed(b"plain \x1b[31mred\x1b[0m text\r\n") is None
    assert sniffer.title is None
