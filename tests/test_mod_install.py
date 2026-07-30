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

import asyncio
import json
import os
import threading
import time
from pathlib import Path

import pytest

import webterm.broker.app as app_mod
from .auth_helpers import TEST_TOKEN, authed, authed_reusable, with_token
from webterm.broker import modinstall, ui
from webterm.broker.app import create_app

_app_seq = 0

JS_CTYPE = "application/javascript; charset=utf-8"


# --------------------------------------------------------------------------- #
# fixtures
# --------------------------------------------------------------------------- #

def content_address(manifest_obj, raw_files):
    """The ``gen`` these bytes hash to, or ``None`` if they are not a valid
    package at all. Used so a raw-planted VALID package still lands in a
    correctly named directory -- the scanner refuses a generation whose name
    does not content-address its own bytes."""
    try:
        # Only the files the SCANNER would capture: it ignores inert junk
        # (unknown suffixes, a stray .DS_Store) rather than refusing the mod, so
        # the junk must not contribute to the hash either.
        captured = {n: d.decode("utf-8") for n, d in raw_files.items()
                    if modinstall.file_name_error(n) is None}
        canonical, records = modinstall.validate_package(manifest_obj, captured)
    except (modinstall.ValidationError, UnicodeDecodeError, AttributeError):
        return None
    return modinstall.compute_gen(canonical, records)


def plant_raw(mods_dir, mod_id, manifest_obj, raw_files, *, gen=None,
              previous=None, installed_at=1_700_000_000, current=True,
              marker=True):
    """Write one generation into the store WITHOUT validating it first.

    The primitive behind ``plant``: it is what lets a test plant a BOM, a
    missing final newline or a 300 KiB file and then assert the scanner refuses
    it. ``raw_files`` maps name -> bytes."""
    mods_dir = Path(mods_dir)
    mods_dir.mkdir(parents=True, exist_ok=True)
    if marker:
        (mods_dir / modinstall.MARKER_NAME).write_bytes(b"")
    gen = gen or content_address(manifest_obj, raw_files) or ("a" * 64)
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
            json.dumps({"gen": gen, "previous": previous}), encoding="utf-8")
    return gen


def plant(mods_dir, manifest, files, **kw):
    """Write a VALID mod, content-addressed exactly as an install would.

    Successive calls chain the retention pointer, so replacing v1 with v2 leaves
    CURRENT naming v2 with v1 as its retained predecessor -- exactly what the
    installer writes."""
    canonical, records = modinstall.validate_package(manifest, files)
    gen = kw.pop("gen", None) or modinstall.compute_gen(canonical, records)
    if "previous" not in kw:
        live, _ = modinstall.read_current(Path(mods_dir) / canonical["id"])
        kw["previous"] = live if live and live != gen else None
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
        # JavaScript ends a line on CR, U+2028 and U+2029 too, so counting only
        # "\n" would wave a 100k-line wall of code straight through the cap.
        ("\r" * (ui._MAX_LINES + 1) + "\n", "file_too_large"),
        ("\u2028" * (ui._MAX_LINES + 1) + "\n", "file_too_large"),
        ("\u2029" * (ui._MAX_LINES + 1) + "\n", "file_too_large"),
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
    # CRLF is ONE line terminator, not two -- a file written on Windows must
    # not count double against the cap.
    modinstall.validate_package(manifest(),
                                {"x-notes.js": "\r\n" * ui._MAX_LINES})
    assert modinstall.count_lines("a\r\nb\rc\nd") == 3


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
        # No url() at all -- an enumerate-the-syntax rule sails straight past
        # image-set(), src(), and whatever CSS adds next.
        '.a { background: image-set("https://evil.example/p.png" 1x); }\n',
        '.a { background: -webkit-image-set("//evil.example/p.png" 1x); }\n',
        '@font-face { src: src("https://evil.example/f.woff2"); }\n',
        # A comment stripper that does not know about strings reads the "/*" as
        # an unterminated comment and drops everything after it -- so the load
        # on the next line is never seen. The browser reads it as a string.
        '.a::before { content: "/*"; }\n'
        ".b { background: url(https://evil.example/p.png); }\n",
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


def test_css_validation_is_linear_on_hostile_input():
    """It runs ON THE EVENT LOOP against up to 256 KiB of attacker-chosen text.

    The obvious ``/\\*.*?\\*/`` and ``url\\((.*?)\\)`` are QUADRATIC on input
    that opens thousands of comments (or ``url(``) and never closes them -- one
    installed stylesheet would stall every terminal on the broker for minutes.
    Deadlined rather than eyeballed, because the failure mode is "slow", which
    is exactly what a passing-but-quadratic implementation looks like on a small
    fixture.
    """
    for filler in ("/*", "url(", "/*url("):
        css = (filler * 40_000) + "\n"
        started = time.monotonic()
        modinstall.validate_package(manifest(styles=["x-notes.css"]),
                                    {"x-notes.js": "//\n", "x-notes.css": css})
        assert time.monotonic() - started < 2.0, \
            f"{filler!r} x 40000 took too long -- the scan is not linear"
    # An unterminated comment runs to end of file, exactly as a browser reads
    # it, so what it hides is genuinely hidden.
    modinstall.validate_package(
        manifest(styles=["x-notes.css"]),
        {"x-notes.js": "//\n",
         "x-notes.css": "/* @import url(https://evil.example/x);\n"})


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
    assert rec["previous"] is None
    assert set(rec["files"]) == {"x-notes.js", "x-notes.css", "help.md"}
    # Only .js/.css are servable; help.md is a broker-side input, never a URL.
    assert set(index["assets"]) == {f"x-notes/{gen}/x-notes.js",
                                    f"x-notes/{gen}/x-notes.css"}
    assert index["assets"][f"x-notes/{gen}/x-notes.js"][1] == JS_CTYPE
    assert index["skipped"] == {}


def test_help_is_captured_case_insensitively(tmp_path):
    # "Help.md" passes the grammar, so an exact "help.md" lookup would capture
    # nothing and the mod's help would silently never appear.
    plant(tmp_path / "mods", manifest(),
          {"x-notes.js": "//\n", "Help.md": "# Notes\n\nhelp\n"})
    rec = modinstall.scan(tmp_path / "mods")["mods"]["x-notes"]
    assert rec["help_md"].startswith("# Notes")


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


# --------------------------------------------------------------------------- #
# POST /mods/install -- the commit protocol
# --------------------------------------------------------------------------- #

def payload(**kw):
    out = {"manifest": manifest(styles=["x-notes.css"]),
           "files": {"x-notes.js": "registerMod({ id: 'x-notes' });\n",
                     "x-notes.css": ".notes { color: red; }\n",
                     "help.md": "# Notes\n\nhelp\n"}}
    out.update(kw)
    return out


def test_install_writes_the_store_commits_and_serves_it(tmp_path, monkeypatch):
    app = make_app(tmp_path, monkeypatch)
    _, r = authed(app).post("/mods/install", json=payload())
    assert r.status == 200, r.json
    gen = r.json["gen"]
    assert modinstall.GEN_RE.fullmatch(gen)
    assert r.json["id"] == "x-notes" and r.json["replaced"] is False
    # D1: global lexical bindings cannot be removed, so there is no live
    # install. The response says so rather than letting a client imply it.
    assert r.json["applies"] == "next_page_load"
    assert r.json["adopts_existing_state"] == {"mod_store": False, "pin": False}
    assert r.json["mod"]["source"] == "installed"
    assert r.json["mod"]["default_enabled"] is False

    root = tmp_path / "mods"
    assert (root / modinstall.MARKER_NAME).is_file()      # written on first use
    gdir = root / "x-notes" / gen
    # CURRENT is the commit; it names a COMPLETE generation, and the retained
    # predecessor is WRITTEN rather than inferred from directory mtimes.
    assert json.loads((root / "x-notes" / modinstall.CURRENT_NAME)
                      .read_text(encoding="utf-8")) == {"gen": gen,
                                                        "previous": None}
    assert (gdir / "x-notes.js").read_bytes() == \
        b"registerMod({ id: 'x-notes' });\n"
    # mod.json is BROKER-written from the canonical manifest.
    written = json.loads((gdir / modinstall.MANIFEST_NAME)
                         .read_text(encoding="utf-8"))
    assert written["id"] == "x-notes" and "defaultEnabled" not in written
    meta = json.loads((gdir / modinstall.GEN_META_NAME)
                      .read_text(encoding="utf-8"))
    assert meta["gen"] == gen and isinstance(meta["installed_at"], int)

    # Live, with no restart: the asset route serves it immediately.
    _, a = app.test_client.get(f"/mods/x-notes/{gen}/x-notes.js")
    assert a.status == 200 and a.body == b"registerMod({ id: 'x-notes' });\n"
    _, info = authed(app).get("/info")
    assert [m["id"] for m in info.json["mods"]][-1] == "x-notes"

    # And a restart re-derives exactly the same state off disk.
    again = make_app(tmp_path, monkeypatch)
    assert again.ctx.mods_index["mods"]["x-notes"]["gen"] == gen
    assert again.ctx.mods_index["skipped"] == {}


def test_install_refuses_a_payload_supplied_mod_json(tmp_path, monkeypatch):
    # mod.json is broker-written from the CANONICAL manifest, so a payload can
    # never ship one that disagrees with what was validated.
    app = make_app(tmp_path, monkeypatch)
    body = payload()
    body["files"][modinstall.MANIFEST_NAME] = '{"id":"x-evil"}\n'
    _, r = authed(app).post("/mods/install", json=body)
    assert r.status == 400 and r.json["error"] == "reserved_file_name"
    assert app.ctx.mods_index["mods"] == {}


def test_install_error_matrix(tmp_path, monkeypatch):
    app = make_app(tmp_path, monkeypatch)
    client = authed(app)
    big = "x" * (modinstall.MAX_FILE_BYTES + 1) + "\n"
    cases = [
        ({}, 400, "bad_json"),
        ({"manifest": [], "files": {}}, 400, "bad_json"),
        (payload(files=[]), 400, "bad_json"),
        (payload(replace="yes"), 400, "bad_manifest_field"),
        ({"manifest": manifest(id="clock"), "files": {"clock.js": "//\n"}},
         400, "reserved_id"),
        ({"manifest": manifest(id="Clock"), "files": {"clock.js": "//\n"}},
         400, "bad_mod_id"),
        ({"manifest": manifest(), "files": {"../x.js": "//\n"}},
         400, "bad_file_name"),
        ({"manifest": manifest(), "files": {"CURRENT": "//\n"}},
         400, "reserved_file_name"),
        ({"manifest": manifest(),
          "files": {f"f{i}.js": "//\n"
                    for i in range(modinstall.MAX_FILES + 1)}},
         400, "too_many_files"),
        ({"manifest": manifest(), "files": {"x-notes.js": big}},
         413, "file_too_large"),
        ({"manifest": manifest(),
          "files": {"x-notes.js": "x" * (modinstall.MAX_FILE_BYTES - 1) + "\n",
                    "b.js": "x" * (modinstall.MAX_FILE_BYTES - 1) + "\n",
                    "c.js": "x" * (modinstall.MAX_FILE_BYTES - 1) + "\n"}},
         413, "total_too_large"),
        ({"manifest": manifest(), "files": {"x-notes.js": "no newline"}},
         400, "bad_encoding"),
        ({"manifest": manifest(), "files": {"x-notes.js": 7}},
         400, "bad_encoding"),
        ({"manifest": {"id": "x-notes"}, "files": {"x-notes.js": "//\n"}},
         400, "bad_scripts"),
        ({"manifest": manifest(styles=["nope.css"]),
          "files": {"x-notes.js": "//\n"}}, 400, "bad_styles"),
        ({"manifest": manifest(requires=["x-notes"]),
          "files": {"x-notes.js": "//\n"}}, 400, "bad_requires"),
        ({"manifest": manifest(tiers=["x" * 33]),
          "files": {"x-notes.js": "//\n"}}, 400, "bad_manifest_field"),
        ({"manifest": manifest(nope=1), "files": {"x-notes.js": "//\n"}},
         400, "unknown_manifest_key"),
        ({"manifest": manifest(styles=["x-notes.css"]),
          "files": {"x-notes.js": "//\n",
                    "x-notes.css": "@import url(https://evil.example/x);\n"}},
         400, "css_external_reference"),
    ]
    for body, status, code in cases:
        _, r = client.post("/mods/install", json=body)
        assert r.status == status, (code, r.status, r.json)
        assert r.json["error"] == code, (code, r.json)
    # Not a byte was written by any of them.
    assert app.ctx.mods_index["mods"] == {}
    assert not (tmp_path / "mods").exists()
    # Non-JSON at all.
    _, r = client.post("/mods/install", data="not json")
    assert r.status == 400 and r.json["error"] == "bad_json"


def test_install_body_cap_is_checked_before_the_parse(tmp_path, monkeypatch):
    # A 200 MiB "manifest" must not be JSON-decoded just to be rejected.
    app = make_app(tmp_path, monkeypatch)
    oversized = "x" * (modinstall.MAX_BODY_BYTES + 64)
    _, r = authed(app).post(
        "/mods/install",
        json={"manifest": manifest(), "files": {"x-notes.js": oversized}})
    assert r.status == 413 and r.json["error"] == "too_large"
    assert app.ctx.mods_index["mods"] == {}


def test_install_needs_the_token(tmp_path, monkeypatch):
    app = make_app(tmp_path, monkeypatch)
    for path, body in (("/mods/install", payload()),
                       ("/mods/uninstall", {"id": "x-notes"}),
                       ("/mods/rescan", {})):
        _, r = app.test_client.post(path, json=body)
        assert r.status == 401, path
        assert r.json["error"] == "auth_required"
    _, r = app.test_client.get("/mods/installed")
    assert r.status == 401
    assert not (tmp_path / "mods").exists()


def test_a_second_install_of_an_id_in_use_is_refused(tmp_path, monkeypatch):
    app = make_app(tmp_path, monkeypatch)
    client = authed(app)
    _, first = client.post("/mods/install", json=payload())
    assert first.status == 200
    _, second = client.post("/mods/install", json=payload())
    assert second.status == 409 and second.json["error"] == "id_in_use"
    # The operator must uninstall first -- which is where the purge option is,
    # and where a DIFFERENT author's x-notes gets noticed.
    assert app.ctx.mods_index["mods"]["x-notes"]["gen"] == first.json["gen"]


def test_replace_upgrades_and_retains_the_previous_generation(tmp_path,
                                                              monkeypatch):
    app = make_app(tmp_path, monkeypatch)
    client = authed(app)
    _, v1 = client.post("/mods/install", json=payload())
    body = payload()
    body["files"]["x-notes.js"] = "registerMod({ id: 'x-notes' }); // v2\n"
    body["replace"] = True
    _, v2 = client.post("/mods/install", json=body)
    assert v2.status == 200 and v2.json["replaced"] is True
    assert v2.json["gen"] != v1.json["gen"]
    # A page that started booting against v1 must not be handed a v2 file, so
    # both generations stay resolvable.
    for gen, marker in ((v1.json["gen"], b"registerMod({ id: 'x-notes' });\n"),
                        (v2.json["gen"], b"// v2\n")):
        _, r = app.test_client.get(f"/mods/x-notes/{gen}/x-notes.js")
        assert r.status == 200 and r.body.endswith(marker)
    # ...but only the current one is advertised.
    _, info = authed(app).get("/info")
    assert [m for m in info.json["mods"]
            if m["id"] == "x-notes"][0]["gen"] == v2.json["gen"]
    # A third install GCs the oldest: RETAINED_GENERATIONS on disk, no more.
    body["files"]["x-notes.js"] = "registerMod({ id: 'x-notes' }); // v3\n"
    _, v3 = client.post("/mods/install", json=body)
    assert v3.status == 200
    on_disk = {d.name for d in (tmp_path / "mods" / "x-notes").iterdir()
               if d.is_dir()}
    assert on_disk == {v2.json["gen"], v3.json["gen"]}
    _, gone = app.test_client.get(f"/mods/x-notes/{v1.json['gen']}/x-notes.js")
    assert gone.status == 404


def test_reinstalling_identical_bytes_never_destroys_the_live_generation(
        tmp_path, monkeypatch):
    # The generation name is CONTENT-ADDRESSED, so a replace with identical
    # bytes targets the directory CURRENT already names. It must be repaired in
    # place, never rmtree'd and rebuilt.
    app = make_app(tmp_path, monkeypatch)
    client = authed(app)
    _, first = client.post("/mods/install", json=payload())
    gen = first.json["gen"]
    body = payload()
    body["replace"] = True
    _, second = client.post("/mods/install", json=body)
    assert second.status == 200 and second.json["gen"] == gen
    gdir = tmp_path / "mods" / "x-notes" / gen
    assert (gdir / "x-notes.js").read_bytes() == \
        b"registerMod({ id: 'x-notes' });\n"
    assert not [p for p in gdir.iterdir() if p.name.startswith(".w-")]
    _, r = app.test_client.get(f"/mods/x-notes/{gen}/x-notes.js")
    assert r.status == 200


def test_too_many_mods_is_refused(tmp_path, monkeypatch):
    root = tmp_path / "mods"
    for i in range(modinstall.MAX_MODS):
        mid = f"x-m{i:03d}"
        plant(root, manifest(mod_id=mid, scripts=[f"{mid}.js"]),
              {f"{mid}.js": "//\n"})
    app = make_app(tmp_path, monkeypatch)
    assert len(app.ctx.mods_index["mods"]) == modinstall.MAX_MODS
    _, r = authed(app).post("/mods/install", json=payload())
    assert r.status == 409 and r.json["error"] == "too_many_mods"
    # ...but REPLACING one of the existing mods still works: the cap is on the
    # count, not on writing.
    mid = "x-m000"
    _, ok = authed(app).post("/mods/install", json={
        "manifest": manifest(mod_id=mid, scripts=[f"{mid}.js"]),
        "files": {f"{mid}.js": "// v2\n"}, "replace": True})
    assert ok.status == 200


def test_install_surfaces_state_it_would_adopt(tmp_path, monkeypatch):
    # #172, said out loud: /mod-store and the pin map already accept any
    # id-shaped key, so a user CAN hold state under "x-notes" before anything
    # named x-notes was installed. Installing would silently adopt it, so the
    # operator is told before they confirm.
    app = make_app(tmp_path, monkeypatch)
    client = authed(app)
    _, p = client.post("/mods/policy", json={"set": {"x-notes": True}})
    assert p.status == 200
    _, s = client.put("/mod-store/x-notes",
                      json={"baseRev": 0, "value": {"note": "hi"}})
    assert s.status == 200, s.json
    _, r = client.post("/mods/install", json=payload())
    assert r.status == 200
    assert r.json["adopts_existing_state"] == {"mod_store": True, "pin": True}


# --------------------------------------------------------------------------- #
# concurrency: the under-lock re-check
# --------------------------------------------------------------------------- #

def test_two_concurrent_installs_of_one_id_cannot_both_win(tmp_path,
                                                           monkeypatch):
    """The pre-lock check is ONLY a fast fail.

    Both requests observe the id absent before either takes the lock, so
    without the re-check INSIDE the critical section both would commit -- the
    second silently replacing a mod the operator never asked to replace, with
    no id_in_use anywhere. The commit is slowed so the second request is
    provably still queued when the first swaps the index.
    """
    app = make_app(tmp_path, monkeypatch)
    real = modinstall.commit_generation

    def slow_commit(*args):
        time.sleep(0.4)                    # in the executor, not on the loop
        return real(*args)

    monkeypatch.setattr(modinstall, "commit_generation", slow_commit)
    with authed_reusable(app) as client:
        url = with_token(f"http://{client.host}:{client.port}/mods/install",
                         app.ctx.auth_token)

        async def _drive():
            return await asyncio.gather(
                client._session.post(url, json=payload(), timeout=30),
                client._session.post(url, json=payload(), timeout=30))

        first, second = client._loop.run_until_complete(_drive())
    statuses = sorted([first.status_code, second.status_code])
    assert statuses == [200, 409], statuses
    loser = first if first.status_code == 409 else second
    assert loser.json()["error"] == "id_in_use"
    assert len(app.ctx.mods_index["mods"]) == 1


def test_a_cancelled_install_still_finishes_its_critical_section(tmp_path,
                                                                 monkeypatch):
    """The ``_shielded_region`` contract, on this endpoint (#160).

    Sanic cancels the handler task on ``connection_lost``, and a RUNNING
    executor future cannot be cancelled. Unshielded, a cancel landing on the
    ``_off_loop`` hop would release ``mods_install_lock`` while the worker was
    still writing the generation, and the post-await accounting -- the index and
    catalog swap -- would never run: the store would hold a committed mod the
    broker did not know it was serving, until a restart.
    """
    app = make_app(tmp_path, monkeypatch)
    real = modinstall.commit_generation
    inside = threading.Event()

    def blocking_commit(*args):
        inside.set()
        time.sleep(0.5)                    # long enough to be cancelled inside
        return real(*args)

    monkeypatch.setattr(modinstall, "commit_generation", blocking_commit)
    with authed_reusable(app) as client:
        url = with_token(f"http://{client.host}:{client.port}/mods/install",
                         app.ctx.auth_token)

        async def _drive():
            task = asyncio.ensure_future(client._session.post(url,
                                                              json=payload()))
            for _ in range(500):           # deadlined: an inert patch must FAIL
                if inside.is_set():
                    break
                await asyncio.sleep(0.01)
            assert inside.is_set(), "the worker never entered its hop"
            task.cancel()
            try:
                await task
            except BaseException:  # noqa: BLE001 - cancellation is the point
                pass
            assert task.cancelled(), \
                "the request completed instead of being cancelled mid-hop"
            for _ in range(500):           # the shielded section drains
                if "x-notes" in app.ctx.mods_index["mods"]:
                    break
                await asyncio.sleep(0.01)

        client._loop.run_until_complete(_drive())
    # The side effects are what the shield buys -- never the Response.
    assert "x-notes" in app.ctx.mods_index["mods"], \
        "the cancelled section never ran its accounting"
    assert not app.ctx.mods_install_lock.locked(), \
        "the shielded task released the lock in the caller, not in itself"
    gen = app.ctx.mods_index["mods"]["x-notes"]["gen"]
    assert json.loads((tmp_path / "mods" / "x-notes" /
                       modinstall.CURRENT_NAME).read_text())["gen"] == gen
    assert [r["id"] for r in app.ctx.mod_catalog][-1] == "x-notes"


# --------------------------------------------------------------------------- #
# crash recovery: a failure injected at every step of the commit protocol
# --------------------------------------------------------------------------- #

def _installed_v1(tmp_path, monkeypatch):
    """A broker with x-notes v1 committed -- the state every injection below
    must leave INTACT."""
    app = make_app(tmp_path, monkeypatch)
    _, r = authed(app).post("/mods/install", json=payload())
    assert r.status == 200
    return app, r.json["gen"]


def _v2_body():
    body = payload()
    body["files"]["x-notes.js"] = "registerMod({ id: 'x-notes' }); // v2\n"
    body["replace"] = True
    return body


def _current_gen(tmp_path):
    return json.loads((tmp_path / "mods" / "x-notes" /
                       modinstall.CURRENT_NAME).read_text(encoding="utf-8"))["gen"]


def test_crash_at_step_3_leaves_the_old_generation_live(tmp_path, monkeypatch):
    # Staging fails: nothing was published, so CURRENT is untouched and the
    # only residue is a .tmp- directory.
    app, v1 = _installed_v1(tmp_path, monkeypatch)

    def boom(*_a, **_k):
        raise OSError("disk full")

    monkeypatch.setattr(modinstall, "_write_generation_dir", boom)
    _, r = authed(app).post("/mods/install", json=_v2_body())
    assert r.status == 500 and r.json["error"] == "write_failed"
    assert _current_gen(tmp_path) == v1
    assert app.ctx.mods_index["mods"]["x-notes"]["gen"] == v1
    _, served = app.test_client.get(f"/mods/x-notes/{v1}/x-notes.js")
    assert served.status == 200


def test_crash_at_step_4_the_commit_leaves_the_old_generation_live(tmp_path,
                                                                   monkeypatch):
    # The new generation is fully written but CURRENT never moves. That is the
    # WHOLE point of a one-small-file commit: the half-done state is a complete
    # unreferenced directory, not a half-replaced mod.
    app, v1 = _installed_v1(tmp_path, monkeypatch)
    real_write = app_mod._write_state_atomic

    def boom(path, state):
        if path.name == modinstall.CURRENT_NAME:
            raise OSError("power cut")
        return real_write(path, state)

    monkeypatch.setattr(app_mod, "_write_state_atomic", boom)
    _, r = authed(app).post("/mods/install", json=_v2_body())
    assert r.status == 500 and r.json["error"] == "write_failed"
    assert _current_gen(tmp_path) == v1
    assert app.ctx.mods_index["mods"]["x-notes"]["gen"] == v1
    orphan = next(d.name for d in (tmp_path / "mods" / "x-notes").iterdir()
                  if d.is_dir() and d.name != v1)
    monkeypatch.undo()
    # A restart adopts the OLD generation, not the orphan: CURRENT is the only
    # thing that decides which generations exist, and it never moved.
    again = make_app(tmp_path, monkeypatch)
    assert again.ctx.mods_index["mods"]["x-notes"]["gen"] == v1
    assert again.ctx.mods_index["mods"]["x-notes"]["previous"] is None
    assert [r["gen"] for r in again.ctx.mod_catalog
            if r["id"] == "x-notes"] == [v1]
    # ...and the orphan is NOT served, even though it is a complete, newer
    # directory. If retention were inferred from mtimes it would have taken the
    # retained slot -- displacing the real predecessor, so a page mid-boot
    # against that one starts 404ing and the next sweep deletes it. Here it is
    # simply litter, and the rescan reclaims it.
    assert not [k for k in again.ctx.mods_index["assets"] if orphan in k]
    _, rescanned = authed(again).post("/mods/rescan", json={})
    assert rescanned.status == 200
    assert {d.name for d in (tmp_path / "mods" / "x-notes").iterdir()
            if d.is_dir()} == {v1}


def test_crash_at_step_5_commits_disk_and_leaves_memory_repairable(tmp_path,
                                                                   monkeypatch):
    # Step 5 is a PURE function of in-memory bytes, so it cannot fail on IO --
    # but if a bug in it raised, the commit has already landed. The contract is
    # that the request fails with the index UNSWAPPED (never half-swapped) and
    # the next scan repairs it.
    app, v1 = _installed_v1(tmp_path, monkeypatch)

    def boom(*_a, **_k):
        raise RuntimeError("a bug in the index rebuild")

    monkeypatch.setattr(modinstall, "index_with", boom)
    _, r = authed(app).post("/mods/install", json=_v2_body())
    assert r.status == 500
    assert app.ctx.mods_index["mods"]["x-notes"]["gen"] == v1     # not swapped
    assert [row["gen"] for row in app.ctx.mod_catalog
            if row["id"] == "x-notes"] == [v1]                    # nor half
    v2 = _current_gen(tmp_path)
    assert v2 != v1                                               # disk ahead
    monkeypatch.undo()
    _, rescanned = authed(app).post("/mods/rescan", json={})
    assert rescanned.status == 200
    assert app.ctx.mods_index["mods"]["x-notes"]["gen"] == v2      # repaired


def test_a_failing_gc_never_fails_the_request(tmp_path, monkeypatch):
    # Step 6 is best-effort BY CONTRACT: the commit already landed, so failing
    # the request over leftover litter would be a lie.
    app, v1 = _installed_v1(tmp_path, monkeypatch)

    def boom(_path):
        raise OSError("in use by another process")

    monkeypatch.setattr(modinstall, "_rmtree_if_ours", boom)
    body = _v2_body()
    body["files"]["x-notes.js"] = "// v2\n"
    _, r = authed(app).post("/mods/install", json=body)
    assert r.status == 200
    body["files"]["x-notes.js"] = "// v3\n"
    _, r3 = authed(app).post("/mods/install", json=body)
    assert r3.status == 200
    # v1 is now litter that could not be reclaimed -- and that is all it is.
    assert v1 in {d.name for d in (tmp_path / "mods" / "x-notes").iterdir()
                  if d.is_dir()}
    assert app.ctx.mods_index["mods"]["x-notes"]["gen"] == r3.json["gen"]


# --------------------------------------------------------------------------- #
# POST /mods/uninstall
# --------------------------------------------------------------------------- #

def test_uninstall_removes_the_code_and_leaves_data_by_default(tmp_path,
                                                               monkeypatch):
    app = make_app(tmp_path, monkeypatch)
    client = authed(app)
    _, r = client.post("/mods/install", json=payload())
    gen = r.json["gen"]
    client.post("/mods/policy", json={"set": {"x-notes": True}})
    client.put("/mod-store/x-notes", json={"baseRev": 0, "value": {"a": 1}})

    _, u = client.post("/mods/uninstall", json={"id": "x-notes"})
    assert u.status == 200
    assert u.json["purged"] == {"mod_store": False, "pin": False}
    assert u.json["applies"] == "next_page_load"
    assert app.ctx.mods_index["mods"] == {}
    assert not (tmp_path / "mods" / "x-notes").exists()
    assert not [p for p in (tmp_path / "mods").iterdir()
                if p.name.startswith(modinstall.OLD_PREFIX)]
    _, gone = app.test_client.get(f"/mods/x-notes/{gen}/x-notes.js")
    assert gone.status == 404
    # Data survives an ordinary uninstall -- deleting it is a separate, explicit
    # choice, because a reinstall is the common case.
    assert app.ctx.mod_policy == {"x-notes": True}
    assert "x-notes" in app.ctx.modstore


def test_uninstall_with_purge_clears_this_brokers_state(tmp_path, monkeypatch):
    app = make_app(tmp_path, monkeypatch)
    client = authed(app)
    client.post("/mods/install", json=payload())
    client.post("/mods/policy", json={"set": {"x-notes": True}})
    client.put("/mod-store/x-notes", json={"baseRev": 0, "value": {"a": 1}})

    _, u = client.post("/mods/uninstall", json={"id": "x-notes",
                                                "purge": True})
    assert u.status == 200
    assert u.json["purged"] == {"mod_store": True, "pin": True}
    assert app.ctx.mod_policy == {}
    assert "x-notes" not in app.ctx.modstore
    # Both sidecars, not just memory.
    assert json.loads((tmp_path / "webterm_mod_policy.json")
                      .read_text())["policy"] == {}
    assert "x-notes" not in json.loads(
        (tmp_path / "webterm_modstore.json").read_text())
    _, info = authed(app).get("/info")
    assert info.json["mod_policy"] == {}


def test_purge_is_data_first_and_code_last(tmp_path, monkeypatch):
    """It is three sidecars and cannot be one transaction, so the ORDER is the
    only guarantee available: a crash leaves code without data (trivially
    recoverable -- uninstall again) rather than data without code, which is the
    shape that silently re-adopts on the next install of that id."""
    app = make_app(tmp_path, monkeypatch)
    client = authed(app)
    client.post("/mods/install", json=payload())
    client.post("/mods/policy", json={"set": {"x-notes": True}})
    client.put("/mod-store/x-notes", json={"baseRev": 0, "value": {"a": 1}})

    def boom(*_a, **_k):
        raise OSError("rename failed")

    monkeypatch.setattr(modinstall, "stage_removal", boom)
    _, u = client.post("/mods/uninstall", json={"id": "x-notes",
                                                "purge": True})
    assert u.status == 500 and u.json["error"] == "write_failed"
    # Data went first, so it is gone; the code is still installed and serving.
    assert app.ctx.mod_policy == {}
    assert "x-notes" not in app.ctx.modstore
    assert "x-notes" in app.ctx.mods_index["mods"]


def test_uninstall_of_an_unknown_id_is_a_404(tmp_path, monkeypatch):
    # NOT idempotent at the HTTP-result level, deliberately: catching a typo is
    # worth more than the ambiguity of a retry after a lost response.
    app = make_app(tmp_path, monkeypatch)
    client = authed(app)
    _, r = client.post("/mods/uninstall", json={"id": "x-nope"})
    assert r.status == 404 and r.json["error"] == "not_installed"
    _, shipped = client.post("/mods/uninstall", json={"id": "clock"})
    assert shipped.status == 404, "a SHIPPED mod is not uninstallable"
    assert [m["id"] for m in ui.mod_catalog() if m["id"] == "clock"]
    for bad in ("../x", "Bad", "", None, 7):
        _, r = client.post("/mods/uninstall", json={"id": bad})
        assert r.status == 400 and r.json["error"] == "bad_mod_id", bad
    _, r = client.post("/mods/uninstall", json={"id": "x-a", "purge": "yes"})
    assert r.status == 400 and r.json["error"] == "bad_manifest_field"


# --------------------------------------------------------------------------- #
# POST /mods/rescan + the sweep
# --------------------------------------------------------------------------- #

def test_rescan_picks_up_a_hand_dropped_mod(tmp_path, monkeypatch):
    app = make_app(tmp_path, monkeypatch)
    assert app.ctx.mods_index["mods"] == {}
    gen = plant(tmp_path / "mods", manifest(), files())
    _, r = authed(app).post("/mods/rescan", json={})
    assert r.status == 200
    assert [m["id"] for m in r.json["mods"]] == ["x-notes"]
    assert app.ctx.mods_index["mods"]["x-notes"]["gen"] == gen
    _, served = app.test_client.get(f"/mods/x-notes/{gen}/x-notes.js")
    assert served.status == 200


def test_rescan_sweeps_staging_and_superseded_generations(tmp_path,
                                                          monkeypatch):
    root = tmp_path / "mods"
    old = plant(root, manifest(), {"x-notes.js": "// v1\n"})
    new = plant(root, manifest(), {"x-notes.js": "// v2\n"})
    third = plant(root, manifest(), {"x-notes.js": "// v3\n"})
    (root / (modinstall.TMP_PREFIX + "abandoned")).mkdir()
    (root / (modinstall.OLD_PREFIX + "x-gone-dead")).mkdir()
    (root / "x-notes" / (modinstall.TMP_PREFIX + "half")).mkdir()
    app = make_app(tmp_path, monkeypatch)
    _, r = authed(app).post("/mods/rescan", json={})
    assert r.status == 200
    top = {p.name for p in root.iterdir()}
    assert not [n for n in top if n.startswith(modinstall.TMP_PREFIX)
                or n.startswith(modinstall.OLD_PREFIX)]
    kept = {d.name for d in (root / "x-notes").iterdir() if d.is_dir()}
    assert third in kept and new in kept and old not in kept
    assert len(kept) == modinstall.RETAINED_GENERATIONS


def test_the_sweep_refuses_a_directory_it_does_not_own(tmp_path, monkeypatch):
    # A mods_dir mis-set to an existing user directory must not be able to lose
    # anything. The marker is what the sweep insists on.
    root = tmp_path / "mods"
    old = plant(root, manifest(), {"x-notes.js": "// v1\n"})
    plant(root, manifest(), {"x-notes.js": "// v2\n"})
    (root / (modinstall.OLD_PREFIX + "precious")).mkdir()
    (root / (modinstall.OLD_PREFIX + "precious") / "notes.txt").write_text(
        "do not delete\n", encoding="utf-8")
    (root / modinstall.MARKER_NAME).unlink()
    app = make_app(tmp_path, monkeypatch)
    _, r = authed(app).post("/mods/rescan", json={})
    assert r.status == 200
    assert (root / (modinstall.OLD_PREFIX + "precious") / "notes.txt").is_file()
    assert (root / "x-notes" / old).is_dir()
    # ...and the scan itself still works, so an unmarked store is READ but never
    # written destructively.
    assert "x-notes" in app.ctx.mods_index["mods"]


def test_the_sweep_never_deletes_a_mod_it_merely_refused(tmp_path, monkeypatch):
    # A directory the scanner refused is somebody's half-unpacked archive or a
    # typo, not litter. GET /mods/installed reports it; the sweep leaves it.
    root = tmp_path / "mods"
    plant(root, manifest(), files())
    plant_raw(root, "x-broken", {"id": "x-broken", "scripts": ["a.js"]},
              {"a.js": b"no newline"})
    app = make_app(tmp_path, monkeypatch)
    _, r = authed(app).post("/mods/rescan", json={})
    assert r.status == 200
    assert r.json["skipped"] == {"x-broken": "bad_encoding"}
    assert (root / "x-broken").is_dir()


def test_a_refused_mod_can_still_be_uninstalled(tmp_path, monkeypatch):
    # GET /mods/installed reports it, so the operator can see it; refusing to
    # remove the one thing they were just told about would leave "stop the
    # broker and delete a directory" as the only cure -- the restart this
    # feature exists to avoid.
    plant_raw(tmp_path / "mods", "x-broken",
              {"id": "x-broken", "scripts": ["a.js"]}, {"a.js": b"no newline"})
    app = make_app(tmp_path, monkeypatch)
    assert app.ctx.mods_index["skipped"] == {"x-broken": "bad_encoding"}
    _, r = authed(app).post("/mods/uninstall", json={"id": "x-broken"})
    assert r.status == 200
    assert not (tmp_path / "mods" / "x-broken").exists()
    assert app.ctx.mods_index["skipped"] == {}


def test_rescan_rejects_a_non_json_body(tmp_path, monkeypatch):
    app = make_app(tmp_path, monkeypatch)
    _, r = authed(app).post("/mods/rescan", data="not json")
    assert r.status == 400 and r.json["error"] == "bad_json"


# --------------------------------------------------------------------------- #
# GET /mods/installed
# --------------------------------------------------------------------------- #

def test_installed_detail_is_the_operator_view(tmp_path, monkeypatch):
    app = make_app(tmp_path, monkeypatch)
    _, ins = authed(app).post("/mods/install", json=payload())
    plant_raw(tmp_path / "mods", "x-broken",
              {"id": "x-broken", "scripts": ["a.js"]}, {"a.js": b"no newline"})
    authed(app).post("/mods/rescan", json={})
    _, r = authed(app).get("/mods/installed")
    assert r.status == 200
    assert r.json["mods_dir"] == str(tmp_path / "mods")
    row = r.json["mods"][0]
    assert row["id"] == "x-notes" and row["gen"] == ins.json["gen"]
    assert isinstance(row["installed_at"], int) and row["installed_at"] > 0
    assert row["has_help"] is True
    names = [f["name"] for f in row["files"]]
    assert names == ["help.md", "x-notes.css", "x-notes.js"]
    assert all(len(f["sha256"]) == 64 and f["bytes"] > 0 for f in row["files"])
    assert row["total_bytes"] == sum(f["bytes"] for f in row["files"])
    # A mod that silently vanishes is worse than one that says why.
    assert r.json["skipped"] == {"x-broken": "bad_encoding"}
    assert r.json["limits"]["max_lines"] == ui._MAX_LINES
    assert r.json["limits"]["max_mods"] == modinstall.MAX_MODS


def test_installed_detail_stays_off_info(tmp_path, monkeypatch):
    # /info is fetched by every peer, so the administrative detail lives on the
    # token-gated operator route instead.
    app = make_app(tmp_path, monkeypatch)
    authed(app).post("/mods/install", json=payload())
    _, info = authed(app).get("/info")
    row = [m for m in info.json["mods"] if m["id"] == "x-notes"][0]
    assert set(row) == {"id", "title", "description", "version",
                        "default_enabled", "requires", "source", "gen",
                        "scripts", "styles", "integrity", "error",
                        "missing_requires"}


# --------------------------------------------------------------------------- #
# GET /help-corpus.json: the installed half is merged at SERVE time
# --------------------------------------------------------------------------- #

def _help_slugs(app):
    # AUTHENTICATED, always: the installed half is served only to a caller
    # holding the token (#173), so a tokenless read here would assert the
    # public response and quietly stop covering the merge at all.
    _, r = authed(app).get("/help-corpus.json")
    assert r.status == 200
    return [s["slug"] for s in r.json["sections"]]


def _public_help_slugs(app):
    _, r = app.test_client.get("/help-corpus.json")
    assert r.status == 200
    return [s["slug"] for s in r.json["sections"]]


def test_installed_help_appears_in_the_served_corpus(tmp_path, monkeypatch):
    # The install/uninstall path re-merges immediately: unlike the mod SCRIPT
    # (D1 -- next page load), Help is server-side data with no lexical bindings
    # to unpick, so the Help window shows it as soon as the page reloads it.
    app = make_app(tmp_path, monkeypatch)
    assert "x-notes" not in _help_slugs(app)
    _, r = authed(app).post("/mods/install", json=payload())
    assert r.status == 200
    slugs = _help_slugs(app)
    assert "x-notes" in slugs
    assert "taskbar" in slugs, "the wiki sections must still be there"
    sec = [s for s in app.ctx.help_corpus["sections"] if s["slug"] == "x-notes"]
    assert sec and sec[0]["mod"] == "x-notes"
    # ...and the packaged corpus the regenerator writes is unaffected: the
    # merge is serve-time only, so the byte-exact drift guard in
    # tests/test_help_corpus.py cannot become machine-specific.
    from webterm.broker import help_corpus as hc
    assert "x-notes" not in [s["slug"] for s in hc.HELP_CORPUS["sections"]]
    assert "x-notes" not in [s["slug"]
                             for s in hc.build_full_corpus()["sections"]]

    _, r = authed(app).post("/mods/uninstall", json={"id": "x-notes"})
    assert r.status == 200
    assert "x-notes" not in _help_slugs(app)
    assert "taskbar" in _help_slugs(app)


def test_help_the_catalog_and_the_index_swap_together(tmp_path, monkeypatch):
    # One assignment step for all three (#163 design 7): a second, separately
    # swapped copy could disagree with the catalog about which mods exist.
    plant(tmp_path / "mods", manifest(),
          {"x-notes.js": "//\n", "help.md": "# Notes\n\nhelp\n"})
    app = make_app(tmp_path, monkeypatch)

    def installed_ids(_app):
        # The help set is read as "every section in the INSTALLED namespace",
        # NOT as an intersection with the index -- intersecting would hide the
        # very failure this is here to catch, a section left behind for a mod
        # the index no longer has.
        return ({r["id"] for r in _app.ctx.mod_catalog
                 if r.get("source") == "installed"},
                set(_app.ctx.mods_index["mods"]),
                {s["slug"] for s in _app.ctx.help_corpus["sections"]
                 if s["slug"].startswith("x-")})

    cat, idx, help_ids = installed_ids(app)
    assert cat == idx == help_ids == {"x-notes"}
    plant(tmp_path / "mods",
          manifest(mod_id="x-two", scripts=["x-two.js"]),
          {"x-two.js": "//\n", "help.md": "# Two\n\nhelp\n"})
    authed(app).post("/mods/rescan", json={})
    cat, idx, help_ids = installed_ids(app)
    assert cat == idx == help_ids == {"x-notes", "x-two"}
    _, r = authed(app).post("/mods/uninstall", json={"id": "x-two"})
    assert r.status == 200
    cat, idx, help_ids = installed_ids(app)
    assert cat == idx == help_ids == {"x-notes"}


def test_replacing_a_mod_replaces_its_help_text(tmp_path, monkeypatch):
    # The merge memoizes parsed cards, so this is the case that would break if
    # the memo were keyed by anything but the help TEXT itself: same id, same
    # slug, new generation, different words.
    app = make_app(tmp_path, monkeypatch)
    body = payload()
    body["files"]["help.md"] = "# Notes\n\n## Old\n\nthe old words\n"
    assert authed(app).post("/mods/install", json=body)[1].status == 200

    def titles():
        _, r = authed(app).get("/help-corpus.json")
        sec = [s for s in r.json["sections"] if s["slug"] == "x-notes"][0]
        return [c["title"] for c in sec["cards"]]

    assert titles() == ["Overview", "Old"]     # the H1 becomes an Overview card
    body["files"]["help.md"] = "# Notes\n\n## New\n\nthe new words\n"
    body["replace"] = True
    assert authed(app).post("/mods/install", json=body)[1].status == 200
    assert titles() == ["Overview", "New"]


def test_a_mod_with_unparseable_help_never_blanks_the_corpus(tmp_path,
                                                             monkeypatch):
    # A careless installed mod must not be able to empty the Help window: its
    # own section is skipped and everything else is served unchanged.
    app = make_app(tmp_path, monkeypatch)
    before = _help_slugs(app)
    # A well-behaved installed mod alongside it, so this proves the bad one is
    # skipped rather than that the whole installed half was dropped.
    good = payload()
    good["manifest"] = manifest(mod_id="x-fine", scripts=["x-fine.js"])
    good["files"] = {"x-fine.js": "//\n", "help.md": "# Fine\n\nhelp\n"}
    assert authed(app).post("/mods/install", json=good)[1].status == 200
    body = payload()
    body["files"]["help.md"] = "<!-- help:ignore-end -->\nunbalanced\n"
    _, r = authed(app).post("/mods/install", json=body)
    assert r.status == 200
    assert _help_slugs(app) == before + ["x-fine"]
    # It is still installed and still served -- only its help is missing.
    _, det = authed(app).get("/mods/installed")
    rows = {m["id"]: m for m in det.json["mods"]}
    assert rows["x-notes"]["has_help"] is True


def test_the_served_corpus_withholds_installed_help_without_a_token(tmp_path,
                                                                    monkeypatch):
    # #173, the inversion of what #163 shipped. The route stays PUBLIC (gating
    # it would 401 the Help window of the very login page the token is typed
    # into) but it now serves TWO bodies: no token -> the wiki + shipped-mod
    # corpus alone; token -> that plus the installed sections.
    #
    # What the public body must not carry is DISCOVERY. The installed .js/.css
    # stay reachable on purpose -- /mods/<id>/<gen>/<name> is forced public
    # because a <script src> cannot carry an Authorization header -- but that
    # route needs the id AND the generation AND the file name (and cannot serve
    # help.md at all), whereas a merged corpus is a directly enumerable list of
    # every installed id.
    app = make_app(tmp_path, monkeypatch)
    _, before = app.test_client.get("/help-corpus.json")
    assert before.status == 200
    body = payload()
    body["manifest"]["help"] = {"label": "Notes", "icon": "N"}
    body["files"]["help.md"] = "# Notes\n\n## Secretish\n\nplain as day\n"
    assert authed(app).post("/mods/install", json=body)[1].status == 200

    _, public = app.test_client.get("/help-corpus.json")
    assert public.status == 200                      # still public, just less
    blob = json.dumps(public.json)
    assert "x-notes" not in blob                     # no id, anywhere
    assert "plain as day" not in blob                # no help text
    # No label and no icon either. Checked structurally, not by substring:
    # "Notes" is an ordinary word the wiki itself uses.
    sections = public.json["sections"]
    assert not [s for s in sections if s["slug"].startswith("x-")]
    assert not [s for s in sections
                if s.get("label") == "Notes" or s.get("icon") == "N"]
    assert [s for s in sections if s["slug"] == "taskbar"], \
        "the wiki sections must still be public"
    # ...and it is the pre-#163 response EXACTLY, not merely a filtered one:
    # the same object the wiki parse produced, and -- asserted on the RAW BYTES,
    # because that is the actual claim -- the same response as before the
    # install happened at all.
    assert public.json == app.ctx.help_corpus_base
    assert public.body == before.body

    # The same URL, with the token, is the merged corpus.
    _, privileged = authed(app).get("/help-corpus.json")
    assert privileged.status == 200
    sec = [s for s in privileged.json["sections"] if s["slug"] == "x-notes"]
    assert sec and sec[0]["label"] == "Notes" and sec[0]["icon"] == "N"
    assert "plain as day" in json.dumps(sec[0])

    # Every way of presenting the credential, and every way of getting it
    # wrong, resolves EXACTLY as it does on a gated route -- auth.provided_token
    # is the one implementation, precedence included. The only difference is the
    # answer: a smaller 200 instead of a 401.
    for label, kwargs in (
            ("query token", {"params": {"token": TEST_TOKEN}}),
            ("query auth", {"params": {"auth": TEST_TOKEN}}),
            # An EMPTY query arg is falsy in provided_token, so it does not
            # shadow the header -- it falls through to it.
            ("empty query, good header",
             {"params": {"token": ""},
              "headers": {"Authorization": "Bearer " + TEST_TOKEN}})):
        _, ok = app.test_client.get("/help-corpus.json", **kwargs)
        assert ok.status == 200, label
        assert ok.json == app.ctx.help_corpus, label
    for label, kwargs in (
            # A WRONG token is not a 401, it is the public body -- the whole
            # point of leaving the route public is that it never refuses the
            # login page.
            ("wrong header", {"headers": {"Authorization": "Bearer nope"}}),
            ("wrong query", {"params": {"token": "nope"}}),
            # ...and a NON-EMPTY query form wins over the header, exactly as
            # provided_token says, so a good header cannot rescue a bad query.
            ("bad query beats good header",
             {"params": {"token": "nope"},
              "headers": {"Authorization": "Bearer " + TEST_TOKEN}})):
        _, r = app.test_client.get("/help-corpus.json", **kwargs)
        assert r.status == 200, label
        assert r.body == before.body, label


def test_help_installed_before_the_broker_started_is_withheld_too(tmp_path,
                                                                  monkeypatch):
    # The other way installed help reaches the corpus: not POST /mods/install
    # but the create_app scan of a store that was already on disk. Same swap,
    # so the same split -- pinned separately because a restart is the normal
    # state of a broker with mods, and the install path is the exception.
    plant(tmp_path / "mods", manifest(),
          {"x-notes.js": "//\n", "help.md": "# Notes\n\nplanted words\n"})
    app = make_app(tmp_path, monkeypatch)
    assert "x-notes" in _help_slugs(app)             # it really did get scanned
    _, public = app.test_client.get("/help-corpus.json")
    assert public.status == 200
    assert "x-notes" not in json.dumps(public.json)
    assert "planted words" not in json.dumps(public.json)
    assert public.json == app.ctx.help_corpus_base


def test_the_served_corpus_is_never_cached_across_the_two_audiences(tmp_path,
                                                                    monkeypatch):
    # One URL, two bodies (#173). Nothing between the broker and the browser --
    # a `tailscale serve` in front of a loopback bind, a corporate proxy, the
    # browser's own disk cache -- may keep one audience's body and hand it to
    # the other. Sanic sets no ETag/Last-Modified/freshness of its own and the
    # CORS middleware adds none, so these two headers are the whole cache story.
    app = make_app(tmp_path, monkeypatch)
    assert authed(app).post("/mods/install", json=payload())[1].status == 200
    for label, (_, r) in (("public", app.test_client.get("/help-corpus.json")),
                          ("authed", authed(app).get("/help-corpus.json"))):
        assert r.status == 200, label
        assert r.headers.get("Cache-Control") == "no-store", label
        # A SET, not the literal header: Vary is a comma-separated,
        # case-insensitive field list, and a later Accept-Encoding or Origin
        # joining it would be perfectly correct.
        vary = {v.strip().lower()
                for v in (r.headers.get("Vary") or "").split(",")}
        assert "authorization" in vary, label
        # No validator, and this is a property not an accident: ONE URL with two
        # bodies must not hand out a token a conditional request could later
        # trade for a 304. If a future ETag/Last-Modified is ever added here it
        # has to be per-audience, and this is where that gets noticed.
        assert r.headers.get("ETag") is None, label
        assert r.headers.get("Last-Modified") is None, label
    # And the two bodies really are different, or the assertions above would be
    # guarding a distinction that no longer exists.
    assert _public_help_slugs(app) != _help_slugs(app)


def test_a_headless_broker_serves_no_help_corpus(tmp_path, monkeypatch):
    app = make_app(tmp_path, monkeypatch, serve_ui=False)
    _, r = app.test_client.get("/help-corpus.json")
    assert r.status == 404
    assert app.ctx.help_corpus == {"sections": []}


# --------------------------------------------------------------------------- #
# realm + gating
# --------------------------------------------------------------------------- #

def test_the_install_api_is_not_lease_gated(tmp_path, monkeypatch):
    # #157's argument for /mods/policy, verbatim. With another browser holding
    # this broker's single-active lease a /state PUT is refused 409 not_active,
    # permanently -- and that is exactly the broker an operator wants to
    # administer. Installing a mod is broker CONFIGURATION, not shared UI state.
    app = make_app(tmp_path, monkeypatch)
    app.ctx.active_client_id = "some-other-browser"
    client = authed(app)
    _, denied = client.put("/state", json={"baseRev": 0, "settings": {},
                                           "layout": {}, "clientId": "me"})
    assert denied.status == 409 and denied.json["error"] == "not_active"
    _, r = client.post("/mods/install", json=payload())
    assert r.status == 200
    _, detail = client.get("/mods/installed")
    assert detail.status == 200
    _, rescan = client.post("/mods/rescan", json={})
    assert rescan.status == 200
    _, u = client.post("/mods/uninstall", json={"id": "x-notes"})
    assert u.status == 200


def test_the_install_api_is_absent_on_a_headless_broker(tmp_path, monkeypatch):
    # #87: no page, so nothing could ever run an installed mod. Accepting one
    # would be storing bytes with no consumer.
    app = make_app(tmp_path, monkeypatch, serve_ui=False)
    for path in ("/mods/install", "/mods/uninstall", "/mods/rescan"):
        _, r = authed(app).post(path, json=payload())
        assert r.status == 404, path
    _, r = authed(app).get("/mods/installed")
    assert r.status == 404
    # ...but /mods/policy is NOT serve_ui-gated: a headless broker is still a
    # host tab, and pinning is meaningful for the browsers that federate to it.
    _, p = authed(app).post("/mods/policy", json={"set": {"clock": False}})
    assert p.status == 200


def test_reinstalling_an_identical_generation_keeps_the_real_predecessor(
        tmp_path, monkeypatch):
    """A no-op replace must not silently delete the retained generation.

    v1 is retained behind v2. POSTing the exact v2 package again computes the
    same content-addressed name, so a naive "previous == gen, therefore retain
    nothing" drops v1 from the index AND garbage-collects it -- every page
    mid-boot against v1 starts 404ing, for an install that changed nothing.
    """
    app = make_app(tmp_path, monkeypatch)
    client = authed(app)
    _, first = client.post("/mods/install", json=payload())
    v1 = first.json["gen"]
    v2_body = _v2_body()
    _, second = client.post("/mods/install", json=v2_body)
    v2 = second.json["gen"]
    _, again = client.post("/mods/install", json=v2_body)     # byte-identical
    assert again.status == 200 and again.json["gen"] == v2
    for gen in (v1, v2):
        _, served = app.test_client.get(f"/mods/x-notes/{gen}/x-notes.js")
        assert served.status == 200, gen
    assert app.ctx.mods_index["mods"]["x-notes"]["previous"] == v1
    assert {d.name for d in (tmp_path / "mods" / "x-notes").iterdir()
            if d.is_dir()} == {v1, v2}


def test_a_corrupt_newer_directory_cannot_suppress_the_predecessor(tmp_path,
                                                                   monkeypatch):
    # Retention is read from CURRENT, so a malformed directory with a newer
    # mtime is simply litter. Inferring it from mtimes would have picked the
    # corrupt one, failed to load it, and left the real predecessor unserved --
    # and then swept it.
    root = tmp_path / "mods"
    v1 = plant(root, manifest(), {"x-notes.js": "// v1\n"})
    v2 = plant(root, manifest(), {"x-notes.js": "// v2\n"})
    junk = root / "x-notes" / ("c" * 64)
    junk.mkdir()
    (junk / "x-notes.js").write_bytes(b"// junk\n")     # no mod.json at all
    app = make_app(tmp_path, monkeypatch)
    assert app.ctx.mods_index["mods"]["x-notes"]["previous"] == v1
    for gen in (v1, v2):
        _, served = app.test_client.get(f"/mods/x-notes/{gen}/x-notes.js")
        assert served.status == 200, gen
    _, rescanned = authed(app).post("/mods/rescan", json={})
    assert rescanned.status == 200
    assert {d.name for d in (root / "x-notes").iterdir()
            if d.is_dir()} == {v1, v2}


def test_a_generation_must_content_address_its_own_bytes(tmp_path, monkeypatch):
    """``gen`` is in the URL, that URL is ``immutable`` for a year, and S4 pins
    an SRI hash of these very bytes.

    So editing a file in place without renaming its directory would republish
    different content behind an identical, already-cached URL: browsers holding
    the old copy fail SRI and the mod does not load at all. Fatal, not a warning
    -- otherwise "the same gen" stops implying "the same bytes", which is the
    one property the generation scheme exists to provide.
    """
    root = tmp_path / "mods"
    gen = plant(root, manifest(), files())
    app = make_app(tmp_path, monkeypatch)
    assert "x-notes" in app.ctx.mods_index["mods"]
    (root / "x-notes" / gen / "x-notes.js").write_bytes(b"// tampered\n")
    _, r = authed(app).post("/mods/rescan", json={})
    assert r.status == 200
    assert r.json["skipped"] == {"x-notes": "bad_generation"}
    assert app.ctx.mods_index["mods"] == {}
    _, gone = app.test_client.get(f"/mods/x-notes/{gen}/x-notes.js")
    assert gone.status == 404


def test_the_repair_branch_removes_stale_files(tmp_path, monkeypatch):
    # Reinstalling over an existing content-addressed directory writes the
    # expected files -- and REMOVES anything else, so a leftover from an install
    # that died mid-write cannot end up served under a generation whose hash it
    # never contributed to.
    app = make_app(tmp_path, monkeypatch)
    client = authed(app)
    _, first = client.post("/mods/install", json=payload())
    gen = first.json["gen"]
    stale = tmp_path / "mods" / "x-notes" / gen / "old.js"
    stale.write_bytes(b"// debris\n")
    body = payload()
    body["replace"] = True
    _, again = client.post("/mods/install", json=body)
    assert again.status == 200 and again.json["gen"] == gen
    assert not stale.exists()
    _, r = client.post("/mods/rescan", json={})
    assert r.json["skipped"] == {}
    _, served = app.test_client.get(f"/mods/x-notes/{gen}/old.js")
    assert served.status == 404


def test_a_lone_surrogate_in_the_manifest_is_a_400_not_a_500(tmp_path,
                                                             monkeypatch):
    # JSON can carry "\ud800"; Python holds it happily as a str and it only
    # explodes at .encode("utf-8") -- which happens in compute_gen, OUTSIDE the
    # validation try/except. Worse, the same manifest on disk would then throw
    # during a scan and take out a startup.
    app = make_app(tmp_path, monkeypatch)
    body = json.dumps({"manifest": {"id": "x-notes", "title": "\ud800",
                                    "scripts": ["x-notes.js"]},
                       "files": {"x-notes.js": "//\n"}})
    _, r = authed(app).post("/mods/install", data=body,
                            headers={"content-type": "application/json"})
    assert r.status == 400, r.json
    assert r.json["error"] == "bad_encoding"
    assert app.ctx.mods_index["mods"] == {}


def test_every_mod_endpoint_caps_its_body_before_parsing(tmp_path, monkeypatch):
    # json.loads runs SYNCHRONOUSLY on the one event loop, so an uncapped body
    # -- even one whose extra megabytes are an ignored padding key -- freezes
    # every HTTP handler, WebSocket and terminal relay for the length of the
    # parse.
    app = make_app(tmp_path, monkeypatch)
    client = authed(app)
    padding = "x" * (modinstall.MAX_SMALL_BODY_BYTES + 64)
    for path, body in (
            ("/mods/uninstall", {"id": "x-notes", "pad": padding}),
            ("/mods/rescan", {"pad": padding})):
        _, r = client.post(path, json=body)
        assert r.status == 413, (path, r.status)
        assert r.json["error"] == "too_large"
    # ...and install's own, larger cap still applies.
    _, r = client.post("/mods/install",
                       json={"manifest": manifest(),
                             "files": {"x-notes.js":
                                       "x" * (modinstall.MAX_BODY_BYTES + 64)}})
    assert r.status == 413 and r.json["error"] == "too_large"


def test_a_failed_purge_leaves_the_code_installed(tmp_path, monkeypatch):
    # DATA FIRST, CODE LAST -- so if the data could not go, the code stays too.
    # Removing it anyway would leave orphaned state while telling the operator
    # the purge succeeded, which is the one outcome the ordering prevents.
    app = make_app(tmp_path, monkeypatch)
    client = authed(app)
    client.post("/mods/install", json=payload())
    client.post("/mods/policy", json={"set": {"x-notes": True}})
    client.put("/mod-store/x-notes", json={"baseRev": 0, "value": {"a": 1}})
    real = app_mod._write_state_atomic

    def boom(path, state):
        if path == app.ctx.mod_policy_path:
            raise OSError("sharing violation")
        return real(path, state)

    monkeypatch.setattr(app_mod, "_write_state_atomic", boom)
    _, r = client.post("/mods/uninstall", json={"id": "x-notes",
                                                "purge": True})
    assert r.status == 500 and r.json["error"] == "write_failed"
    assert "pin" in r.json["detail"]
    assert "x-notes" in app.ctx.mods_index["mods"]
    assert app.ctx.mod_policy == {"x-notes": True}


def test_a_dangling_link_can_be_uninstalled(tmp_path, monkeypatch):
    # Path.exists() is FALSE for a dangling symlink, so an exists()-based check
    # would report a successful uninstall and leave the entry on disk for the
    # next scan to resurrect.
    root = tmp_path / "mods"
    root.mkdir()
    (root / modinstall.MARKER_NAME).write_bytes(b"")
    try:
        os.symlink(str(tmp_path / "nowhere"), str(root / "x-dangling"),
                   target_is_directory=True)
    except (OSError, NotImplementedError, AttributeError) as exc:
        pytest.skip(f"cannot create a symlink here: {exc}")
    app = make_app(tmp_path, monkeypatch)
    assert app.ctx.mods_index["skipped"] == {"x-dangling": "bad_mod_id"}
    _, r = authed(app).post("/mods/uninstall", json={"id": "x-dangling"})
    assert r.status == 200
    assert not os.path.lexists(str(root / "x-dangling"))
    assert app.ctx.mods_index["skipped"] == {}


def test_the_install_api_answers_its_preflights(tmp_path, monkeypatch):
    # The Control Panel driving these is cross-origin whenever the operator
    # administers another broker, and a JSON POST is never a "simple" request.
    app = make_app(tmp_path, monkeypatch)
    for path in ("/mods/install", "/mods/uninstall", "/mods/rescan",
                 "/mods/installed"):
        _, r = authed(app).options(path)
        assert r.status == 204, path
        assert r.headers.get("Access-Control-Allow-Origin") == "*"
