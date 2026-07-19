"""Windowed desktop page for the webterm broker.

The UI is assembled at import time from ordered on-disk fragments next to this
module (``*.html``/``*.css``/``*.js``) and exposed as the single ``INDEX_HTML``
string the broker serves at ``GET /``. It used to be one ~16.8k-line
``index.html`` (and before that an inline triple-quoted string); issue #68 split
it into purpose-scoped fragments so the JS/CSS can be edited without scrolling a
14k-line script, *without* adding a build toolchain and *without* changing what
the broker serves.

The served page stays BYTE-IDENTICAL to the old monolith: each fragment is a
contiguous slice that already ends in its own ``\\n``, and they are joined with
the empty string (a ``"\\n".join`` would inject a double newline at every seam).
``read_text`` uses universal-newline translation, so the result is LF-normalized
regardless of CRLF-on-disk -- exactly as the single ``index.html`` read did. The
served bytes are pinned by ``tests/test_ui_assets.py`` (sha-style sentinels) and
``test_broker_e2e.py``. Edits to a fragment need a broker restart to be picked
up -- same as the inline string / monolith did.

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

import json
import logging
from pathlib import Path, PurePosixPath

_DIR = Path(__file__).resolve().parent
_LOG = logging.getLogger(__name__)

# Every fragment (core, mod .js, and now mod .css) rides the same line cap as the
# #68/#71 split guard, so no mod can smuggle a giant script/stylesheet back in.
_MAX_LINES = 2500

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
    "62_js_workspaces.js",
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
    "82_js_settings_keys_hosts.js",
    "83_js_broker_identity.js",
    "84_js_active_view_lifecycle.js",
    "85_js_startup.js",
    # Frontend mod loader (#71): defines registerMod/loadMods/ctx. Ordered after
    # all core JS so a mod's init(ctx) sees the finished desktop, but BEFORE the
    # in-repo mod scripts (which call registerMod) and the boot fragment.
    "86_js_mod_loader.js",
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
    "mods/agent-docs/agent-docs.js",  # #120 Agent-docs 📋 button + AGENTS.md/CLAUDE.md openers via ctx.windows.onTerminalCreate; requires:['editor'] (MUST load after editor.js); reuses the text-editor kind
    "mods/sticky/sticky.js",   # S8 (#81) sticky-note window kind via ctx.registerWindowKind
    "mods/aistatus/aistatus.js",  # #112 AI-provider status chip + window; ships default-off, polls /status/fetch; ships aistatus.css
    "mods/git/git.js",         # S14 (#116) per-terminal git status widget via ctx.windows.onTerminalCreate + ctx.session.git; default-off; ships git.css
    "mods/clipboard/clipboard.js",  # #106 rolling copy/paste history window via ctx.clipboard.observe + ctx.registerWindowKind; default-off (secrets); ephemeral; ships clipboard.css
    "mods/scratchpad/scratchpad.js",  # #124 singleton server-backed notes window (ctx.serverStore + revision ring) via ctx.registerWindowKind; requires:['editor'] (MUST load after editor.js — shares its single CM build); ships scratchpad.css
    "mods/termfont/termfont.js",  # #126 terminal-font Control Panel select (ctx.settings.select) + xterm applicator via ctx.windows.onTerminalCreate (extracted from core; last core appearance setting); default-off
    "mods/recorder/recorder.js",  # #140 session recorder: per-terminal ⏺ capture via ctx.windows.onTerminalCreate + library/player window kinds via ctx.registerWindowKind; broker /recording/* storage; ships recorder.css
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


def assemble(ordered=_ORDERED, mods=_MODS, base: Path = _DIR) -> str:
    """Five-segment empty-string join: core fragments up to the head-css splice
    point, the mod stylesheets, the rest of core up to the mod-js splice point,
    the mod scripts, then the boot fragment + tail. Every piece already ends in
    its own newline, so the empty join preserves byte layout (a ``"\\n".join``
    would inject a double newline at every seam). With no mod declaring a
    ``styles`` file the css segment is empty and the result is byte-identical to
    the #71 three-segment join."""
    css_cut = ordered.index(_MOD_CSS_AFTER) + 1   # splice css AFTER this fragment
    js_cut = ordered.index(_MOD_SPLICE_BEFORE)     # splice js BEFORE the boot frag

    def _join(names):
        return "".join(_read(_name, base) for _name in names)

    return (
        _join(ordered[:css_cut])
        + _join(_mod_css(mods, base))
        + _join(ordered[css_cut:js_cut])
        + _join(mods)
        + _join(ordered[js_cut:])
    )


INDEX_HTML = assemble()
