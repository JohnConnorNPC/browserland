"""Vendored third-party browser assets, served from the broker itself (#143).

xterm used to load from `cdn.jsdelivr.net`. That put third-party code in the
same origin that holds ``prefs._hosts[].token`` for EVERY configured host --
tokens which gate ``/launch`` and host-wide ``/file/*`` -- so a tampered CDN
response was full compromise of the fleet, not just the box serving the page.
SRI closed that, but only as long as the hashes are right, and it left the app
unusable offline: no network, no terminal at all.

Vendoring removes the origin entirely. There is no CDN to compromise, no hash
to keep in sync with a version bump, and the terminal works on a plane.

Deliberately BYTE-FOR-BYTE the published npm files, not a rebuild: the wheel
ships exactly what jsdelivr served, and ``tests/test_vendor_assets.py`` pins
each sha384 against the values that were cross-checked against BOTH jsdelivr
and unpkg when they were vendored. Upgrading means re-downloading the new
version and updating those hashes in one reviewable commit -- which is the
point, since a CDN bump was previously invisible.

Not vendored: CodeMirror, which the text-editor mod lazy-loads from esm.sh.
Its imports use semver RANGES on purpose (see the comment in
``mods/editor/codemirror.js``) because CodeMirror 6 needs one shared
``@codemirror/state`` instance, and an exact pin that drifts silently kills
syntax highlighting. Vendoring it means resolving and committing a ~50-module
graph -- tracked separately.

Served PUBLIC, like ``GET /`` itself: the browser fetches these before any
token exists (they render the login page), and ``<script src>`` cannot carry an
Authorization header. They are static bytes from the wheel with nothing
host- or session-derived in them.
"""

from __future__ import annotations

from pathlib import Path
from typing import Dict, Tuple

_DIR = Path(__file__).resolve().parent / "vendor"

#: name -> content type. An explicit allowlist, NOT a directory listing: the
#: route looks up this dict and never touches a client-supplied path, so there
#: is no traversal surface to get wrong.
_ASSETS: Dict[str, str] = {
    "xterm.js": "application/javascript; charset=utf-8",
    "xterm-addon-fit.js": "application/javascript; charset=utf-8",
    "xterm-addon-serialize.js": "application/javascript; charset=utf-8",
    "xterm.css": "text/css; charset=utf-8",
}

#: Public URL prefix. Mirrored in 00_head.html / 40_body.html.
URL_PREFIX = "/vendor/"


def load() -> Dict[str, Tuple[bytes, str]]:
    """Read every vendored asset into memory: name -> (bytes, content_type).

    Eager and non-protective, like ui.assemble(): a missing or unreadable
    vendored file is a broken install, and failing loudly at startup beats a
    broker that boots "healthy" and serves a page whose terminal never
    appears."""
    out: Dict[str, Tuple[bytes, str]] = {}
    for name, ctype in _ASSETS.items():
        out[name] = ((_DIR / name).read_bytes(), ctype)
    return out
