"""Tier-2 (pyte) snapshot renderer tests. Skipped when pyte is missing —
raw mode must keep working without it (checked in test_integration)."""

from __future__ import annotations

import pytest

pyte = pytest.importorskip("pyte")

from webterm.agent.snapshot import pyte_snap


def _rerender(payload: bytes, cols: int, rows: int):
    """Feed a rendered snapshot into a fresh pyte screen — the snapshot must
    reproduce the grid (idempotent one-screen redraw)."""
    screen = pyte.Screen(cols, rows)
    stream = pyte.ByteStream(screen)
    stream.feed(payload)
    return screen


def test_preamble_and_postamble():
    out = pyte_snap.render(b"hello", 20, 5)
    assert out.startswith(b"\x1b[0m\x1b[2J\x1b[H")
    assert b"\x1bc" not in out
    # Cursor lands after "hello": row 1, col 6 (1-based).
    assert out.endswith(b"\x1b[0m\x1b[1;6H")


def test_grid_reproduced():
    raw = b"line-one\r\nline-two\r\n\x1b[31mred text\x1b[0m"
    out = pyte_snap.render(raw, 20, 5)
    screen = _rerender(out, 20, 5)
    assert screen.display[0].rstrip() == "line-one"
    assert screen.display[1].rstrip() == "line-two"
    assert screen.display[2].rstrip() == "red text"
    for col in range(8):
        assert screen.buffer[2][col].fg == "red"
    # Unstyled cells stay default.
    assert screen.buffer[0][0].fg == "default"


def test_duplicated_scrollback_fixed():
    """The tier-1 weakness: replaying a ring full of newlines re-scrolls.
    Tier 2 renders only the settled screen."""
    raw = b"".join(b"scrolled line %d\r\n" % i for i in range(50))
    out = pyte_snap.render(raw, 40, 5)
    screen = _rerender(out, 40, 5)
    # Only the last screenful appears, exactly once.
    text = "\n".join(row.rstrip() for row in screen.display)
    assert text.count("scrolled line 46") == 1
    assert "scrolled line 10" not in text


def test_truecolor_and_cursor_position():
    raw = b"\x1b[38;2;10;20;30mX\x1b[0m\x1b[3;4H"
    out = pyte_snap.render(raw, 10, 5)
    assert b"38;2;10;20;30" in out
    assert out.endswith(b"\x1b[3;4H")
    screen = _rerender(out, 10, 5)
    assert screen.buffer[0][0].fg == "0a141e"
    assert (screen.cursor.x, screen.cursor.y) == (3, 2)


def test_sgr_flag_reset_between_cells():
    # bold 'A', then plain 'B' — the diff must reset, not leak bold into B.
    raw = b"\x1b[1mA\x1b[0mB"
    out = pyte_snap.render(raw, 10, 2)
    screen = _rerender(out, 10, 2)
    assert screen.buffer[0][0].bold is True
    assert screen.buffer[0][1].bold is False


def test_render_without_pyte_raises_helpful_error(monkeypatch):
    monkeypatch.setattr(pyte_snap, "pyte", None)
    assert pyte_snap.available() is False
    with pytest.raises(RuntimeError, match="pip install pyte"):
        pyte_snap.render(b"", 80, 24)


# ---- #130: emit_screen keyframe materializer --------------------------------

def test_emit_screen_round_trips_grid_and_cursor():
    # Feed content into a screen, emit it, feed the emitted bytes into a fresh
    # screen: the grid + cursor must reproduce (the keyframe is a faithful,
    # self-contained redraw of the settled grid).
    src = pyte.Screen(20, 5)
    pyte.ByteStream(src).feed(
        b"line-one\r\nline-two\r\n\x1b[31mred\x1b[0m\x1b[4;7H")
    out = pyte_snap.emit_screen(src, 20, 5)
    dst = _rerender(out, 20, 5)
    assert dst.display == src.display
    assert (dst.cursor.x, dst.cursor.y) == (src.cursor.x, src.cursor.y)


def test_render_delegates_to_emit_screen():
    # render() is now emit_screen() over a freshly-fed screen — the public
    # output is byte-identical to feeding+emitting by hand.
    raw = b"hello\r\nworld"
    direct = pyte_snap.render(raw, 20, 5)
    screen = pyte.Screen(20, 5)
    pyte.ByteStream(screen).feed(raw)
    assert direct == pyte_snap.emit_screen(screen, 20, 5)


# ---- #130: _render_screen_text keyframe reconstruction ----------------------

# An alt-screen enter + one-time full paint carrying a unique panel token. A
# real TUI paints this once, then streams only diffs.
_ALT_PAINT = b"\x1b[?1049h\x1b[2J\x1b[1;1HLEGENDPANEL\x1b[2;1Hstatus"


def test_reconstruct_restores_panel_after_eviction():
    from webterm.agent.agent import _render_screen_text
    # First read seeds the keyframe from the full paint (marker present).
    r0 = _render_screen_text(_ALT_PAINT, 40, 6, alt_screen=True, evicted=False,
                             total_appended=len(_ALT_PAINT))
    assert "LEGENDPANEL" in r0["text"]
    assert "partial" not in r0
    assert r0["degraded"] is False
    assert "keyframe" in r0 and r0["keyframe_k"] == len(_ALT_PAINT)

    # Now the paint (incl. the ?1049h marker) has been evicted; only a later
    # diff survives in the ring, which does NOT repaint the panel.
    diffs = b"\x1b[4;1Hupdated-line"
    r1 = _render_screen_text(
        diffs, 40, 6, alt_screen=True, evicted=True,
        keyframe=r0["keyframe"], keyframe_k=r0["keyframe_k"],
        keyframe_dims=(40, 6),
        evicted_total=len(_ALT_PAINT),          # off = keyframe_k - evicted = 0
        total_appended=len(_ALT_PAINT) + len(diffs))
    # The statically-painted panel SURVIVES (reconstructed from the keyframe)
    # and the later diff is applied on top.
    assert "LEGENDPANEL" in r1["text"]
    assert "updated-line" in r1["text"]
    assert "partial" not in r1              # a full reconstruction, not partial
    assert r1["degraded"] is False
    # A fresh keyframe is re-stashed, tagged at the new absolute offset.
    assert "keyframe" in r1
    assert r1["keyframe_k"] == len(_ALT_PAINT) + len(diffs)


def test_stale_keyframe_falls_back_to_partial():
    from webterm.agent.agent import _render_screen_text
    diffs = b"\x1b[4;1Honly-a-diff"
    # keyframe_k < evicted_total -> off < 0: eviction overtook the keyframe, so
    # the middle diffs are gone and we cannot reconstruct -> partial, no keyframe.
    r = _render_screen_text(
        diffs, 40, 6, alt_screen=True, evicted=True,
        keyframe=_ALT_PAINT, keyframe_k=10, keyframe_dims=(40, 6),
        evicted_total=5000, total_appended=6000)
    assert r.get("partial") is True
    assert r["degraded"] is False
    assert "keyframe" not in r


def test_dim_mismatched_keyframe_falls_back_to_partial():
    from webterm.agent.agent import _render_screen_text
    diffs = b"\x1b[4;1Honly-a-diff"
    # Keyframe captured at other dims -> its per-row CUP would misposition, so
    # reconstruction is skipped and the read is flagged partial.
    r = _render_screen_text(
        diffs, 40, 6, alt_screen=True, evicted=True,
        keyframe=_ALT_PAINT, keyframe_k=100, keyframe_dims=(80, 24),
        evicted_total=0, total_appended=100 + len(diffs))
    assert r.get("partial") is True
    assert "keyframe" not in r


def test_no_partial_when_marker_survives():
    from webterm.agent.agent import _render_screen_text
    # Evicted + alt-screen, but the ring still carries a restart marker + repaint
    # (best > 0): the normal trim path handles it, no partial, no keyframe needed
    # from reconstruction — and a fresh keyframe is still emitted for the chain.
    data = b"leading-noise\x1b[2J\x1b[1;1HFULLREPAINT"
    r = _render_screen_text(data, 40, 6, alt_screen=True, evicted=True,
                            total_appended=len(data))
    assert "FULLREPAINT" in r["text"]
    assert "partial" not in r
    assert "keyframe" in r


def test_marker_at_index_zero_is_complete_not_partial():
    from webterm.agent.agent import _render_screen_text
    # Evicted + alt-screen where the surviving ring begins EXACTLY at a restart
    # marker (best == 0): _trim_for_screen keeps the full ring, so the normal
    # replay reproduces a COMPLETE frame — everything after the clear survived
    # eviction from the front. This must NOT be flagged partial (false alarm),
    # and a fresh keyframe MUST still be emitted so the chain doesn't stall.
    # No keyframe is supplied, to prove the complete frame stands on its own.
    data = b"\x1b[2J\x1b[1;1HFULLREPAINT-COMPLETE"
    r = _render_screen_text(data, 40, 6, alt_screen=True, evicted=True,
                            total_appended=len(data))
    assert "FULLREPAINT-COMPLETE" in r["text"]
    assert "partial" not in r
    assert r["degraded"] is False
    assert "keyframe" in r and r["keyframe_k"] == len(data)


def test_non_alt_read_emits_no_keyframe():
    from webterm.agent.agent import _render_screen_text
    # A primary-screen (non-alt) read never participates in the keyframe chain,
    # so no keyframe is emitted and it can never be flagged partial.
    r = _render_screen_text(b"just a shell line\r\n", 40, 6,
                            alt_screen=False, evicted=True,
                            total_appended=19)
    assert "keyframe" not in r
    assert "partial" not in r


# ---- #128: attr_runs — surface the color/reverse-video the text drops -------

def test_attr_runs_reverse_video():
    # A reverse-video run (a color-only menu selection marker, e.g. Dwarf
    # Fortress) with default fg/bg — invisible in the plain text — is surfaced.
    screen = _rerender(b"\x1b[7mABANDON\x1b[0m", 20, 3)
    assert pyte_snap.attr_runs(screen, 20, 3) == [
        {"row": 0, "col": 0, "len": 7,
         "fg": "default", "bg": "default", "reverse": True}]


def test_attr_runs_color_without_reverse():
    # A fg/bg colour swap with NO reverse flag is caught too (some TUIs mark the
    # selection that way), so the run map isn't reverse-only.
    screen = _rerender(b"\x1b[31;44mHI\x1b[0m", 20, 2)
    assert pyte_snap.attr_runs(screen, 20, 2) == [
        {"row": 0, "col": 0, "len": 2,
         "fg": "red", "bg": "blue", "reverse": False}]


def test_attr_runs_unstyled_screen_is_empty():
    # A plain screen has no styled cells -> an empty list (baseline read from the
    # screen's own default_char, not a hardcoded constant).
    screen = _rerender(b"plain line\r\nsecond row", 20, 3)
    assert pyte_snap.attr_runs(screen, 20, 3) == []


def test_attr_runs_group_adjacent_and_split_on_default():
    # Adjacent same-style cells coalesce into one run; an unstyled cell splits.
    screen = _rerender(b"\x1b[7mAA\x1b[0m  \x1b[7mCC\x1b[0m", 20, 2)
    assert pyte_snap.attr_runs(screen, 20, 2) == [
        {"row": 0, "col": 0, "len": 2,
         "fg": "default", "bg": "default", "reverse": True},
        {"row": 0, "col": 4, "len": 2,
         "fg": "default", "bg": "default", "reverse": True}]


def test_attr_runs_bounded_by_cap():
    # One styled cell per row; the cap truncates, keeping the top rows (walked
    # top-to-bottom) so a real menu's selection near the top survives.
    raw = b"".join(b"\x1b[7mX\x1b[0m\r\n" for _ in range(5))
    screen = _rerender(raw, 10, 6)
    runs = pyte_snap.attr_runs(screen, 10, 6, cap=2)
    assert len(runs) == 2
    assert [r["row"] for r in runs] == [0, 1]


def test_render_screen_text_attrs_opt_in():
    # The agent render exposes attr_runs only when attrs=True; the default read
    # is byte-for-byte unchanged (no attr_runs key, same text) — back-compat.
    from webterm.agent.agent import _render_screen_text
    raw = b"\x1b[7mSEL\x1b[0m\r\nplain"
    on = _render_screen_text(raw, 20, 4, attrs=True)
    assert on["attr_runs"] == [
        {"row": 0, "col": 0, "len": 3,
         "fg": "default", "bg": "default", "reverse": True}]
    off = _render_screen_text(raw, 20, 4)
    assert "attr_runs" not in off
    assert off["text"] == on["text"]
