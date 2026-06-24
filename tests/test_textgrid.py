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
    text, degraded = agent_mod._render_screen_text(data, 60, 8)
    assert degraded is False                  # a real grid render, not a dump
    assert len(text) == 60 * 8 + 7            # bounded to the window
    assert "49%" in text and "⣿" in text  # settled frame + braille survive


def test_agent_pyte_path_renders_grid():
    pytest.importorskip("pyte")
    from webterm.agent import agent as agent_mod

    text, degraded = agent_mod._render_screen_text(_tui_stream(), 60, 8)
    assert degraded is False
    assert "49%" in text
