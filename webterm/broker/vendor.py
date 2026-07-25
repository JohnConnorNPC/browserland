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

CodeMirror followed in #146 and lives one level down, in ``vendor/codemirror/``.
It could not be hash-pinned the way xterm is: the text-editor mod imports
``@codemirror/{state,view,language}`` by semver RANGE on purpose (see the
comment in ``mods/editor/codemirror.js``), because CodeMirror 6 splits across
many packages that must share ONE instance of each, and an exact pin that
drifts behind the transitive ranges loads a second instance whose facet
identities no longer match -- silently killing syntax highlighting. Ranges are
mutable, so there was nothing to hash, so ``https://esm.sh`` stayed in
``script-src``. Vendoring resolves the graph once, at generate time, and
asserts the single-instance property there instead
(``webterm/broker/vendor_codemirror.py``, run by hand on upgrade). With it gone
the page has no third-party origin left at all.

Its allowlist is not written out below because it is ~140 machine-generated
filenames; it is read from the committed ``codemirror/manifest.json``. That is
still an explicit allowlist -- a file under review in the repo -- and NOT a
directory listing, so the "a client-supplied name can only hit a known key"
property is unchanged.

Served PUBLIC, like ``GET /`` itself: the browser fetches these before any
token exists (they render the login page), and ``<script src>`` cannot carry an
Authorization header. They are static bytes from the wheel with nothing
host- or session-derived in them.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, Tuple

_DIR = Path(__file__).resolve().parent / "vendor"

JS_CTYPE = "application/javascript; charset=utf-8"

#: name -> content type. An explicit allowlist, NOT a directory listing: the
#: route looks up this dict and never touches a client-supplied path, so there
#: is no traversal surface to get wrong.
_ASSETS: Dict[str, str] = {
    "xterm.js": JS_CTYPE,
    "xterm-addon-fit.js": JS_CTYPE,
    "xterm-addon-serialize.js": JS_CTYPE,
    "xterm.css": "text/css; charset=utf-8",
}

#: Public URL prefix. Mirrored in 00_head.html / 40_body.html.
URL_PREFIX = "/vendor/"

#: Sub-namespace for the generated CodeMirror graph, both on disk and in the
#: URL. Its own route, rather than widening the existing one to ``<path:path>``
#: -- one more literal segment adds no traversal surface.
CODEMIRROR = "codemirror"

CODEMIRROR_PREFIX = URL_PREFIX + CODEMIRROR + "/"

MANIFEST = "manifest.json"


def codemirror_manifest() -> Dict[str, object]:
    """The committed CodeMirror manifest: served allowlist, upstream URLs,
    per-file sha384 and the ``CM_VER key -> entry-<key>.mjs`` map."""
    return json.loads(
        (_DIR / CODEMIRROR / MANIFEST).read_text(encoding="utf-8"))


def _codemirror_assets() -> Dict[str, str]:
    """``codemirror/<file>`` -> content type, from the manifest.

    Every module in the graph is ES module JavaScript; the manifest carries no
    other kind, and a wrong content type here is a module the browser refuses
    to execute.
    """
    return {f"{CODEMIRROR}/{name}": JS_CTYPE
            for name in codemirror_manifest()["modules"]}


def load() -> Dict[str, Tuple[bytes, str]]:
    """Read every vendored asset into memory: name -> (bytes, content_type).

    Eager and non-protective, like ui.assemble(): a missing or unreadable
    vendored file is a broken install, and failing loudly at startup beats a
    broker that boots "healthy" and serves a page whose terminal never
    appears."""
    out: Dict[str, Tuple[bytes, str]] = {}
    for name, ctype in {**_ASSETS, **_codemirror_assets()}.items():
        out[name] = ((_DIR / name).read_bytes(), ctype)
    return out
