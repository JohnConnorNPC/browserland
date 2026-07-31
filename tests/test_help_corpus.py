"""Tests for the wiki -> in-app Help corpus parser (issue #60).

Covers the parser contract (sections/cards/typed fragments, help:ignore,
cross-nav drop, table flattening, inline markdown, kbd vs code, entities),
the XSS-safety invariant (typed plain data only — never HTML), parity with the
old hand-written HELP_ENTRIES_STATIC topic set, the regenerate-and-diff drift
guard against the packaged help_corpus.json, and a static check that the
frontend's Help render path never uses innerHTML on corpus content.
"""

import json
import re

import pytest

from webterm.broker import help_corpus as hc
# The desktop page is assembled from on-disk fragments by ui.py (issue #68);
# import the byte-identical assembled string rather than reading a single file.
from webterm.broker.ui import INDEX_HTML


# --------------------------------------------------------------------------- #
# fake-wiki fixture
# --------------------------------------------------------------------------- #

def _write_wiki(tmp_path, pages):
    wiki = tmp_path / "wiki"
    wiki.mkdir(parents=True, exist_ok=True)
    for name, text in pages.items():
        (wiki / name).write_text(text, encoding="utf-8")
    return wiki


SIDEBAR = """### Wiki

**Start here**
- [[Home]]
- [[Getting Started|Getting-Started]]

**Building layouts**
- [[Sample Page|Sample]]
- [[Workspaces]]
"""

SAMPLE = """This is the **intro** paragraph with a [[link label|Window-Modes]] and an entity &lt;x&gt;.

## First section

Some prose with `code` and a `Ctrl+Alt+p` combo and *italic* text.

### A sub-heading

- bullet one with **bold**
- bullet two with `Escape`

## Table section

| Action | Binding |
|---|---|
| Focus left | `Ctrl+Alt+ArrowLeft` |
| Path | `/state` |

> Tip: hold it still for the dwell.

## Related pages

- [[Workspaces]]
"""


# --------------------------------------------------------------------------- #
# structure: sections, ordering, exclusions
# --------------------------------------------------------------------------- #

def test_sections_excluded_and_ordered(tmp_path):
    wiki = _write_wiki(tmp_path, {
        "_Sidebar.md": SIDEBAR,
        "_Footer.md": "boilerplate footer",
        "Home.md": "# Home\n\nlanding page",
        "Getting-Started.md": "intro\n\n## Open\n\nbody",
        "Sample.md": SAMPLE,
        "Workspaces.md": "intro\n\n## Switch\n\nbody",
    })
    corpus = hc.build_corpus(wiki)
    slugs = [s["slug"] for s in corpus["sections"]]
    # Home / _Sidebar / _Footer never become sections.
    assert "home" not in slugs and "_footer" not in slugs
    # Sidebar order is honored (Getting-Started before Sample before Workspaces).
    assert slugs == ["getting-started", "sample", "workspaces"]
    labels = {s["slug"]: s["label"] for s in corpus["sections"]}
    assert labels["sample"] == "Sample Page"      # label from [[Label|Slug]]
    assert labels["workspaces"] == "Workspaces"   # bare [[Page]]


def test_pages_not_in_sidebar_appended_in_filename_order(tmp_path):
    wiki = _write_wiki(tmp_path, {
        "_Sidebar.md": "- [[Workspaces]]\n",
        "Workspaces.md": "intro\n\n## A\n\nx",
        "Zebra.md": "intro\n\n## Z\n\nx",
        "Alpha.md": "intro\n\n## A\n\nx",
    })
    corpus = hc.build_corpus(wiki)
    slugs = [s["slug"] for s in corpus["sections"]]
    # Sidebar-listed first, then unlisted in filename order (alpha, zebra).
    assert slugs == ["workspaces", "alpha", "zebra"]
    assert corpus["sections"][1]["label"] == "Alpha"  # humanized fallback


def test_intro_becomes_overview_card_and_heading_cards(tmp_path):
    wiki = _write_wiki(tmp_path, {"_Sidebar.md": "- [[Sample]]\n",
                                  "Sample.md": SAMPLE})
    corpus = hc.build_corpus(wiki)
    cards = corpus["sections"][0]["cards"]
    titles = [c["title"] for c in cards]
    assert titles[0] == "Overview"
    assert "First section" in titles
    assert "Table section" in titles
    # Trailing cross-nav "## Related pages" dropped by rule.
    assert "Related pages" not in titles


def test_crossnav_only_dropped_when_trailing(tmp_path):
    # A "## See also" that is NOT the last section must be kept.
    page = "intro\n\n## See also\n\nmid body\n\n## Real last\n\ntail"
    wiki = _write_wiki(tmp_path, {"_Sidebar.md": "- [[P]]\n", "P.md": page})
    titles = [c["title"] for c in hc.build_corpus(wiki)["sections"][0]["cards"]]
    assert "See also" in titles          # not trailing -> kept
    assert "Real last" in titles


# --------------------------------------------------------------------------- #
# help:ignore markers
# --------------------------------------------------------------------------- #

def test_help_ignore_excludes_region(tmp_path):
    page = ("intro\n\n## Keep\n\nkept body\n\n"
            "<!-- help:ignore-start -->\n## Drop\n\ndropped body\n"
            "<!-- help:ignore-end -->\n\n## After\n\nafter body")
    wiki = _write_wiki(tmp_path, {"_Sidebar.md": "- [[P]]\n", "P.md": page})
    titles = [c["title"] for c in hc.build_corpus(wiki)["sections"][0]["cards"]]
    assert "Keep" in titles and "After" in titles
    assert "Drop" not in titles
    # The dropped body text must not leak into any card's search string.
    blob = " ".join(c["search"] for c in
                    hc.build_corpus(wiki)["sections"][0]["cards"])
    assert "dropped body" not in blob


def test_unbalanced_ignore_raises(tmp_path):
    wiki = _write_wiki(tmp_path, {"_Sidebar.md": "- [[P]]\n",
                                  "P.md": "intro\n\n<!-- help:ignore-start -->\nx"})
    with pytest.raises(hc.BuildError):
        hc.build_corpus(wiki)
    wiki2 = _write_wiki(tmp_path / "b", {"_Sidebar.md": "- [[P]]\n",
                                         "P.md": "intro\n<!-- help:ignore-end -->"})
    with pytest.raises(hc.BuildError):
        hc.build_corpus(wiki2)


# --------------------------------------------------------------------------- #
# inline parsing, tables, kbd-vs-code, entities, link search
# --------------------------------------------------------------------------- #

def _spans(blocks):
    return [(b["t"], [(s["t"], s["v"]) for s in b["spans"]]) for b in blocks]


def test_inline_bold_code_kbd_italic():
    spans = hc.parse_inline("plain **bold** `code` `Ctrl+Alt+p` *ital*")
    kinds = [(s["t"], s["v"]) for s in spans]
    assert ("strong", "bold") in kinds
    assert ("code", "code") in kinds
    assert ("kbd", "Ctrl+Alt+p") in kinds
    # italic degrades to plain text (markers stripped), merged with neighbours
    joined = "".join(s["v"] for s in spans if s["t"] == "text")
    assert "ital" in joined and "*" not in joined


@pytest.mark.parametrize("code,expect", [
    ("Ctrl+Alt+p", "kbd"),
    ("Ctrl+Alt+ArrowLeft", "kbd"),
    ("Ctrl+Alt+Shift+ArrowLeft", "kbd"),
    ("Ctrl+Alt+1", "kbd"),
    ("Escape", "kbd"),
    ("Enter", "kbd"),
    ("/state", "code"),
    ("broker.py", "code"),
    ("mcp/input", "code"),
    ("0", "code"),
    ("3000", "code"),
    ("+", "code"),
    ("max", "code"),
    ("t", "code"),
])
def test_combo_classification(code, expect):
    span = hc.parse_inline("`%s`" % code)[0]
    assert span["t"] == expect


def test_wiki_link_renders_label_but_search_has_target():
    extra = []
    spans = hc.parse_inline("see [[the snap gesture|Snapping-and-Pop-out]] now", extra)
    text = "".join(s["v"] for s in spans)
    assert "the snap gesture" in text
    assert "Snapping-and-Pop-out" not in text       # target not rendered
    assert "snapping and pop out" in " ".join(extra)  # target words searchable


def test_entities_decoded():
    spans = hc.parse_inline("Send to &lt;workspace&gt; &amp; more")
    text = "".join(s["v"] for s in spans)
    assert "<workspace>" in text and "&" in text and "&lt;" not in text


# --------------------------------------------------------------------------- #
# emphasis that CONTAINS a code span (A35)
#
# parse_inline used to split on code spans first, so the `**` opener and closer
# of a bold holding one landed in two different non-code parts, could never
# pair, and rendered as literal asterisks — on essentially every developer page.
#
# The span model is FLAT, so the fix is NOT "a strong span containing a code
# span": the markers are consumed, the code span stays a code span (it already
# has its own visual treatment), and the emphasised text AROUND it becomes
# `strong`. Everything the old code-first split got right — bold without code,
# italic, kbd, links, an unpaired marker, a `**` inside a code span — must come
# out byte-identical, which is what the rest of this section pins.
# --------------------------------------------------------------------------- #

def _pairs(spans):
    return [(s["t"], s["v"]) for s in spans]


def test_bold_containing_a_code_span_keeps_its_emphasis():
    spans = hc.parse_inline("There is **no `index.html` in this repo** and more")
    assert _pairs(spans) == [("text", "There is "),
                             ("strong", "no "),
                             ("code", "index.html"),
                             ("strong", " in this repo"),
                             ("text", " and more")]
    assert not any(s["t"] == "text" and "**" in s["v"] for s in spans)


@pytest.mark.parametrize("line,expect", [
    # code at the end / at the start / the whole emphasis: no empty strong span
    ("**bold with `code`**", [("strong", "bold with "), ("code", "code")]),
    ("**`code` then bold**", [("code", "code"), ("strong", " then bold")]),
    ("**`code`**", [("code", "code")]),
    # a combo inside emphasis is still classified as a kbd chip
    ("**press `Ctrl+Alt+p` now**", [("strong", "press "),
                                    ("kbd", "Ctrl+Alt+p"),
                                    ("strong", " now")]),
    # two code spans in one emphasis
    ("**`a` and `b`**", [("code", "a"), ("strong", " and "), ("code", "b")]),
    # emphasis with code, twice on one line, with a bare code span between them
    ("**x `a`** `m` **`b` y**", [("strong", "x "), ("code", "a"),
                                 ("text", " "), ("code", "m"),
                                 ("text", " "),
                                 ("code", "b"), ("strong", " y")]),
])
def test_emphasis_with_code_shapes(line, expect):
    assert _pairs(hc.parse_inline(line)) == expect


def test_emphasis_with_code_emits_no_nested_span():
    # The renderer knows a fixed, flat set of types; a "strong" carrying spans
    # would silently render as nothing.
    for span in hc.parse_inline("**a `b` c**"):
        assert set(span) == {"t", "v"}
        assert span["t"] in _ALLOWED_SPAN
        assert isinstance(span["v"], str)


def test_a_link_inside_bold_is_not_dropped():
    # The text around the code span goes through the ordinary non-code parse, so
    # a wiki link inside emphasis still renders its label and still feeds search.
    extra = []
    spans = hc.parse_inline("**see [[the pager|Workspaces]] and `/state`**", extra)
    assert _pairs(spans) == [("strong", "see the pager and "), ("code", "/state")]
    assert "workspaces" in " ".join(extra)
    # ...and entities inside emphasis are decoded exactly like ordinary text.
    assert _pairs(hc.parse_inline("**&lt;x&gt; `y`**")) == [("strong", "<x> "),
                                                           ("code", "y")]


@pytest.mark.parametrize("line", [
    "plain **bold** and `code` and *ital* and _em_",
    "**bold** [[Window-Modes]] [label](http://example.com) `Ctrl+Alt+p`",
    "a ** unpaired opener with `code` here",           # unpaired: stays literal
    "`a **b** c` is literal",                          # markers inside code
    "**a **b `c` d",                                   # closer-less trailing bold
    "**bold** then `code` then **more bold**",
])
def test_lines_without_the_construct_are_byte_unchanged(line):
    # The historical parse, verbatim, as the oracle: split on code spans first,
    # everything else through _parse_non_code. Any line with no bold-that-holds-
    # code must come out of the new scanner identically.
    def old(text):
        spans = []
        for i, part in enumerate(hc._CODE.split(text)):
            if i % 2 == 1:
                spans.append(hc._code_span(part))
            else:
                spans.extend(hc._parse_non_code(part, None))
        return hc._coalesce(spans)

    assert hc.parse_inline(line) == old(line)


def test_an_unpaired_marker_and_a_fenced_marker_stay_literal():
    assert "**" in "".join(s["v"] for s in
                           hc.parse_inline("an ** unpaired opener with `x`")
                           if s["t"] == "text")
    # A `**` that lives inside a code span is content: the code span OPENS first,
    # so it still wins over any emphasis that would swallow it.
    assert _pairs(hc.parse_inline("`a **b** c` literal")) == \
        [("code", "a **b** c"), ("text", " literal")]


def test_no_wiki_emphasis_with_code_renders_a_literal_marker():
    # The drift guard for the 132 single-line occurrences this construct has in
    # wiki/ today: emphasis holding a code span renders as emphasis + code, never
    # as literal asterisks. (Two OTHER defects still leave `**` in the corpus and
    # are deliberately out of scope here: bold with a nested *italic* inside,
    # which _BOLD's [^*]+ cannot span, and bold whose opener and closer land in
    # different BLOCKS because a wrapped list item's continuation lines become a
    # separate paragraph.)
    seen = 0
    for path in sorted(hc.WIKI_DIR.glob("*.md")):
        for line in path.read_text(encoding="utf-8").split("\n"):
            for m in hc._BOLD.finditer(line):
                if not hc._CODE.search(m.group(1)):
                    continue
                seen += 1
                spans = hc.parse_inline(m.group(0))
                assert not any(s["t"] == "text" and "**" in s["v"]
                               for s in spans), "%s: %s" % (path.name, m.group(0))
                assert any(s["t"] in ("code", "kbd") for s in spans)
    assert seen >= 100, "wiki occurrences not found (%d)" % seen


def test_table_flattens_with_all_cell_text_in_search(tmp_path):
    wiki = _write_wiki(tmp_path, {"_Sidebar.md": "- [[Sample]]\n", "Sample.md": SAMPLE})
    cards = {c["title"]: c for c in hc.build_corpus(wiki)["sections"][0]["cards"]}
    tbl = cards["Table section"]
    blocks = _spans(tbl["body"])
    # header row -> a 'sub' block; data rows -> 'bullet' blocks.
    assert blocks[0][0] == "sub"
    bullets = [b for b in blocks if b[0] == "bullet"]
    assert len(bullets) == 2
    # inline parsing inside cells is preserved: combo cell -> kbd span.
    assert any(t == "kbd" and v == "Ctrl+Alt+ArrowLeft"
               for _, spans in bullets for t, v in spans)
    assert any(t == "code" and v == "/state"
               for _, spans in bullets for t, v in spans)
    # every cell's text lands in search
    for needle in ("action", "binding", "focus left", "ctrl+alt+arrowleft",
                   "path", "/state"):
        assert needle in tbl["search"]


def test_blockquote_folds_to_tip(tmp_path):
    wiki = _write_wiki(tmp_path, {"_Sidebar.md": "- [[Sample]]\n", "Sample.md": SAMPLE})
    cards = {c["title"]: c for c in hc.build_corpus(wiki)["sections"][0]["cards"]}
    blocks = _spans(cards["Table section"]["body"])
    tip = [b for b in blocks if any(s == ("strong", "Tip: ") for s in b[1])]
    assert tip, "blockquote should fold into a 'Tip:' paragraph"


def test_subheading_becomes_sub_block(tmp_path):
    wiki = _write_wiki(tmp_path, {"_Sidebar.md": "- [[Sample]]\n", "Sample.md": SAMPLE})
    cards = {c["title"]: c for c in hc.build_corpus(wiki)["sections"][0]["cards"]}
    blocks = _spans(cards["First section"]["body"])
    assert any(t == "sub" and any(v == "A sub-heading" for _, v in spans)
               for t, spans in blocks)


# --------------------------------------------------------------------------- #
# fenced code blocks -> a verbatim 'pre' block
# --------------------------------------------------------------------------- #

# Three lines whose LEADING whitespace is content: four spaces, a real tab, and
# none. A systemd unit / YAML / JSON / shell continuation lives or dies on this.
FENCE_LINES = ["    indented", "\ttabbed", "plain"]
FENCE_BODY = ("before the fence\n\n"
              "```\n" + "\n".join(FENCE_LINES) + "\n```\n\n"
              "after the fence\n")


def test_fenced_block_is_one_verbatim_pre_block():
    blocks = hc.parse_blocks(FENCE_BODY, [])
    pres = [b for b in blocks if b["t"] == "pre"]
    # Exactly ONE block for the whole fence — not one per line.
    assert len(pres) == 1
    assert [b["t"] for b in blocks] == ["p", "pre", "p"]
    spans = pres[0]["spans"]
    # ONE code span holding the whole block.
    assert len(spans) == 1
    assert spans[0]["t"] == "code"
    # Verbatim: source lines joined by \n, leading whitespace intact.
    assert spans[0]["v"] == "\n".join(FENCE_LINES)
    assert spans[0]["v"] == "    indented\n\ttabbed\nplain"


def test_fenced_block_survives_crlf_without_carrying_the_cr():
    # CRLF input normalises to \n only — the \r is line-ending machinery, and a
    # stray one would show up as a control glyph in the rendered <pre>.
    blocks = hc.parse_blocks(FENCE_BODY.replace("\n", "\r\n"), [])
    pre = [b for b in blocks if b["t"] == "pre"][0]
    assert pre["spans"][0]["v"] == "\n".join(FENCE_LINES)
    assert "\r" not in pre["spans"][0]["v"]


def _legacy_fence_search(title, blocks, extra):
    """``_card_search`` as it read BEFORE 'pre': each fence line stripped and
    space-joined into a single ``p``/``code`` block."""
    legacy = []
    for b in blocks:
        if b["t"] == "pre":
            v = " ".join(l.strip()
                         for l in b["spans"][0]["v"].split("\n")).strip()
            legacy.append({"t": "p", "spans": [{"t": "code", "v": v}]})
        else:
            legacy.append(b)
    return hc._card_search(title, legacy, extra)


def test_fenced_block_search_string_is_unchanged_and_normalised():
    extra: list = []
    blocks = hc.parse_blocks(FENCE_BODY, extra)
    search = hc._card_search("Fenced", blocks, extra)
    # Regression: verbatim storage must not change what search sees, because
    # _card_search collapses every whitespace run to one space.
    assert search == _legacy_fence_search("Fenced", blocks, extra)
    assert search == ("fenced before the fence indented tabbed plain "
                      "after the fence")
    # Asserted explicitly so a future change to _card_search that drops the
    # \s+ collapse cannot silently push raw tabs/newlines into the search index.
    assert "\n" not in search and "\t" not in search and "  " not in search


def test_empty_fence_emits_no_block():
    assert hc.parse_blocks("```\n```\n", []) == []
    # ...and a fence holding only blank/whitespace lines is still not a block.
    assert hc.parse_blocks("```\n   \n\n\t\n```\n", []) == []


# --------------------------------------------------------------------------- #
# fence-aware page split: a ## inside a fence is code, not a card boundary
#
# parse_page used to re.split the WHOLE page on ^##\s+(.*)$ BEFORE parse_blocks
# ever saw a fence, so a heading-shaped line inside a fenced block silently cut
# the page in two. No shipped page trips it — a page of multi-line curl /
# systemd / JSON examples is where the first one appears.
# --------------------------------------------------------------------------- #

FENCE_H2_PAGE = ("Intro prose.\n"
                 "\n"
                 "## Real heading\n"
                 "\n"
                 "before the fence\n"
                 "\n"
                 "```\n"
                 "## Not A Heading\n"
                 "still code\n"
                 "```\n"
                 "\n"
                 "after the fence\n")


def test_h2_inside_a_fence_yields_one_card_not_two(tmp_path):
    wiki = _write_wiki(tmp_path, {"_Sidebar.md": "- [[P]]\n",
                                  "P.md": FENCE_H2_PAGE})
    cards = hc.build_corpus(wiki)["sections"][0]["cards"]
    assert [c["title"] for c in cards] == ["Overview", "Real heading"]
    card = cards[1]
    # The fenced heading survives as LITERAL text inside the one verbatim pre.
    assert [b["t"] for b in card["body"]] == ["p", "pre", "p"]
    pres = [b for b in card["body"] if b["t"] == "pre"]
    assert len(pres) == 1
    assert pres[0]["spans"][0]["v"] == "## Not A Heading\nstill code"
    assert "## not a heading" in card["search"]


def test_one_fence_recogniser_serves_both_consumers(monkeypatch):
    # The whole point of the shared _is_fence: teaching the module a new fence
    # syntax in ONE place has to move the page splitter AND the block parser
    # together. If either grows its own copy of the rule, this fails — and a
    # drifted pair is exactly how a page gets cut in half mid-fence.
    page = "intro\n\n~~~\n## Not A Heading\n~~~\n"
    # Control: ~~~ is not a fence today, so the splitter DOES cut here.
    assert len(hc._page_chunks(page)) == 3

    monkeypatch.setattr(hc, "_is_fence",
                        lambda line: line.strip().startswith(("```", "~~~")))
    assert len(hc._page_chunks(page)) == 1          # splitter followed
    blocks = hc.parse_blocks(page, [])
    assert [b["t"] for b in blocks] == ["p", "pre"]  # block parser followed
    assert blocks[1]["spans"][0]["v"] == "## Not A Heading"


def test_unclosed_fence_raises_naming_the_file_and_the_opening_line(tmp_path):
    # Silently turning the rest of a developer page into code is worse than
    # failing the build. The fence opens on line 7 of P.md.
    page = ("intro\n"          # 1
            "\n"               # 2
            "## S\n"           # 3
            "\n"               # 4
            "body\n"           # 5
            "\n"               # 6
            "```\n"            # 7  <- opens, never closes
            "code\n")          # 8
    wiki = _write_wiki(tmp_path, {"_Sidebar.md": "- [[P]]\n", "P.md": page})
    with pytest.raises(hc.BuildError) as excinfo:
        hc.build_corpus(wiki)
    msg = str(excinfo.value)
    assert "P.md" in msg and "line 7" in msg, msg


@pytest.mark.parametrize("page,lines", [
    # region opens OUTSIDE a fence, closes INSIDE one
    (("intro\n"                          # 1
      "\n"                               # 2
      "<!-- help:ignore-start -->\n"     # 3  outside
      "```\n"                            # 4
      "code\n"                           # 5
      "<!-- help:ignore-end -->\n"       # 6  inside
      "```\n"                            # 7
      "\n"
      "tail\n"), ("line 3", "line 6")),
    # ...and the mirror image: opens INSIDE, closes OUTSIDE
    (("intro\n"                          # 1
      "\n"                               # 2
      "```\n"                            # 3
      "code\n"                           # 4
      "<!-- help:ignore-start -->\n"     # 5  inside
      "```\n"                            # 6
      "<!-- help:ignore-end -->\n"       # 7  outside
      "\n"
      "tail\n"), ("line 5", "line 7")),
])
def test_ignore_region_crossing_a_fence_raises(tmp_path, page, lines):
    # _strip_ignored runs BEFORE anything else looks at the page, so a region
    # that swallows one half of a fence rewrites the fence topology in secret.
    wiki = _write_wiki(tmp_path, {"_Sidebar.md": "- [[P]]\n", "P.md": page})
    with pytest.raises(hc.BuildError) as excinfo:
        hc.build_corpus(wiki)
    msg = str(excinfo.value)
    assert "fence" in msg and "P.md" in msg, msg
    for needle in lines:
        assert needle in msg, msg


def test_fence_wholly_inside_an_ignored_region_is_fine(tmp_path):
    # The region contains a WHOLE fence, so stripping it leaves the rest of the
    # page's fence topology intact — no error, and nothing leaks.
    page = ("intro\n"
            "\n"
            "<!-- help:ignore-start -->\n"
            "## Dropped\n"
            "```\n"
            "secret code\n"
            "```\n"
            "<!-- help:ignore-end -->\n"
            "\n"
            "## Kept\n"
            "\n"
            "kept body\n")
    wiki = _write_wiki(tmp_path, {"_Sidebar.md": "- [[P]]\n", "P.md": page})
    cards = hc.build_corpus(wiki)["sections"][0]["cards"]
    assert [c["title"] for c in cards] == ["Overview", "Kept"]
    assert "secret code" not in " ".join(c["search"] for c in cards)


def test_ignore_marker_inside_a_fence_is_literal_code(tmp_path):
    # A page DOCUMENTING the ignore markers puts them in a fenced block. Both
    # markers sit inside the same fence, so they are content, not a directive —
    # they render verbatim and strip nothing. (Pre-A2 the stripper was fence-
    # blind and obeyed them, eating the fence's own closing delimiter.)
    page = ("intro\n"
            "\n"
            "## Markers\n"
            "\n"
            "```\n"
            "<!-- help:ignore-start -->\n"
            "documented, not obeyed\n"
            "<!-- help:ignore-end -->\n"
            "```\n")
    wiki = _write_wiki(tmp_path, {"_Sidebar.md": "- [[P]]\n", "P.md": page})
    cards = hc.build_corpus(wiki)["sections"][0]["cards"]
    assert [c["title"] for c in cards] == ["Overview", "Markers"]
    pres = [b for b in cards[1]["body"] if b["t"] == "pre"]
    assert len(pres) == 1
    assert pres[0]["spans"][0]["v"] == ("<!-- help:ignore-start -->\n"
                                        "documented, not obeyed\n"
                                        "<!-- help:ignore-end -->")


@pytest.mark.parametrize("marker", ["<!-- help:ignore-start -->",
                                    "<!-- help:ignore-end -->"])
def test_a_lone_ignore_marker_inside_a_fence_is_not_unbalanced(tmp_path, marker):
    # The natural way to document this system is ONE marker per code sample, not
    # a matched pair. Only markers outside every fence are directives, so a lone
    # one inside a fence is literal text — not an unbalanced region. (It used to
    # raise, naming a region the page does not contain: the pairing walked
    # in-fence markers onto the same stack as real ones.)
    page = ("intro\n"
            "\n"
            "## Markers\n"
            "\n"
            "```\n"
            f"{marker}\n"
            "sample\n"
            "```\n")
    wiki = _write_wiki(tmp_path, {"_Sidebar.md": "- [[P]]\n", "P.md": page})
    cards = hc.build_corpus(wiki)["sections"][0]["cards"]
    pres = [b for b in cards[1]["body"] if b["t"] == "pre"]
    assert pres[0]["spans"][0]["v"] == f"{marker}\nsample"


def test_a_fence_indented_under_a_list_item_is_dedented_to_its_delimiter(tmp_path):
    # CommonMark: the opening fence's own indentation is layout, not content.
    # A fence nested under a bullet would otherwise render shifted right inside
    # an already-narrow Help card, and copy out with the leading spaces.
    page = ("## Steps\n"
            "\n"
            "1. Run it:\n"
            "\n"
            "       ```bash\n"
            "       tailscale serve --bg 4445\n"
            "         nested deeper\n"
            "       ```\n")
    wiki = _write_wiki(tmp_path, {"_Sidebar.md": "- [[P]]\n", "P.md": page})
    cards = hc.build_corpus(wiki)["sections"][0]["cards"]
    pre = [b for b in cards[0]["body"] if b["t"] == "pre"][0]
    # The delimiter's 7 columns are gone; the extra 2 columns of the second
    # line — relative indentation INSIDE the block — survive.
    assert pre["spans"][0]["v"] == ("tailscale serve --bg 4445\n"
                                    "  nested deeper")


def test_a_column_zero_fence_keeps_every_space_it_had(tmp_path):
    # The mirror of the test above, and the reason the dedent is capped at the
    # delimiter's own indent rather than being a blanket lstrip: an ASCII
    # diagram in an unindented fence is content, all of it.
    page = ("## Topology\n"
            "\n"
            "```\n"
            "  Machine A          Machine B\n"
            "      |                  |\n"
            "```\n")
    wiki = _write_wiki(tmp_path, {"_Sidebar.md": "- [[P]]\n", "P.md": page})
    cards = hc.build_corpus(wiki)["sections"][0]["cards"]
    pre = [b for b in cards[0]["body"] if b["t"] == "pre"][0]
    assert pre["spans"][0]["v"] == ("  Machine A          Machine B\n"
                                    "      |                  |")


def _pre_a2_page_chunks(text):
    """The page splitter EXACTLY as it read before the fence-aware scanner.

    Inlined here rather than left behind in the module: the old rule has to be
    runnable to prove the new one is equivalent, but dead code in help_corpus.py
    is how a second, drifting fence rule gets born.
    """
    out = []
    depth = 0
    for line in text.splitlines():
        stripped = line.strip()
        if stripped == hc._IGNORE_START:
            depth += 1
            continue
        if stripped == hc._IGNORE_END:
            depth -= 1
            if depth < 0:
                raise hc.BuildError("unbalanced help:ignore (end before start)")
            continue
        if depth == 0:
            out.append(line)
    if depth != 0:
        raise hc.BuildError("unbalanced help:ignore (missing %d end)" % depth)
    return re.split(r"(?m)^##\s+(.*)$", "\n".join(out))


def test_new_scanner_matches_the_old_regex_split_on_every_shipped_page():
    # The equivalence PROOF behind "A2 is pure hardening": for every page that
    # ships today, the line-state scanner emits the very same chunk sequence the
    # regex did. Anything that changes here changed a live page.
    pages = sorted(hc.WIKI_DIR.glob("*.md"))
    # Guard against a vacuous loop (16 wiki pages today; more are welcome).
    assert len(pages) >= 16, "wiki pages not found"
    pages += sorted(hc.MODS_DIR.glob("*/help.md"))
    for path in pages:
        raw = path.read_text(encoding="utf-8")
        assert hc._page_chunks(raw, path.name) == _pre_a2_page_chunks(raw), \
            "%s: fence-aware split diverged from the old regex split" % path


# --------------------------------------------------------------------------- #
# <!-- help:tier dev --> front matter (A3)
#
# A page declares who it is for so the Help window can default to the end-user
# guide. It is a magic HTML comment, and a LENIENT parse of one is how internals
# get silently published into the end-user guide — so every test below is really
# the same assertion: a marker that is not exactly right is LOUD, never shrugged
# off, and a page with no marker is byte-for-byte what it is today.
# --------------------------------------------------------------------------- #

TIER_PAGE = ("<!-- help:tier dev -->\n"
             "\n"
             "## Only section\n"
             "\n"
             "body text\n")


def test_tier_dev_front_matter_tags_the_section(tmp_path):
    wiki = _write_wiki(tmp_path, {"_Sidebar.md": "- [[P]]\n", "P.md": TIER_PAGE})
    sec = hc.build_corpus(wiki)["sections"][0]
    assert sec["tier"] == "dev"


def test_tier_marker_tolerates_leading_blank_lines(tmp_path):
    # "First MEANINGFUL line" — blank/whitespace lines above it are fine.
    page = "\n\n   \n" + TIER_PAGE
    wiki = _write_wiki(tmp_path, {"_Sidebar.md": "- [[P]]\n", "P.md": page})
    assert hc.build_corpus(wiki)["sections"][0]["tier"] == "dev"


@pytest.mark.parametrize("page", [
    "intro\n\n## S\n\nbody\n",                          # no marker at all
    "<!-- help:tier user -->\n\nintro\n\n## S\n\nbody\n",  # explicitly default
])
def test_user_tier_emits_no_key_at_all(tmp_path, page):
    # The whole point of "only when dev": every existing section keeps its exact
    # bytes, so the unauthenticated /help-corpus.json payload is unchanged for
    # the default view. An explicit "user" is accepted but still emits nothing.
    sec = _write_wiki(tmp_path, {"_Sidebar.md": "- [[P]]\n", "P.md": page})
    sec = hc.build_corpus(sec)["sections"][0]
    assert "tier" not in sec, sec.get("tier")


def test_tier_marker_is_consumed_before_block_parsing(tmp_path):
    # If the marker survived into parse_blocks it would render as visible text
    # AND — since it sits above the first heading — conjure an Overview card out
    # of a page that has no intro.
    wiki = _write_wiki(tmp_path, {"_Sidebar.md": "- [[P]]\n", "P.md": TIER_PAGE})
    cards = hc.build_corpus(wiki)["sections"][0]["cards"]
    assert [c["title"] for c in cards] == ["Only section"]
    blob = " ".join(s["v"] for c in cards for b in c["body"] for s in b["spans"])
    assert "help:tier" not in blob
    assert "help:tier" not in " ".join(c["search"] for c in cards)


def test_parse_page_consumes_the_marker_even_when_it_discards_the_tier():
    # parse_page is what the installed-mod merge path calls: it drops the tier,
    # but the marker must still never reach the reader.
    cards = hc.parse_page(TIER_PAGE, "P.md")
    assert [c["title"] for c in cards] == ["Only section"]
    assert "help:tier" not in " ".join(c["search"] for c in cards)


@pytest.mark.parametrize("page,needle", [
    ("<!-- help:tier internal -->\n\nintro\n", "'internal'"),
    ("<!-- help:tier DEV -->\n\nintro\n", "'DEV'"),   # values are case-sensitive
    ("<!-- help:tier -->\n\nintro\n", "''"),          # no value at all
    ("<!-- help:tier dev user -->\n\nintro\n", "'dev user'"),
])
def test_unrecognised_tier_raises_naming_the_file_and_the_value(tmp_path, page,
                                                                needle):
    # A typo must not silently publish internals into the end-user guide.
    wiki = _write_wiki(tmp_path, {"_Sidebar.md": "- [[P]]\n", "P.md": page})
    with pytest.raises(hc.BuildError) as excinfo:
        hc.build_corpus(wiki)
    msg = str(excinfo.value)
    assert "P.md" in msg and needle in msg, msg


@pytest.mark.parametrize("page", [
    # agreeing duplicates are STILL an error — there is no "last wins" rule
    "<!-- help:tier dev -->\n<!-- help:tier dev -->\n\nintro\n",
    # ...and disagreeing ones obviously are
    "<!-- help:tier dev -->\n\nintro\n\n<!-- help:tier user -->\n",
])
def test_duplicate_tier_marker_raises(tmp_path, page):
    wiki = _write_wiki(tmp_path, {"_Sidebar.md": "- [[P]]\n", "P.md": page})
    with pytest.raises(hc.BuildError) as excinfo:
        hc.build_corpus(wiki)
    assert "P.md" in str(excinfo.value)


def test_tier_marker_below_body_content_raises(tmp_path):
    # NOT silently ignored: a marker the author believed was doing something is
    # the same hazard as a mistyped one.
    page = ("intro\n"                      # 1
            "\n"                           # 2
            "<!-- help:tier dev -->\n"     # 3  <- too late
            "\n"                           # 4
            "## S\n\nbody\n")
    wiki = _write_wiki(tmp_path, {"_Sidebar.md": "- [[P]]\n", "P.md": page})
    with pytest.raises(hc.BuildError) as excinfo:
        hc.build_corpus(wiki)
    msg = str(excinfo.value)
    assert "P.md" in msg and "line 3" in msg, msg


@pytest.mark.parametrize("page", [
    "Some prose <!-- help:tier dev --> more prose\n",
    "<!-- help:tier dev --> and then prose\n",
])
def test_inline_tier_marker_raises(tmp_path, page):
    # Standalone: it is a directive, not inline prose. Both of these are on the
    # first line, so only the "alone on its line" rule can catch them.
    wiki = _write_wiki(tmp_path, {"_Sidebar.md": "- [[P]]\n", "P.md": page})
    with pytest.raises(hc.BuildError):
        hc.build_corpus(wiki)


def test_tier_marker_inside_a_fence_is_literal_code(tmp_path):
    # A page DOCUMENTING the marker puts it in a fenced block — same convention
    # help:ignore already has, and the same single fence recogniser decides it.
    page = ("Intro prose.\n"
            "\n"
            "## Declaring a tier\n"
            "\n"
            "```\n"
            "<!-- help:tier dev -->\n"
            "```\n")
    wiki = _write_wiki(tmp_path, {"_Sidebar.md": "- [[P]]\n", "P.md": page})
    sec = hc.build_corpus(wiki)["sections"][0]
    assert "tier" not in sec, "a fenced marker is code, not a directive"
    pres = [b for c in sec["cards"] for b in c["body"] if b["t"] == "pre"]
    assert len(pres) == 1
    assert pres[0]["spans"][0]["v"] == "<!-- help:tier dev -->"


def test_a_fenced_marker_is_not_a_duplicate_of_the_real_one(tmp_path):
    # The page that documents the marker is also the page most likely to declare
    # one. The fenced copy must neither trip the duplicate rule nor be stripped.
    page = ("<!-- help:tier dev -->\n"
            "\n"
            "## Declaring a tier\n"
            "\n"
            "```\n"
            "<!-- help:tier dev -->\n"
            "```\n")
    wiki = _write_wiki(tmp_path, {"_Sidebar.md": "- [[P]]\n", "P.md": page})
    sec = hc.build_corpus(wiki)["sections"][0]
    assert sec["tier"] == "dev"
    pres = [b for c in sec["cards"] for b in c["body"] if b["t"] == "pre"]
    assert [p["spans"][0]["v"] for p in pres] == ["<!-- help:tier dev -->"]


def test_tier_marker_inside_an_ignored_region_has_no_effect(tmp_path):
    # The region is removed before the tier is read, so there is nothing to obey.
    page = ("<!-- help:ignore-start -->\n"
            "<!-- help:tier dev -->\n"
            "<!-- help:ignore-end -->\n"
            "intro\n"
            "\n"
            "## S\n\nbody\n")
    wiki = _write_wiki(tmp_path, {"_Sidebar.md": "- [[P]]\n", "P.md": page})
    sec = hc.build_corpus(wiki)["sections"][0]
    assert "tier" not in sec
    assert "help:tier" not in " ".join(c["search"] for c in sec["cards"])


# --------------------------------------------------------------------------- #
# XSS safety: corpus is typed plain data, never HTML
# --------------------------------------------------------------------------- #

_ALLOWED_BLOCK = {"p", "bullet", "sub", "pre"}
_ALLOWED_SPAN = {"text", "strong", "code", "kbd"}


def test_corpus_is_typed_plain_data_no_html(tmp_path):
    page = ('intro with <img src=x onerror=alert(1)> and `<script>bad()</script>`\n\n'
            "## S\n\n- <b>raw</b> &lt;script&gt;esc&lt;/script&gt;\n")
    wiki = _write_wiki(tmp_path, {"_Sidebar.md": "- [[P]]\n", "P.md": page})
    corpus = hc.build_corpus(wiki)
    for sec in corpus["sections"]:
        for card in sec["cards"]:
            assert isinstance(card["search"], str)
            for block in card["body"]:
                assert block["t"] in _ALLOWED_BLOCK
                for span in block["spans"]:
                    assert span["t"] in _ALLOWED_SPAN
                    assert isinstance(span["v"], str)
    # The angle brackets survive as LITERAL text (the renderer uses textContent,
    # so this never becomes a live element); nothing is stripped into markup.
    blob = " ".join(s["v"] for sec in corpus["sections"] for c in sec["cards"]
                    for b in c["body"] for s in b["spans"])
    assert "<img" in blob and "onerror" in blob
    assert "<script>bad()</script>" in blob   # code span kept literal


# --------------------------------------------------------------------------- #
# duplicate slug guard
# --------------------------------------------------------------------------- #

def test_duplicate_slug_raises(tmp_path):
    # Two files whose stems differ only by case collapse to one slug. This can
    # only physically exist on a case-sensitive filesystem (Linux); a
    # case-insensitive FS (Windows/macOS) prevents the collision at the FS layer.
    wiki = _write_wiki(tmp_path, {"_Sidebar.md": "- [[Dup]]\n",
                                  "Dup.md": "intro\n\n## A\n\nx"})
    (wiki / "DUP.md").write_text("intro\n\n## B\n\ny", encoding="utf-8")
    if sum(1 for p in wiki.glob("*.md") if p.stem.lower() == "dup") < 2:
        pytest.skip("case-insensitive filesystem cannot hold Dup.md + DUP.md")
    with pytest.raises(hc.BuildError):
        hc.build_corpus(wiki)


# --------------------------------------------------------------------------- #
# real wiki: parity, drift, serialization
# --------------------------------------------------------------------------- #

def test_real_wiki_builds():
    corpus = hc.build_corpus(hc.WIKI_DIR)
    # 13 end-user pages survive #113 (only mod-OWNED sections were migrated out),
    # plus the 5 developer/operator pages that replaced docs/, plus the 2 that
    # closed the coverage gaps (Themes & Appearance, Installing Mods).
    assert len(corpus["sections"]) == 20
    # The developer pages are the ONLY tiered ones, and every end-user page must
    # stay untagged -- an untagged section defaults to the user tier, so a stray
    # marker on an end-user page would hide it from the default Help view, and a
    # MISSING marker on a developer page publishes operator prose into it.
    dev = {s["slug"] for s in corpus["sections"] if s.get("tier") == "dev"}
    assert dev == {"setup-and-onboarding", "launch-profiles", "writing-a-mod",
                   "technical-reference", "upgrading"}
    assert all("tier" not in s for s in corpus["sections"]
               if s["slug"] not in dev)
    total = sum(len(s["cards"]) for s in corpus["sections"])
    # #113 moved the mod-owned cards (sticky/editor/file-manager/task-manager
    # from Window-Types, the Taskbar clock/help chips, the Getting-Started in-app
    # guide) into mod help.md, so the wiki-only floor dropped from 75 to 68. The
    # migrated cards now ride build_full_corpus() (see below), not build_corpus().
    assert total >= 68


def test_real_wiki_section_label_parity():
    labels = {s["label"] for s in hc.build_corpus(hc.WIKI_DIR)["sections"]}
    for expected in ["Getting Started", "Keyboard Shortcuts", "Window Modes",
                     "Arranging Windows", "Columns & Widths", "Snapping & Pop-out",
                     "Floating Window Controls", "Workspaces", "Taskbar",
                     "Context Menus", "Window Types", "Hosts & Multi-Browser",
                     "MCP & AI Agents"]:
        assert expected in labels


def test_real_wiki_card_title_parity():
    titles = {c["title"] for s in hc.build_corpus(hc.WIKI_DIR)["sections"]
              for c in s["cards"]}
    # Representative topics that the old static guide covered must survive.
    for expected in ["The Control Panel", "Snap a floating window into the grid",
                     "Pop a tiled window out to a float", "Column width presets",
                     "The drop-zone cheat sheet", "Pin a window (lock to screen)",
                     "Add a remote host", "Enable MCP for a host",
                     "Hold delay (configurable)"]:
        assert expected in titles, expected


def test_keyboard_default_table_excluded():
    # The Keyboard-Shortcuts default-binding table is wrapped in help:ignore;
    # the static "Default bindings" card must NOT appear (live entries cover it).
    titles = {c["title"] for s in hc.build_corpus(hc.WIKI_DIR)["sections"]
              for c in s["cards"]}
    assert "Default bindings" not in titles


def test_serialize_is_deterministic():
    corpus = hc.build_corpus(hc.WIKI_DIR)
    assert hc.serialize_corpus(corpus) == hc.serialize_corpus(corpus)


def test_packaged_json_in_sync_with_wiki():
    # Regenerate-and-diff drift guard: the checked-in/packaged help_corpus.json
    # must byte-match a fresh parse of wiki/ + the mod help.md files (#113 — it is
    # tooling-generated, never hand-edited). If this fails: run the regenerator
    # `python -m webterm.broker.help_corpus`.
    fresh = hc.serialize_corpus(hc.build_full_corpus())
    assert hc.PACKAGED_JSON.read_bytes() == fresh, \
        "help_corpus.json is stale — run: python -m webterm.broker.help_corpus"


def test_load_corpus_falls_back_when_wiki_missing(monkeypatch, tmp_path):
    # wiki absent -> packaged JSON is used (graceful, no exception).
    monkeypatch.setattr(hc, "WIKI_DIR", tmp_path / "nope")
    corpus = hc.load_corpus()
    assert corpus["sections"], "should fall back to packaged json"


# --------------------------------------------------------------------------- #
# mod-owned help.md -> tagged corpus sections (issue #113)
# --------------------------------------------------------------------------- #

def _write_mods(tmp_path, mods):
    """Build a fake mods/ tree. ``mods`` maps mod id -> (manifest, help_md):
    a None manifest / None help_md omits that file (to exercise the "needs
    both" gate)."""
    root = tmp_path / "mods"
    root.mkdir(parents=True, exist_ok=True)
    for mod_id, (manifest, help_md) in mods.items():
        d = root / mod_id
        d.mkdir(parents=True, exist_ok=True)
        if manifest is not None:
            (d / "mod.json").write_text(json.dumps(manifest), encoding="utf-8")
        if help_md is not None:
            (d / "help.md").write_text(help_md, encoding="utf-8")
    return root


def test_mod_help_builds_section():
    # A real shipped mod (clock) drops a help.md the same parser reads, tagged
    # with its owner id and carrying the slug/label/icon from its help block.
    secs = {s["slug"]: s for s in hc.build_mod_sections()}
    clock = secs["clock"]
    assert clock["mod"] == "clock"
    assert clock["label"] == "Clock"
    assert clock["icon"] == "\U0001f550"        # 🕐 declared in mod.json help
    assert clock["cards"], "clock help.md should yield at least one card"


def test_full_corpus_includes_mod_sections():
    full = hc.build_full_corpus()
    slugs = [s["slug"] for s in full["sections"]]
    assert len(slugs) == len(set(slugs)), "no duplicate slug across wiki + mods"
    assert "taskbar" in slugs                    # a surviving wiki section
    for mod_slug in ("sticky", "editor", "file-manager",
                     "task-manager", "clock", "help", "aistatus", "git",
                     "clipboard", "scratchpad", "recorder", "host-registry",
                     "mousemode", "mod-sync", "update"):
        assert mod_slug in slugs
    # #177 retired agent-docs to mods-deprecated/, which build_mod_sections does
    # not scan — so its Help section goes with it.
    assert "agent-docs" not in slugs
    # every mod section is tagged and sorts AFTER every wiki section.
    mod_orders = [s["order"] for s in full["sections"] if "mod" in s]
    wiki_orders = [s["order"] for s in full["sections"] if "mod" not in s]
    assert len(mod_orders) == 18   # +git (#116) +clipboard (#106) +scratchpad (#124) +recorder (#140) +host-registry (#65) +mousemode (#155) +mod-sync (#158) -agent-docs (#177) +update (#182) +theme +pattern +termfont
    assert min(mod_orders) > max(wiki_orders)


def test_mod_sections_absent_when_no_mods_dir(tmp_path):
    assert hc.build_mod_sections(tmp_path / "nope") == []


def test_mod_needs_both_manifest_and_help(tmp_path):
    root = _write_mods(tmp_path, {
        "onlyhelp": (None, "intro\n\n## A\n\nbody"),
        "onlymanifest": ({"id": "onlymanifest"}, None),
        "good": ({"id": "good", "title": "Good"}, "intro body"),
    })
    slugs = {s["slug"] for s in hc.build_mod_sections(root)}
    assert slugs == {"good"}


def test_mod_help_block_fallbacks(tmp_path):
    # No/blank/malformed help fields fall back deterministically; a non-int order
    # is rejected (kept comparable) and a blank icon is dropped.
    root = _write_mods(tmp_path, {
        "aaa": ({"id": "aaa", "title": "Ayy", "help": "not-a-dict"}, "prose"),
        "zzz-mod": ({"id": "zzz-mod", "help": {"order": "10", "icon": ""}}, "prose"),
    })
    secs = {s["slug"]: s for s in hc.build_mod_sections(root)}
    assert secs["aaa"]["label"] == "Ayy"                    # title fallback
    assert secs["aaa"]["order"] == hc._MOD_ORDER_BASE + 0   # index fallback
    assert "icon" not in secs["aaa"]                        # help wasn't a dict
    assert secs["zzz-mod"]["label"] == "zzz mod"            # humanized id
    assert secs["zzz-mod"]["order"] == hc._MOD_ORDER_BASE + 1  # "10" rejected
    assert "icon" not in secs["zzz-mod"]                    # "" icon dropped


def test_mod_help_declares_a_tier_the_same_way_a_wiki_page_does(tmp_path):
    # A mod's help.md IS a wiki page — same parser, same front-matter rule, same
    # "only when dev" emission. No special case on either side.
    root = _write_mods(tmp_path, {
        "devmod": ({"id": "devmod"},
                   "<!-- help:tier dev -->\n\n## A\n\nbody\n"),
        "usermod": ({"id": "usermod"}, "prose\n"),
    })
    secs = {s["slug"]: s for s in hc.build_mod_sections(root)}
    assert secs["devmod"]["tier"] == "dev"
    # ...and the marker was consumed, so it makes no phantom Overview card.
    assert [c["title"] for c in secs["devmod"]["cards"]] == ["A"]
    assert "tier" not in secs["usermod"]


def test_mod_help_with_a_bad_tier_raises(tmp_path):
    root = _write_mods(tmp_path, {
        "bad": ({"id": "bad"}, "<!-- help:tier nope -->\n\nprose\n"),
    })
    with pytest.raises(hc.BuildError) as excinfo:
        hc.build_mod_sections(root)
    assert "mods/bad/help.md" in str(excinfo.value)


def test_mod_help_empty_page_skipped(tmp_path):
    # A help.md that parses to no cards yields no section (silent skip).
    root = _write_mods(tmp_path, {"blank": ({"id": "blank"}, "\n\n")})
    assert hc.build_mod_sections(root) == []


def test_duplicate_mod_slug_raises(tmp_path):
    root = _write_mods(tmp_path, {
        "one": ({"id": "one", "help": {"slug": "dup"}}, "a"),
        "two": ({"id": "two", "help": {"slug": "dup"}}, "b"),
    })
    with pytest.raises(hc.BuildError):
        hc.build_mod_sections(root)


def test_mod_slug_colliding_with_wiki_raises(tmp_path, monkeypatch):
    # A mod whose slug shadows a real wiki page must fail the merge (build_full_
    # corpus looks up MODS_DIR as a live global, so this monkeypatch takes effect).
    root = _write_mods(tmp_path, {
        "shadow": ({"id": "shadow", "help": {"slug": "taskbar"}}, "x"),
    })
    monkeypatch.setattr(hc, "MODS_DIR", root)
    with pytest.raises(hc.BuildError):
        hc.build_full_corpus()


def test_bad_mod_manifest_does_not_crash(tmp_path):
    # An unparseable / non-object mod.json degrades to fallbacks (id from dir),
    # never crashing the build. The help.md still becomes a section.
    root = tmp_path / "mods"
    (root / "brokenmod").mkdir(parents=True)
    (root / "brokenmod" / "mod.json").write_text("{not json", encoding="utf-8")
    (root / "brokenmod" / "help.md").write_text("intro body", encoding="utf-8")
    secs = hc.build_mod_sections(root)
    assert len(secs) == 1
    assert secs[0]["slug"] == "brokenmod" and secs[0]["mod"] == "brokenmod"


# --------------------------------------------------------------------------- #
# installed-mod help (#163): merged at SERVE time only
# --------------------------------------------------------------------------- #

def _installed_index(mods):
    """A modinstall index carrying the given installed mods.

    ``mods`` maps id -> (manifest extras, help.md text or None). Built through
    the REAL validator/index builders, so what the merge sees here is shaped
    exactly like what an install or a scan produces.
    """
    from webterm.broker import modinstall
    index = modinstall.empty_index()
    for mod_id, (extra, help_md) in mods.items():
        files = {"%s.js" % mod_id: "//\n"}
        if help_md is not None:
            files["help.md"] = help_md
        meta = {"id": mod_id, "version": "1.0.0", "ctxVersion": 1,
                "scripts": ["%s.js" % mod_id]}
        meta.update(extra)
        canonical, records = modinstall.validate_package(meta, files)
        index = modinstall.index_with(
            index, mod_id, canonical, records,
            modinstall.compute_gen(canonical, records), 1_700_000_000)
    return index


def test_installed_help_sections_are_serve_time_only():
    # THE constraint of #163's help slice: what is installed on this machine may
    # never reach build_full_corpus() or the packaged JSON, or the byte-exact
    # drift guard above becomes machine-specific.
    index = _installed_index({
        "x-notes": ({"title": "Notes", "help": {"icon": "📓"}},
                    "A note pad.\n\n## Writing a note\n\nType and it saves.\n"),
    })
    base = hc.build_full_corpus()
    base_slugs = [s["slug"] for s in base["sections"]]
    assert "x-notes" not in base_slugs, \
        "build_full_corpus must never consult the installed index"

    merged = hc.merge_installed_sections(base, index)
    sec = {s["slug"]: s for s in merged["sections"]}["x-notes"]
    assert sec["mod"] == "x-notes"          # tagged, so Help hides it when off
    assert sec["label"] == "Notes"          # manifest title
    assert sec["icon"] == "📓"
    assert sec["cards"], "help.md should yield at least one card"
    assert sec["order"] > max(s["order"] for s in base["sections"])

    # The base is untouched — it is the import-time HELP_CORPUS, reused by every
    # index swap — and the packaged bytes still match a fresh shipped-only build.
    assert [s["slug"] for s in base["sections"]] == base_slugs
    assert hc.PACKAGED_JSON.read_bytes() == \
        hc.serialize_corpus(hc.build_full_corpus())


def test_no_shipped_help_slug_is_in_the_installed_namespace():
    # This is what makes "an installed section can never shadow a shipped one"
    # STRUCTURAL rather than a hope. An installed section's slug is forced to
    # its mod id and an installed id must start with "x-", so the other half of
    # the proof is that nothing on the shipped side ever claims an "x-" slug.
    #
    # It is not implied by the CI rule that no shipped mod ID starts with "x-":
    # a wiki page could be named X-Notes.md, and a shipped mod.json may set an
    # explicit help.slug unrelated to its id (mod-sync does). Both land in
    # build_full_corpus, so guard the built slugs, not the ids.
    #
    # If this ever fails, the collision is not a crash — merge_installed_sections
    # keeps the shipped section and silently drops the installed mod's help.
    for sec in hc.build_full_corpus()["sections"]:
        assert not sec["slug"].startswith("x-"), \
            "%s claims a slug in the installed namespace" % sec["slug"]


def test_an_installed_section_never_displaces_a_base_section():
    # The unreachable-by-construction branch above, exercised anyway: if a base
    # section somehow already owns the slug, the BASE wins and the corpus is
    # otherwise untouched — one section per slug, never two, never a raise.
    index = _installed_index({"x-notes": ({"title": "Notes"}, "installed\n")})
    base = {"sections": [{"slug": "x-notes", "label": "Squatter", "order": 5,
                          "cards": [{"title": "Base", "body": [],
                                     "search": "base"}]}]}
    merged = hc.merge_installed_sections(base, index)
    assert len(merged["sections"]) == 1
    assert merged["sections"][0]["label"] == "Squatter"
    assert "mod" not in merged["sections"][0]


def test_installed_help_slug_is_forced_to_the_mod_id():
    # help.slug is dropped by modinstall's canonical manifest and ignored here,
    # so an installed section can never land on a wiki or shipped slug.
    index = _installed_index({"x-taskbar": ({"title": "Nope"}, "prose\n")})
    index["mods"]["x-taskbar"]["manifest"]["help"] = {"slug": "taskbar",
                                                      "label": "Taskbar"}
    merged = hc.merge_installed_sections(hc.build_full_corpus(), index)
    slugs = [s["slug"] for s in merged["sections"]]
    assert "x-taskbar" in slugs
    assert slugs.count("taskbar") == 1
    taskbar = next(s for s in merged["sections"] if s["slug"] == "taskbar")
    assert "mod" not in taskbar, "the wiki section must survive intact"


def test_installed_help_never_raises_or_blanks_help():
    # A careless installed mod must not be able to empty the Help window: every
    # malformed shape is SKIPPED and the rest of the corpus is served.
    base = hc.build_full_corpus()
    index = _installed_index({"x-good": ({}, "prose\n")})
    good = index["mods"]["x-good"]
    _MAX = hc._MAX_INSTALLED_SECTIONS
    for broken in (None, 7, b"bytes", "", "   \n", "\n\n",
                   "x" * (hc._MAX_INSTALLED_HELP_CHARS + 1)):
        bad = {"mods": {"x-bad": dict(good, id="x-bad", help_md=broken),
                        "x-good": good}}
        merged = hc.merge_installed_sections(base, bad)
        slugs = [s["slug"] for s in merged["sections"]]
        assert "x-bad" not in slugs, repr(broken)[:40]
        assert "x-good" in slugs
        assert len(slugs) == len(base["sections"]) + 1

    # ...and a wholly malformed index / corpus degrades to the base, not a throw.
    for junk in (None, {}, {"mods": None}, {"mods": {"x-a": None}},
                 {"mods": {"x-a": {"help_md": "p\n", "manifest": 7}}},
                 {"mods": {7: {"help_md": "p\n"}}}):
        assert len(hc.merge_installed_sections(base, junk)["sections"]) \
            >= len(base["sections"])
    assert hc.merge_installed_sections({"sections": []}, index)["sections"]
    assert hc.merge_installed_sections(None, index) is None

    # One unusable KEY must not take the whole merge down with it: sorting a
    # mixed-type key set raises, and the outer guard would then drop every
    # OTHER mod's help too.
    mixed = hc.merge_installed_sections(base, {"mods": {7: {"help_md": "p\n"},
                                                        "x-good": good}})
    assert "x-good" in [s["slug"] for s in mixed["sections"]]

    # The aggregate parse this does on the event loop is bounded even for an
    # index no validator ever saw.
    many = {"mods": {"x-%03d" % n: dict(good, help_md="mod %d prose\n" % n)
                     for n in range(_MAX + 5)}}
    merged = hc.merge_installed_sections(base, many)
    assert len(merged["sections"]) == len(base["sections"]) + _MAX


def test_installed_help_falls_back_and_sorts_deterministically():
    index = _installed_index({
        "x-zed": ({}, "zed prose\n"),
        "x-aaa": ({"help": {"order": 10}}, "aaa prose\n"),
        "x-mid": ({"title": "Middle"}, "mid prose\n"),
        "x-nohelp": ({}, None),
    })
    merged = hc.merge_installed_sections({"sections": []}, index)
    rows = {s["slug"]: s for s in merged["sections"]}
    assert "x-nohelp" not in rows                    # no help.md, no section
    # No help.label and no title -> the canonical manifest already defaulted
    # title to the id, so the label is the id (the _humanize fallback below it
    # only fires for a hand-built index carrying no manifest at all).
    assert rows["x-zed"]["label"] == "x-zed"
    assert rows["x-mid"]["label"] == "Middle"        # manifest title
    assert rows["x-aaa"]["order"] == 10              # explicit help.order wins
    # Sorted by (order, slug): the explicit 10 sorts before the derived bases.
    assert [s["slug"] for s in merged["sections"]] == \
        ["x-aaa", "x-mid", "x-zed"]
    # Deterministic and idempotent across repeated swaps (the parse cache is
    # keyed by the help TEXT, so it can never serve a stale section).
    again = hc.merge_installed_sections({"sections": []}, index)
    assert hc.serialize_corpus(again) == hc.serialize_corpus(merged)


# --------------------------------------------------------------------------- #
# frontend XSS-safety: Help render path uses no innerHTML on corpus content
# --------------------------------------------------------------------------- #

def test_help_render_path_has_no_innerhtml():
    html = INDEX_HTML  # already a str (assembled by ui.py); byte-identical
    start = html.index("function helpAppendHighlighted(")
    end = html.index("function findHelpWindow(")
    region = html[start:end]
    assert region, "could not locate Help render region"
    for forbidden in ("innerHTML", "insertAdjacentHTML", "outerHTML",
                      "DOMParser", ".innerHTML", "document.write"):
        assert forbidden not in region, \
            "Help render path must not use %s on corpus content" % forbidden
