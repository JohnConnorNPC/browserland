"""Tests for the dependency-free screen-grid renderer (issue #15).

`textgrid.render` is the fallback the agent uses for the MCP screen read when
pyte is absent. The headline guarantees: the output is a BOUNDED rows*cols grid
(never an unbounded raw-ANSI dump), box-drawing/braille glyphs survive, the
*settled* frame is shown, and no escape/control bytes leak into the text.
"""

from __future__ import annotations

import sys

import pytest

from webterm.agent.snapshot import textgrid


# A btop-like stream: enter alt screen, then many synchronized repaint frames,
# each clearing + redrawing a box with a changing value plus braille meters.
def _tui_stream(frames: int = 50, last_value: int = 49) -> bytes:
    assert frames - 1 == last_value or last_value < frames  # last frame wins
    buf = bytearray()
    buf += b"\x1b[?1049h"  # enter alternate screen
    for f in range(frames):
        buf += b"\x1b[?2026h\x1b[2J\x1b[H"          # begin sync, clear, home
        buf += b"\x1b[1;1H\x1b[38;5;39m\xe2\x94\x8c" + b"\xe2\x94\x80" * 10 + b"\xe2\x94\x90"
        buf += b"\x1b[2;1H\xe2\x94\x82 cpu " + f"{f:02d}%".encode() + b" \xe2\xa3\xbf\xe2\xa3\x80 \xe2\x94\x82"
        buf += b"\x1b[3;1H\xe2\x94\x94" + b"\xe2\x94\x80" * 10 + b"\xe2\x94\x98"
        buf += b"\x1b[?2026l"                        # end sync
    return bytes(buf)


def _grid(text: str):
    return text.split("\n")


# ---- shape / boundedness --------------------------------------------------

@pytest.mark.parametrize("cols,rows", [(80, 24), (122, 24), (1, 1), (200, 50)])
def test_output_is_bounded_rows_by_cols(cols, rows):
    text = textgrid.render(_tui_stream(), cols, rows)
    lines = _grid(text)
    assert len(lines) == rows
    assert all(len(ln) == cols for ln in lines)
    # The raw stream is kilobytes of repeated frames; the grid stays tiny.
    assert len(text) == rows * cols + (rows - 1)


def test_huge_stream_stays_bounded():
    # 5000 frames -> ~700 KB of raw ANSI in, a few KB grid out.
    big = _tui_stream(frames=200, last_value=199 % 100)
    text = textgrid.render(big, 60, 8)
    assert len(text) <= 60 * 8 + 8


def test_empty_input_is_blank_grid():
    text = textgrid.render(b"", 10, 3)
    assert text == "\n".join([" " * 10] * 3)


# ---- fidelity: glyphs, settled frame, no escapes --------------------------

def test_box_and_braille_glyphs_preserved():
    text = textgrid.render(_tui_stream(), 60, 8)
    assert "┌" in text and "┐" in text and "└" in text  # ┌ ┐ └
    assert "─" in text                                            # ─
    assert "⣿" in text and "⣀" in text                       # ⣿ ⣀


def test_shows_settled_frame_not_stale():
    text = textgrid.render(_tui_stream(frames=50), 60, 8)
    assert "49%" in text          # last frame
    assert "00%" not in text      # earlier frames were cleared each repaint


def test_no_escape_or_control_bytes_leak():
    text = textgrid.render(_tui_stream(), 60, 8)
    assert "\x1b" not in text
    assert all(ord(c) >= 0x20 or c == "\n" for c in text)


# ---- emulator primitives --------------------------------------------------

def test_cursor_addressing_places_text():
    # CUP to row 2, col 3 (1-based), write "Hi".
    text = textgrid.render(b"\x1b[2;3HHi", 10, 3)
    lines = _grid(text)
    assert lines[1] == "  Hi      "
    assert lines[0] == " " * 10 and lines[2] == " " * 10


def test_relative_cursor_moves():
    # Home, down 1, forward 2, write.
    text = textgrid.render(b"\x1b[H\x1b[1B\x1b[2CX", 6, 3)
    assert _grid(text)[1] == "  X   "


def test_erase_display_clears_everything():
    # ED(2) wipes the grid; apps then home the cursor before redrawing.
    text = textgrid.render(b"junk everywhere\x1b[2J\x1b[Hkept", 8, 2)
    assert "junk" not in text
    assert _grid(text)[0] == "kept    "


def test_erase_line_to_end():
    text = textgrid.render(b"ABCDEF\r\x1b[3CXY\x1b[K", 6, 1)
    # Write ABCDEF, CR home, forward 3 -> col4, write XY (cols 4-5), EL(0)
    # clears col 6 onward; cols 1-3 keep ABC.
    assert _grid(text)[0] == "ABCXY "


def test_autowrap_to_next_line():
    text = textgrid.render(b"ABCDEF", 3, 3)
    lines = _grid(text)
    assert lines[0] == "ABC" and lines[1] == "DEF"


def test_cr_lf_bs_tab_controls():
    assert _grid(textgrid.render(b"abc\rX", 5, 1))[0] == "Xbc  "
    assert _grid(textgrid.render(b"ab\bX", 5, 1))[0] == "aX   "
    assert _grid(textgrid.render(b"a\tb", 10, 1))[0] == "a       b "  # tab -> col 8
    # Bare LF is a pure line feed — the column is NOT reset (matches pyte/xterm
    # with LNM off); real PTY output carries CRLF, which resets via the CR.
    two = _grid(textgrid.render(b"a\nb", 3, 2))
    assert two[0] == "a  " and two[1] == " b "


def test_lf_scrolls_when_past_bottom():
    text = textgrid.render(b"1\r\n2\r\n3\r\n4", 4, 3)
    # Rows scroll up: line 1 falls off the top, 2/3/4 remain.
    assert _grid(text) == ["2   ", "3   ", "4   "]


def test_osc_title_is_stripped():
    text = textgrid.render(b"\x1b]0;my title\x07visible", 12, 1)
    assert "my title" not in text
    assert "visible" in text


def test_control_string_families_are_skipped():
    # DCS / PM / APC / SOS payload must not leak into the grid as text.
    for intro, term in ((b"\x1bP", b"\x1b\\"), (b"\x1b^", b"\x1b\\"),
                        (b"\x1b_", b"\x07"), (b"\x1bX", b"\x1b\\")):
        text = textgrid.render(intro + b"SECRETpayload" + term + b"ok", 12, 1)
        assert "SECRET" not in text and "payload" not in text
        assert "ok" in text


def test_csi_intermediate_byte_consumed():
    # `CSI SP q` (DECSCUSR cursor style) — the SP is an intermediate; the 'q'
    # must not be rendered as text.
    text = textgrid.render(b"\x1b[1 qHi", 6, 1)
    assert _grid(text)[0] == "Hi    "


def test_c1_controls_not_printed():
    # UTF-8-encoded C1 control U+009B/U+009D must be dropped, not rendered.
    text = textgrid.render("ABC".encode("utf-8"), 6, 1)
    assert _grid(text)[0] == "ABC   "


def test_alt_screen_enter_clears():
    # Shell remnants on the primary screen must not bleed behind a TUI.
    text = textgrid.render(b"shell prompt$ \x1b[?1049hTUI", 14, 1)
    assert "shell" not in text and "prompt" not in text
    assert "TUI" in text


def test_cursor_save_restore():
    # Write at home, save, move + write, restore, overwrite.
    text = textgrid.render(b"\x1b[1;1HX\x1b7\x1b[1;5HY\x1b8Z", 6, 1)
    # X then Y at col5, restore to col2 (just after X), write Z.
    assert _grid(text)[0] == "XZ  Y "


def test_reverse_index_scrolls_at_top():
    # Write A/B/C down the screen, home, RI at the top scrolls the screen DOWN:
    # a blank line appears at the top and the bottom line (C) falls off. Matches
    # pyte exactly.
    text = textgrid.render(b"\x1b[1;1HA\x1b[2;1HB\x1b[3;1HC\x1b[1;1H\x1bM", 2, 3)
    assert _grid(text) == ["  ", "A ", "B "]


def test_dimensions_clamped():
    # An absurd resize must not allocate an enormous grid.
    text = textgrid.render(b"hi", 10_000, 10_000)
    lines = _grid(text)
    assert len(lines) == 1000          # clamped to _MAX_DIM
    assert all(len(ln) == 1000 for ln in lines)


def test_sgr_and_private_modes_ignored_not_printed():
    text = textgrid.render(b"\x1b[1;38;5;42mA\x1b[0m\x1b[?25lB", 5, 1)
    assert _grid(text)[0] == "AB   "


def test_render_never_raises_on_garbage():
    # Truncated CSI, lone ESC, random high bytes, broken UTF-8, negative param,
    # and a CSI digit run long enough to trip CPython's int-from-str limit
    # (>4300 digits) — none of these may escape render().
    for bad in (b"\x1b[", b"\x1b", b"\x1b[999;999H", b"\xff\xfe\x1b]nope",
                b"\x1b[?", b"plain", bytes(range(0, 32)),
                b"\x1b[-1D", b"\x1b[" + b"9" * 5000 + b"H",
                b"\x1b[" + b"9" * 5000 + b"J"):
        out = textgrid.render(bad, 8, 2)
        assert isinstance(out, str)
        assert len(_grid(out)) == 2
        assert "\x1b" not in out


# ---- parity with pyte for the common sequences ----------------------------

def test_parity_with_pyte_on_tui_stream():
    pyte = pytest.importorskip("pyte")
    cols, rows = 60, 8
    data = _tui_stream()
    screen = pyte.Screen(cols, rows)
    stream = pyte.ByteStream(screen)
    stream.feed(data)
    pyte_grid = "\n".join(screen.display)
    assert textgrid.render(data, cols, rows) == pyte_grid


# ---- agent integration: the no-pyte fallback is a bounded, non-degraded grid

def test_agent_uses_bounded_grid_without_pyte(monkeypatch):
    from webterm.agent import agent as agent_mod

    # Force `import pyte` to fail so _render_screen_text takes the textgrid path.
    monkeypatch.setitem(sys.modules, "pyte", None)
    data = _tui_stream()
    r = agent_mod._render_screen_text(data, 60, 8)
    assert r["degraded"] is False             # a real grid render, not a dump
    assert len(r["text"]) == 60 * 8 + 7       # bounded to the window
    assert "49%" in r["text"] and "⣿" in r["text"]
    assert r["view"] == "screen" and r["cursor"] is not None


def test_agent_pyte_path_renders_grid():
    pytest.importorskip("pyte")
    from webterm.agent import agent as agent_mod

    r = agent_mod._render_screen_text(_tui_stream(), 60, 8)
    assert r["degraded"] is False
    assert "49%" in r["text"]
    assert r["cursor"] is not None


def test_render_screen_text_alt_forces_screen_view():
    from webterm.agent import agent as agent_mod
    # alt_screen=True must override view=scrollback (the grid is the whole story).
    r = agent_mod._render_screen_text(b"a\r\nb\r\n", 20, 3,
                                      view="scrollback", lines=50,
                                      alt_screen=True)
    assert r["alt_screen"] is True
    assert r["view"] == "screen" and r["history_lines"] == 0


def test_render_screen_text_degraded_cursor_null(monkeypatch):
    from webterm.agent import agent as agent_mod
    from webterm.agent.snapshot import textgrid as tg

    def _boom(*a, **k):
        raise RuntimeError("boom")

    monkeypatch.setitem(sys.modules, "pyte", None)   # skip the pyte path
    monkeypatch.setattr(tg, "render_screen", _boom)  # force the raw fallback
    r = agent_mod._render_screen_text(b"\x1b[31mhi", 10, 3)
    assert r["degraded"] is True
    assert r["view"] == "raw" and r["cursor"] is None


# ---- #134: the no-pyte textgrid fallback flags sparse alt-screen reads -------
# The pyte path reconstructs an evicted alt-screen frame from an immutable
# keyframe (#130); the textgrid fallback has NO such repair, so under the same
# bug condition (evicted + alt-screen + no surviving restart marker) it is always
# possibly-incomplete and must honestly return `partial` — otherwise a pyte-less
# agent silently returns a sparse frame. `_NO_MARKER_TAIL` is a diff-only tail (no
# ?1049h / 2J), so no restart marker survives the trim (best < 0).
_NO_MARKER_TAIL = b"\x1b[3;1Hstatus-update-line"


def test_render_screen_text_partial_when_evicted_alt_no_marker_no_pyte(monkeypatch):
    from webterm.agent import agent as agent_mod
    monkeypatch.setitem(sys.modules, "pyte", None)   # force the textgrid path
    r = agent_mod._render_screen_text(_NO_MARKER_TAIL, 40, 6,
                                      alt_screen=True, evicted=True)
    assert r["degraded"] is False                    # still a real grid render
    assert r["cursor"] is not None
    assert r["partial"] is True                      # can't reconstruct -> honest


def test_render_screen_text_partial_absent_when_marker_present_no_pyte(monkeypatch):
    # A surviving restart marker anchors a COMPLETE replay, so even evicted+alt is
    # NOT flagged partial (mirrors the pyte path's best>=0 rationale).
    from webterm.agent import agent as agent_mod
    monkeypatch.setitem(sys.modules, "pyte", None)
    data = b"\x1b[?1049h\x1b[2J\x1b[1;1HPANEL" + _NO_MARKER_TAIL
    r = agent_mod._render_screen_text(data, 40, 6, alt_screen=True, evicted=True)
    assert r.get("partial") is not True


def test_render_screen_text_partial_absent_when_not_evicted_no_pyte(monkeypatch):
    # Not evicted: the ring head is the true start, nothing was lost.
    from webterm.agent import agent as agent_mod
    monkeypatch.setitem(sys.modules, "pyte", None)
    r = agent_mod._render_screen_text(_NO_MARKER_TAIL, 40, 6,
                                      alt_screen=True, evicted=False)
    assert r.get("partial") is not True


def test_render_screen_text_partial_absent_when_not_alt_no_pyte(monkeypatch):
    # A primary-screen (non-alt) read is never flagged partial — no static paint
    # to lose, and scrollback owns its own history.
    from webterm.agent import agent as agent_mod
    monkeypatch.setitem(sys.modules, "pyte", None)
    r = agent_mod._render_screen_text(_NO_MARKER_TAIL, 40, 6,
                                      alt_screen=False, evicted=True)
    assert r.get("partial") is not True


# ---- #21: render_screen (cursor + scrollback + alt-aware history) ----------

def test_render_screen_basic_cursor():
    # CUP to row2 col3 (1-based), write Hi -> cursor ends at row1, col5 (0-based).
    r = textgrid.render_screen(b"\x1b[2;3HHi", 10, 3)
    assert r["history_lines"] == 0
    assert r["cursor"] == {"row": 1, "col": 4}   # after 'i' at col3 -> col4
    assert _grid(r["text"])[1] == "  Hi      "


def test_render_screen_scrollback_captures_primary_history():
    data = b"".join(f"line{i}\r\n".encode() for i in range(20))
    r = textgrid.render_screen(data, 12, 5, view="scrollback", lines=10)
    assert r["history_lines"] > 0
    # early lines that scrolled off the top are now in the prepended history
    assert "line0" in r["text"] or "line1" in r["text"]
    # screen-only view does NOT include the old lines
    s = textgrid.render_screen(data, 12, 5)
    assert s["history_lines"] == 0 and "line0" not in s["text"]


def test_render_screen_scrollback_excludes_alt_but_keeps_primary_history():
    # Real primary history scrolls off, THEN a TUI session, THEN primary again.
    data = (b"".join(f"sh{i}\r\n".encode() for i in range(12))   # scroll off
            + b"\x1b[?1049h"
            + b"".join(f"tui{i}\r\n".encode() for i in range(30))
            + b"\x1b[?1049l"
            + b"DONE\r\n")
    r = textgrid.render_screen(data, 14, 4, view="scrollback", lines=50)
    # TUI scroll must NOT pollute shell scrollback...
    assert "tui0" not in r["text"] and "tui20" not in r["text"]
    # ...but pre-TUI scrolled history survives (#21 review P1: scrollback must
    # NOT use the screen-only trim, which would discard it).
    assert "sh0" in r["text"]


def test_render_screen_scrollback_survives_clear():
    # history -> CSI 2J -> new output: scrolled-off history survives the clear
    # for scrollback, but the screen-only (trimmed) view starts after the 2J.
    data = (b"".join(f"old{i}\r\n".encode() for i in range(12))
            + b"\x1b[2J\x1b[H"
            + b"".join(f"new{i}\r\n".encode() for i in range(6)))
    r = textgrid.render_screen(data, 14, 4, view="scrollback", lines=50)
    assert "old0" in r["text"]                       # scrollback keeps it
    s = textgrid.render_screen(data, 14, 4)          # screen view trims at 2J
    assert "old0" not in s["text"]


def test_render_screen_scrollback_bounded():
    # 5000 lines -> history capped (deque maxlen + cell budget), not unbounded.
    data = b"".join(f"L{i}\r\n".encode() for i in range(5000))
    r = textgrid.render_screen(data, 80, 24, view="scrollback", lines=100000)
    assert r["history_lines"] <= 1000


def test_render_screen_history_cell_bounded_for_wide_grid():
    # Wide grid: retained history is bounded by CELLS, not 1000 full rows.
    data = b"".join(f"W{i}\r\n".encode() for i in range(3000))
    r = textgrid.render_screen(data, 500, 4, view="scrollback", lines=100000)
    assert r["history_lines"] <= 100_000 // 500   # == 200


# ---- #28: menu-teardown ops (scroll regions, IL/DL, ICH/DCH/ECH, SU/SD) -----
#
# A TUI tears a menu down with line/char insert-delete, scroll-region resets or
# private erases. The old textgrid dropped all of these, so closed menus left
# ghost text. These verify the ops are modeled — by PARITY WITH PYTE for every
# sequence pyte itself dispatches, and by direct assertion for SU/SD (which pyte
# does not implement, so a parity check there would be meaningless).

def _pyte_grid(data: bytes, cols: int, rows: int) -> str:
    pyte = pytest.importorskip("pyte")
    screen = pyte.Screen(cols, rows)
    stream = pyte.ByteStream(screen)
    stream.feed(data)
    return "\n".join(screen.display)


def _fill(cols: int, rows: int) -> bytes:
    # CUP-place AAA, BBB, CCC ... one label per row (no reliance on LF/scroll).
    return b"".join(b"\x1b[%d;1H%s" % (i + 1, (chr(ord("A") + i) * 3).encode())
                    for i in range(rows))


@pytest.mark.parametrize("seq", [
    b"\x1b[2;1H\x1b[L",             # IL: insert 1 blank line at row 2
    b"\x1b[2;1H\x1b[2L",            # IL x2
    b"\x1b[2;1H\x1b[M",             # DL: delete 1 line at row 2
    b"\x1b[2;1H\x1b[3M",            # DL x3 (more than rows below)
    b"\x1b[1;2H\x1b[2@",            # ICH: insert 2 blanks
    b"\x1b[1;2H\x1b[9@",            # ICH past the right edge (clip)
    b"\x1b[1;2H\x1b[2P",            # DCH: delete 2 chars
    b"\x1b[1;2H\x1b[9P",            # DCH past the end (clamp)
    b"\x1b[1;3H\x1b[3X",            # ECH: erase 3 chars in place
    b"\x1b[3;1H\x1b[?0J",           # DECSED ?0J (== ED 0)
    b"\x1b[3;1H\x1b[?J",            # DECSED ?J default
    b"\x1b[3;1H\x1b[?1J",           # DECSED ?1J (start -> cursor)
    b"\x1b[1;2H\x1b[?0K",           # DECSEL ?0K (== EL 0)
    b"\x1b[1;2H\x1b[?2K",           # DECSEL ?2K (whole line)
    b"\x1b[2;4r",                   # DECSTBM set region (homes cursor)
    b"\x1b[2;4r\x1b[H",             # DECSTBM + explicit home
    b"\x1b[r",                      # DECSTBM reset to full screen
    b"\x1b[4;2r",                   # DECSTBM invalid (top>=bot): no-op
    b"\x1b[2;4r\x1b[2;1H\x1b[L",    # IL inside a scroll region
    b"\x1b[2;4r\x1b[4;1H\x1b[M",    # DL at the region's bottom row
    b"\x1b[2;3r\x1b[5;1H\x1b[L",    # IL with cursor OUTSIDE region -> no-op
    b"\x1b[2;3r\x1b[5;1H\x1b[M",    # DL with cursor outside region -> no-op
])
def test_menu_ops_parity_with_pyte(seq):
    pytest.importorskip("pyte")
    cols, rows = 8, 5
    data = _fill(cols, rows) + seq
    assert textgrid.render(data, cols, rows) == _pyte_grid(data, cols, rows)


def test_decstbm_scroll_within_region_parity():
    # Set a 3-row region, home, then feed CRLF-separated lines so the region
    # scrolls while the rows outside it stay fixed. Must match pyte exactly.
    cols, rows = 8, 5
    data = _fill(cols, rows) + b"\x1b[2;4r\x1b[2;1H" + b"".join(
        b"r%d\r\n" % i for i in range(6))
    assert textgrid.render(data, cols, rows) == _pyte_grid(data, cols, rows)


def test_su_scrolls_full_screen_up():
    # SU 1 with the default region: every row moves up, blank at the bottom.
    text = textgrid.render(_fill(8, 5) + b"\x1b[S", 8, 5)
    assert _grid(text) == ["BBB     ", "CCC     ", "DDD     ", "EEE     ",
                           "        "]


def test_sd_scrolls_full_screen_down():
    text = textgrid.render(_fill(8, 5) + b"\x1b[T", 8, 5)
    assert _grid(text) == ["        ", "AAA     ", "BBB     ", "CCC     ",
                           "DDD     "]


def test_su_sd_within_scroll_region_keep_outside_rows():
    # Region = rows 2-4; SU/SD scroll only inside it. Row 1 (AAA) and row 5
    # (EEE) are sentinels that must never move.
    up = textgrid.render(_fill(8, 5) + b"\x1b[2;4r\x1b[S", 8, 5)
    assert _grid(up) == ["AAA     ", "CCC     ", "DDD     ", "        ",
                         "EEE     "]
    down = textgrid.render(_fill(8, 5) + b"\x1b[2;4r\x1b[T", 8, 5)
    assert _grid(down) == ["AAA     ", "        ", "BBB     ", "CCC     ",
                           "EEE     "]


def test_ich_clips_at_right_edge():
    data = b"\x1b[1;1HABCDEFGH\x1b[1;3H\x1b[2@"   # insert 2 blanks at col 3
    assert _grid(textgrid.render(data, 8, 1))[0] == "AB  CDEF"   # GH pushed off
    assert _grid(textgrid.render(data, 8, 1))[0] == _pyte_grid(data, 8, 1)


def test_dch_pads_right_with_blanks():
    data = b"\x1b[1;1HABCDEFGH\x1b[1;3H\x1b[2P"   # delete CD, shift left
    assert _grid(textgrid.render(data, 8, 1))[0] == "ABEFGH  "
    assert _grid(textgrid.render(data, 8, 1))[0] == _pyte_grid(data, 8, 1)


def test_ech_erases_in_place_without_shifting():
    data = b"\x1b[1;1HABCDEFGH\x1b[1;3H\x1b[2X"   # blank cols 3-4 only
    assert _grid(textgrid.render(data, 8, 1))[0] == "AB  EFGH"
    assert _grid(textgrid.render(data, 8, 1))[0] == _pyte_grid(data, 8, 1)


def test_char_ops_at_last_column_dont_overflow():
    # n far larger than the columns remaining must clamp, never raise/overflow.
    for op in (b"\x1b[9@", b"\x1b[9P", b"\x1b[9X"):
        data = b"\x1b[1;1HABCDEFGH\x1b[1;8H" + op
        out = _grid(textgrid.render(data, 8, 2))
        assert len(out) == 2 and all(len(ln) == 8 for ln in out)


def test_menu_teardown_leaves_no_ghost_text():
    # The #28 symptom: draw a menu (rows 2-4), then tear it down with DL. The
    # rows below close up and NO menu glyph survives (the old textgrid kept
    # them as ghost text because it ignored DL).
    base = (b"\x1b[1;1Hheader"
            + b"\x1b[2;1HMENU-AAAA"
            + b"\x1b[3;1HMENU-BBBB"
            + b"\x1b[4;1HMENU-CCCC"
            + b"\x1b[5;1Hfooter")
    text = textgrid.render(base + b"\x1b[2;1H\x1b[3M", 10, 5)   # DL x3 at row 2
    assert "MENU" not in text
    assert "header" in text and "footer" in text


def test_scroll_region_does_not_pollute_history_when_top_nonzero():
    # A partial-region scroll (top>0) is a TUI menu scroll, NOT real shell
    # scrollback — it must not leak into history (#21/#28 review point).
    data = _fill(8, 5) + b"\x1b[2;4r\x1b[4;1H" + b"".join(
        b"x%d\r\n" % i for i in range(10))
    r = textgrid.render_screen(data, 8, 5, view="scrollback", lines=50)
    assert r["history_lines"] == 0


def test_one_row_grid_scroll_ops_never_raise():
    # Degenerate 1-row grid: region is a single line; scroll/IL/DL must be safe.
    for seq in (b"\x1b[S", b"\x1b[T", b"\x1b[L", b"\x1b[M", b"\x1b[1;1r\x1bM"):
        out = textgrid.render(b"\x1b[1;1HX" + seq, 4, 1)
        assert len(_grid(out)) == 1 and len(_grid(out)[0]) == 4


# ---- #28 part 2: evicted-ring head resync (don't mis-decode a cut sequence) -

def test_trim_for_screen_resyncs_evicted_head():
    # Head evicted mid-CSI: starts with '2;5HGHOST' (the ESC '[' was dropped).
    data = b"2;5HGHOST\x1b[1;1Hclean"
    # evicted + no restart marker -> resync to the first ESC, dropping the cut.
    assert textgrid._trim_for_screen(data, evicted=True) == b"\x1b[1;1Hclean"
    # not evicted -> the head is the true start, kept verbatim.
    assert textgrid._trim_for_screen(data, evicted=False) == data
    # a restart marker always wins, evicted or not.
    d2 = b"old stuff\x1b[2Jnew"
    assert textgrid._trim_for_screen(d2, evicted=True) == b"\x1b[2Jnew"
    assert textgrid._trim_for_screen(d2, evicted=False) == b"\x1b[2Jnew"


def test_trim_for_screen_matches_raw_trim_behaviour():
    # _trim_for_screen must mirror snapshot/raw._trim for the evicted case.
    from webterm.agent.snapshot import raw as raw_snap
    data = b"23;42Hgarbage\x1b[31mred"   # the test_snapshot_raw fixture shape
    assert textgrid._trim_for_screen(data, evicted=True) == \
        raw_snap._trim(data, evicted=True)


@pytest.mark.parametrize("force_textgrid", [False, True])
def test_render_screen_text_evicted_resync(monkeypatch, force_textgrid):
    # Both render paths (pyte and the textgrid fallback) must drop a cut leading
    # sequence when the ring evicted its head, instead of rendering ghost text.
    from webterm.agent import agent as agent_mod
    if force_textgrid:
        monkeypatch.setitem(sys.modules, "pyte", None)
    else:
        pytest.importorskip("pyte")
    data = b"2;5HGHOST\x1b[1;1Hclean"
    r = agent_mod._render_screen_text(data, 20, 3, evicted=True)
    assert "GHOST" not in r["text"]
    assert "clean" in r["text"]
