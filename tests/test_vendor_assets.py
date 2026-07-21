"""Vendored third-party assets are byte-pinned (#143).

xterm used to load from cdn.jsdelivr.net into the origin that holds every
configured host's token. It now ships in the wheel under webterm/broker/vendor/.

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
    assert set(loaded) == set(vendor._ASSETS)
    for name, (body, ctype) in loaded.items():
        assert isinstance(body, bytes) and body, name
        # Content type matters: xterm.js served as text/plain does not execute.
        assert ("javascript" in ctype) == name.endswith(".js"), name
        assert ("css" in ctype) == name.endswith(".css"), name


def test_no_vendored_asset_is_empty_or_html():
    """A CDN error page saved as xterm.js would still hash-mismatch above, but
    this gives the failure an obvious name if the pins are ever regenerated
    from a bad download."""
    for name in VENDORED:
        head = (VENDOR_DIR / name).read_bytes()[:200].lstrip().lower()
        assert not head.startswith(b"<!doctype"), f"{name} looks like HTML"
        assert not head.startswith(b"<html"), f"{name} looks like HTML"
