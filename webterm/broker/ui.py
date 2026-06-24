"""Windowed desktop page for the webterm broker.

The actual UI lives in ``index.html`` next to this module (it used to be
an inline ~2400-line triple-quoted string here; extracted so JS/HTML can
be edited without Python string-escaping hazards). This module just reads
it at import time and exposes the same ``INDEX_HTML`` name, so the rest
of the broker is untouched. Edits to index.html need a broker restart to
be picked up — same as the inline string did.

INDEX_HTML is held at module scope (not as a closure) so that Sanic's
``_determine_error_format`` introspection (inspect.getsource + dedent +
ast.parse on the handler) succeeds — a multiline raw HTML string closed
over by the handler breaks that with IndentationError.

See index.html's leading comment-free design notes in README.md; the UI
covers: draggable/resizable windows, taskbar, tiling, per-window colors,
prefs persistence, token login via localStorage, and multi-host
federation (settings host list, per-host polling and status chips).
"""

from pathlib import Path

INDEX_HTML = (Path(__file__).resolve().parent / "index.html").read_text(
    encoding="utf-8")
