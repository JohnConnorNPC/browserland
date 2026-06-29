"""Guards for the fragment-assembled desktop UI (issue #68).

The served page (``webterm/broker/ui.py``'s ``INDEX_HTML``) used to be one
~16.8k-line ``index.html``; #68 split it into ordered on-disk fragments that
``ui.py`` concatenates at import. These tests lock the acceptance criteria and
guard against regressions in the assembly:

* the assembled page still imports as a module-scope ``str`` and looks like the
  same document (DOCTYPE / ``</html>`` / no BOM / served-page sentinels);
* the monolith is gone and the UI is genuinely split into many small files
  (no multi-thousand-line script survives);
* the on-disk fragment set matches ``ui._ORDERED`` exactly -- no fragment is
  dropped from the package and no stray file is swept in.

Byte-identity vs the pre-split page is verified once, out of band, via a sha256
gate; it is deliberately NOT asserted here so ordinary UI edits stay free.
"""

from pathlib import Path

from webterm.broker import ui
from webterm.broker.ui import INDEX_HTML

BROKER_DIR = Path(ui.__file__).resolve().parent


# --------------------------------------------------------------------------- #
# assembled-page shape
# --------------------------------------------------------------------------- #

def test_index_html_is_module_scope_str():
    # Must stay a module-scope str so Sanic's _determine_error_format
    # introspection of the `return html(INDEX_HTML)` handler keeps working.
    assert isinstance(INDEX_HTML, str)
    assert INDEX_HTML, "assembled page is empty"


def test_index_html_document_boundaries():
    assert INDEX_HTML.startswith("<!DOCTYPE html>")
    assert INDEX_HTML.rstrip().endswith("</html>")
    assert INDEX_HTML.endswith("</html>\n"), "trailing newline must be preserved"


def test_index_html_has_no_bom():
    # A Windows editor / PowerShell write could prepend a UTF-8 BOM; the empty
    # join would then carry U+FEFF into the served bytes.
    assert "﻿" not in INDEX_HTML


def test_index_html_served_sentinels_present():
    for sentinel in (
        "<title>Browserland</title>",
        "term-window",
        "_hosts",
        "hostHttpUrl",
        "host-status",
    ):
        assert sentinel in INDEX_HTML, f"missing served-page sentinel: {sentinel!r}"


def test_index_html_never_puts_token_in_url():
    # Security invariant carried over from the monolith: the page must not push
    # the auth token into the address bar.
    assert "searchParams.set('token'" not in INDEX_HTML


# --------------------------------------------------------------------------- #
# the split actually happened
# --------------------------------------------------------------------------- #

def test_monolith_is_gone():
    assert not (BROKER_DIR / "index.html").exists(), \
        "the old monolithic index.html must be deleted"


def test_fragment_counts():
    js = list(BROKER_DIR.glob("*.js"))
    css = list(BROKER_DIR.glob("*.css"))
    assert len(js) >= 15, f"expected the JS split into many files, got {len(js)}"
    assert len(css) >= 2, f"expected the CSS split into >=2 files, got {len(css)}"


def test_no_multi_thousand_line_fragment():
    # The whole point of #68: no fragment is a giant script again.
    cap = 2500
    for name in ui._ORDERED:
        lines = (BROKER_DIR / name).read_text(encoding="utf-8").count("\n")
        assert lines <= cap, f"{name} has {lines} lines (> {cap}); split it further"


# --------------------------------------------------------------------------- #
# assembly integrity
# --------------------------------------------------------------------------- #

def test_ordered_list_matches_disk_exactly():
    # Every fragment ui.py expects exists, and nothing else (no stray .bak /
    # Zone.Identifier / forgotten file) lives alongside them. Mismatch here is
    # exactly the failure mode the explicit _ORDERED list exists to prevent.
    ordered = set(ui._ORDERED)
    on_disk = {p.name for p in BROKER_DIR.iterdir()
               if p.suffix in (".html", ".css", ".js")}
    missing = ordered - on_disk
    extra = on_disk - ordered
    assert not missing, f"fragments in _ORDERED but missing on disk: {sorted(missing)}"
    assert not extra, f"fragment-typed files on disk not in _ORDERED: {sorted(extra)}"


def test_assembled_equals_join_of_fragments():
    rebuilt = "".join((BROKER_DIR / name).read_text(encoding="utf-8")
                      for name in ui._ORDERED)
    assert rebuilt == INDEX_HTML
