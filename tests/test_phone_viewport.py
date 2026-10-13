"""#249: a phone must lay the page out at its real width.

Without a viewport meta a phone renders the page at its 980px legacy width and
zooms it to ~40%, so a tiled terminal fits 61x120 of unreadable text. These pins
guard the two halves of the fix; the layout itself is observed by
``tests/playwright_phone.py`` (a phone 390x844 and a rect-identical desktop):

  * the meta: in the served page exactly once, inside ``<head>``, and WITHOUT
    ``interactive-widget`` (resizes-content would resize the PTY on every
    on-screen-keyboard toggle; the lease holder owns the size, #248);
  * the phone CSS: every rule that exists only for a phone lives inside a
    ``max-width`` media block (the touch-target one is ``pointer: coarse`` AND
    ``max-width``), never at the top level, so a desktop browser cannot be
    changed by it.
"""
import re
from pathlib import Path

from webterm.broker import ui

BROKER_DIR = Path(ui.__file__).parent
META = '<meta name="viewport" content="width=device-width, initial-scale=1">'
BEGIN = "/* ---- #249 phone layout"
END = "/* ---- end #249 phone layout */"


def _head(html):
    return html[html.index("<head>"):html.index("</head>")]


def test_viewport_meta_once_in_head():
    html = ui.INDEX_HTML
    assert len(re.findall(r'<meta\s+name="viewport"', html)) == 1
    assert html.count(META) == 1
    assert META in _head(html)
    assert "interactive-widget" not in re.sub(r"<!--.*?-->", "", html, flags=re.S)


def _phone_block():
    css = (BROKER_DIR / "15_css_dialogs.css").read_text(encoding="utf-8")
    assert css.count(BEGIN) == 1 and css.count(END) == 1
    return css[css.index(BEGIN):css.index(END)]


def _blocks(css):
    """Top-level (prelude, body) pairs of a comment-free stylesheet."""
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    out, depth, start, pre = [], 0, 0, ""
    for i, ch in enumerate(css):
        if ch == "{":
            if depth == 0:
                pre = css[start:i].strip()
                start = i + 1
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                out.append((pre, css[start:i]))
                start = i + 1
    assert depth == 0
    return out


def test_phone_css_has_no_top_level_rule():
    blocks = _blocks(_phone_block())
    assert blocks, "the #249 block is empty"
    for pre, _ in blocks:
        assert pre.startswith("@media"), "top-level rule in the phone block: " + pre
        assert re.fullmatch(r"@media (\(pointer: coarse\) and )?\(max-width: \d+px\)", pre), pre


def test_phone_rules_sit_in_the_right_gates():
    by = {pre: body for pre, body in _blocks(_phone_block())}
    narrow = by["@media (max-width: 520px)"]
    for needle in ("#clock-chip { display: none !important; }",
                   "#taskbar { gap: 4px; padding: 0 4px; }",
                   "#taskbar-items { gap: 4px; }",
                   ".title-bar .ti-id-badge",
                   ".title-bar .color-btn"):
        assert needle in narrow, needle
    # a recorder button that is recording/paused is the ONLY way to stop it, so
    # it is hidden only while idle; the MCP button is a security indicator and
    # min/close are the window controls: none of those may be hidden.
    assert ".btn-rec:not(.recording):not(.rec-paused)" in narrow
    assert re.search(r"\.btn-rec[ ,{]", narrow) is None
    for kept in ("mcp-btn", "btn-min", "btn-close", "title-text"):
        assert kept not in narrow, kept
    coarse = by["@media (pointer: coarse) and (max-width: 900px)"]
    assert ".term-window .tb-btn { width: 30px; height: 24px; }" in coarse


def test_phone_selectors_are_not_set_outside_a_media_block_in_the_core_css():
    """The phone-only declarations must not leak to the top level of any core
    fragment (a copy pasted out of its @media block would change desktop)."""
    for frag in ("10_css_root.css", "11_css_apps.css", "12_css_help.css", "13_css_tiling.css",
                 "14_css_dragdrop.css", "15_css_dialogs.css"):
        css = (BROKER_DIR / frag).read_text(encoding="utf-8")
        for pre, body in _blocks(css):
            if pre.startswith("@"):
                continue
            sels = [s.strip() for s in pre.split(",")]
            assert "#clock-chip" not in sels, frag
            for s in sels:
                assert not re.search(r"\.title-bar \.(ti-id-badge|btn-rec|color-btn)", s), (frag, s)
            if "#taskbar" in sels:
                assert "gap: 4px" not in body, frag
            if ".term-window .tb-btn" in sels:
                assert "width: 30px" not in body, frag
