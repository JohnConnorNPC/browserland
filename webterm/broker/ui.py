"""Windowed desktop page for the webterm broker.

The UI is assembled at import time from ordered on-disk fragments next to this
module (``*.html``/``*.css``/``*.js``) and exposed as the single ``INDEX_HTML``
string the broker serves at ``GET /``. It used to be one ~16.8k-line
``index.html`` (and before that an inline triple-quoted string); issue #68 split
it into purpose-scoped fragments so the JS/CSS can be edited without scrolling a
14k-line script, *without* adding a build toolchain and *without* changing what
the broker serves.

The served page is the fragment join, modulo ONE substitution: each fragment is
a contiguous slice that already ends in its own ``\\n``, and they are joined with
the empty string (a ``"\\n".join`` would inject a double newline at every seam);
then the single build-stamp ``<meta>`` in ``00_head.html`` (which carries the
``__WEBTERM_BUILD__`` placeholder on disk) becomes the same element carrying the
running build id -- see ``_BUILD_PLACEHOLDER``. That substitution is the only way
the served bytes differ from what is on disk, and
``tests/test_ui_assets.py::test_assembled_equals_segment_join`` states it in
exactly that form. ``read_text`` uses universal-newline translation, so the
result is LF-normalized regardless of CRLF-on-disk -- exactly as the single
``index.html`` read did. The served bytes are pinned by
``tests/test_ui_assets.py`` (sha-style sentinels) and ``test_broker_e2e.py``.
Edits to a fragment need a broker restart to be picked up -- same as the inline
string / monolith did.

``_ORDERED`` is an explicit list (not glob+sort) so assembly order is
deterministic and a stray file (editor ``.bak``, ``Zone.Identifier``, etc.) can
never be swept in; a forgotten fragment is caught by the byte-identity tests.

INDEX_HTML is held at module scope (not as a closure) so that Sanic's
``_determine_error_format`` introspection (inspect.getsource + dedent +
ast.parse on the *handler*) succeeds -- a multiline raw HTML string closed over
by the handler breaks that with IndentationError. The internal assembly here is
irrelevant to that introspection; only the module-scope ``str`` and the handler
shape matter. The handler (``app._index``) now reads the assembled value off
``request.app.ctx.index_html`` -- stashed there by ``create_app`` so a headless
broker can skip this module entirely (#87) -- but still returns via a plain
``html(...)`` call, so the introspection that scans for the response-fn name is
unaffected.

See README.md for the UI overview; it covers draggable/resizable windows,
taskbar, tiling, per-window colors, prefs persistence, token login via
localStorage, and multi-host federation (settings host list, per-host polling
and status chips).
"""

import base64
import hashlib
import json
import logging
import re
from html import escape as _attr_escape
from pathlib import Path, PurePosixPath

from .. import build_version

_DIR = Path(__file__).resolve().parent
_LOG = logging.getLogger(__name__)

# Every fragment (core, mod .js, and now mod .css) rides the same line cap as the
# #68/#71 split guard, so no mod can smuggle a giant script/stylesheet back in.
_MAX_LINES = 2500

# #22: the build stamp. ``00_head.html`` carries _BUILD_META with the
# placeholder as its content, exactly once, and assembly swaps that whole
# element for the same element carrying webterm.build_version(). Why it exists:
# a broker serves the page it assembled at IMPORT, so a `git pull` changes
# nothing until the process restarts, and until now nothing in the served bytes
# said which code was actually running.
#   curl -s <host>/ | grep webterm-build     # vs git rev-parse --short HEAD
# build_version() is cached per process, so the stamp names the RUNNING PROCESS
# rather than the checkout on disk -- which is precisely the question -- and is
# never recomputed per request (assembly happens once). Be honest about the
# limit, though: it reports the revision HEAD pointed at when this process first
# asked, so it cannot see uncommitted edits, and a pull that lands mid-run
# between two imports is not something any single stamp can describe.
#
# The substitution targets the exact ELEMENT, not the bare token: a stray
# `__WEBTERM_BUILD__` in a mod comment or a sticky note then stays literal
# instead of being silently stamped. A missing element is a no-op here (the
# synthetic fragment trees in tests have no head) -- that the real page carries
# exactly one is a test assertion rather than an import-time raise, matching how
# the rest of this module keeps assembly failures out of the boot path.
#
# It lives in a <meta>, NOT in the page's one inline script element, because the
# CSP sha256 in app.py is computed over that element's exact bytes: a per-commit
# string inside it would change the CSP hash on every commit for no reason.
# test_ui_assets.py pins that the hash is stamp-independent. Script that wants
# the value reads the tag:
#     document.querySelector('meta[name="webterm-build"]').content
# The head comment next to the tag stays short on purpose -- it ships to every
# client, and every extra mention of the name is another line the grep returns.
_BUILD_PLACEHOLDER = "__WEBTERM_BUILD__"
_BUILD_META = '<meta name="webterm-build" content="{}">'

# Page order, top to bottom. The numeric filename prefixes mirror this order so
# the directory reads top-to-bottom too, but THIS list is authoritative.
_ORDERED = [
    "00_head.html",
    # CSS (was lines 8-1709 of the monolith), in cascade order
    "10_css_root.css",
    "11_css_apps.css",
    "12_css_help.css",
    "13_css_tiling.css",
    "14_css_dragdrop.css",
    "15_css_dialogs.css",
    # </style> .. body markup .. xterm CDN <script src> .. opening <script>
    "40_body.html",
    # JS (was lines 1990-16833), one classic <script>'s worth of top-level
    # globals -- execution order matters, so this order is load-bearing.
    "50_js_constants.js",
    "51_js_prefs.js",
    "52_js_state_sync.js",
    "53_js_remote_host_cache.js",
    "54_js_app_windows_store.js",
    "55_js_settings_model.js",
    "56_js_hosts.js",
    "57_js_tiling_model.js",
    "58_js_layout_mutators.js",
    "59_js_tiled_drag.js",
    "60_js_strip_engine.js",
    "61_js_resize_gutters.js",
    # 62 was one 899-line fragment holding four unrelated concerns (#148). What
    # is left here is TILING core: the strip scrollbar, the floating scroll-lock,
    # the window lock, the float<->tile layer moves and parkWindow. The workspace
    # feature went to mods/workspaces/ (see _MODS) and the taskbar-ordering pair
    # (spatialKeyOrder/reorderTaskbarItems) into 75, where its only other caller
    # already was.
    "62a_js_strip_and_layers.js",
    "63_js_clipboard_auth.js",
    "64_js_sessions_poll_control.js",
    "65_js_display_theming.js",
    "66_js_notices_zorder.js",
    "67_js_window_lifecycle.js",
    "68_js_app_windows_files.js",
    # Reusable styled dialog primitive (#72, Part A): openDialog + openTextPrompt
    # / openConfirmDialog / openInfoModal, hoisted globals for core + mods.
    "69_js_dialog.js",
    # Reusable single browse-pane component (#93): createBrowsePane -- the host-
    # and I/O-agnostic directory-browser kernel shared by openFileDialog (68)
    # and the file-manager mod, so the editor dialog still browses mods-off.
    "70_js_browse_pane.js",
    # The 69_js_codemirror.js + (old) 70_js_editor_app.js fragments were
    # EXTRACTED to mods/editor/ (#83/S10) -- that 70 was a DIFFERENT, now-deleted
    # file, unrelated to 70_js_browse_pane.js above; 71_js_file_manager.js went
    # to mods/file-manager/ (#84/S11); 72_js_task_manager.js to mods/task-
    # manager/ (#85/S12); the dispatcher openAppWindow moved to 54. See _MODS.
    "73_js_window_runtime.js",
    "74_js_drag_resize.js",
    "75_js_taskbar_hosts.js",
    "76_js_launch_fullscreen.js",
    "77_js_context_menu.js",
    "78_js_keybindings.js",
    "79_js_settings_modal.js",
    "80_js_help_window.js",
    "81_js_control_panel.js",
    # #181: the Control Panel's applet grid -- the applet table + icons, the one
    # visibility arbiter (reconcileControlPanel), the filter and the "show
    # everything" toggle. Split out rather than grown into 81 so neither file
    # approaches _MAX_LINES. Must load BEFORE 86 (the mod loader's
    # _controlSection calls cpAppletFor / cpModBadge) and before 90's loadMods,
    # which is when the first mod section mounts; its own top-level consts are
    # initialized here at eval, well before either runs.
    "81a_js_control_panel_applets.js",
    "82_js_settings_keys_hosts.js",
    "83_js_broker_identity.js",
    "84_js_active_view_lifecycle.js",
    "85_js_startup.js",
    # Frontend mod loader (#71): defines registerMod/loadMods/ctx. Ordered after
    # all core JS so a mod's init(ctx) sees the finished desktop, but BEFORE the
    # in-repo mod scripts (which call registerMod) and the boot fragment.
    "86_js_mod_loader.js",
    # #168: the free-text settings primitive, split out when the loader
    # passed the 2500-line per-fragment cap. Same <script>, same scope,
    # immediately after it -- see the fragment header.
    "86a_js_mod_settings_text.js",
    # #163: runtime-INSTALLED mod packages -- the topological sort, the
    # <script src="/mods/<id>/<gen>/<file>"> loader, the late-registration
    # path and the union status model. Split out for the same 2500-line cap
    # reason 86a was; same <script>, same scope, after it.
    "86b_js_mod_packages.js",
    # #194: NEW per-mod ctx surface. The loader owns ctx v1 and the extender
    # REGISTRY (_ctxExtenders / _registerCtxExtender / _applyCtxExtenders);
    # every family added after it is declared here and registered into that
    # registry, because 86 is at the 2500-line cap and the rule is split, never
    # trim. Extenders run in THIS list's order, so a fragment ordered later sees
    # what an earlier one put on the ctx. Same <script>, same scope, and it must
    # stay BEFORE the mod-script splice: a mod's init reads the finished ctx.
    "86c_js_mod_ctx_ext.js",
    # #194: #78/S5's help-card sanitizer + the ctx.registerHelpCards registry,
    # moved VERBATIM out of 86 to get back under the cap (the same split 86a and
    # 86b are). Declarations only -- makeCtx / setModEnabled / _applyPolicyLive
    # (86) and _lateRegister (86b) call into them at runtime, which the one
    # shared <script> scope makes work in either direction.
    "86d_js_mod_help_cards.js",
    # Single `loadMods();` -- ordered LAST among the JS so every mod has been
    # registered (the mod scripts run between the loader and this).
    "90_js_mod_boot.js",
    # </script> </body> </html>  (trailing newline preserved)
    "99_tail.html",
]

# In-repo mod scripts (#71), concatenated into the one <script> BETWEEN the
# loader (86) and the boot fragment (90). Each calls registerMod({id, ...});
# loadMods() (90) then inits them. Like _ORDERED this is an explicit list (not a
# glob) so a stray file in mods/ can never be swept into the served page, and a
# forgotten mod script trips the drift guard in tests/test_ui_assets.py.
_MODS = [
    "mods/theme/theme.js",     # S2 (#75) color-scheme radio + the six chrome vars
    "mods/pattern/pattern.js", # S3 (#76) background-pattern select (theme-var-aware)
    "mods/clock/clock.js",     # F057 clock, extracted as the reference mod
    "mods/help/help.js",       # S5 (#78) Help window + ? chip + ctx.registerHelpCards; ships help.css
    "mods/task-manager/task-manager.js",  # S12 (#85) live task-manager window kind via ctx.registerWindowKind + ctx.session (ephemeral)
    "mods/file-manager/file-manager.js",  # S11 (#84) dual-pane file-manager window kind via ctx.registerWindowKind + ctx.file
    "mods/editor/codemirror.js",  # S10 (#83) CodeMirror 6 lazy loader (was 69), helpers only
    "mods/editor/editor.js",   # S10 (#83) text-editor window kind via ctx.registerWindowKind + ctx.file
    # (#177 retired mods/agent-docs/ -- the 📋 AGENTS.md/CLAUDE.md openers -- into
    # mods-deprecated/agent-docs/. Dropping the line here is what un-ships it;
    # see webterm/broker/mods-deprecated/README.md to copy it back.)
    "mods/sticky/sticky.js",   # S8 (#81) sticky-note window kind via ctx.registerWindowKind
    "mods/aistatus/aistatus.js",  # #112 AI-provider status chip + window; ships default-off, polls /status/fetch; ships aistatus.css
    "mods/update/update-policy.js",  # #182 Part 2 (atom A4) policy words/helpers companion, starts with RESTART_REASONS/restartReasonWords -- helpers only
    "mods/update/update-apply.js",  # #182 Part 2 (A29/A30) pure apply/post-apply helpers, helpers only -- same split as editor/codemirror.js
    "mods/update/update-widgets.js",  # #188 precondition (atom A2) DOM widget helpers (mkEl/addRow/addNote/addHead/applyOpNotes/APPLY_BUTTONS) split out of update.js to stay under the fragment cap -- helpers only, no registerMod
    "mods/update/update.js",   # #182 is this build current with upstream? taskbar chip + detail window over the broker's GET /update/check; default-off, and the BROKER has its own update_check_enabled gate on top; ships update.css
    "mods/git/git.js",         # S14 (#116) per-terminal git status widget via ctx.windows.onTerminalCreate + ctx.session.git; default-off; ships git.css
    "mods/clipboard/clipboard.js",  # #106 rolling copy/paste history window via ctx.clipboard.observe + ctx.registerWindowKind; default-off (secrets); ephemeral; ships clipboard.css
    "mods/scratchpad/scratchpad.js",  # #124 singleton server-backed notes window (ctx.serverStore + revision ring) via ctx.registerWindowKind; requires:['editor'] (MUST load after editor.js — shares its single CM build); ships scratchpad.css
    "mods/termfont/termfont.js",  # #126 terminal-font Control Panel select (ctx.settings.select) + xterm applicator via ctx.windows.onTerminalCreate (extracted from core; last core appearance setting); default-off
    "mods/recorder/recorder.js",  # #140 session recorder: per-terminal ⏺ capture via ctx.windows.onTerminalCreate + library/player window kinds via ctx.registerWindowKind; broker /recording/* storage; ships recorder.css
    "mods/host-registry/host-registry.js",  # #65 optional shared broker list: publish/pull prefs._hosts via ctx.serverStore (opts.host routing + purgeRevisions); browser-mounted registerSettingsPane; ships host-registry.css
    "mods/mousemode/mousemode.js",  # #155 ambient 🖱 title-bar chip while an app owns the mouse: samples term.modes.mouseTrackingMode on term.onWriteParsed via ctx.windows.onTerminalCreate; default-ON (invisible until tracking is on); ships mousemode.css
    "mods/mod-sync/mod-sync.js",  # #158 push this broker's mod setup to selected peers (their #157 pins via saveModPins + their /state mod settings) / adopt a peer's into this browser; browser-mounted registerSettingsPane; ships mod-sync.css
    "mods/workspaces/workspaces.js",  # #148 vertical workspaces, extracted from the tiling core: ctx.desktop.columnFilter picks which of the ONE desktop's columns the strip draws (nothing is moved, so a disable is non-destructive + reversible) + ctx.registerKeyActions/WindowMenuItems/DesktopMenuItems + ctx.taskbar.onItemsRendered/interceptActivate; default-ON; ships workspaces.css
]

# The fragment the mod scripts are spliced in front of -- loadMods() must run
# after every registerMod() call, so the splice point is the boot fragment.
_MOD_SPLICE_BEFORE = "90_js_mod_boot.js"

# Mod stylesheets (#77/S4). A mod manifest (mod.json) MAY declare `styles`: a
# list of bare ``<file>.css`` filenames in its own dir. ui.py concatenates them
# into the head <style> zone immediately AFTER this core CSS fragment -- i.e.
# BEFORE 40_body.html's closing </style> -- so a CSS-heavy mod (Help/S5) ships a
# real stylesheet instead of inline styles. Routing happens at ASSEMBLY time,
# exactly like the mod .js splice and INDEPENDENT of the mods_enabled RUNTIME
# gate: a disabled mod's CSS is present-but-inert (its selectors match nothing
# until the mod's JS -- which loadMods() gates -- adds its markup/classes), the
# same posture as the spliced-but-not-initialized mod JS. With no manifest
# declaring `styles`, the css segment is empty and the page is byte-identical to
# the #71 join.
_MOD_CSS_AFTER = "15_css_dialogs.css"


def _read(name: str, base: Path = _DIR) -> str:
    # Text mode -> universal-newline translation -> LF-normalized, exactly as
    # the old single-file read. Raises FileNotFoundError naming the missing
    # fragment if one is dropped from the package. `base` is threaded through so
    # assemble() can be driven against a fixture tree in tests.
    return (Path(base) / name).read_text(encoding="utf-8")


def _mod_dirs(mods):
    """Ordered-unique mod directories ('mods/<id>') derived from the _MODS .js
    entries, first-seen order preserved -- so a mod's manifest is read once even
    if it ever ships multiple .js files."""
    seen, out = set(), []
    for rel in mods:
        d = PurePosixPath(rel).parent.as_posix()
        if d not in seen:
            seen.add(d)
            out.append(d)
    return out


def _manifest(mod_dir: str, base: Path = _DIR) -> dict:
    """Parsed mod.json for one mod dir, best-effort: any read/parse problem (or a
    non-object payload) logs a warning and yields ``{}`` so a malformed manifest
    can never crash assembly at import."""
    p = Path(base) / mod_dir / "mod.json"
    try:
        meta = json.loads(p.read_text(encoding="utf-8"))
    except Exception as exc:  # missing / bad JSON / unreadable
        _LOG.warning("mod manifest unreadable (%s): %s", p, exc)
        return {}
    if not isinstance(meta, dict):
        _LOG.warning("mod manifest is not a JSON object (%s)", p)
        return {}
    return meta


def _is_bare_css(name) -> bool:
    """A manifest ``styles`` entry must be a bare ``<file>.css`` filename so it can
    only resolve INSIDE its own mod dir: no path separator ('/' or '\\'), no
    '..'/absolute escape, no nested dir, must end in '.css'. Rejects the
    adversarial set '../x.css', '..\\x.css', '/abs.css', 'nested/x.css', 'x.js',
    '', and non-strings."""
    return (
        isinstance(name, str)
        and name.endswith(".css")
        and "/" not in name
        and "\\" not in name
        and name not in (".", "..")
        and PurePosixPath(name).name == name
    )


def _css_servable(rel: str, base: Path = _DIR) -> bool:
    """True iff the mod css at ``<base>/<rel>`` is safe to splice into the served
    page: it exists, carries no UTF-8 BOM, ends in its own newline (the empty
    join depends on it), is valid UTF-8, and rides the same <=2500-line cap as
    every other fragment. Read as BYTES so the BOM and final-newline checks see
    the file as written (pre universal-newline translation). Any reject logs +
    returns False -- best-effort: the broker still boots, the css is just
    dropped; the strict drift/identity tests fail CI on the same conditions."""
    p = Path(base) / rel
    try:
        raw = p.read_bytes()
    except Exception as exc:
        _LOG.warning("mod css unreadable (%s): %s", p, exc)
        return False
    if raw.startswith(b"\xef\xbb\xbf"):
        _LOG.warning("mod css carries a UTF-8 BOM (%s)", p)
        return False
    if not raw.endswith(b"\n"):
        _LOG.warning("mod css does not end in a newline (%s)", p)
        return False
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        _LOG.warning("mod css is not valid UTF-8 (%s): %s", p, exc)
        return False
    if text.count("\n") > _MAX_LINES:
        _LOG.warning("mod css exceeds %d lines (%s)", _MAX_LINES, p)
        return False
    return True


def _mod_css(mods, base: Path = _DIR):
    """Repo-relative ``mods/<id>/<file>.css`` paths to splice into the head, in
    _MODS order then manifest ``styles`` order, deduped. Best-effort throughout:
    a missing/non-list ``styles``, a non-bare entry, or an unservable file is
    skipped + logged so a packaging mistake degrades to "no css from that mod",
    never an import crash. The strict equivalents (drift + per-file guards) live
    in tests/test_ui_assets.py."""
    out, seen = [], set()
    for mod_dir in _mod_dirs(mods):
        styles = _manifest(mod_dir, base).get("styles", [])
        if not isinstance(styles, list):
            _LOG.warning("mod %s: `styles` must be a list, got %s",
                         mod_dir, type(styles).__name__)
            continue
        for name in styles:
            if not _is_bare_css(name):
                _LOG.warning("mod %s: ignoring non-bare/non-css style %r",
                             mod_dir, name)
                continue
            rel = (PurePosixPath(mod_dir) / name).as_posix()
            if rel in seen:
                continue
            if _css_servable(rel, base):
                seen.add(rel)
                out.append(rel)
    return out


def mod_catalog(mods=_MODS, base: Path = _DIR):
    """The mods this broker SERVES, as plain data for ``GET /mods`` (#157) --
    one entry per mod dir in _MODS order::

        [{"id": "clock", "title": "Clock", "description": "...",
          "version": "1.0.0", "default_enabled": True, "requires": []}, ...]

    Derived from the same ``_MODS`` list + ``mod.json`` manifests that assembly
    itself walks, so the catalog can never advertise a mod the page does not
    carry. Best-effort like ``_mod_css``: a missing/unreadable manifest still
    yields a row (id falling back to the DIRECTORY name, which is the id by
    convention -- `mods/<id>/`), because "this mod is served" is true whether or
    not its manifest parses.

    Every row carries ``"source": "shipped"`` (#163). The catalog gained a
    SECOND source -- runtime-installed mods, from ``modinstall.catalog()`` --
    and provenance has to be a fact on the row rather than something the reader
    infers, because the Control Panel labels it and a peer's rows are
    self-asserted. This function stays SHIPPED-ONLY: it is derived from _MODS,
    so it can never advertise a mod the page does not carry, and app.py
    concatenates the two halves.

    ``default_enabled`` and ``requires`` are the manifest's declarations of what
    the mod's registerMod() call says in JS. They are duplicated deliberately:
    without the default, the remote policy editor's "Default" option cannot say
    whether default MEANS on or off (four shipped mods are default-off), and
    without ``requires`` it cannot show that pinning a mod on also forces its
    dependency on. tests/test_ui_assets.py pins both against the JS so the copy
    cannot drift. ``tiers`` is NOT reported -- it is display-only trust metadata
    with no behaviour behind it, and duplicating fifteen more arrays to show a
    peer's badges is not worth the drift surface."""
    out = []
    for mod_dir in _mod_dirs(mods):
        meta = _manifest(mod_dir, base)
        mid = meta.get("id")
        if not isinstance(mid, str) or not mid:
            mid = PurePosixPath(mod_dir).name

        def _text(key, default=""):
            v = meta.get(key, default)
            return v if isinstance(v, str) else default

        reqs = meta.get("requires", [])
        if not isinstance(reqs, list):
            _LOG.warning("mod %s: `requires` must be a list, got %s",
                         mod_dir, type(reqs).__name__)
            reqs = []
        out.append({"id": mid, "title": _text("title", mid),
                    "description": _text("description"),
                    "version": _text("version"),
                    "source": "shipped",
                    # Absent == the registerMod default (on); only an explicit
                    # `false` ships a mod off, matching the JS's `!== false`.
                    "default_enabled": meta.get("defaultEnabled") is not False,
                    "requires": [r for r in reqs if isinstance(r, str) and r]})
    return out


def stamp_build(page: str, version: str = None) -> str:
    """Substitute the build stamp into an assembled page (#22).

    Separate from the join so the invariant stays sayable in one line: the
    served page is the fragment join with the ONE placeholder-carrying
    ``_BUILD_META`` element replaced by the same element carrying the running
    build id, and nothing else. ``version`` is an injection point for tests (two
    different stamps must produce the same CSP hash); production passes nothing
    and gets ``build_version()``, which is computed ONCE per process and cached
    -- assembly calls it a single time at import, never per request.

    HTML-escaped even though ``git rev-parse --short HEAD`` yields hex: this
    lands in a quoted attribute, and a value that could close the attribute must
    not be able to reach it."""
    stamp = build_version() if version is None else str(version)
    return page.replace(_BUILD_META.format(_BUILD_PLACEHOLDER),
                        _BUILD_META.format(_attr_escape(stamp, quote=True)))


def assemble(ordered=_ORDERED, mods=_MODS, base: Path = _DIR,
             version: str = None) -> str:
    """Five-segment empty-string join, then the build stamp: core fragments up
    to the head-css splice point, the mod stylesheets, the rest of core up to
    the mod-js splice point, the mod scripts, then the boot fragment + tail.
    Every piece already ends in its own newline, so the empty join preserves
    byte layout (a ``"\\n".join`` would inject a double newline at every seam).
    With no mod declaring a ``styles`` file the css segment is empty and the
    result is the #71 three-segment join. ``stamp_build`` then swaps the one
    ``__WEBTERM_BUILD__`` token in the head for the running build id -- the only
    difference between the served page and the bytes on disk."""
    css_cut = ordered.index(_MOD_CSS_AFTER) + 1   # splice css AFTER this fragment
    js_cut = ordered.index(_MOD_SPLICE_BEFORE)     # splice js BEFORE the boot frag

    def _join(names):
        return "".join(_read(_name, base) for _name in names)

    return stamp_build(
        _join(ordered[:css_cut])
        + _join(_mod_css(mods, base))
        + _join(ordered[css_cut:js_cut])
        + _join(mods)
        + _join(ordered[js_cut:]),
        version,
    )


INDEX_HTML = assemble()


def inline_script_hash(html: str = None) -> str:
    """The CSP ``'sha256-…'`` source for the page's ONE inline ``<script>`` (#143).

    Lets ``script-src`` authorize our own bundle WITHOUT ``'unsafe-inline'``, so
    an injected inline script has no way in. Derived from the very string that
    is served, so it can never drift from the bundle: edit a fragment or add a
    mod and the hash follows.

    The hash MUST cover the element's text child exactly -- every byte between
    ``<script>`` and ``</script>``, including the leading newline and the
    trailing indentation. Stripping any of it, or hashing the concatenated JS
    before HTML assembly, yields a policy that blocks the whole application and
    leaves a blank page. That is the one way this bricks the app, so the
    extraction asserts rather than guesses.

    Beware: ``<script`` appears ~9 more times in the bundle, all inside JS
    COMMENTS (fragments discussing "the one concatenated <script>"). Those are
    invisible to the HTML parser -- only ``</script>`` can close the element --
    so the anchor is the exact ``<script>`` open tag (no attributes; the CDN
    tags all carry ``src=``) paired with the first following ``</script>``."""
    html = INDEX_HTML if html is None else html
    blocks = re.findall(r"<script>(.*?)</script>", html, re.S)
    if len(blocks) != 1:
        raise RuntimeError(
            "expected exactly one inline <script> in the assembled page, found "
            f"{len(blocks)}; the CSP hash would authorize the wrong bytes")
    digest = hashlib.sha256(blocks[0].encode("utf-8")).digest()
    return "sha256-" + base64.b64encode(digest).decode("ascii")
