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
