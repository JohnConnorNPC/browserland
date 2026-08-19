"""Tests for the ctx documentation-drift gate (#203 §4).

Two assertions carry the gate:

* ``test_packaged_json_in_sync_with_fragments`` -- the checked-in
  ``ctx_inventory.json`` must byte-match a fresh scan of the loader family,
  the way ``test_help_corpus.py::test_packaged_json_in_sync_with_wiki`` pins
  the help corpus.
* ``test_every_inventory_path_is_named_in_wiki_section_8`` -- every scanned
  member must be named in ``wiki/Writing-a-Mod.md`` §8, so a ctx addition
  without a docs touch fails CI whichever fragment it landed in.

The rest pin the scanner's own failure modes: it must fail LOUD rather than
report fewer members, and it must be newline-agnostic (this repo has CRLF
working copies).
"""

import re

import pytest

from webterm.broker import ctx_inventory as ci
from webterm.broker import ui

WIKI_MOD_DOC = ci._DIR.parent.parent / "wiki" / "Writing-a-Mod.md"


def _wiki_section_8() -> str:
    """The text of §8 (`The ctx surface`) with newlines normalized."""
    text = WIKI_MOD_DOC.read_bytes().decode("utf-8").replace(
        "\r\n", "\n").replace("\r", "\n")
    start = text.index("\n## 8.")
    end = text.index("\n## 9.", start)
    return text[start:end]


# --------------------------------------------------------------------------- #
# the two drift assertions
# --------------------------------------------------------------------------- #

def test_packaged_json_in_sync_with_fragments():
    # Regenerate-and-diff drift guard: the checked-in ctx_inventory.json must
    # byte-match a fresh scan of the 86* loader family. If this fails: run
    # `python -m webterm.broker.ctx_inventory` and name the new member in
    # wiki/Writing-a-Mod.md §8.
    fresh = ci.serialize_inventory(ci.build_inventory())
    assert ci.INVENTORY_JSON.read_bytes() == fresh, \
        "ctx_inventory.json is stale — run: python -m webterm.broker.ctx_inventory"


def _documented_paths(section):
    """Paths §8 actually names, scoped PER FAMILY.

    The obvious rule -- "the family appears somewhere, and the member name
    appears somewhere" -- has a hole exactly where new members land. §8 is
    108K characters, so a leaf like `list`, `get`, `set`, `open` or `url` is
    already a word somewhere in it under a DIFFERENT family; an undocumented
    `ctx.assets.list` would sail through green. Verified: it did.

    So a member counts only inside a region that also names its family. §8
    writes members three ways and all three are honoured -- the dotted form
    (`ctx.storage.get`), the bold bullet (**`closeAll()`**), and the
    slash-separated list under a family bullet (`ctx.file` -- `read` / `write`
    / `list`), which is how fifteen of the file members are written.
    """
    ok = set()
    # 1. the dotted form, anywhere.
    for m in re.finditer(r"ctx\.([A-Za-z_$][\w$]*)\.([A-Za-z_$][\w$]*)", section):
        ok.add(m.group(1) + "." + m.group(2))
    # 2. bare members, scoped to the ### subsection that names their family.
    for sub in re.split(r"\n(?=### )", section):
        families = {m.group(1) for m
                    in re.finditer(r"ctx\.([A-Za-z_$][\w$]*)", sub)}
        if not families:
            continue
        words = {m.group(1) for m
                 in re.finditer(r"`([A-Za-z_$][\w$]*)[`(]", sub)}
        for fam in families:
            for w in words:
                ok.add(fam + "." + w)
    return ok


def test_every_inventory_path_is_named_in_wiki_section_8():
    # The docs half of the gate. This pins NAME COVERAGE -- exactly what the
    # module docstring claims and no more -- but scoped per family, so a new
    # member cannot ride in on a leaf name another family already documents.
    section = _wiki_section_8()
    documented = _documented_paths(section)
    inv = ci.load_inventory()
    missing = []
    for path in inv["ctx"]:
        family = path.split(".")[0]
        if "ctx." + family not in section:
            missing.append(path + " (family `ctx.%s` absent)" % family)
        elif "." in path and path not in documented:
            missing.append(path + " (member not named under its family)")
    for path in inv["info"]:
        member = path.split(".")[-1]
        if not re.search(r"\b%s\b" % re.escape(member), section):
            missing.append(path + " (info member absent)")
    assert not missing, (
        "ctx members not documented in wiki/Writing-a-Mod.md §8: %s" % missing)


# --------------------------------------------------------------------------- #
# what the scan covers
# --------------------------------------------------------------------------- #

def test_family_is_discovered_from_ordered_not_hardcoded(monkeypatch):
    names = [p.name for p in ci.family_fragments()]
    assert names == [n for n in ui._ORDERED
                     if n.startswith("86") and "_js_mod_" in n]
    assert len(names) > 10, "the loader family is a family, not one file"
    # A fragment registered in _ORDERED but absent from disk is an error, not a
    # silent skip — that is the shape a typo'd new fragment takes.
    monkeypatch.setattr(ui, "_ORDERED", ui._ORDERED + ["86zz_js_mod_nope.js"])
    with pytest.raises(ci.CtxScanError):
        ci.family_fragments()


def test_inventory_covers_both_scanned_shapes():
    inv = ci.build_inventory()
    # from makeCtx's v1 literal
    assert "id" in inv["ctx"]
    assert "storage.get" in inv["ctx"]
    assert "settings.text" in inv["ctx"]
    assert "serverStore.getRevision" in inv["ctx"]
    # from extension-fragment extenders (86e…86p)
    assert "serverStore.saveChain" in inv["ctx"]
    assert "prefs.get" in inv["ctx"]
    assert "hosts.list" in inv["ctx"]
    assert "dialog.open" in inv["ctx"]
    assert "capabilities" in inv["ctx"]  # Object.defineProperty shape
    # info bag members
    assert "info.tapOutput" in inv["info"]
    # registerMod fields are OUT of scope (module docstring)
    assert "ctxVersion" in inv["ctx"] and "requires" not in inv["ctx"]


def test_serialize_is_deterministic_and_lf():
    data = ci.build_inventory()
    blob = ci.serialize_inventory(data)
    assert blob == ci.serialize_inventory(data)
    assert b"\r" not in blob and blob.endswith(b"\n")


# --------------------------------------------------------------------------- #
# CRLF: the bytes must not depend on the working copy's line endings
# --------------------------------------------------------------------------- #

def test_crlf_fragment_scans_identically(tmp_path):
    src_path = ci._DIR / "86_js_mod_loader.js"
    lf = ci.read_fragment(src_path)
    crlf_path = tmp_path / "86_js_mod_loader.js"
    crlf_path.write_bytes(lf.replace("\n", "\r\n").encode("utf-8"))
    assert b"\r\n" in crlf_path.read_bytes()
    assert ci.read_fragment(crlf_path) == lf
    mask = ci.mask_source(ci.read_fragment(crlf_path))
    assert ci.scan_ctx_literal(lf, mask, "crlf") == \
        ci.scan_ctx_literal(lf, ci.mask_source(lf), "lf")


def test_full_inventory_is_crlf_independent(tmp_path, monkeypatch):
    # The whole document, not just one fragment: a CRLF checkout must produce
    # the same JSON bytes, which is what makes the drift test portable.
    real = ci.family_fragments()
    lf_bytes = ci.serialize_inventory(ci.build_inventory())
    copies = []
    for p in real:
        q = tmp_path / p.name
        q.write_bytes(ci.read_fragment(p).replace("\n", "\r\n").encode("utf-8"))
        copies.append(q)
    monkeypatch.setattr(ci, "family_fragments", lambda: copies)
    # Compared against a fresh scan of the real tree, NOT the checked-in file:
    # this test is about line endings, and must not double as the drift guard.
    assert ci.serialize_inventory(ci.build_inventory()) == lf_bytes


# --------------------------------------------------------------------------- #
# it fails LOUD — a quiet partial scan is a green build over a missed member
# --------------------------------------------------------------------------- #

def test_unparseable_ctx_literal_raises():
    src = "function makeCtx(a, b) {\n    const ctx = {\n        id: a,\n"
    with pytest.raises(ci.CtxScanError):
        ci.scan_ctx_literal(src, ci.mask_source(src), "fake.js")


def test_missing_ctx_literal_raises():
    src = "function makeCtx(a, b) {\n    return {};\n}\n"
    with pytest.raises(ci.CtxScanError):
        ci.scan_ctx_literal(src, ci.mask_source(src), "fake.js")


def test_empty_ctx_literal_raises():
    src = "function makeCtx(a, b) {\n    const ctx = {};\n    return ctx;\n}\n"
    with pytest.raises(ci.CtxScanError):
        ci.scan_ctx_literal(src, ci.mask_source(src), "fake.js")


def test_extender_without_a_declaration_raises():
    src = "_registerCtxExtender(_ctxGhost);\n"
    with pytest.raises(ci.CtxScanError):
        ci.scan_extender(src, ci.mask_source(src), "_ctxGhost", "fake.js")


def test_extender_that_installs_nothing_raises():
    src = ("function _ctxQuiet(ctx) {\n"
           "    // ctx.thing = 1 -- only in a comment\n"
           "    return;\n"
           "}\n")
    with pytest.raises(ci.CtxScanError):
        ci.scan_extender(src, ci.mask_source(src), "_ctxQuiet", "fake.js")


def test_extender_with_an_unbalanced_body_raises():
    src = "function _ctxBroken(ctx) {\n    ctx.x = 1;\n"
    with pytest.raises(ci.CtxScanError):
        ci.scan_extender(src, ci.mask_source(src), "_ctxBroken", "fake.js")


def test_masking_keeps_offsets_and_hides_comments_and_strings():
    src = "const a = '{'; // }\nconst b = {x: 1};\n"
    mask = ci.mask_source(src)
    assert len(mask) == len(src)
    assert mask.count("\n") == src.count("\n")
    # the brace inside the string and the one in the comment are gone
    assert mask.count("{") == 1 and mask.count("}") == 1


def test_extender_shapes_the_scanner_knows():
    src = ("function _ctxDemo(ctx, rec) {\n"
           "    const fam = (ctx.zoo && typeof ctx.zoo === 'object')\n"
           "        ? ctx.zoo : {};\n"
           "    fam.feed = function () {};\n"
           "    ctx.zoo = fam;\n"
           "    ctx.bag = {\n"
           "        one: function () {},\n"
           "        two: 2,\n"
           "    };\n"
           "    Object.defineProperty(ctx, 'lazy', {get: function () {}});\n"
           "}\n")
    got = set(ci.scan_extender(src, ci.mask_source(src), "_ctxDemo", "fake.js"))
    assert got == {"zoo", "zoo.feed", "bag", "bag.one", "bag.two", "lazy"}
