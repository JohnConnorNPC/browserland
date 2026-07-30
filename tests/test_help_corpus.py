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
# XSS safety: corpus is typed plain data, never HTML
# --------------------------------------------------------------------------- #

_ALLOWED_BLOCK = {"p", "bullet", "sub"}
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
    # All 13 pages survive #113 (only mod-OWNED sections were migrated out).
    assert len(corpus["sections"]) == 13
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
                     "mousemode", "mod-sync"):
        assert mod_slug in slugs
    # #177 retired agent-docs to mods-deprecated/, which build_mod_sections does
    # not scan — so its Help section goes with it.
    assert "agent-docs" not in slugs
    # every mod section is tagged and sorts AFTER every wiki section.
    mod_orders = [s["order"] for s in full["sections"] if "mod" in s]
    wiki_orders = [s["order"] for s in full["sections"] if "mod" not in s]
    assert len(mod_orders) == 14   # +git (#116) +clipboard (#106) +scratchpad (#124) +recorder (#140) +host-registry (#65) +mousemode (#155) +mod-sync (#158) -agent-docs (#177)
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
