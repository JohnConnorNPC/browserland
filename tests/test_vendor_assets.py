"""Vendored third-party assets are byte-pinned (#143, #146).

xterm used to load from cdn.jsdelivr.net, and CodeMirror from esm.sh, into the
origin that holds every configured host's token. Both now ship in the wheel
under webterm/broker/vendor/ and there is no third-party origin left.

These hashes are the whole point of vendoring: a CDN could previously change
what it served with no trace in the repo, whereas any change to these bytes now
has to arrive as a commit that also updates this file. Upgrading xterm is
therefore a deliberate, reviewable act:

    1. download the new version
    2. update VENDORED below (version + sha384)
    3. commit both together

The sha384s below were taken from cdn.jsdelivr.net AND independently
cross-checked against unpkg.com at vendoring time -- two independent CDNs
serving identical bytes -- so they attest to authentic published npm content,
not to one CDN's word for it.

These tests are OFFLINE: they hash the files in the wheel. Nothing here fetches
the network, so the suite still passes on a plane.
"""

from __future__ import annotations

import base64
import hashlib
import re
from pathlib import Path

import pytest

from webterm.broker import vendor

VENDOR_DIR = Path(vendor.__file__).resolve().parent / "vendor"

#: filename -> (upstream version, sha384 of the exact published bytes)
VENDORED = {
    "xterm.js": (
        "xterm@5.3.0",
        "sha384-/nfmYPUzWMS6v2atn8hbljz7NE0EI1iGx34lJaNzyVjWGDzMv+ciUZUeJpKA3Glc"),
    "xterm-addon-fit.js": (
        "xterm-addon-fit@0.8.0",
        "sha384-AQLWHRKAgdTxkolJcLOELg4E9rE89CPE2xMy3tIRFn08NcGKPTsELdvKomqji+DL"),
    "xterm-addon-serialize.js": (
        "xterm-addon-serialize@0.11.0",
        "sha384-7roHFPP+/ZPshuYVlciTYFLzsCOAkjdrjVTZwWpc3QrCVZKJWYC38IZpuKbwQLbv"),
    "xterm.css": (
        "xterm@5.3.0",
        "sha384-LJcOxlx9IMbNXDqJ2axpfEQKkAYbFjJfhXexLfiRJhjDU81mzgkiQq8rkV0j6dVh"),
}


def _sri(path: Path) -> str:
    return "sha384-" + base64.b64encode(
        hashlib.sha384(path.read_bytes()).digest()).decode("ascii")


@pytest.mark.parametrize("name", sorted(VENDORED))
def test_vendored_file_matches_its_pinned_hash(name):
    """Any edit to a vendored file -- an accidental reformat, a line-ending
    translation, a supply-chain tamper in a PR -- fails here."""
    path = VENDOR_DIR / name
    assert path.is_file(), f"{name} is missing from webterm/broker/vendor/"
    version, expected = VENDORED[name]
    assert _sri(path) == expected, (
        f"{name} does not match the pinned bytes for {version}. If this is a "
        f"deliberate upgrade, update VENDORED in this file in the same commit; "
        f"if not, the file has been tampered with or mangled (line endings?).")


def test_vendor_allowlist_matches_whats_on_disk():
    """The route serves from vendor._ASSETS, not a directory listing, so the
    two must not drift: a file on disk with no allowlist entry is unreachable
    dead weight, and an entry with no file is a 500 at startup."""
    on_disk = {p.name for p in VENDOR_DIR.iterdir() if p.is_file()}
    assert set(vendor._ASSETS) == on_disk, (
        f"allowlist/disk drift -- only in allowlist: "
        f"{set(vendor._ASSETS) - on_disk}; only on disk: "
        f"{on_disk - set(vendor._ASSETS)}")
    assert set(VENDORED) == on_disk, "this test file has drifted from vendor/"


def test_load_returns_bytes_and_content_types():
    loaded = vendor.load()
    assert set(loaded) == set(vendor._ASSETS) | set(vendor._codemirror_assets())
    for name, (body, ctype) in loaded.items():
        assert isinstance(body, bytes) and body, name
        # Content type matters: xterm.js served as text/plain does not execute,
        # and a .mjs served as anything but JavaScript is a module the browser
        # refuses outright.
        assert ("javascript" in ctype) == name.endswith((".js", ".mjs")), name
        assert ("css" in ctype) == name.endswith(".css"), name


def test_no_vendored_asset_is_empty_or_html():
    """A CDN error page saved as xterm.js would still hash-mismatch above, but
    this gives the failure an obvious name if the pins are ever regenerated
    from a bad download."""
    for name in VENDORED:
        head = (VENDOR_DIR / name).read_bytes()[:200].lstrip().lower()
        assert not head.startswith(b"<!doctype"), f"{name} looks like HTML"
        assert not head.startswith(b"<html"), f"{name} looks like HTML"


# ---------------------------------------------------------------------------
# CodeMirror (#146)
#
# xterm is four files with a hand-maintained hash each. CodeMirror is ~116
# generated modules, so the per-file hashes live in the committed
# codemirror/manifest.json and only the MANIFEST's own hash is pinned here --
# one hand-updated constant per upgrade, same change-control property.
#
# Being accurate about what that buys: like the xterm pins, it is review
# friction, not a security guarantee. Whoever regenerates the files regenerates
# the manifest, and can update the constant below in the same commit. What it
# stops is a silent change -- a file edited in place, a bad partial download, a
# line-ending mangle -- reaching a wheel without a diff that says so.
# ---------------------------------------------------------------------------

CODEMIRROR_DIR = VENDOR_DIR / "codemirror"

#: sha384 of vendor/codemirror/manifest.json. Regenerate with
#: `python -m webterm.broker.vendor_codemirror` and update this in the same
#: commit; the generator's own `--check` prints nothing to copy, so take it
#: from the assertion message below.
CODEMIRROR_MANIFEST = \
    "sha384-yPnqg0O9gF3TAwMf8Tv+6H9+vzBVVnrGiN1MXGzWggtc/SVVM/E9hTUSsC9NmRo4"


def test_codemirror_manifest_matches_its_pin():
    got = _sri(CODEMIRROR_DIR / "manifest.json")
    assert got == CODEMIRROR_MANIFEST, (
        f"vendor/codemirror/manifest.json changed. If this is a deliberate "
        f"re-vendor, set CODEMIRROR_MANIFEST in this file to {got!r} in the "
        f"same commit.")


def test_every_codemirror_module_matches_the_manifest():
    """The per-file equivalent of the xterm pins, driven off the manifest."""
    modules = vendor.codemirror_manifest()["modules"]
    on_disk = {p.name for p in CODEMIRROR_DIR.iterdir()
               if p.is_file()} - {vendor.MANIFEST}
    assert on_disk == set(modules), (
        f"manifest/disk drift -- only in manifest: {sorted(set(modules) - on_disk)}; "
        f"only on disk: {sorted(on_disk - set(modules))}")
    for name, meta in sorted(modules.items()):
        assert _sri(CODEMIRROR_DIR / name) == meta["sha384"], \
            f"{name} does not match the manifest -- re-run the generator"


def test_codemirror_graph_has_one_build_of_each_package():
    """THE regression test for the silent-highlighting bug (#36).

    CodeMirror 6 splits across packages that must share one @codemirror/state,
    view and language instance. A second concrete build of `state` throws and
    drops the editor to a plain textarea; a second `view` or `language` is
    SILENT -- highlighting just stops. esm.sh used to enforce this at request
    time by resolving every range to one build; vendoring moved the guarantee
    here, so it has to be asserted or it quietly stops holding.

    Asserted for EVERY package, not just the three: the first run of the
    generator found autocomplete, lang-html and lang-javascript each loading
    twice, because our exact pins had drifted behind what the transitive `^`
    ranges resolved to.
    """
    from webterm.broker import vendor_codemirror as gen

    manifest = vendor.codemirror_manifest()
    builds = gen.concrete_builds(
        m["url"][len(manifest["origin"]):] for m in manifest["modules"].values())
    duplicated = {p: sorted(v) for p, v in builds.items() if len(v) > 1}
    assert not duplicated, f"packages vendored more than once: {duplicated}"
    for pkg in gen.SHARED_FACET_PACKAGES:
        assert pkg in builds, f"{pkg} is not in the vendored graph at all"


def test_codemirror_graph_is_self_contained_and_consistent():
    """Everything the generator's --check proves, run as part of the suite.

    Covers: every import resolves to a vendored sibling, nothing still points at
    esm.sh, every module is reachable from an entry point, and every CM_VER key
    has the entry module the loader will ask for. All offline -- it reads the
    committed files and fetches nothing, so the suite still passes on a plane.
    """
    from webterm.broker import vendor_codemirror as gen

    assert gen.check(CODEMIRROR_DIR) == []


def test_codemirror_entry_modules_cover_cm_ver():
    """The loader builds its specifier as `'entry-' + k + '.mjs'` from CM_VER's
    keys, so a key with no matching file is a 404 -> textarea fallback."""
    from webterm.broker import vendor_codemirror as gen

    entries = vendor.codemirror_manifest()["entries"]
    assert set(entries) == set(gen.read_cm_ver())
    for key, name in sorted(entries.items()):
        assert name == f"entry-{key}.mjs"
        assert (CODEMIRROR_DIR / name).is_file()


def test_codemirror_files_ship_in_the_wheel():
    """package-data is glob-based, so a file whose extension no pattern covers
    is simply absent from an installed wheel -- and a source-tree test run would
    never notice.

    Read as text rather than parsed: tomllib is 3.11+, and webterm supports 3.9.
    """
    pyproject = (Path(vendor.__file__).resolve().parents[2] / "pyproject.toml"
                 ).read_text(encoding="utf-8")
    patterns = set(re.findall(r'"(vendor/codemirror/\*[^"]*)"', pyproject))
    assert patterns, "pyproject ships nothing from vendor/codemirror/"
    suffixes = {p.rsplit("*", 1)[-1] for p in patterns}
    for path in CODEMIRROR_DIR.iterdir():
        assert path.suffix in suffixes, \
            f"{path.name} matches no package-data pattern {sorted(patterns)}"
