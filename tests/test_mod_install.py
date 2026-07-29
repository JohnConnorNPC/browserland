"""Runtime-installed mods: the store, the scanner, the generations and the asset
route (#163), plus the ``x-`` id namespace they live in (#172).

An installed mod never enters ``INDEX_HTML``. It lives in a broker-config'd
``mods_dir`` beside ``webterm_state.json``, is content-addressed by generation,
and is served from an in-memory allowlist dict at
``GET /mods/<id>/<gen>/<name>`` -- so ``mods/``, ``ui._MODS``, the assembled
bundle, its CSP hash and every drift guard around them are untouched, and the
catalog simply gains a second, LABELLED source.

Three properties get most of the attention here, because each one is a place
where a plausible implementation is wrong on THIS platform:

* the filename grammar is Windows-specific and load-bearing. ``base.css:x.js``
  is an NTFS alternate data stream that passes any "it's a bare name" test;
  ``CON.js`` is the console device in every directory; ``A.js`` and ``a.js`` are
  one file and two index entries on a case-insensitive volume; a trailing dot is
  silently stripped by Win32. The tests below assert distinct FILE IDENTITIES,
  not just distinct names.
* the asset route can only ever perform a dict get, so traversal has to be
  unrepresentable rather than filtered. The traversal set is asserted to be
  404s, but the property being tested is that none of them ever reaches a
  filesystem call at all.
* the sort is over ONE graph of shipped ∪ installed ids, an edge to an unknown
  id is DROPPED rather than counted (or an installed mod that requires a shipped
  one would come out cyclic), and Kahn's residual is split into in-cycle vs
  blocked-by-cycle -- they are different statuses and different user-facing
  answers.

Fixtures are planted by writing the store exactly as the broker writes it
(canonical ``mod.json``, content-addressed generation dir, ``CURRENT`` pointer,
``.gen.json``), so the scanner is exercised against realistic bytes rather than
a shape invented for the test.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from .auth_helpers import TEST_TOKEN, authed
from webterm.broker import modinstall, ui
from webterm.broker.app import create_app

_app_seq = 0

JS_CTYPE = "application/javascript; charset=utf-8"


# --------------------------------------------------------------------------- #
# fixtures
# --------------------------------------------------------------------------- #

def plant_raw(mods_dir, mod_id, manifest_obj, raw_files, *, gen=None,
              installed_at=1_700_000_000, current=True, marker=True):
    """Write one generation into the store WITHOUT validating it first.

    The primitive behind ``plant``: it is what lets a test plant a BOM, a
    missing final newline or a 300 KiB file and then assert the scanner refuses
    it. ``raw_files`` maps name -> bytes."""
    mods_dir = Path(mods_dir)
    mods_dir.mkdir(parents=True, exist_ok=True)
    if marker:
        (mods_dir / modinstall.MARKER_NAME).write_bytes(b"")
    gen = gen or ("a" * 64)
    gdir = mods_dir / mod_id / gen
    gdir.mkdir(parents=True, exist_ok=True)
    if manifest_obj is not None:
        (gdir / modinstall.MANIFEST_NAME).write_text(
            json.dumps(manifest_obj), encoding="utf-8")
    for name, data in raw_files.items():
        (gdir / name).write_bytes(data)
    (gdir / modinstall.GEN_META_NAME).write_text(
        json.dumps({"gen": gen, "installed_at": installed_at, "files": {}}),
        encoding="utf-8")
    if current:
        (mods_dir / mod_id / modinstall.CURRENT_NAME).write_text(
            json.dumps({"gen": gen}), encoding="utf-8")
    return gen


def plant(mods_dir, manifest, files, **kw):
    """Write a VALID mod, content-addressed exactly as an install would."""
    canonical, records = modinstall.validate_package(manifest, files)
    gen = kw.pop("gen", None) or modinstall.compute_gen(canonical, records)
    plant_raw(mods_dir, canonical["id"], canonical,
              {name: rec["data"] for name, rec in records.items()},
              gen=gen, **kw)
    return gen


def manifest(mod_id="x-notes", **extra):
    out = {"id": mod_id, "version": "1.0.0", "ctxVersion": 1,
           "title": "Notes", "description": "a note pad",
           "scripts": [f"{mod_id}.js"]}
    out.update(extra)
    return out


def files(mod_id="x-notes", js="registerMod({ id: 'x-notes' });\n"):
    return {f"{mod_id}.js": js}


def make_app(tmp_path, monkeypatch, **cfg):
    global _app_seq
    _app_seq += 1
    monkeypatch.delenv("WEB_TERMINAL_TOKEN", raising=False)
    conf = {"state_path": str(tmp_path / "webterm_state.json"),
            "auth_token": TEST_TOKEN,
            "mods_dir": str(tmp_path / "mods")}
    conf.update(cfg)
    return create_app(conf, name=f"webterm-modinstall-test-{_app_seq}")


# --------------------------------------------------------------------------- #
# the filename grammar (Windows-specific, load-bearing)
# --------------------------------------------------------------------------- #

def test_filename_grammar_refuses_the_windows_and_url_hazards():
    ok = ["a.js", "notes.css", "help.md", "a-b_c.1.js", "A.JS",
          ("x" * 61) + ".js"]
    for name in ok:
        assert modinstall.file_name_error(name) is None, name

    bad = {
        # path escapes
        "../x.js": "bad_file_name", "..\\x.js": "bad_file_name",
        "/abs.js": "bad_file_name", "a/b.js": "bad_file_name",
        "a\\b.js": "bad_file_name", "..": "bad_file_name",
        ".": "bad_file_name", "": "bad_file_name",
        # NTFS alternate data stream: writing "base.css:payload.js" creates a
        # HIDDEN STREAM on the file "base.css", which a naive bare-name check
        # waves through because it contains no separator at all.
        "base.css:payload.js": "bad_file_name",
        "a.js:$DATA": "bad_file_name",
        # not a safe URL segment
        "a%2e.js": "bad_file_name", "a#b.js": "bad_file_name",
        "a?b.js": "bad_file_name",
        # control characters
        "a\x00.js": "bad_file_name", "a\n.js": "bad_file_name",
        "a\x7f.js": "bad_file_name",
        # Win32 device names, with and without an extension
        "con.js": "bad_file_name", "CON.js": "bad_file_name",
        "nul.css": "bad_file_name", "COM1.js": "bad_file_name",
        "lpt9.md": "bad_file_name", "aux.js": "bad_file_name",
        "con.foo.js": "bad_file_name",
        # trailing dot -- Win32 STRIPS it, so "a.js." would collide with "a.js"
        "a.js.": "bad_file_name",
        # 8.3 short-name shape (~ is simply not in the charset)
        "LONGFI~1.JS": "bad_file_name",
        # leading dot / hyphen, spaces, over-length
        ".hidden.js": "bad_file_name", "-lead.js": "bad_file_name",
        "a b.js": "bad_file_name", ("x" * 65) + ".js": "bad_file_name",
        # suffixes we do not ship
        "a.json": "bad_file_name", "a.exe": "bad_file_name", "a": "bad_file_name",
        # broker-owned names
        "mod.json": "reserved_file_name", "MOD.JSON": "reserved_file_name",
        "CURRENT": "reserved_file_name", ".gen.json": "reserved_file_name",
    }
    for name, code in bad.items():
        assert modinstall.file_name_error(name) == code, name
    for junk in (None, 7, b"a.js", [], True):
        assert modinstall.file_name_error(junk) == "bad_file_name"


def test_case_folded_collisions_are_refused():
    # On a case-insensitive volume A.js and a.js are ONE file and TWO index
    # entries: the second write silently overwrites the first, so the mod ships
    # bytes nobody reviewed under a name the manifest still lists.
    with pytest.raises(modinstall.ValidationError) as exc:
        modinstall.validate_package(
            manifest(scripts=["a.js"]),
            {"a.js": "registerMod({});\n", "A.js": "evil();\n"})
    assert exc.value.code == "bad_file_name"


@pytest.mark.skipif(os.name != "nt", reason="Win32 filename identity")
def test_accepted_names_are_distinct_files_on_windows(tmp_path):
    """The grammar's real obligation: two accepted names must be two FILES.

    Name-level distinctness is not enough on NTFS -- ``a.js:x`` is a stream on
    ``a.js``, ``a.js.`` is ``a.js``, and ``LONGFI~1.JS`` can be an alias for a
    long name. Each of those is refused by the grammar; this asserts the
    positive half, that everything the grammar ACCEPTS lands on its own file
    identity."""
    names = ["a.js", "ab.js", "a-b.js", "a_b.js", "a.b.js", "notes.css",
             "help.md", "A1.js"]
    identities = {}
    for i, name in enumerate(names):
        p = tmp_path / name
        p.write_text(f"// {i}\n", encoding="utf-8")
        st = p.stat()
        key = (st.st_dev, st.st_ino)
        assert key not in identities, \
            f"{name} shares a file identity with {identities.get(key)}"
        identities[key] = name
    assert len(identities) == len(names)
    # ...and no accepted name grew a hidden alternate data stream.
    for name in names:
        assert (tmp_path / name).read_text(encoding="utf-8").startswith("// ")


def test_the_line_cap_is_literally_the_fragment_line_cap():
    # The installed-file rules ARE ui._css_servable's rules; the cap is imported
    # rather than retyped so the two rule sets cannot drift.
    assert modinstall.line_cap() == ui._MAX_LINES


# --------------------------------------------------------------------------- #
# per-file byte rules
# --------------------------------------------------------------------------- #

def test_byte_rules_reject_bom_missing_newline_and_oversize():
    bad = [
        ("\ufeffregisterMod({});\n", "bad_encoding"),        # UTF-8 BOM
        ("registerMod({});", "bad_encoding"),                # no final newline
        ("\n" * (ui._MAX_LINES + 1), "file_too_large"),      # over the line cap
        ("/" * (modinstall.MAX_FILE_BYTES + 1) + "\n", "file_too_large"),
        (b"registerMod({});\n", "bad_encoding"),             # not a str
        ("\ud800\n", "bad_encoding"),                        # lone surrogate
    ]
    for body, code in bad:
        with pytest.raises(modinstall.ValidationError) as exc:
            modinstall.validate_package(manifest(), {"x-notes.js": body})
        assert exc.value.code == code, body if isinstance(body, str) else "bytes"
    # Exactly at the caps still passes.
    ok = "x" * (modinstall.MAX_FILE_BYTES - 1) + "\n"
    modinstall.validate_package(manifest(), {"x-notes.js": ok})
    modinstall.validate_package(manifest(),
                                {"x-notes.js": "\n" * ui._MAX_LINES})


def test_package_totals_are_capped():
    half = "x" * (modinstall.MAX_FILE_BYTES - 1) + "\n"
    with pytest.raises(modinstall.ValidationError) as exc:
        modinstall.validate_package(
            manifest(scripts=["x-notes.js"]),
            {"x-notes.js": half, "b.js": half, "c.js": half})
    assert exc.value.code == "total_too_large"
    with pytest.raises(modinstall.ValidationError) as exc:
        modinstall.validate_package(
            manifest(),
            {f"f{i}.js": "//\n" for i in range(modinstall.MAX_FILES + 1)})
    assert exc.value.code == "too_many_files"


# --------------------------------------------------------------------------- #
# CSS may not reach an external origin
# --------------------------------------------------------------------------- #

def test_installed_css_may_not_reference_an_external_origin():
    # The CSP sets ONLY script-src and frame-ancestors -- no default-src, no
    # style-src -- so a stylesheet's @import / url() is entirely unrestricted
    # and would be a SILENT egress channel out of a broker whose only outbound
    # HTTP is the deliberately-closed /status/fetch.
    refused = [
        "@import url('https://evil.example/x.css');\n",
        '@import "https://evil.example/x.css";\n',
        ".a { background: url(https://evil.example/p.png); }\n",
        ".a { background: url(http://evil.example/p.png); }\n",
        ".a { background: url(//evil.example/p.png); }\n",
        '.a { background: url("//evil.example/p.png"); }\n',
        "@font-face { src: url(https://evil.example/f.woff2); }\n",
    ]
    for css in refused:
        with pytest.raises(modinstall.ValidationError) as exc:
            modinstall.validate_package(
                manifest(styles=["x-notes.css"]),
                {"x-notes.js": "//\n", "x-notes.css": css})
        assert exc.value.code == "css_external_reference", css
    allowed = [
        ".a { background: url(bg.png); }\n",
        ".a { background: url('./bg.png'); }\n",
        ".a { background: url(data:image/gif;base64,R0lGOD); }\n",
        "/* @import url(https://evil.example/x.css); */\n.a { color: red; }\n",
        ".a { color: red; }\n",
    ]
    for css in allowed:
        modinstall.validate_package(
            manifest(styles=["x-notes.css"]),
            {"x-notes.js": "//\n", "x-notes.css": css})


# --------------------------------------------------------------------------- #
# the manifest
# --------------------------------------------------------------------------- #

def test_manifest_id_must_be_in_the_installed_namespace():
    # #172: the unprefixed namespace is RESERVED for shipped mods. Installing
    # "clock" would inherit the shipped clock's pins, its /mod-store value and
    # its webterm:mod:clock:* localStorage keys.
    for mod_id, code in (("clock", "reserved_id"), ("editor", "reserved_id"),
                         ("Notes", "bad_mod_id"), ("x notes", "bad_mod_id"),
                         ("../x", "bad_mod_id"), ("", "bad_mod_id"),
                         (None, "bad_mod_id"), ("a" * 65, "bad_mod_id")):
        with pytest.raises(modinstall.ValidationError) as exc:
            modinstall.validate_package(manifest(mod_id="x-notes", id=mod_id),
                                        {"x-notes.js": "//\n"})
        assert exc.value.code == code, mod_id
    canonical, _ = modinstall.validate_package(manifest(),
                                               {"x-notes.js": "//\n"})
    assert canonical["id"] == "x-notes"


def test_unknown_manifest_keys_are_refused_not_ignored():
    # A typo ("styles" -> "style") must be LOUD, not a mod that silently ships
    # without its stylesheet.
    with pytest.raises(modinstall.ValidationError) as exc:
        modinstall.validate_package(manifest(style=["x-notes.css"]),
                                    {"x-notes.js": "//\n"})
    assert exc.value.code == "unknown_manifest_key"
    with pytest.raises(modinstall.ValidationError) as exc:
        modinstall.validate_package(manifest(help={"labl": "Notes"}),
                                    {"x-notes.js": "//\n"})
    assert exc.value.code == "unknown_manifest_key"


def test_scripts_are_required_and_entry_is_not_accepted():
    # `entry` is a shipped-tree legacy field. Accepting it here would give two
    # ways to say the same thing and one of them unvalidated.
    for meta, code in (
            ({"id": "x-notes", "entry": "x-notes.js"}, "unknown_manifest_key"),
            ({"id": "x-notes"}, "bad_scripts"),
            ({"id": "x-notes", "scripts": []}, "bad_scripts"),
            ({"id": "x-notes", "scripts": "x-notes.js"}, "bad_scripts"),
            ({"id": "x-notes", "scripts": ["missing.js"]}, "bad_scripts"),
            ({"id": "x-notes", "scripts": ["x-notes.css"]}, "bad_scripts"),
            ({"id": "x-notes", "scripts": ["../x.js"]}, "bad_scripts"),
            ({"id": "x-notes",
              "scripts": ["x-notes.js", "x-notes.js"]}, "bad_scripts")):
        with pytest.raises(modinstall.ValidationError) as exc:
            modinstall.validate_package(meta, {"x-notes.js": "//\n"})
        assert exc.value.code == code, meta


def test_styles_requires_tiers_and_help_are_typed():
    base = {"x-notes.js": "//\n", "x-notes.css": ".a{}\n"}
    cases = [
        (manifest(styles=["missing.css"]), "bad_styles"),
        (manifest(styles=["x-notes.js"]), "bad_styles"),
        (manifest(styles="x-notes.css"), "bad_styles"),
        (manifest(requires=["x-notes"]), "bad_requires"),      # self-reference
        (manifest(requires=["Bad Id"]), "bad_requires"),
        (manifest(requires="editor"), "bad_requires"),
        (manifest(tiers=["x" * 33]), "bad_manifest_field"),
        (manifest(tiers=list(range(9))), "bad_manifest_field"),
        (manifest(title="x" * 81), "bad_manifest_field"),
        (manifest(description="x" * 401), "bad_manifest_field"),
        (manifest(version=1.0), "bad_manifest_field"),
        (manifest(ctxVersion="1"), "bad_manifest_field"),
        (manifest(ctxVersion=True), "bad_manifest_field"),
        (manifest(help=[]), "bad_manifest_field"),
        (manifest(help={"order": "2100"}), "bad_manifest_field"),
        (manifest(help={"icon": "x" * 9}), "bad_manifest_field"),
        (manifest(defaultEnabled="true"), "bad_manifest_field"),
    ]
    for meta, code in cases:
        with pytest.raises(modinstall.ValidationError) as exc:
            modinstall.validate_package(meta, base)
        assert exc.value.code == code, meta


def test_canonical_manifest_drops_defaultEnabled_and_help_slug():
    canonical, _ = modinstall.validate_package(
        manifest(defaultEnabled=True, requires=["editor", "editor"],
                 help={"label": "Notes", "icon": "N", "order": 2100,
                       "slug": "clock"}),
        {"x-notes.js": "//\n"})
    # defaultEnabled is accepted and IGNORED -- installing on one broker must
    # not silently switch a mod on for every browser that loads its page.
    assert "defaultEnabled" not in canonical
    # help.slug is dropped too: an installed section's slug is FORCED to the mod
    # id, so a collision with a wiki or shipped slug is impossible rather than
    # merely handled.
    assert canonical["help"] == {"label": "Notes", "icon": "N", "order": 2100}
    assert canonical["requires"] == ["editor"]          # deduped
    assert set(canonical) == {"id", "version", "ctxVersion", "title",
                              "description", "scripts", "styles", "requires",
                              "tiers", "help"}


def test_the_generation_hash_covers_the_manifest_not_just_the_files():
    # A mod whose only change is its `requires` list has IDENTICAL files. Reusing
    # the old gen would leave every cached URL pointing at the old graph.
    payload = {"x-notes.js": "//\n"}
    a = modinstall.compute_gen(*modinstall.validate_package(manifest(), payload))
    b = modinstall.compute_gen(
        *modinstall.validate_package(manifest(requires=["editor"]), payload))
    c = modinstall.compute_gen(
        *modinstall.validate_package(manifest(), {"x-notes.js": "// v2\n"}))
    assert a != b and a != c and b != c
    assert modinstall.GEN_RE.fullmatch(a)


# --------------------------------------------------------------------------- #
# the scanner
# --------------------------------------------------------------------------- #

def test_scan_reads_a_planted_mod(tmp_path):
    gen = plant(tmp_path / "mods", manifest(styles=["x-notes.css"]),
                {"x-notes.js": "registerMod({ id: 'x-notes' });\n",
                 "x-notes.css": ".notes { color: red; }\n",
                 "help.md": "# Notes\n\nsome help\n"})
    index = modinstall.scan(tmp_path / "mods")
    assert set(index["mods"]) == {"x-notes"}
    rec = index["mods"]["x-notes"]
    assert rec["gen"] == gen
    assert rec["installed_at"] == 1_700_000_000        # from .gen.json, not mtime
    assert rec["help_md"].startswith("# Notes")
    assert set(rec["files"]) == {"x-notes.js", "x-notes.css", "help.md"}
    # Only .js/.css are servable; help.md is a broker-side input, never a URL.
    assert set(index["assets"]) == {f"x-notes/{gen}/x-notes.js",
                                    f"x-notes/{gen}/x-notes.css"}
    assert index["assets"][f"x-notes/{gen}/x-notes.js"][1] == JS_CTYPE
    assert index["skipped"] == {}


def test_scan_of_a_missing_or_empty_dir_is_empty(tmp_path):
    assert modinstall.scan(None) == modinstall.empty_index()
    assert modinstall.scan(tmp_path / "nope") == modinstall.empty_index()
    (tmp_path / "mods").mkdir()
    assert modinstall.scan(tmp_path / "mods") == modinstall.empty_index()


def test_scan_skips_a_non_x_directory(tmp_path):
    # #172: a hand-dropped mods_dir/clock/ must NEVER shadow the shipped clock.
    plant_raw(tmp_path / "mods", "clock",
              {"id": "clock", "scripts": ["clock.js"]},
              {"clock.js": b"registerMod({ id: 'clock' });\n"})
    index = modinstall.scan(tmp_path / "mods")
    assert index["mods"] == {}
    assert index["skipped"] == {"clock": "reserved_id"}


def test_scan_skips_a_mod_whose_manifest_claims_another_id(tmp_path):
    plant_raw(tmp_path / "mods", "x-notes",
              {"id": "x-other", "scripts": ["a.js"]}, {"a.js": b"//\n"})
    index = modinstall.scan(tmp_path / "mods")
    assert index["mods"] == {}
    assert index["skipped"] == {"x-notes": "bad_mod_id"}


def test_scan_skips_bad_bytes_and_says_why(tmp_path):
    cases = {
        "x-bom": (b"\xef\xbb\xbfregisterMod({});\n", "bad_encoding"),
        "x-nonl": (b"registerMod({});", "bad_encoding"),
        "x-latin": (b"// caf\xe9\n", "bad_encoding"),
        "x-lines": (b"\n" * (ui._MAX_LINES + 1), "file_too_large"),
        "x-big": (b"/" * (modinstall.MAX_FILE_BYTES + 1) + b"\n",
                  "file_too_large"),
    }
    for mod_id, (body, _code) in cases.items():
        plant_raw(tmp_path / "mods", mod_id,
                  {"id": mod_id, "scripts": ["a.js"]}, {"a.js": body})
    index = modinstall.scan(tmp_path / "mods")
    assert index["mods"] == {}
    assert index["skipped"] == {mid: code for mid, (_b, code) in cases.items()}


def test_scan_skips_css_reaching_an_external_origin(tmp_path):
    plant_raw(tmp_path / "mods", "x-leak",
              {"id": "x-leak", "scripts": ["a.js"], "styles": ["a.css"]},
              {"a.js": b"//\n",
               "a.css": b"@import url(https://evil.example/x.css);\n"})
    index = modinstall.scan(tmp_path / "mods")
    assert index["skipped"] == {"x-leak": "css_external_reference"}


def test_scan_ignores_inert_junk_but_refuses_a_missing_declaration(tmp_path):
    # An unknown suffix is inert; refusing a whole mod over a stray .DS_Store is
    # hostile. A file the manifest actually DECLARES is a different matter.
    plant_raw(tmp_path / "mods", "x-notes",
              {"id": "x-notes", "scripts": ["a.js"]},
              {"a.js": b"//\n", ".DS_Store": b"junk", "notes.bak": b"junk",
               "read me.txt": b"junk"})
    index = modinstall.scan(tmp_path / "mods")
    assert set(index["mods"]) == {"x-notes"}
    assert set(index["mods"]["x-notes"]["files"]) == {"a.js"}

    plant_raw(tmp_path / "mods2", "x-gone",
              {"id": "x-gone", "scripts": ["a.js", "b.js"]}, {"a.js": b"//\n"})
    assert modinstall.scan(tmp_path / "mods2")["skipped"] == \
        {"x-gone": "bad_scripts"}


def test_scan_needs_a_well_formed_current_pointer(tmp_path):
    root = tmp_path / "mods"
    plant(root, manifest(), files())
    current = root / "x-notes" / modinstall.CURRENT_NAME
    for junk in ('{"gen": "nope"}', "{ truncated", '{"gen": 7}', "[]",
                 '{"gen": "../../etc"}', '{"gen": "' + "A" * 64 + '"}'):
        current.write_text(junk, encoding="utf-8")
        index = modinstall.scan(root)
        assert index["mods"] == {}, junk
        assert index["skipped"] == {"x-notes": "not_installed"}, junk
    # A pointer naming a generation that is not on disk is equally not-installed.
    current.write_text(json.dumps({"gen": "b" * 64}), encoding="utf-8")
    assert modinstall.scan(root)["skipped"] == {"x-notes": "bad_mod_id"}


def test_scan_refuses_a_symlinked_mod_directory(tmp_path):
    real = tmp_path / "outside"
    plant(real, manifest(), files())
    root = tmp_path / "mods"
    root.mkdir()
    (root / modinstall.MARKER_NAME).write_bytes(b"")
    try:
        os.symlink(str(real / "x-notes"), str(root / "x-notes"),
                   target_is_directory=True)
    except (OSError, NotImplementedError, AttributeError) as exc:
        pytest.skip(f"cannot create a symlink here: {exc}")
    index = modinstall.scan(root)
    assert index["mods"] == {}
    assert index["skipped"] == {"x-notes": "bad_mod_id"}


def test_scan_refuses_a_junction_to_an_outside_tree(tmp_path):
    # The Windows-native version of the same escape: a directory junction is a
    # reparse point that os.path.islink() reports as a plain directory before
    # 3.12, so islink() alone is not enough.
    if os.name != "nt":
        pytest.skip("junctions are Windows-only")
    real = tmp_path / "outside"
    plant(real, manifest(), files())
    root = tmp_path / "mods"
    root.mkdir()
    (root / modinstall.MARKER_NAME).write_bytes(b"")
    rc = os.system(f'mklink /J "{root / "x-notes"}" "{real / "x-notes"}" >nul')
    if rc != 0 or not (root / "x-notes").exists():
        pytest.skip("could not create a junction here")
    index = modinstall.scan(root)
    assert index["mods"] == {}
    assert index["skipped"] == {"x-notes": "bad_mod_id"}


def test_scan_is_bounded_by_max_mods(tmp_path):
    root = tmp_path / "mods"
    for i in range(modinstall.MAX_MODS + 3):
        mid = f"x-m{i:03d}"
        plant(root, manifest(mod_id=mid, scripts=[f"{mid}.js"]),
              {f"{mid}.js": "//\n"})
    index = modinstall.scan(root)
    assert len(index["mods"]) == modinstall.MAX_MODS
    assert set(index["skipped"].values()) == {"too_many_mods"}


def test_scan_retains_the_previous_generation_for_assets_only(tmp_path):
    root = tmp_path / "mods"
    old = plant(root, manifest(), {"x-notes.js": "// v1\n"})
    new = plant(root, manifest(), {"x-notes.js": "// v2\n"})
    assert old != new
    index = modinstall.scan(root)
    # The catalog and Help describe CURRENT and nothing else...
    assert index["mods"]["x-notes"]["gen"] == new
    # ...but a page that started booting against the old generation can still
    # fetch its files, so it can never be handed a mix of the two.
    assert index["assets"][f"x-notes/{old}/x-notes.js"][0] == b"// v1\n"
    assert index["assets"][f"x-notes/{new}/x-notes.js"][0] == b"// v2\n"


def test_scan_retains_at_most_one_predecessor(tmp_path):
    root = tmp_path / "mods"
    gens = [plant(root, manifest(), {"x-notes.js": f"// v{i}\n"})
            for i in range(4)]
    index = modinstall.scan(root)
    served = {key.split("/")[1] for key in index["assets"]}
    assert len(served) == modinstall.RETAINED_GENERATIONS
    assert gens[-1] in served


# --------------------------------------------------------------------------- #
# the catalog: one graph, dropped edges, cycles vs blocked-by-cycle
# --------------------------------------------------------------------------- #

def _plant_graph(root, graph):
    for mod_id, requires in graph.items():
        plant(root, manifest(mod_id=mod_id, scripts=[f"{mod_id}.js"],
                             requires=list(requires)),
              {f"{mod_id}.js": "//\n"})
    return modinstall.scan(root)


def test_catalog_row_shape(tmp_path):
    gen = plant(tmp_path / "mods",
                manifest(styles=["x-notes.css"], requires=["editor"],
                         tiers=["settings"]),
                {"x-notes.js": "//\n", "x-notes.css": ".a{}\n"})
    rows = modinstall.catalog(modinstall.scan(tmp_path / "mods"),
                              [m["id"] for m in ui.mod_catalog()])
    assert len(rows) == 1
    row = rows[0]
    assert row == {
        "id": "x-notes", "title": "Notes", "description": "a note pad",
        "version": "1.0.0",
        # ALWAYS false for an installed mod, whatever the manifest declared.
        "default_enabled": False,
        "requires": ["editor"], "source": "installed", "gen": gen,
        "scripts": ["x-notes.js"], "styles": ["x-notes.css"],
        "integrity": row["integrity"], "error": None, "missing_requires": [],
    }
    assert set(row["integrity"]) == {"x-notes.js", "x-notes.css"}
    for value in row["integrity"].values():
        assert value.startswith("sha256-")
    # The shipped half is labelled too, and stays shipped-only.
    assert {m["source"] for m in ui.mod_catalog()} == {"shipped"}


def test_a_declared_default_enabled_is_ignored(tmp_path):
    plant(tmp_path / "mods", manifest(defaultEnabled=True), files())
    rows = modinstall.catalog(modinstall.scan(tmp_path / "mods"))
    assert rows[0]["default_enabled"] is False


def test_an_edge_to_a_shipped_mod_is_satisfied_not_missing(tmp_path):
    # THE bug the one-graph rule exists to prevent: counting an edge to an id
    # outside the installed set would leave x-notes with a permanent indegree,
    # i.e. marked cyclic for requiring a mod that is right there in the page.
    index = _plant_graph(tmp_path / "mods", {"x-notes": ["editor"]})
    rows = modinstall.catalog(index, [m["id"] for m in ui.mod_catalog()])
    assert rows[0]["error"] is None
    assert rows[0]["missing_requires"] == []


def test_an_edge_to_an_unknown_id_is_reported_not_counted(tmp_path):
    index = _plant_graph(tmp_path / "mods", {"x-notes": ["x-absent"]})
    rows = modinstall.catalog(index, [m["id"] for m in ui.mod_catalog()])
    assert rows[0]["error"] is None                # loadable, just incomplete
    assert rows[0]["missing_requires"] == ["x-absent"]


def test_installed_rows_are_topologically_sorted(tmp_path):
    # x-c -> x-b -> x-a, planted in an order that is NOT the dependency order.
    index = _plant_graph(tmp_path / "mods",
                         {"x-c": ["x-b"], "x-b": ["x-a"], "x-a": []})
    order = [r["id"] for r in modinstall.catalog(index)]
    assert order == ["x-a", "x-b", "x-c"]
    assert all(r["error"] is None for r in modinstall.catalog(index))


def test_a_cycle_and_what_is_merely_blocked_by_it_are_different(tmp_path):
    # Kahn's residual for A<->B, C->A is {A,B,C} -- but C is not IN a cycle, it
    # is blocked BY one, which is a different status and a different fix.
    index = _plant_graph(tmp_path / "mods",
                         {"x-a": ["x-b"], "x-b": ["x-a"], "x-c": ["x-a"],
                          "x-d": []})
    rows = {r["id"]: r for r in modinstall.catalog(index)}
    assert rows["x-a"]["error"] == "requires_cycle"
    assert rows["x-b"]["error"] == "requires_cycle"
    assert rows["x-c"]["error"] == "blocked_by_cycle"
    assert rows["x-d"]["error"] is None          # unrelated mods are unaffected


def test_the_sort_is_stable_and_deterministic(tmp_path):
    index = _plant_graph(tmp_path / "mods",
                         {"x-z": [], "x-a": [], "x-m": ["x-z"]})
    first = [r["id"] for r in modinstall.catalog(index)]
    assert first == [r["id"] for r in modinstall.catalog(index)]
    assert first.index("x-z") < first.index("x-m")


# --------------------------------------------------------------------------- #
# GET /mods/<id>/<gen>/<name>
# --------------------------------------------------------------------------- #

def test_asset_route_serves_installed_bytes_immutably(tmp_path, monkeypatch):
    gen = plant(tmp_path / "mods", manifest(styles=["x-notes.css"]),
                {"x-notes.js": "registerMod({ id: 'x-notes' });\n",
                 "x-notes.css": ".notes { color: red; }\n",
                 "help.md": "# Notes\n\nhelp\n"})
    app = make_app(tmp_path, monkeypatch)
    # PUBLIC on purpose: a <script src> cannot carry an Authorization header.
    _, r = app.test_client.get(f"/mods/x-notes/{gen}/x-notes.js")
    assert r.status == 200
    assert r.body == b"registerMod({ id: 'x-notes' });\n"
    assert r.headers["Content-Type"] == JS_CTYPE
    # immutable is honest because the URL is CONTENT-ADDRESSED.
    assert r.headers["Cache-Control"] == "public, max-age=31536000, immutable"
    _, css = app.test_client.get(f"/mods/x-notes/{gen}/x-notes.css")
    assert css.status == 200
    assert css.headers["Content-Type"] == "text/css; charset=utf-8"
    # help.md is a broker-side input, not a servable asset.
    _, md = app.test_client.get(f"/mods/x-notes/{gen}/help.md")
    assert md.status == 404
    for name in (modinstall.MANIFEST_NAME, modinstall.GEN_META_NAME,
                 modinstall.CURRENT_NAME):
        _, hidden = app.test_client.get(f"/mods/x-notes/{gen}/{name}")
        assert hidden.status == 404, name


def test_asset_route_404s_an_unknown_id_gen_or_name(tmp_path, monkeypatch):
    gen = plant(tmp_path / "mods", manifest(), files())
    app = make_app(tmp_path, monkeypatch)
    for path in (f"/mods/x-other/{gen}/x-notes.js",
                 "/mods/x-notes/" + "b" * 64 + "/x-notes.js",
                 f"/mods/x-notes/{gen}/other.js",
                 f"/mods/x-notes/{gen[:-1]}/x-notes.js",
                 f"/mods/x-notes/{gen.upper()}/x-notes.js",
                 f"/mods/clock/{gen}/x-notes.js"):
        _, r = app.test_client.get(path)
        assert r.status == 404, path


def test_asset_route_cannot_express_a_traversal(tmp_path, monkeypatch):
    gen = plant(tmp_path / "mods", manifest(), files())
    app = make_app(tmp_path, monkeypatch)
    secret = tmp_path / "secret.js"
    secret.write_text("// secret\n", encoding="utf-8")
    hostile = [
        f"/mods/x-notes/{gen}/../../../app.py",
        f"/mods/x-notes/{gen}/..%2f..%2fapp.py",
        f"/mods/x-notes/{gen}/%2e%2e%2fapp.py",
        f"/mods/x-notes/{gen}/..\\..\\app.py",
        f"/mods/x-notes/{gen}/{secret}",
        f"/mods/x-notes/{gen}/C:/Windows/win.ini",
        f"/mods/x-notes/{gen}/x-notes.js:$DATA",
        f"/mods/x-notes/{gen}/con.js",
        f"/mods/x-notes/{gen}/X-NOTES.JS",
        f"/mods/x-notes/{gen}/x-notes.js.",
        f"/mods/x-notes/{gen}/",
        f"/mods/x-notes/{gen}",
        f"/mods/../../etc/passwd/{gen}/x-notes.js",
        f"/mods/x-notes/../../{gen}/x-notes.js",
    ]
    for path in hostile:
        _, r = app.test_client.get(path)
        assert r.status != 200, path
        assert b"secret" not in (r.body or b""), path
        assert b"import" not in (r.body or b"")[:200], path


def test_asset_route_is_absent_on_a_headless_broker(tmp_path, monkeypatch):
    gen = plant(tmp_path / "mods", manifest(), files())
    app = make_app(tmp_path, monkeypatch, serve_ui=False)
    # #87: no page, so no mods -- the store is not even scanned.
    assert app.ctx.mods_index == modinstall.empty_index()
    _, r = app.test_client.get(f"/mods/x-notes/{gen}/x-notes.js")
    assert r.status == 404
    _, info = authed(app).get("/info")
    assert info.json["mods"] == []
    assert info.json["serve_ui"] is False


def test_the_asset_route_does_not_shadow_mods_policy(tmp_path, monkeypatch):
    # Four segments vs two. A future single- or double-segment /mods/* route
    # WOULD collide, which is why this is pinned rather than assumed.
    plant(tmp_path / "mods", manifest(), files())
    app = make_app(tmp_path, monkeypatch)
    _, r = authed(app).post("/mods/policy", json={"set": {"x-notes": True}})
    assert r.status == 200
    assert r.json["policy"] == {"x-notes": True}


# --------------------------------------------------------------------------- #
# GET /info: two sources, one list
# --------------------------------------------------------------------------- #

def test_info_reports_shipped_then_installed_with_provenance(tmp_path,
                                                             monkeypatch):
    plant(tmp_path / "mods", manifest(requires=["editor"]), files())
    plant(tmp_path / "mods",
          manifest(mod_id="x-dep", scripts=["x-dep.js"], requires=["x-notes"]),
          {"x-dep.js": "//\n"})
    app = make_app(tmp_path, monkeypatch)
    _, r = authed(app).get("/info")
    assert r.status == 200
    rows = r.json["mods"]
    sources = [m["source"] for m in rows]
    # Shipped first, installed after: modPolicyImplied and #158's planFor walk
    # the catalog BACKWARDS assuming a dependency precedes its dependent.
    assert sources == ["shipped"] * len(ui.mod_catalog()) + \
        ["installed", "installed"]
    ids = [m["id"] for m in rows]
    assert ids[:len(ui.mod_catalog())] == [m["id"] for m in ui.mod_catalog()]
    assert ids[-2:] == ["x-notes", "x-dep"]        # topologically sorted
    by_id = {m["id"]: m for m in rows}
    assert by_id["x-notes"]["default_enabled"] is False
    assert by_id["x-notes"]["error"] is None
    assert by_id["x-notes"]["missing_requires"] == []
    # Administrative detail stays OFF /info -- every peer fetches it.
    assert "installed_at" not in by_id["x-notes"]
    assert "files" not in by_id["x-notes"]


def test_info_is_still_token_gated(tmp_path, monkeypatch):
    plant(tmp_path / "mods", manifest(), files())
    app = make_app(tmp_path, monkeypatch)
    _, r = app.test_client.get("/info")
    assert r.status == 401


def test_a_broken_mod_never_breaks_boot(tmp_path, monkeypatch):
    # Protective like _load_state: a hand-mangled store degrades to "that mod is
    # not served", never to a broker that will not start.
    root = tmp_path / "mods"
    plant(root, manifest(), files())
    plant_raw(root, "x-bad", {"id": "x-bad", "scripts": ["a.js"]},
              {"a.js": b"\xef\xbb\xbf//\n"})
    (root / "x-junk").mkdir()
    (root / "loose-file.js").write_bytes(b"//\n")
    app = make_app(tmp_path, monkeypatch)
    _, r = authed(app).get("/info")
    assert [m["id"] for m in r.json["mods"] if m["source"] == "installed"] \
        == ["x-notes"]
    assert app.ctx.mods_index["skipped"] == {"x-bad": "bad_encoding",
                                             "x-junk": "not_installed"}
