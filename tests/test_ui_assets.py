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

import json
from pathlib import Path, PurePosixPath

from webterm.broker import ui
from webterm.broker.ui import INDEX_HTML

BROKER_DIR = Path(ui.__file__).resolve().parent


def _declared_mod_css():
    """Every ``mods/<id>/<file>.css`` the in-repo manifests claim to ship, read
    STRICTLY (a bad manifest raises here, by design) and deduped in _MODS-then-
    `styles` order. This is the strict source of truth the drift + per-file
    guards compare against ui's best-effort ``_mod_css`` (#77/S4)."""
    out, seen = [], set()
    for mod_dir in dict.fromkeys(
            PurePosixPath(m).parent.as_posix() for m in ui._MODS):
        meta = json.loads((BROKER_DIR / mod_dir / "mod.json").read_text(encoding="utf-8"))
        for name in meta.get("styles", []):
            rel = f"{mod_dir}/{name}"
            if rel not in seen:
                seen.add(rel)
                out.append(rel)
    return out


# --- #77/S4 fixture helpers: a synthetic fragment tree in tmp_path lets us drive
# ui.assemble() against a mod that actually ships .css without touching the real
# served page (which stays byte-identical because no in-repo mod declares css).
_SYNTH_ORDERED = [
    "00_head.html", "15_css_dialogs.css", "40_body.html",
    "86_js_mod_loader.js", "90_js_mod_boot.js", "99_tail.html",
]


def _write_synth_core(base):
    # Realistic head/body/script boundaries so position asserts mean what the
    # served page means: 15 is the last core css, 40 closes </style> + opens the
    # one <script>, 90 is loadMods(). The css/js splice anchors are the SAME
    # module constants assemble() uses.
    (base / "00_head.html").write_text("<!DOCTYPE html>\n<head><style>\n", encoding="utf-8")
    (base / "15_css_dialogs.css").write_text("/*DIALOGS*/\n", encoding="utf-8")
    (base / "40_body.html").write_text("</style></head>\n<body>\n<script>\n", encoding="utf-8")
    (base / "86_js_mod_loader.js").write_text("/*LOADER*/\n", encoding="utf-8")
    (base / "90_js_mod_boot.js").write_text("loadMods();\n", encoding="utf-8")
    (base / "99_tail.html").write_text("</script></body></html>\n", encoding="utf-8")


def _write_fixture_mod(base, mod_id, styles, files):
    md = base / "mods" / mod_id
    md.mkdir(parents=True, exist_ok=True)
    meta = {"id": mod_id, "ctxVersion": 1, "entry": f"{mod_id}.js", "styles": styles}
    (md / "mod.json").write_text(json.dumps(meta) + "\n", encoding="utf-8")
    (md / f"{mod_id}.js").write_text(f"/*{mod_id.upper()}-JS*/\n", encoding="utf-8")
    for fn, content in files.items():
        (md / fn).write_text(content, encoding="utf-8")
    return f"mods/{mod_id}/{mod_id}.js"


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
        "set-profiles-list",     # #70 launch-profile editor markup
        "renderProfilesEditor",  # #70 editor logic
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
    # The whole point of #68: no fragment is a giant script again. Mod scripts
    # (#71) and mod stylesheets (#77) ride the same cap.
    cap = 2500
    for name in (*ui._ORDERED, *ui._MODS, *_declared_mod_css()):
        lines = (BROKER_DIR / name).read_text(encoding="utf-8").count("\n")
        assert lines <= cap, f"{name} has {lines} lines (> {cap}); split it further"


def test_every_fragment_ends_in_newline_and_has_no_bom():
    # The empty-string join (#68) relies on each piece ending in its own \n; a
    # missing trailing newline fuses two statements/rules across a seam, and a
    # UTF-8 BOM mid-stream injects U+FEFF into the served bytes. Covers the mod
    # scripts (#71) AND mod stylesheets (#77), since both splice into the same
    # one <script> / one <style>.
    for name in (*ui._ORDERED, *ui._MODS, *_declared_mod_css()):
        raw = (BROKER_DIR / name).read_text(encoding="utf-8")
        assert raw.endswith("\n"), f"{name} must end in a newline"
        assert "﻿" not in raw, f"{name} carries a UTF-8 BOM"


# --------------------------------------------------------------------------- #
# assembly integrity
# --------------------------------------------------------------------------- #

def test_ordered_list_matches_disk_exactly():
    # Every fragment ui.py expects exists, and nothing else (no stray .bak /
    # Zone.Identifier / forgotten file) lives alongside them. Mismatch here is
    # exactly the failure mode the explicit _ORDERED list exists to prevent.
    # p.is_file() hardening (#71): the mods/ subdir is a directory, not a stray
    # fragment, so it must never count as "extra".
    ordered = set(ui._ORDERED)
    on_disk = {p.name for p in BROKER_DIR.iterdir()
               if p.is_file() and p.suffix in (".html", ".css", ".js")}
    missing = ordered - on_disk
    extra = on_disk - ordered
    assert not missing, f"fragments in _ORDERED but missing on disk: {sorted(missing)}"
    assert not extra, f"fragment-typed files on disk not in _ORDERED: {sorted(extra)}"


def test_assembled_equals_segment_join():
    # #71 splices the mod scripts (ui._MODS) into the one <script> BETWEEN the
    # loader and the boot fragment; #77 additionally splices each mod's manifest
    # .css into the head <style> zone, AFTER ui._MOD_CSS_AFTER and before
    # 40_body.html's </style>. So the served page is a 5-segment join, not a flat
    # join of _ORDERED. Rebuild it the same way ui.assemble does (mod-css comes
    # from the same best-effort ui._mod_css) and assert byte-equality with what
    # gets served. With no in-repo mod declaring `styles`, the css segment is
    # empty and this reduces to the #71 three-segment join.
    css_cut = ui._ORDERED.index(ui._MOD_CSS_AFTER) + 1
    js_cut = ui._ORDERED.index(ui._MOD_SPLICE_BEFORE)

    def _j(names):
        return "".join((BROKER_DIR / n).read_text(encoding="utf-8") for n in names)

    rebuilt = (
        _j(ui._ORDERED[:css_cut])
        + _j(ui._mod_css(ui._MODS, BROKER_DIR))
        + _j(ui._ORDERED[css_cut:js_cut])
        + _j(ui._MODS)
        + _j(ui._ORDERED[js_cut:])
    )
    assert rebuilt == INDEX_HTML


def test_mod_css_declared_matches_disk():
    # Drift guard for mod stylesheets (#77), the .css analogue of the _MODS .js
    # guard: every .css under mods/ is declared in some manifest's `styles`, and
    # every declared .css exists -- no orphan stylesheet silently absent from the
    # page, no dangling reference. (Both sides are empty until a mod ships css.)
    declared = set(_declared_mod_css())
    on_disk = {p.relative_to(BROKER_DIR).as_posix()
               for p in (BROKER_DIR / "mods").rglob("*.css")}
    assert declared == on_disk, (
        f"mods/ *.css drift: declared={sorted(declared)} on_disk={sorted(on_disk)}")


def test_mod_css_routed_into_head_style_zone(tmp_path):
    # A mod that ships a .css has it served INSIDE the still-open head <style>:
    # after the last core css fragment and before 40_body.html's </style>. Drive
    # the REAL ui.assemble against a synthetic fragment tree so the assertion
    # exercises production routing, not a parallel harness.
    _write_synth_core(tmp_path)
    js = _write_fixture_mod(tmp_path, "probe", ["probe.css"],
                            {"probe.css": "/*PROBE-CSS*/\n"})
    page = ui.assemble(ordered=_SYNTH_ORDERED, mods=[js], base=tmp_path)
    assert page.count("/*PROBE-CSS*/") == 1
    assert page.index("/*DIALOGS*/") < page.index("/*PROBE-CSS*/") < page.index("</style>")
    # ...and the mod .js still splices between the loader and loadMods() (#71).
    assert page.index("/*LOADER*/") < page.index("/*PROBE-JS*/") < page.index("loadMods();")


def test_malformed_mod_css_skipped_best_effort(tmp_path):
    # A malformed mod css (here: no trailing newline) is skipped + logged, never
    # crashes assembly -- the broker still boots and the rest of the page (incl.
    # the mod's own .js) is unaffected. INDEX_HTML stays a module-scope str.
    _write_synth_core(tmp_path)
    js = _write_fixture_mod(tmp_path, "probe", ["probe.css"],
                            {"probe.css": "/*NO-NEWLINE*/"})  # missing trailing \n
    page = ui.assemble(ordered=_SYNTH_ORDERED, mods=[js], base=tmp_path)
    assert "/*NO-NEWLINE*/" not in page          # the bad css is dropped
    assert "</style>" in page and "loadMods();" in page   # page still assembled
    assert "/*PROBE-JS*/" in page                # the mod's js is unaffected


def test_mod_css_rejects_unsafe_paths_and_dedupes(tmp_path):
    # Packaging/security edges: a `styles` entry that escapes the mod dir
    # ('../abs.css', '/abs.css'), nests ('nested/x.css'), or isn't css ('probe.js')
    # is rejected; a duplicate is emitted once. An out-of-dir abs.css that DOES
    # exist proves the '../' reference can't reach it.
    _write_synth_core(tmp_path)
    (tmp_path / "abs.css").write_text("/*ABS-ESCAPE*/\n", encoding="utf-8")
    styles = ["../abs.css", "/abs.css", "nested/x.css", "probe.js",
              "probe.css", "probe.css"]
    js = _write_fixture_mod(tmp_path, "probe", styles,
                            {"probe.css": "/*GOOD-CSS*/\n"})
    page = ui.assemble(ordered=_SYNTH_ORDERED, mods=[js], base=tmp_path)
    assert page.count("/*GOOD-CSS*/") == 1       # the one valid css, deduped to once
    assert "/*ABS-ESCAPE*/" not in page          # '../' / '/' never resolved out of dir
    assert "</style>" in page                    # assembly completed despite the junk
    # _mod_css returns exactly the one safe, repo-relative path.
    assert ui._mod_css([js], tmp_path) == ["mods/probe/probe.css"]


def test_mod_css_absent_styles_is_empty_and_noop(tmp_path):
    # A manifest with no `styles` (the state of every in-repo mod today) yields no
    # css segment, so the served page is byte-identical to the #71 join -- the
    # "no UI behavior change for existing features" guarantee.
    _write_synth_core(tmp_path)
    md = tmp_path / "mods" / "bare"
    md.mkdir(parents=True)
    (md / "mod.json").write_text(json.dumps({"id": "bare", "entry": "bare.js"}) + "\n",
                                 encoding="utf-8")
    (md / "bare.js").write_text("/*BARE-JS*/\n", encoding="utf-8")
    assert ui._mod_css(["mods/bare/bare.js"], tmp_path) == []
    page = ui.assemble(ordered=_SYNTH_ORDERED, mods=["mods/bare/bare.js"], base=tmp_path)
    # css zone is empty: </style> immediately follows the last core css.
    assert "/*DIALOGS*/\n</style>" in page


# --------------------------------------------------------------------------- #
# mod system (#71)
# --------------------------------------------------------------------------- #

def test_mod_loader_fragments_present_and_ordered():
    # The loader defines registerMod; the boot fragment runs loadMods() last.
    for frag in ("86_js_mod_loader.js", "90_js_mod_boot.js"):
        assert frag in ui._ORDERED, f"{frag} must be wired into _ORDERED"
    # loadMods() must be ordered after the loader so it's defined; the splice
    # point guarantees the mod scripts (registerMod) run before it.
    assert ui._ORDERED.index("86_js_mod_loader.js") \
        < ui._ORDERED.index("90_js_mod_boot.js")
    assert ui._MOD_SPLICE_BEFORE == "90_js_mod_boot.js"


def test_mod_scripts_exist_on_disk_and_match_mods_dir():
    # _MODS drift guard: the declared mod scripts exist, and every *.js under
    # mods/ is declared (no orphan mod script silently absent from the page).
    for rel in ui._MODS:
        assert (BROKER_DIR / rel).is_file(), f"declared mod missing on disk: {rel}"
    on_disk = {p.relative_to(BROKER_DIR).as_posix()
               for p in (BROKER_DIR / "mods").rglob("*.js")}
    declared = set(ui._MODS)
    assert on_disk == declared, (
        f"mods/ *.js drift: declared={sorted(declared)} on_disk={sorted(on_disk)}")


def test_clock_mod_packaged_and_manifest_agrees():
    import json
    mod_dir = BROKER_DIR / "mods" / "clock"
    js = mod_dir / "clock.js"
    manifest = mod_dir / "mod.json"
    assert js.is_file() and manifest.is_file()
    meta = json.loads(manifest.read_text(encoding="utf-8"))
    assert meta["id"] == "clock"
    assert meta["ctxVersion"] == 1
    # The script registers the same id/ctxVersion the manifest declares.
    src = js.read_text(encoding="utf-8")
    assert "registerMod(" in src
    assert "id: 'clock'" in src
    assert "ctxVersion: 1" in src


def test_set_mods_mount_and_loader_api_present():
    # The Control Panel mount point for mod-contributed settings, plus the public
    # loader API the mods + tests depend on, are in the served page.
    assert 'id="set-mods"' in INDEX_HTML
    for sym in ("function registerMod", "function loadMods",
                "function notifyModSettings", "function localInfo",
                "renderModSettingsToggles", "window.__mods"):
        assert sym in INDEX_HTML, f"missing loader symbol: {sym!r}"


def test_settings_extension_api_present():
    # #74 (S1): the generalized Control Panel settings-extension surface — radio,
    # select, and a full custom registerSettingsPane — rides in the served loader
    # alongside the unchanged boolean. These are the symbols mods (S2/S3/S5) and
    # the Playwright acceptance depend on.
    for sym in (
        "registerSettingsPane: function",
        "function _modSettingChoice",
        "function _modRegisterPane",
        "function _controlSection",
        "function _normChoiceOptions",
    ):
        assert sym in INDEX_HTML, f"missing settings-extension symbol: {sym!r}"
    # ctx.settings now exposes radio/select/combo next to the unchanged boolean.
    for sym in ("boolean: function", "radio: function", "select: function",
                "combo: function"):
        assert sym in INDEX_HTML, f"missing ctx.settings widget: {sym!r}"
    # The #set-mods host is no longer itself browser-global (visibility is now
    # per-mounted-section, driven by each control's isBrowserGlobal opt), so a
    # non-global mod control can show on a remote host tab.
    assert '<div class="set-section" id="set-mods"></div>' in INDEX_HTML


def test_clock_symbols_removed_from_core_fragments():
    # The clock is now a mod: its core renderer/handlers/markup are gone. Scope
    # the check to the CORE fragments it was extracted from (the mod script
    # legitimately still names clock-chip / the `clock` key).
    core = {
        "65_js_display_theming.js": ("applyClock", "_renderClock", "_clockTimer"),
        "40_body.html": ('id="clock-chip"', 'id="set-clock"'),
        "11_css_apps.css": ("#clock-chip",),
        "79_js_settings_modal.js": ("setClockEl",),
        "81_js_control_panel.js": ("setClockEl",),
    }
    for name, symbols in core.items():
        text = (BROKER_DIR / name).read_text(encoding="utf-8")
        for sym in symbols:
            assert sym not in text, f"{sym!r} should be gone from core fragment {name}"


# --------------------------------------------------------------------------- #
# theme mod (#75 / S2)
# --------------------------------------------------------------------------- #

def test_theme_symbols_removed_from_core_fragments():
    # The color scheme is now a mod (#75): its THEMES palette / labels /
    # applyTheme, the #set-theme radio markup + CSS, the core normalization, and
    # its Control Panel reflect/handler are gone from core. Scope the check to the
    # CORE fragments it was extracted from (the mod script legitimately still
    # names THEMES / applyTheme / the `theme` key). applyThemeSettings (the still-
    # core convergence entry point) deliberately survives — the sentinels below
    # are specific enough not to match it.
    core = {
        "65_js_display_theming.js": ("const THEMES", "THEME_LABELS", "applyTheme(name)"),
        "55_js_settings_model.js": ("hasOwnProperty.call(THEMES",),
        "40_body.html": ('id="set-theme"',),
        "15_css_dialogs.css": ("#set-theme",),
        "79_js_settings_modal.js": ("setThemeEl", "Object.keys(THEMES)"),
        "81_js_control_panel.js": ("setThemeEl", "applyTheme("),
    }
    for name, symbols in core.items():
        text = (BROKER_DIR / name).read_text(encoding="utf-8")
        for sym in symbols:
            assert sym not in text, f"{sym!r} should be gone from core fragment {name}"


def test_theme_mod_packaged_and_manifest_agrees():
    import json
    mod_dir = BROKER_DIR / "mods" / "theme"
    js = mod_dir / "theme.js"
    manifest = mod_dir / "mod.json"
    assert js.is_file() and manifest.is_file()
    meta = json.loads(manifest.read_text(encoding="utf-8"))
    assert meta["id"] == "theme"
    assert meta["ctxVersion"] == 1
    assert meta["entry"] == "theme.js"
    # The script registers the theme mod, owns the synced `theme` key through the
    # #74 radio API, and carries the moved palette + apply function.
    src = js.read_text(encoding="utf-8")
    assert "registerMod(" in src
    assert "id: 'theme'" in src
    assert "ctxVersion: 1" in src
    assert "ctx.settings.radio('theme'" in src
    assert "const THEMES" in src
    assert "function applyTheme(name)" in src
    # And the mod ships in the served page (present in the mod / gone from core).
    assert "ctx.settings.radio('theme'" in INDEX_HTML
    assert "id: 'theme'" in INDEX_HTML
    # The default stays night: it is the first option, the radio's `def`, and
    # still equals the :root CSS, so the visual default survives a pre-load paint.
    assert "def: 'night'" in src


# --------------------------------------------------------------------------- #
# pattern mod (#76 / S3)
# --------------------------------------------------------------------------- #

def test_pattern_symbols_removed_from_core_fragments():
    # The desktop background pattern is now a mod (#76): its PATTERNS list /
    # labels / the theme-var-aware applyPattern painter, the #set-pattern <select>
    # markup, the core normalization, and its Control Panel reflect/handler are
    # gone from core. Scope the check to the CORE fragments it was extracted from
    # (the mod script legitimately still names PATTERNS / applyPattern / the
    # `pattern` key; comments may still mention the word "pattern"). The sentinels
    # are specific enough not to match the surviving prose.
    core = {
        "65_js_display_theming.js": ("const PATTERNS", "PATTERN_LABELS", "function applyPattern"),
        "55_js_settings_model.js": ("PATTERNS.indexOf",),
        "40_body.html": ('id="set-pattern"',),
        "79_js_settings_modal.js": ("setPatternEl", "of PATTERNS"),
        "81_js_control_panel.js": ("setPatternEl", "applyPattern("),
    }
    for name, symbols in core.items():
        text = (BROKER_DIR / name).read_text(encoding="utf-8")
        for sym in symbols:
            assert sym not in text, f"{sym!r} should be gone from core fragment {name}"


def test_pattern_mod_packaged_and_manifest_agrees():
    import json
    mod_dir = BROKER_DIR / "mods" / "pattern"
    js = mod_dir / "pattern.js"
    manifest = mod_dir / "mod.json"
    assert js.is_file() and manifest.is_file()
    meta = json.loads(manifest.read_text(encoding="utf-8"))
    assert meta["id"] == "pattern"
    assert meta["ctxVersion"] == 1
    assert meta["entry"] == "pattern.js"
    # The script registers the pattern mod, owns the synced `pattern` key through
    # the #74 select API, and carries the moved list + labels + painter.
    src = js.read_text(encoding="utf-8")
    assert "registerMod(" in src
    assert "id: 'pattern'" in src
    assert "ctxVersion: 1" in src
    assert "ctx.settings.select('pattern'" in src
    assert "const PATTERNS" in src
    assert "function applyPattern" in src
    # The default stays none: it is the first option AND the select's `def`, and
    # applyPattern('none') clears the inline background, so the visual default is
    # preserved with no core normalization.
    assert "def: 'none'" in src
    # And the mod ships in the served page (present in the mod / gone from core),
    # registered AFTER the theme mod so notifyModSettings writes the chrome vars
    # before the pattern repaints on a both-changed /state pull, and applyPattern
    # is a hoisted global the theme mod's coupling can still reach.
    assert "ctx.settings.select('pattern'" in INDEX_HTML
    assert "id: 'pattern'" in INDEX_HTML
    assert "function applyPattern" in INDEX_HTML
    assert INDEX_HTML.index("id: 'theme'") < INDEX_HTML.index("id: 'pattern'")


def test_clock_tz_selector_packaged_and_manifest_agrees():
    import json
    mod_dir = BROKER_DIR / "mods" / "clock"
    js = mod_dir / "clock.js"
    manifest = mod_dir / "mod.json"
    assert js.is_file() and manifest.is_file()
    meta = json.loads(manifest.read_text(encoding="utf-8"))
    assert meta["id"] == "clock"
    assert meta["ctxVersion"] == 1
    assert meta["entry"] == "clock.js"
    # #104: the clock now owns a synced `clockTz` time-zone key through the new
    # searchable combo API (browser-global, def '' == follow the viewing
    # browser). The zone list is built dynamically from Intl.supportedValuesOf
    # with a curated fallback (Asia/Tokyo is one of the fallback markers). The
    # mod declares the `settings` tier on top of `taskbar` (order must match
    # _EXPECTED_TIERS).
    src = js.read_text(encoding="utf-8")
    for needle in ("registerMod(", "id: 'clock'", "ctxVersion: 1",
                   "tiers: ['taskbar', 'settings']",
                   "ctx.settings.combo('clockTz'", "def: ''",
                   "(browser default)", "Intl.supportedValuesOf", "Asia/Tokyo"):
        assert needle in src, f"missing clock-tz sentinel in mod src: {needle!r}"
    # And it ships in the served page — the mod script + the combo primitive it
    # relies on (the datalist-backed searchable input).
    for needle in ("ctx.settings.combo('clockTz'", "def: ''",
                   "(browser default)", "Intl.supportedValuesOf", "Asia/Tokyo",
                   "createElement('datalist')"):
        assert needle in INDEX_HTML, f"missing clock-tz sentinel in page: {needle!r}"


# --------------------------------------------------------------------------- #
# help mod (#78 / S5)
# --------------------------------------------------------------------------- #

def test_help_symbols_removed_from_core_fragments():
    # The Help WINDOW, the taskbar "?" chip, the show/hide toggle, the chip
    # wiring and the render machinery are now a mod (#78): their core markup /
    # handlers / CSS are gone. Scope the check to the CORE fragments they were
    # extracted from. The corpus DATA pipeline (fetchHelpCorpus / buildHelpEntries
    # / /help-corpus.json) deliberately STAYS in core 80 (it reads core state), so
    # the sentinels target only the moved window/chip/toggle, never the kept
    # corpus (see test_help_corpus_pipeline_kept_in_core).
    core = {
        "65_js_display_theming.js": ("applyHelpButton",),
        "40_body.html": ('id="help-chip"', 'id="set-help-button"'),
        "12_css_help.css": ("#help-chip", ".app-help"),
        "79_js_settings_modal.js": ("setHelpButtonEl",),
        "81_js_control_panel.js": ("setHelpButtonEl", "function focusOrOpenHelp",
                                   "wireHelpChip", "maybeShowHelpHint",
                                   "applyHelpButton"),
        "80_js_help_window.js": ("function openHelpWindow", "function buildHelpBody",
                                 "function renderHelpInto", "function findHelpWindow"),
    }
    for name, symbols in core.items():
        text = (BROKER_DIR / name).read_text(encoding="utf-8")
        for sym in symbols:
            assert sym not in text, f"{sym!r} should be gone from core fragment {name}"


def test_help_corpus_pipeline_kept_in_core():
    # Issue #78 keeps the corpus + the buildHelpEntries merge in core (they read
    # core state: KEY_ACTIONS / profilesCache / mcpConfigCache); the help mod
    # calls these hoisted functions. They must NOT have been swept into the mod.
    src = (BROKER_DIR / "80_js_help_window.js").read_text(encoding="utf-8")
    for sym in ("function buildHelpEntries", "function fetchHelpCorpus",
                "function flattenHelpCorpus", "function helpTextBlock"):
        assert sym in src, f"{sym!r} must stay in core 80_js_help_window.js"
    # And they remain reachable in the served page for the mod to call.
    assert "function buildHelpEntries" in INDEX_HTML


def test_help_mod_packaged_and_manifest_agrees():
    import json
    mod_dir = BROKER_DIR / "mods" / "help"
    js = mod_dir / "help.js"
    css = mod_dir / "help.css"
    manifest = mod_dir / "mod.json"
    assert js.is_file() and css.is_file() and manifest.is_file()
    meta = json.loads(manifest.read_text(encoding="utf-8"))
    assert meta["id"] == "help"
    assert meta["ctxVersion"] == 1
    assert meta["entry"] == "help.js"
    # First mod to ship a packaged stylesheet via the S4 route (#77).
    assert meta["styles"] == ["help.css"]
    # The script registers the help mod, contributes the 'help' window kind through
    # ctx.registerWindowKind (#100, so its (+) launcher rides the mod's enable/
    # disable), and carries the moved window factory + chip. The redundant
    # showHelpButton toggle is gone (#101) — the chip follows the mod's enabled state.
    src = js.read_text(encoding="utf-8")
    assert "registerMod(" in src
    assert "id: 'help'" in src
    assert "ctxVersion: 1" in src
    assert "ctx.registerWindowKind(" in src
    assert "appKind: 'help'" in src
    assert "showHelpButton" not in src
    assert "function openHelpWindow" in src
    assert "function applyHelpButton" in src
    # XSS render-order invariant: helpAppendHighlighted must precede
    # findHelpWindow with no innerHTML between them (test_help_corpus.py's
    # test_help_render_path_has_no_innerhtml slices INDEX_HTML between the two).
    assert src.index("function helpAppendHighlighted(") \
        < src.index("function findHelpWindow(")
    # And the mod ships in the served page (present in the mod / gone from core),
    # registered AFTER the clock so the clock's "addStatusItem before #help-chip"
    # slot is preserved.
    assert "function openHelpWindow" in INDEX_HTML
    assert "id: 'help'" in INDEX_HTML
    assert INDEX_HTML.index("id: 'clock'") < INDEX_HTML.index("id: 'help'")


def test_register_help_cards_capability_present():
    # #78 (S5): ctx.registerHelpCards + the loader-side sanitizer (DOM-safe typed
    # block/span schema, never raw HTML) + the window.__mods.helpCards registry
    # ride in the served loader. The Playwright acceptance (a fixture mod's cards
    # appear in Help) depends on these.
    for sym in ("registerHelpCards: function", "function _modRegisterHelpCards",
                "function _sanitizeHelpCard", "function _sanitizeHelpBlocks",
                "helpCards:"):
        assert sym in INDEX_HTML, f"missing registerHelpCards symbol: {sym!r}"


# --------------------------------------------------------------------------- #
# window-kind registry (#80 / S7)
# --------------------------------------------------------------------------- #

def test_window_kind_registry_core_present():
    # The registry primitives ride in the served page: the no-TDZ getter, the
    # register/lookup/list/delete helpers, the shared serializer, and the lazy
    # built-in population. The Playwright acceptance drives these via globals +
    # window.__mods.__test.windowKinds.
    for sym in ("function _windowKindRegistry", "function registerWindowKind",
                "function deleteWindowKind", "function lookupWindowKind",
                "function windowKindMenuList", "function registerBuiltinWindowKinds",
                "function serializeAppWindow", "function openNoteOrEditorWindow",
                "windowKinds: function"):
        assert sym in INDEX_HTML, f"missing window-kind registry symbol: {sym!r}"


def test_register_window_kind_capability_present():
    # #80 (S7): ctx.registerWindowKind + its loader-side wrapper (validate via the
    # core registerWindowKind, teardown that removes exactly this registration).
    # The fixture-mod acceptance (a brand-new kind end-to-end) depends on these.
    for sym in ("registerWindowKind: function", "function _modRegisterWindowKind",
                "deleteWindowKind(entry.appKind, entry)"):
        assert sym in INDEX_HTML, f"missing registerWindowKind symbol: {sym!r}"


def test_window_kind_builtins_registered_in_menu_order():
    # registerBuiltinWindowKinds registers the ONE remaining core kind (control-
    # panel); sticky-note left for the S8 mod #81, text-editor for the S10 mod #83,
    # file-manager for the S11 mod #84, task-manager for the S12 mod #85, and help
    # for the #100 mod. Registration order is the historical (+) launch-menu order
    # (Map iteration order drives the menu).
    src = (BROKER_DIR / "54_js_app_windows_store.js").read_text(encoding="utf-8")
    order = ["control-panel"]
    positions = []
    for kind in order:
        needle = f"appKind: '{kind}'"
        assert needle in src, f"built-in kind not registered: {kind}"
        positions.append(src.index(needle))
    assert positions == sorted(positions), \
        "built-in kinds must register in the historical menu order"
    # sticky-note, text-editor, file-manager, task-manager + help are now mods, never
    # core built-ins (each appends through ctx at loadMods time). The sticky note's
    # retain-on-close rode with it; the editor / file-manager / task-manager / help
    # specs rode with them too (#100 moved Help's registration into mods/help/).
    assert "appKind: 'sticky-note'" not in src
    assert "appKind: 'text-editor'" not in src
    assert "appKind: 'file-manager'" not in src
    assert "appKind: 'task-manager'" not in src
    assert "appKind: 'help'" not in src
    assert "retainOnClose: function (rec)" not in src
    # No persisted CORE built-in remains: the file-manager's serializeAppWindow
    # reference moved to mods/file-manager/ (text-editor's to mods/editor/,
    # sticky's to mods/sticky/), so core registers ZERO `serialize:` built-ins
    # (the sole survivor control-panel is ephemeral).
    assert src.count("serialize: serializeAppWindow") == 0


def test_sticky_symbols_removed_from_core_fragments():
    # The sticky note is now a mod (#81/S8): its registry registration is gone from
    # core 54, and its launcher + Closed-notes builder are gone from core 76 (both
    # moved verbatim into mods/sticky/sticky.js). The shared builder
    # (openNoteOrEditorWindow) moved to mods/editor/ (#83/S10, the text editor owns
    # it now); the serializer (serializeAppWindow) deliberately STAYS in core (the
    # file-manager built-in + both mods share it). The sticky mod calls back into
    # openNoteOrEditorWindow + serializeAppWindow — both reachable in the served
    # page regardless of which fragment/mod ships them.
    core = {
        "54_js_app_windows_store.js": ("appKind: 'sticky-note'",
                                       "retainOnClose: function (rec)"),
        "76_js_launch_fullscreen.js": ("function launchStickyNote",
                                       "function closedAppMenuItems"),
    }
    for name, symbols in core.items():
        text = (BROKER_DIR / name).read_text(encoding="utf-8")
        for sym in symbols:
            assert sym not in text, f"{sym!r} should be gone from core fragment {name}"
    # The helpers the sticky mod calls back into are still present + reachable in
    # the served page (openNoteOrEditorWindow now ships in mods/editor/).
    for sym in ("function openNoteOrEditorWindow", "function serializeAppWindow"):
        assert sym in INDEX_HTML, f"{sym!r} must stay reachable in the served page"


def test_sticky_mod_packaged_and_manifest_agrees():
    import json
    mod_dir = BROKER_DIR / "mods" / "sticky"
    js = mod_dir / "sticky.js"
    manifest = mod_dir / "mod.json"
    assert js.is_file() and manifest.is_file()
    meta = json.loads(manifest.read_text(encoding="utf-8"))
    assert meta["id"] == "sticky"
    assert meta["ctxVersion"] == 1
    assert meta["entry"] == "sticky.js"
    # The script registers the sticky mod and contributes the sticky-note window
    # kind through ctx.registerWindowKind, reusing the core serializer + builder so
    # persistence (webterm:appwindows:v1) stays byte-identical.
    src = js.read_text(encoding="utf-8")
    assert "registerMod(" in src
    assert "id: 'sticky'" in src
    assert "ctxVersion: 1" in src
    assert "ctx.registerWindowKind(" in src
    assert "appKind: 'sticky-note'" in src
    assert "serialize: serializeAppWindow" in src
    assert "return openNoteOrEditorWindow(d)" in src
    # The retain trim + Closed-notes menu rode along with the kind.
    assert "retainOnClose: function (rec)" in src
    assert "Closed notes" in src
    # And the mod ships in the served page (present in the mod / gone from core),
    # registered AFTER the help mod (its position in _MODS).
    assert "id: 'sticky'" in INDEX_HTML
    assert INDEX_HTML.index("id: 'help'") < INDEX_HTML.index("id: 'sticky'")


# --------------------------------------------------------------------------- #
# text-editor mod (#83 / S10)
# --------------------------------------------------------------------------- #

def test_editor_symbols_removed_from_core_fragments():
    # The text editor is now a mod (#83/S10): its built-in registration is gone
    # from core 54, its launcher from core 76, and the AGENTS.md hooks
    # (openAgentDocsWindow + openAgentsMdEditor) from core 73 — all moved into
    # mods/editor/. The CodeMirror fragment (69) + editor fragment (70) are
    # DELETED; openAppWindow (the dispatcher) moved into core 54.
    assert not (BROKER_DIR / "69_js_codemirror.js").exists()
    assert not (BROKER_DIR / "70_js_editor_app.js").exists()
    gone = {
        "54_js_app_windows_store.js": ("appKind: 'text-editor'",
                                       "function openNoteOrEditorWindow"),
        "73_js_window_runtime.js": ("function openAgentDocsWindow",
                                    "function openAgentsMdEditor"),
        "76_js_launch_fullscreen.js": ("function launchTextEditor",),
    }
    for name, symbols in gone.items():
        text = (BROKER_DIR / name).read_text(encoding="utf-8")
        for sym in symbols:
            assert sym not in text, f"{sym!r} should be gone from core fragment {name}"
    # openAppWindow (the central dispatcher) moved into core 54, NOT the mod.
    s54 = (BROKER_DIR / "54_js_app_windows_store.js").read_text(encoding="utf-8")
    assert "function openAppWindow" in s54
    # And the moved builder/hooks are present + reachable in the served page (they
    # ship in mods/editor/ as hoisted functions, so core reaches them mods-off).
    for sym in ("function openNoteOrEditorWindow", "function loadCodeMirror",
                "function openAgentDocsWindow", "function openAgentsMdEditor",
                "function launchTextEditor"):
        assert sym in INDEX_HTML, f"{sym!r} must stay reachable in the served page"


def test_editor_mod_packaged_and_manifest_agrees():
    import json
    mod_dir = BROKER_DIR / "mods" / "editor"
    editor_js = mod_dir / "editor.js"
    cm_js = mod_dir / "codemirror.js"
    manifest = mod_dir / "mod.json"
    assert editor_js.is_file() and cm_js.is_file() and manifest.is_file()
    meta = json.loads(manifest.read_text(encoding="utf-8"))
    assert meta["id"] == "editor"
    assert meta["ctxVersion"] == 1
    assert meta["entry"] == "editor.js"
    # Both mod scripts are declared in _MODS (the codemirror lazy loader + the
    # editor), so the .js drift guard accepts them.
    assert "mods/editor/codemirror.js" in ui._MODS
    assert "mods/editor/editor.js" in ui._MODS
    src = editor_js.read_text(encoding="utf-8")
    # Registers the editor mod + contributes the text-editor window kind through
    # ctx.registerWindowKind, reusing the shared core serializer + builder.
    assert "registerMod(" in src
    assert "id: 'editor'" in src
    assert "ctxVersion: 1" in src
    assert "ctx.registerWindowKind(" in src
    assert "appKind: 'text-editor'" in src
    assert "serialize: serializeAppWindow" in src
    assert "return openNoteOrEditorWindow(d)" in src
    assert "return launchTextEditor()" in src
    # File I/O rides ctx.file (#82): the mod stashes ctx.file and every /file/*
    # call flows through editorFile() — NO direct fileApiPost survives in the mod.
    assert "editorFile.cap = ctx.file;" in src
    assert "editorFile().read(" in src
    assert "editorFile().write(" in src
    assert "editorFile().list(" in src
    assert "fileApiPost(" not in src, "editor mod must route I/O through ctx.file"
    # The CodeMirror loader rode along as a separate file (helpers only, no
    # registerMod), so it can stay a small fragment.
    cm = cm_js.read_text(encoding="utf-8")
    assert "function loadCodeMirror" in cm and "function detectLanguage" in cm
    assert "registerMod(" not in cm
    # Ships in the served page, AFTER the help mod, BEFORE the sticky mod (so the
    # (+) menu lists Text editor before Sticky note, after the core built-ins).
    assert "id: 'editor'" in INDEX_HTML
    assert INDEX_HTML.index("id: 'help'") < INDEX_HTML.index("id: 'editor'")
    assert INDEX_HTML.index("id: 'editor'") < INDEX_HTML.index("id: 'sticky'")


def test_editor_serialized_fields_preserved():
    # The hard #83 requirement: every editor serialized field round-trips. They
    # live in the SHARED core serializeAppWindow (54), unchanged by the extraction.
    s54 = (BROKER_DIR / "54_js_app_windows_store.js").read_text(encoding="utf-8")
    for field in ("filePath:", "wrap:", "lineNums:", "startDir:", "docs:",
                  "activeTab:", "agentsMdCwd:", "fileHostId:", "encoding:"):
        assert field in s54, f"serializeAppWindow lost the {field!r} editor field"


def test_window_kind_sites_use_registry():
    # The seven hardcoded appKind branches are replaced by registry lookups, and
    # the old per-kind branches are gone from each fragment they lived in.
    s54 = (BROKER_DIR / "54_js_app_windows_store.js").read_text(encoding="utf-8")
    assert "const kind = lookupWindowKind(win.appKind);" in s54
    for gone in ("win.appKind === 'task-manager'", "win.appKind === 'control-panel'",
                 "win.appKind === 'help'"):
        assert gone not in s54, f"old saveAppWindow branch survived: {gone!r}"

    # openAppWindow (the dispatcher) moved from the deleted 70 into core 54
    # (#83/S10) when the editor was extracted; it still dispatches via the registry
    # with the unknown-kind openNoteOrEditorWindow fallback, and the old per-kind
    # branches stay gone.
    assert "const kind = lookupWindowKind(appData.appKind);" in s54
    assert "return openNoteOrEditorWindow(appData);" in s54
    for gone in ("return openFileManagerWindow(appData)",
                 "return openTaskManagerWindow(appData)",
                 "return openControlPanelWindow(appData)",
                 "return openHelpWindow(appData)"):
        assert gone not in s54, f"old openAppWindow dispatch branch survived: {gone!r}"

    s73 = (BROKER_DIR / "73_js_window_runtime.js").read_text(encoding="utf-8")
    assert "kind.retainOnClose(rec)" in s73
    assert "rec.appKind === 'sticky-note'" not in s73

    s84 = (BROKER_DIR / "84_js_active_view_lifecycle.js").read_text(encoding="utf-8")
    assert "lookupWindowKind(rec && rec.appKind)" in s84
    assert "=== 'task-manager'" not in s84   # the old explicit skip list is gone

    s76 = (BROKER_DIR / "76_js_launch_fullscreen.js").read_text(encoding="utf-8")
    assert "windowKindMenuList()" in s76


# --------------------------------------------------------------------------- #
# file-manager mod (#84 / S11)
# --------------------------------------------------------------------------- #

def test_filemanager_symbols_removed_from_core_fragments():
    # The file manager is now a mod (#84/S11): its built-in registration is gone
    # from core 54 and its launcher from core 76 — both moved into
    # mods/file-manager/. The core fragment 71_js_file_manager.js is DELETED.
    assert not (BROKER_DIR / "71_js_file_manager.js").exists()
    assert "71_js_file_manager.js" not in ui._ORDERED
    gone = {
        "54_js_app_windows_store.js": ("appKind: 'file-manager'",
                                       "function openFileManagerWindow",
                                       "return openFileManagerWindow(d)"),
        "76_js_launch_fullscreen.js": ("function launchFileManager",),
    }
    for name, symbols in gone.items():
        text = (BROKER_DIR / name).read_text(encoding="utf-8")
        for sym in symbols:
            assert sym not in text, f"{sym!r} should be gone from core fragment {name}"
    # The moved builder + launcher are present + reachable in the served page (they
    # ship in mods/file-manager/ as hoisted functions).
    for sym in ("function openFileManagerWindow", "function launchFileManager"):
        assert sym in INDEX_HTML, f"{sym!r} must stay reachable in the served page"


def test_openappwindow_fallback_does_not_coerce_unknown_kinds():
    # mods-off safety (#84): a persisted file-manager record must NOT be coerced
    # into a sticky note by the unknown-kind fallback (which would mis-render it
    # AND rewrite its stored record, destroying it). openAppWindow's fallback only
    # builds the note/editor for the note/editor kinds (+ a legacy record with no
    # appKind); any other unregistered kind returns null, leaving its record intact.
    s54 = (BROKER_DIR / "54_js_app_windows_store.js").read_text(encoding="utf-8")
    assert "if (ak && ak !== 'sticky-note' && ak !== 'text-editor') return null;" in s54
    # The note/editor builder is still the fallback for the kinds it owns.
    assert "return openNoteOrEditorWindow(appData);" in s54


def test_filemanager_mod_packaged_and_manifest_agrees():
    import json
    mod_dir = BROKER_DIR / "mods" / "file-manager"
    fm_js = mod_dir / "file-manager.js"
    manifest = mod_dir / "mod.json"
    assert fm_js.is_file() and manifest.is_file()
    meta = json.loads(manifest.read_text(encoding="utf-8"))
    assert meta["id"] == "file-manager"
    assert meta["ctxVersion"] == 1
    assert meta["entry"] == "file-manager.js"
    assert "mods/file-manager/file-manager.js" in ui._MODS
    src = fm_js.read_text(encoding="utf-8")
    # Registers the file-manager mod + contributes the file-manager window kind
    # through ctx.registerWindowKind, reusing the shared core serializer + builder.
    assert "registerMod(" in src
    assert "id: 'file-manager'" in src
    assert "ctxVersion: 1" in src
    assert "ctx.registerWindowKind(" in src
    assert "appKind: 'file-manager'" in src
    assert "serialize: serializeAppWindow" in src
    assert "return openFileManagerWindow(d)" in src
    assert "return launchFileManager()" in src
    # File I/O (incl. the DESTRUCTIVE delete + upload) rides ctx.file (#82): the
    # mod stashes ctx.file and every /file/* call flows through fmFile() — NO direct
    # fileApiPost AND no raw upload fetch (hostHttpUrl) survives in the mod.
    assert "fmFile.cap = ctx.file;" in src
    assert "fmFile().list(" in src
    assert "fmFile().read(" in src
    assert "fmFile().delete(" in src
    assert "fmFile().upload(" in src
    assert "fileApiPost(" not in src, "file-manager mod must route I/O through ctx.file"
    assert "hostHttpUrl(" not in src, "the raw upload fetch must be gone"
    # Ships in the served page, AFTER the help mod and BEFORE the editor mod (so the
    # (+) menu lists File manager right after the core built-ins, ahead of the
    # text-editor + sticky-note mods).
    assert "id: 'file-manager'" in INDEX_HTML
    assert INDEX_HTML.index("id: 'help'") < INDEX_HTML.index("id: 'file-manager'")
    assert INDEX_HTML.index("id: 'file-manager'") < INDEX_HTML.index("id: 'editor'")


def test_filemanager_serialized_fields_preserved():
    # The hard #84 requirement: every file-manager serialized field round-trips.
    # They live in the SHARED core serializeAppWindow (54), unchanged by the
    # extraction (the mod reuses it as its `serialize`).
    s54 = (BROKER_DIR / "54_js_app_windows_store.js").read_text(encoding="utf-8")
    for field in ("fmLeft:", "fmRight:", "fmLeftHostId:", "fmRightHostId:",
                  "fileHostId:"):
        assert field in s54, f"serializeAppWindow lost the {field!r} file-manager field"


# --------------------------------------------------------------------------- #
# ctx.file capability (#82 / S9)
# --------------------------------------------------------------------------- #

def test_file_capability_present():
    # #82 (S9): the ctx.file wrapper over /file/* + its host-routing helpers ride
    # in the served loader. These are the symbols the Playwright acceptance (and
    # any S10/S11 mod) depends on. ctxVersion stays 1 (additive capability).
    loader = (BROKER_DIR / "86_js_mod_loader.js").read_text(encoding="utf-8")
    # The capability object + its five methods, on the per-mod ctx.
    for sym in ("file: {",
                "read: function (path, opts)",
                "write: function (path, content, opts)",
                "list: function (path, opts)",
                "'delete': function (path, opts)",
                "upload: function (path, contentB64, opts)"):
        assert sym in loader, f"missing ctx.file method: {sym!r}"
    # Each method targets the matching /file/* route, wrapped here.
    for route in ("'/file/read'", "'/file/write'", "'/file/list'",
                  "'/file/delete'", "'/file/upload'"):
        assert route in loader, f"ctx.file does not wrap route {route!r}"
    # The host-routing helpers: fail-closed resolution + the synthetic error.
    for sym in ("function _modFileHost", "function _modFileApi",
                "error: 'host_not_found'"):
        assert sym in loader, f"missing ctx.file host-routing symbol: {sym!r}"
    # Routing reuses the EXISTING core helpers (no parallel host logic).
    for sym in ("hostById(hostId)", "return localHost();",
                "fileApiPost(route, body, host)"):
        assert sym in loader, f"ctx.file must reuse core host helper: {sym!r}"
    # ctxVersion is unchanged — ctx.file is additive.
    assert "ctxVersion: 1" in loader
    # And it all reaches the served page.
    for sym in ("function _modFileApi", "file: {", "error: 'host_not_found'"):
        assert sym in INDEX_HTML, f"ctx.file missing from served page: {sym!r}"


def test_dialog_component_present():
    # #72 (Part A): the reusable styled dialog primitive + wrappers ship as the
    # new 69_js_dialog.js fragment, registered right after the file-dialog
    # fragment, and reach the served page; its CSS rides the shared dialogs
    # fragment by folding .app-dialog into the existing selector groups.
    assert "69_js_dialog.js" in ui._ORDERED
    assert ui._ORDERED.index("69_js_dialog.js") == \
        ui._ORDERED.index("68_js_app_windows_files.js") + 1
    assert (BROKER_DIR / "69_js_dialog.js").is_file()
    src = (BROKER_DIR / "69_js_dialog.js").read_text(encoding="utf-8")
    for sym in ("function openDialog", "function openTextPrompt",
                "function openConfirmDialog", "function openInfoModal"):
        assert sym in src, f"dialog fragment missing {sym!r}"
        assert sym in INDEX_HTML, f"dialog symbol missing from served page: {sym!r}"
    css = (BROKER_DIR / "15_css_dialogs.css").read_text(encoding="utf-8")
    for sel in (".app-dialog-overlay", ".app-dialog button.danger",
                ".app-dialog-rows"):
        assert sel in css, f"dialog CSS missing {sel!r}"
    assert ".app-dialog" in INDEX_HTML


def test_browse_pane_component_present():
    # #93: the reusable single browse-pane kernel ships as the new
    # 70_js_browse_pane.js fragment, ordered right after the dialog fragment,
    # and reaches the served page. BOTH consumers — the editor's openFileDialog
    # (core 68) and the file-manager mod — instantiate it, which is what proves
    # the two drifted directory-browsers were actually collapsed onto one.
    assert "70_js_browse_pane.js" in ui._ORDERED
    assert ui._ORDERED.index("70_js_browse_pane.js") == \
        ui._ORDERED.index("69_js_dialog.js") + 1
    frag = BROKER_DIR / "70_js_browse_pane.js"
    assert frag.is_file()
    src = frag.read_text(encoding="utf-8")
    assert "function createBrowsePane" in src
    assert "function createBrowsePane" in INDEX_HTML
    # Both consumers instantiate the component (the duplication is gone).
    dlg = (BROKER_DIR / "68_js_app_windows_files.js").read_text(encoding="utf-8")
    fm = (BROKER_DIR / "mods" / "file-manager" / "file-manager.js").read_text(
        encoding="utf-8")
    assert "createBrowsePane(" in dlg, "the editor dialog must use createBrowsePane"
    assert "createBrowsePane(" in fm, "the file manager must use createBrowsePane"
    # The component is strictly host-/IO-agnostic: it must NOT reach for hosts,
    # the file API, or persistence — those are injected per-consumer via hooks.
    # Locking this keeps the editor dialog working mods-off and the FM's
    # fail-closed host semantics where they belong (the consumer).
    for banned in ("fileApiPost(", "hostHttpUrl(", "saveAppWindow(",
                   "paneHost(", "fmFile("):
        assert banned not in src, \
            f"browse-pane component must stay I/O-agnostic, found {banned!r}"


def test_sticky_pin_button_present():
    # #95: a sticky note's titlebar gains an always-on-top (▲/△) toggle. The
    # feature is three wired edits — a per-note `pinned` flag (default true)
    # persisted by the shared serializer, a z-tier gate so an unpinned note drops
    # out of the high NOTE_Z_BASE tier, and the titlebar button itself — so lock
    # each edit at its source AND in the served page. (Real click/z-order
    # behavior is verified out of band via Playwright; this is the presence gate.)
    editor_js = (BROKER_DIR / "mods" / "editor" / "editor.js").read_text(
        encoding="utf-8")
    store_js = (BROKER_DIR / "54_js_app_windows_store.js").read_text(
        encoding="utf-8")
    poll_js = (BROKER_DIR / "64_js_sessions_poll_control.js").read_text(
        encoding="utf-8")
    css = (BROKER_DIR / "10_css_root.css").read_text(encoding="utf-8")

    # The titlebar button (class hook + accessible title) ships in the editor mod
    # and reaches the served page.
    for needle in ("btn-pin", "always on top"):
        assert needle in editor_js, f"editor mod missing pin marker {needle!r}"
        assert needle in INDEX_HTML, f"pin marker missing from served page: {needle!r}"
    # The flag is persisted unconditionally by the shared serializer.
    assert "pinned: !!win.pinned" in store_js
    assert "pinned: !!win.pinned" in INDEX_HTML
    # The note z-tier is gated on the flag, still SCOPED to sticky notes so
    # `pinned` never becomes a cross-app z-capability.
    assert "win.pinned !== false" in poll_js
    assert "appKind === 'sticky-note'" in poll_js
    assert "win.pinned !== false" in INDEX_HTML
    # And the CSS styling/test hook exists.
    assert ".btn-pin" in css


def test_control_panel_floats_above_sticky_notes():
    # #98: the floating Control Panel rides a z-tier ABOVE the sticky-note
    # always-on-top tier (single floatZIndex source of truth, core 64).
    src = (BROKER_DIR / "64_js_sessions_poll_control.js").read_text(encoding="utf-8")
    assert "CONTROL_PANEL_Z_BASE" in src
    assert "appKind === 'control-panel'" in src
    assert "NOTE_Z_BASE = 90000" in src          # tier sits above the note tier
    assert "CONTROL_PANEL_Z_BASE" in INDEX_HTML   # and reaches the served page


def test_no_native_dialogs_in_served_page():
    # #89: the whole app routes every confirm/prompt through the styled dialog
    # component — NO native confirm()/prompt()/alert() survives anywhere in the
    # served page (core + every mod, assembled in one shot). The lookbehind skips
    # method calls / longer identifiers, and the styled wrappers are capitalized
    # (openConfirmDialog / openTextPrompt) so they never trip the lowercase match.
    import re
    assert not re.search(r"(?<![\w.])(confirm|prompt|alert)\s*\(", ui.INDEX_HTML), \
        "native confirm()/prompt()/alert() must not survive (use the styled dialog)"
    for sym in ("openConfirmDialog(", "openTextPrompt("):
        assert sym in ui.INDEX_HTML, f"styled dialog wrapper missing: {sym!r}"


def test_file_capability_richer_ops_present():
    # #72: ctx.file gains mkdir/copy/move/zip/unzip/stat and a recursive flag on
    # delete; ctxVersion stays 1 (additive). The SAME methods are mirrored in the
    # file-manager's fmFile() fallback so its I/O is identical mods on or off.
    loader = (BROKER_DIR / "86_js_mod_loader.js").read_text(encoding="utf-8")
    fm = (BROKER_DIR / "mods" / "file-manager" / "file-manager.js").read_text(
        encoding="utf-8")
    for src, label in ((loader, "loader ctx.file"), (fm, "fmFile fallback")):
        for sym in ("mkdir: function", "copy: function", "move: function",
                    "zip: function", "unzip: function", "stat: function",
                    "setattr: function"):                       # #96
            assert sym in src, f"{label} missing #72 method: {sym!r}"
        for route in ("'/file/mkdir'", "'/file/copy'", "'/file/move'",
                      "'/file/zip'", "'/file/unzip'", "'/file/stat'",
                      "'/file/setattr'"):                       # #96
            assert route in src, f"{label} does not wrap route {route!r}"
        # delete carries the recursive flag.
        assert "recursive: !!(opts && opts.recursive)" in src, \
            f"{label} delete missing recursive flag"
    # ctxVersion unchanged (additive capability).
    assert "ctxVersion: 1" in loader
    # And the new routes reach the served page.
    for route in ("'/file/copy'", "'/file/zip'", "'/file/stat'",
                  "'/file/setattr'"):                           # #96
        assert route in INDEX_HTML, f"#72 route missing from served page: {route!r}"


def test_file_capability_chunked_ops_present():
    # #108: ctx.file gains readChunk + the upload-session trio (uploadBegin/
    # uploadChunk/uploadCommit/uploadAbort); ctxVersion stays 1 (additive). The
    # SAME methods are mirrored in the file-manager fmFile() fallback so its I/O is
    # identical mods on or off, and the transfer + download rewrites drive them.
    loader = (BROKER_DIR / "86_js_mod_loader.js").read_text(encoding="utf-8")
    fm = (BROKER_DIR / "mods" / "file-manager" / "file-manager.js").read_text(
        encoding="utf-8")
    for src, label in ((loader, "loader ctx.file"), (fm, "fmFile fallback")):
        for sym in ("readChunk: function", "uploadBegin: function",
                    "uploadChunk: function", "uploadCommit: function",
                    "uploadAbort: function"):
            assert sym in src, f"{label} missing #108 method: {sym!r}"
        for route in ("'/file/read_chunk'", "'/file/upload_begin'",
                      "'/file/upload_chunk'", "'/file/upload_commit'",
                      "'/file/upload_abort'"):
            assert route in src, f"{label} does not wrap route {route!r}"
    # ctxVersion unchanged (additive capability).
    assert "ctxVersion: 1" in loader
    # The new routes reach the served page.
    for route in ("'/file/read_chunk'", "'/file/upload_begin'",
                  "'/file/upload_commit'"):
        assert route in INDEX_HTML, \
            f"#108 route missing from served page: {route!r}"
    # The transfer + download rewrites actually DRIVE the session (not the old
    # whole-file read/upload): the chunked calls appear in the mod, and the in-app
    # download opens the File System Access save picker.
    for sym in ("fmFile().uploadBegin(", "fmFile().readChunk(",
                "fmFile().uploadChunk(", "fmFile().uploadCommit(",
                "fmFile().uploadAbort(", "showSaveFilePicker"):
        assert sym in fm, f"file manager missing #108 wiring: {sym!r}"
    # The dead download >5 MiB special-casing is gone from the byte path (the
    # OS-drop whole-file upload keeps its cap, out of scope for #108).
    assert "too large to download" not in fm, \
        "dead download >5 MiB copy remains on a #108 byte path"
    # The mod still routes ALL I/O through the capability — no raw fetch snuck in
    # with the streaming rewrite.
    assert "fileApiPost(" not in fm and "hostHttpUrl(" not in fm


def test_transfer_progress_window_present():
    # #109: cross-host transfer + in-app download show a Win9x-style modal
    # progress window with a byte-accurate bar + a working Cancel. Core adds ONE
    # reusable helper (openProgressDialog) that owns its AbortController; the
    # file-manager threads that handle's byte-progress + signal into the #108
    # chunk loops. Behavior is exercised live (Playwright); these sentinels lock
    # the wiring. No server test is needed — Cancel reuses the #108
    # /file/upload_abort path, whose partial-dest removal + idempotency is covered
    # by tests/test_file_api.py::test_upload_abort_removes_temp_and_is_idempotent.
    dlg = (BROKER_DIR / "69_js_dialog.js").read_text(encoding="utf-8")
    assert "function openProgressDialog" in dlg
    assert "function openProgressDialog" in INDEX_HTML
    # The helper owns the AbortController that Cancel aborts + the loop reads.
    assert "new AbortController" in dlg

    fm = (BROKER_DIR / "mods" / "file-manager" / "file-manager.js").read_text(
        encoding="utf-8")
    # Opened at BOTH call sites — cross-host transfer (doTransfer) + download
    # (downloadRow).
    assert fm.count("openProgressDialog(") >= 2, \
        "openProgressDialog must be wired at both the transfer and download sites"
    # The handle's byte progress + AbortSignal are threaded into transferTo's
    # existing chunk-loop opts; the download drives update/close directly.
    assert "onProgress:" in fm and "signal:" in fm
    assert "progress.update" in fm
    assert "progress.close(" in fm
    # Cancel's server-side partial-dest teardown still rides the #108 abort path.
    assert "fmFile().uploadAbort(" in fm
    # The mod still routes ALL I/O through the capability — no raw fetch snuck in
    # with the progress/cancel wiring.
    assert "fileApiPost(" not in fm and "hostHttpUrl(" not in fm

    css = (BROKER_DIR / "15_css_dialogs.css").read_text(encoding="utf-8")
    assert ".app-dialog-progress" in css
    assert ".app-dialog-progress-fill" in css
    assert ".app-dialog-progress" in INDEX_HTML


def test_filemanager_richer_menu_present():
    # #72: the file manager grows a full right-click menu set + clipboard + drag.
    # These symbol sentinels lock the wiring (the Playwright flow exercises the
    # behavior). The FM routes every confirm/prompt through the styled dialog
    # component — NO native confirm()/prompt() survives in the mod.
    fm = (BROKER_DIR / "mods" / "file-manager" / "file-manager.js").read_text(
        encoding="utf-8")
    for sym in ("const doTransfer", "const buildRowMenu", "const buildEmptyMenu",
                "const setClipboard", "const pasteInto", "const validateName",
                "const newFolder", "const renameRow", "const deleteRow",
                "const downloadRow", "const zipRow", "const unzipRow",
                "const showProperties", "const makeDraggable",
                "win.fmClipboard"):
        assert sym in fm, f"file manager missing #72 symbol: {sym!r}"
    # Uses the styled dialog component, not native modals. Properties moved from
    # the read-only openInfoModal to the editable openDialog primitive (#96), so
    # the mod now calls openDialog directly (openInfoModal stays defined in core).
    for sym in ("openConfirmDialog(", "openTextPrompt(", "openDialog("):
        assert sym in fm, f"file manager should use styled dialog: {sym!r}"
    import re
    assert not re.search(r"(?<![A-Za-z])confirm\(", fm), \
        "native confirm() must be gone from the file manager (use openConfirmDialog)"
    assert not re.search(r"(?<![A-Za-z])prompt\(", fm), \
        "native prompt() must be gone from the file manager (use openTextPrompt)"
    # The drag payload now carries the entry type (cross-host dir refusal).
    assert "type: ent.type" in fm
    # And the menu wiring reaches the served page.
    assert "buildRowMenu" in INDEX_HTML and "buildEmptyMenu" in INDEX_HTML


def test_properties_dialog_editable_present():
    # #96: Properties is editable + platform-aware. The dialog Saves via the
    # capability wrapper (never a raw fetch) and carries both the Windows
    # 'Attributes' block and the POSIX 'Permissions' grid. Lock the sentinels in
    # the mod AND in the served page (a one-sided drift would otherwise slip by).
    fm = (BROKER_DIR / "mods" / "file-manager" / "file-manager.js").read_text(
        encoding="utf-8")
    for sym in ("fmFile().setattr(", "'Attributes'", "'Permissions'"):
        assert sym in fm, f"editable Properties dialog missing {sym!r}"
        assert sym in INDEX_HTML, \
            f"editable Properties sentinel missing from served page: {sym!r}"


def test_file_capability_trust_doc_present():
    # The trust-tier doc ships in-code WITH the capability: ctx.file is operator-
    # granted REVIEW HYGIENE, not enforcement (a same-origin mod can already POST
    # /file/* directly), and there is NO editor_root confinement.
    loader = (BROKER_DIR / "86_js_mod_loader.js").read_text(encoding="utf-8")
    assert "REVIEW HYGIENE" in loader
    assert "permission boundary" in loader
    assert "POST to /file/* directly" in loader
    assert "editor_root confinement" in loader


# --------------------------------------------------------------------------- #
# task-manager mod (#85 / S12)
# --------------------------------------------------------------------------- #

def test_taskmanager_symbols_removed_from_core_fragments():
    # The task manager is now a mod (#85/S12): its built-in registration is gone
    # from core 54 and its launcher from core 76 — both moved into
    # mods/task-manager/. The core fragment 72_js_task_manager.js is DELETED.
    assert not (BROKER_DIR / "72_js_task_manager.js").exists()
    assert "72_js_task_manager.js" not in ui._ORDERED
    gone = {
        "54_js_app_windows_store.js": ("appKind: 'task-manager'",
                                       "return openTaskManagerWindow(d)"),
        "76_js_launch_fullscreen.js": ("function launchTaskManager",),
    }
    for name, symbols in gone.items():
        text = (BROKER_DIR / name).read_text(encoding="utf-8")
        for sym in symbols:
            assert sym not in text, f"{sym!r} should be gone from core fragment {name}"
    # The moved builder + launcher are present + reachable in the served page (they
    # ship in mods/task-manager/ as hoisted functions).
    for sym in ("function openTaskManagerWindow", "function launchTaskManager"):
        assert sym in INDEX_HTML, f"{sym!r} must stay reachable in the served page"


def test_taskmanager_mod_packaged_and_manifest_agrees():
    import json
    mod_dir = BROKER_DIR / "mods" / "task-manager"
    tm_js = mod_dir / "task-manager.js"
    manifest = mod_dir / "mod.json"
    assert tm_js.is_file() and manifest.is_file()
    meta = json.loads(manifest.read_text(encoding="utf-8"))
    assert meta["id"] == "task-manager"
    assert meta["ctxVersion"] == 1
    assert meta["entry"] == "task-manager.js"
    assert "mods/task-manager/task-manager.js" in ui._MODS
    src = tm_js.read_text(encoding="utf-8")
    # Registers the task-manager mod + contributes the task-manager window kind
    # through ctx.registerWindowKind.
    assert "registerMod(" in src
    assert "id: 'task-manager'" in src
    assert "ctxVersion: 1" in src
    assert "ctx.registerWindowKind(" in src
    assert "appKind: 'task-manager'" in src
    assert "return openTaskManagerWindow(d)" in src
    assert "return launchTaskManager()" in src
    # EPHEMERAL: the kind is registered with NO serialize (never persisted), so
    # there is no `serialize:` key in the spec.
    assert "serialize:" not in src, "task-manager is ephemeral — no serialize key"
    # Session RPC (incl. the DESTRUCTIVE kill / session destroy) rides ctx.session
    # (#85): the mod stashes ctx.session and EVERY /session/* call flows through
    # tmSession() carrying the session's own host id — NO raw inline fetch
    # (hostHttpUrl) and NO surviving inline sessionPost in the mod.
    assert "tmSession.cap = ctx.session;" in src
    assert "tmSession().procs(sess.id, { host: sess.hostId })" in src
    assert "tmSession().kill(sess.id, sess.pid, { host: sess.hostId })" in src
    assert "tmSession().kill(sess.id, pid, { host: sess.hostId })" in src
    assert "hostHttpUrl(" not in src, "the raw inline session fetch must be gone"
    assert "sessionPost(" not in src, "the old inline sessionPost must be gone"
    # Teardown closes any live task-manager window WHILE the kind is still
    # registered (so saveAppWindow early-returns — no junk record persists), then
    # drops the cap. The close-on-unload is registered AFTER registerWindowKind so
    # LIFO teardown runs it BEFORE deleteWindowKind.
    assert "closeWindow(w.id)" in src
    assert "tmSession.cap = null;" in src
    # Ships in the served page, AFTER the help mod and BEFORE the file-manager mod
    # (so the (+) menu lists Task manager right after the core built-ins, ahead of
    # the file-manager / editor / sticky mods).
    assert "id: 'task-manager'" in INDEX_HTML
    assert INDEX_HTML.index("id: 'help'") < INDEX_HTML.index("id: 'task-manager'")
    assert INDEX_HTML.index("id: 'task-manager'") < INDEX_HTML.index("id: 'file-manager'")


# --------------------------------------------------------------------------- #
# ctx.session capability (#85 / S12)
# --------------------------------------------------------------------------- #

def test_session_capability_present():
    # #85 (S12): the ctx.session wrapper over /session/procs + the DESTRUCTIVE
    # /session/kill, plus its host-routing helpers, ride in the served loader. The
    # task-manager mod (and the Playwright acceptance) depend on these. ctxVersion
    # stays 1 (additive capability).
    loader = (BROKER_DIR / "86_js_mod_loader.js").read_text(encoding="utf-8")
    # The capability object + its two methods, on the per-mod ctx.
    for sym in ("session: {",
                "procs: function (id, opts)",
                "kill: function (id, pid, opts)"):
        assert sym in loader, f"missing ctx.session method: {sym!r}"
    # Each method targets the matching /session/* route, wrapped here.
    for route in ("'/session/procs'", "'/session/kill'"):
        assert route in loader, f"ctx.session does not wrap route {route!r}"
    # The host-routing helpers: fail-closed resolution + the task-manager's OWN
    # synthetic no_host error (NOT ctx.file's host_not_found), so rendered errors
    # stay byte-identical to the old inline sessionPost.
    for sym in ("function _modSessionHost", "function _modSessionApi",
                "error: 'no_host'"):
        assert sym in loader, f"missing ctx.session host-routing symbol: {sym!r}"
    # Routing reuses the EXISTING core host helpers (no parallel host logic).
    for sym in ("hostById(hostId)", "return localHost();",
                "hostHttpUrl(host, route)"):
        assert sym in loader, f"ctx.session must reuse core host helper: {sym!r}"
    # The {status,json} contract PRESERVES the HTTP status (so a 409 + session_gone
    # stays a 409 — the session-destroy success path) and never rejects.
    assert "{ status: r.status, json: j }" in loader
    # ctxVersion is unchanged — ctx.session is additive.
    assert "ctxVersion: 1" in loader
    # And it all reaches the served page.
    for sym in ("function _modSessionApi", "session: {", "error: 'no_host'"):
        assert sym in INDEX_HTML, f"ctx.session missing from served page: {sym!r}"


def test_session_capability_trust_doc_present():
    # The trust-tier doc ships in-code WITH the capability: ctx.session is operator-
    # granted REVIEW HYGIENE for a HIGH-trust (destructive) RPC, not enforcement (a
    # same-origin mod can already POST /session/* directly). The 409 session_gone
    # destroy-success path is documented in-code.
    loader = (BROKER_DIR / "86_js_mod_loader.js").read_text(encoding="utf-8")
    assert "REVIEW HYGIENE" in loader
    assert "POST to /session/*" in loader
    assert "session_gone" in loader


# --------------------------------------------------------------------------- #
# packaging + enable/permission UI (#86 / S13)
# --------------------------------------------------------------------------- #

# The trust-tier vocabulary the in-repo mods declare. Kept here (not in the
# loader) as the test's source of truth: a typo or a new unreviewed token trips
# this guard. Mirrors the ctx-capability families a mod can use.
_KNOWN_TIERS = {"settings", "taskbar", "file", "session", "window", "storage"}

# What each shipped mod is reviewed to use, derived from its actual `ctx.` usage
# (see each mod's registerMod). Hardcoded like the other drift sentinels so an
# accidental tier change in a mod surfaces here for re-review.
_EXPECTED_TIERS = {
    "theme": ["settings"],
    "pattern": ["settings"],
    "clock": ["taskbar", "settings"],
    "help": ["taskbar"],   # #101: dropped the synced showHelpButton key; chip only
    "task-manager": ["session", "window"],
    "file-manager": ["file", "window"],
    "editor": ["file", "window"],
    "sticky": ["window"],
}


def test_mods_manager_pane_and_enable_api_present():
    # #86 (S13): the per-mod enable state (loader-private localStorage), the
    # persist+apply-live setter, and the "Mods" Control Panel pane all ride in the
    # served loader. The Playwright acceptance (list + toggle + master gate) drives
    # these via window.__mods.__test.
    loader = (BROKER_DIR / "86_js_mod_loader.js").read_text(encoding="utf-8")
    for sym in (
        "'webterm:mods:disabled'",       # the loader-private persistence key
        "function _modsDisabled",
        "function _writeModsDisabled",
        "function isModEnabled",
        "function setModEnabled",
        "function _mountModsManagerPane",
        "window.__mods.masterEnabled",   # master-gate state the live setter honors
        "set-mods-list",                  # the pane's list container class
    ):
        assert sym in loader, f"missing S13 loader symbol: {sym!r}"
    # The pane is built on the S1 pane scaffold (reuse, not a parallel renderer).
    assert "_modRegisterPane(rec, {" in loader
    # The per-mod enable test surface the acceptance drives.
    for sym in ("setEnabled: function", "isEnabled: function",
                "disabledIds: function", "masterEnabled: function"):
        assert sym in loader, f"missing S13 test-API symbol: {sym!r}"
    # And it all reaches the served page (mounts into the existing #set-mods host).
    for sym in ("function setModEnabled", "function _mountModsManagerPane",
                "set-mods-list", 'id="set-mods"'):
        assert sym in INDEX_HTML, f"S13 surface missing from served page: {sym!r}"


def test_mods_manager_pane_styles_present():
    # The pane's core chrome CSS ships in the head <style> (core fragment 15, not a
    # mod stylesheet, so the served page stays free of any mod-css splice).
    css = (BROKER_DIR / "15_css_dialogs.css").read_text(encoding="utf-8")
    for sel in (".set-mods-list", ".set-mod-row", ".set-mod-tier",
                ".set-mod-status"):
        assert sel in css, f"missing S13 pane style: {sel!r}"
    assert ".set-mods-list" in INDEX_HTML


def test_per_mod_enable_is_loader_private_not_state_schema():
    # The per-mod enable is deliberately PER-BROWSER (localStorage), NOT a synced
    # /state settings field: it must not have leaked a new key into the backend
    # /state normalizer (the inherited "no schema change for new keys" rule), and
    # the loader documents the per-browser choice.
    settings = (BROKER_DIR / "55_js_settings_model.js").read_text(encoding="utf-8")
    assert "modsDisabled" not in settings
    assert "webterm:mods:disabled" not in settings
    loader = (BROKER_DIR / "86_js_mod_loader.js").read_text(encoding="utf-8")
    assert "PER-BROWSER" in loader


def test_loadmods_honors_per_mod_disabled_under_master_gate():
    # Boot skips a per-mod-disabled mod, but ONLY after the master gate passes
    # (master off still returns first => every mod off). The pane mounts only when
    # the master gate is on, so master-off means no mod UI at all.
    loader = (BROKER_DIR / "86_js_mod_loader.js").read_text(encoding="utf-8")
    boot = loader[loader.index("async function loadMods"):]
    # master gate returns BEFORE the pane mount + the per-mod skip.
    assert boot.index("mods_enabled=false") < boot.index("_mountModsManagerPane()")
    # #116: the per-mod skip now gates on the resolved enable state (default XOR
    # override) instead of a raw disabled-set lookup, so a default-off mod stays
    # off until opted in. The pane still mounts before the skip.
    assert boot.index("_mountModsManagerPane()") < boot.index("isModEnabled(decl.id)")
    # stale ids are pruned so the set can't grow junk.
    assert "pruned" in boot


def test_mods_declare_reviewed_trust_tiers():
    # Every in-repo mod declares a `tiers:` array in its registerMod, the values
    # are from the known vocabulary, and they match the reviewed expectation for
    # that mod (derived from its actual ctx usage). This is the declared-tier drift
    # guard the "Mods" pane's permission review depends on.
    import re
    for mod_dir in dict.fromkeys(
            PurePosixPath(m).parent.as_posix() for m in ui._MODS):
        mod_id = PurePosixPath(mod_dir).name
        if mod_id not in _EXPECTED_TIERS:
            continue  # codemirror.js shares the editor dir; only registrants count
        # The registerMod-bearing script is the one named <id>.js.
        src = (BROKER_DIR / mod_dir / f"{mod_id}.js").read_text(encoding="utf-8")
        m = re.search(r"id:\s*'%s'.*?tiers:\s*\[([^\]]*)\]" % re.escape(mod_id),
                      src, re.S)
        assert m, f"mod {mod_id!r} must declare a tiers: [...] array in registerMod"
        tokens = re.findall(r"'([a-z-]+)'", m.group(1))
        assert tokens, f"mod {mod_id!r} declared an empty tiers array"
        assert set(tokens) <= _KNOWN_TIERS, (
            f"mod {mod_id!r} declares unknown tier(s): "
            f"{sorted(set(tokens) - _KNOWN_TIERS)}")
        assert tokens == _EXPECTED_TIERS[mod_id], (
            f"mod {mod_id!r} tiers drifted: declared {tokens}, "
            f"reviewed {_EXPECTED_TIERS[mod_id]}")


# --------------------------------------------------------------------------- #
# control-panel secrets are masked text, not native password inputs (#99)
# --------------------------------------------------------------------------- #

def test_control_panel_has_no_native_password_inputs():
    # #99: closing the Control Panel reparents its #settings-modal subtree (with a
    # populated MCP token field, next to text "username" fields) into a hidden
    # overlay, which Chromium reads as a completed login and offers to "Save
    # password?". The root-cause fix removes password-field classification: the two
    # control-panel secrets are now masked type=text inputs, so the password
    # manager can't engage. Lock that no native password input survives in the
    # panel — neither the old markup (regression) nor any future one.
    for old in ('type="password" id="set-mcp-token"',
                'type="password" id="set-host-pass"'):
        assert old not in INDEX_HTML, \
            f"control-panel secret regressed to a native password input: {old!r}"
    # Both secrets ship as masked text inputs (CSS-masked, .value read identically).
    for masked in ('type="text" id="set-mcp-token" class="masked-secret"',
                   'type="text" id="set-host-pass" class="masked-secret"'):
        assert masked in INDEX_HTML, f"masked secret input missing: {masked!r}"
    # The visual mask rides the assembled CSS.
    assert "-webkit-text-security: disc" in INDEX_HTML, \
        "masked-secret CSS mask missing from the served page"
    # The ONLY native password input left anywhere is the separate #auth-form
    # re-auth field (out of scope — a real login form where saving may be wanted).
    import re
    pw_ids = re.findall(r'type="password"\s+id="([^"]+)"', INDEX_HTML)
    assert pw_ids == ["auth-token"], \
        f"unexpected native password input(s) survive: {pw_ids}"
