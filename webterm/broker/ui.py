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
ast.parse on the *handler*, which just does ``return html(INDEX_HTML)``)
succeeds -- a multiline raw HTML string closed over by the handler breaks that
with IndentationError. The internal assembly here is irrelevant to that
introspection; only the module-scope ``str`` and the handler shape matter.

See README.md for the UI overview; it covers draggable/resizable windows,
taskbar, tiling, per-window colors, prefs persistence, token login via
localStorage, and multi-host federation (settings host list, per-host polling
and status chips).
"""

from pathlib import Path

_DIR = Path(__file__).resolve().parent

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
    "69_js_codemirror.js",
    "70_js_editor_app.js",
    "71_js_file_manager.js",
    "72_js_task_manager.js",
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
]

# The fragment the mod scripts are spliced in front of -- loadMods() must run
# after every registerMod() call, so the splice point is the boot fragment.
_MOD_SPLICE_BEFORE = "90_js_mod_boot.js"


def _read(name: str) -> str:
    # Text mode -> universal-newline translation -> LF-normalized, exactly as
    # the old single-file read. Raises FileNotFoundError naming the missing
    # fragment if one is dropped from the package.
    return (_DIR / name).read_text(encoding="utf-8")


def assemble() -> str:
    """Three-segment empty-string join: core fragments up to the boot splice
    point, then the in-repo mod scripts, then the boot fragment + tail. Every
    piece already ends in its own newline, so the empty join preserves byte
    layout (a ``"\\n".join`` would inject a double newline at every seam)."""
    cut = _ORDERED.index(_MOD_SPLICE_BEFORE)
    pre, post = _ORDERED[:cut], _ORDERED[cut:]
    return (
        "".join(_read(_name) for _name in pre)
        + "".join(_read(_name) for _name in _MODS)
        + "".join(_read(_name) for _name in post)
    )


INDEX_HTML = assemble()
