"""Unit tests for the host-wide file-API path resolver (#35).

``_resolve_host_path`` replaced the ``editor_root`` sandbox: the file tools now
browse the whole host (gated by the same auth as ``/launch``, which already
grants shell-level filesystem access). These cover the resolution rules and the
half-absolute / ADS rejections an adversarial review (codex) flagged."""

import os
import stat
import sys
import zipfile
from pathlib import Path

import pytest

import gzip

import webterm.broker.app as app_mod
from webterm.broker.app import (_NotText, _decode_file_text, _encode_file_text,
                                _looks_binary, _resolve_host_path, create_app)

WIN = sys.platform == "win32"


# --------------------------------------------------------------------------- #
# /file/* handler tests (#72) — driven through the in-process Sanic test client
# --------------------------------------------------------------------------- #
# Each create_app needs a UNIQUE Sanic name (Sanic refuses two same-named apps
# in one process). editor_root points at tmp_path so a relative body path
# resolves into the test's sandbox; state_path keeps the identity/state files
# out of the repo. No token => a loopback request (the test client dials
# 127.0.0.1) passes the gate.
_app_seq = 0


def _make_file_app(tmp_path, monkeypatch, token=None):
    global _app_seq
    _app_seq += 1
    monkeypatch.delenv("WEB_TERMINAL_TOKEN", raising=False)
    cfg = {"editor_root": str(tmp_path),
           "state_path": str(tmp_path / "webterm_state.json")}
    if token:
        cfg["auth_token"] = token
    return create_app(cfg, name=f"webterm-file-test-{_app_seq}")


# --------------------------------------------------------------------------- #
# #97 text-encoding helpers — pure, stdlib-only, unit-tested directly
# --------------------------------------------------------------------------- #

def test_decode_utf8_plain_and_bom():
    assert _decode_file_text(b"h\xc3\xa9llo") == ("héllo", "utf-8")
    # UTF-8 BOM (ef bb bf) -> stripped, labelled utf-8-sig.
    assert _decode_file_text("héllo".encode("utf-8-sig")) == ("héllo", "utf-8-sig")


def test_decode_utf16_le_bom_the_97_repro():
    # The #97 repro: PowerShell `echo . > x.md` writes UTF-16LE + BOM.
    raw = b"\xff\xfe" + "héllo".encode("utf-16-le")
    assert _decode_file_text(raw) == ("héllo", "utf-16-le")


def test_decode_utf16_be_bom():
    raw = b"\xfe\xff" + "héllo".encode("utf-16-be")
    assert _decode_file_text(raw) == ("héllo", "utf-16-be")


def test_decode_cp1252_and_latin1_fallback():
    # 0x97 is the cp1252 em-dash (invalid as UTF-8 -> cp1252 path).
    assert _decode_file_text(b"a\x97b") == ("a—b", "cp1252")
    # 0x90 is UNDEFINED in cp1252 (raises) -> total latin-1 fallback.
    assert _decode_file_text(b"a\x90b") == ("a\x90b", "latin-1")


def test_decode_utf32_bom_rejected():
    with pytest.raises(_NotText):
        _decode_file_text(b"\xff\xfe\x00\x00rest")        # UTF-32 LE BOM
    with pytest.raises(_NotText):
        _decode_file_text(b"\x00\x00\xfe\xffrest")        # UTF-32 BE BOM


def test_decode_malformed_bom_is_not_text_not_500():
    # A declared BOM that doesn't decode (odd-length UTF-16, invalid UTF-8 after
    # the BOM) must map to _NotText -> not_utf8, never an unhandled 500.
    with pytest.raises(_NotText):
        _decode_file_text(b"\xff\xfeA")                   # UTF-16LE BOM, odd tail
    with pytest.raises(_NotText):
        _decode_file_text(b"\xfe\xffA")                   # UTF-16BE BOM, odd tail
    with pytest.raises(_NotText):
        _decode_file_text(b"\xef\xbb\xbf\xff")            # UTF-8 BOM + invalid


def test_decode_bomless_embedded_nul_is_not_text():
    # codex: a BOM-less embedded-NUL file (incl. BOM-less UTF-16) is valid UTF-8
    # but must reject as binary, not open as NUL-riddled garbage text.
    assert _looks_binary(b"A\x00B\x00C\x00") is True
    with pytest.raises(_NotText):
        _decode_file_text(b"A\x00B\x00C\x00")


def test_looks_binary_and_decode_rejects_binary():
    # NUL anywhere -> binary; the full byte range contains NUL.
    assert _looks_binary(bytes(range(256))) is True
    # codex: high-regular NUL (BOM-less UTF-16-looking) must NOT become text.
    assert _looks_binary(b"A\x00B\x00C\x00" + os.urandom(64)) is True
    assert _looks_binary(os.urandom(4096)) is True
    assert _looks_binary(gzip.compress(b"hello world" * 100)) is True
    assert _looks_binary(b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR") is True
    # Plain text isn't binary.
    assert _looks_binary(b"hello\tworld\r\n") is False
    for blob in (bytes(range(256)), b"A\x00B\x00C\x00" + os.urandom(64),
                 os.urandom(4096), gzip.compress(b"x" * 200),
                 b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"):
        with pytest.raises(_NotText):
            _decode_file_text(blob)


@pytest.mark.parametrize("label", ["utf-8", "utf-8-sig", "utf-16-le",
                                   "utf-16-be", "cp1252", "latin-1"])
def test_encode_decode_round_trip(label):
    # Every label round-trips ASCII + an in-range non-ASCII char byte-faithfully.
    s = "abc — déf"                                       # em-dash is in cp1252
    if label == "latin-1":
        s = "abc \x90 déf"                                # cp1252-undefined byte
    assert _decode_file_text(_encode_file_text(s, label)) == (s, label)


def test_encode_legacy_cant_store_emoji_raises():
    with pytest.raises(UnicodeEncodeError):
        _encode_file_text("smile 😀", "cp1252")
    with pytest.raises(UnicodeEncodeError):
        _encode_file_text("smile 😀", "latin-1")


# ---- /file/mkdir ---------------------------------------------------------

def test_mkdir_creates_dir(tmp_path, monkeypatch):
    app = _make_file_app(tmp_path, monkeypatch)
    _, r = app.test_client.post("/file/mkdir", json={"path": "newdir"})
    assert r.status == 200 and r.json["ok"] is True
    assert (tmp_path / "newdir").is_dir()


def test_mkdir_existing_is_conflict(tmp_path, monkeypatch):
    (tmp_path / "d").mkdir()
    app = _make_file_app(tmp_path, monkeypatch)
    _, r = app.test_client.post("/file/mkdir", json={"path": "d"})
    assert r.status == 409 and r.json["error"] == "exists"


def test_mkdir_parent_missing(tmp_path, monkeypatch):
    # os.mkdir, not makedirs: the missing intermediate parents are NOT created.
    app = _make_file_app(tmp_path, monkeypatch)
    _, r = app.test_client.post("/file/mkdir",
                                json={"path": "no/such/parent/leaf"})
    assert r.status == 400 and r.json["error"] == "parent_missing"
    assert not (tmp_path / "no").exists()


def test_mkdir_bad_path(tmp_path, monkeypatch):
    app = _make_file_app(tmp_path, monkeypatch)
    _, r = app.test_client.post("/file/mkdir", json={"path": ""})
    assert r.status == 400 and r.json["error"] == "bad_path"


# ---- cross-cutting route guard (#72 P2-3) --------------------------------

def test_every_file_route_has_options_and_enforces_auth(tmp_path, monkeypatch):
    # Every POST /file/* route must (a) carry a matching OPTIONS preflight — a
    # missing one silently 405s every cross-origin (remote-pane) call — and (b)
    # enforce the token gate (401 when a token is configured and absent).
    # Enumerated from the LIVE router so a future endpoint can't skip the guard.
    app = _make_file_app(tmp_path, monkeypatch, token="sekrit")
    posts, options = set(), set()
    for route in app.router.routes:
        path = "/" + route.path.lstrip("/")
        if not path.startswith("/file/"):
            continue
        methods = set(route.methods or ())
        if "OPTIONS" in methods:
            options.add(path)
        if methods - {"OPTIONS"}:
            posts.add(path)
    assert posts, "no /file/* POST routes discovered"
    missing = posts - options
    assert not missing, f"/file/* routes without an OPTIONS preflight: {sorted(missing)}"
    for path in sorted(posts):
        _, r = app.test_client.post(path, json={"path": "x"})
        assert r.status == 401, f"{path} skipped the auth gate (got {r.status})"
        _, r = app.test_client.options(path)
        assert r.status == 204, f"{path} OPTIONS not 204 (got {r.status})"


def test_empty_resolves_to_default(tmp_path):
    assert _resolve_host_path("", tmp_path) == tmp_path.resolve()


def test_relative_joins_default(tmp_path):
    assert _resolve_host_path("sub/leaf", tmp_path) == \
        (tmp_path / "sub" / "leaf").resolve()


def test_absolute_outside_default_is_allowed(tmp_path):
    # Host-wide: an absolute path OUTSIDE the default dir is NOT rejected (the
    # whole point of #35 — no editor_root containment).
    other = tmp_path.parent
    assert _resolve_host_path(str(other), tmp_path) == other.resolve()


def test_dotdot_escapes_default_dir(tmp_path):
    # `..` is collapsed and escaping the start dir is allowed by design (#35),
    # not a traversal bug to defend against.
    sub = tmp_path / "a" / "b"
    sub.mkdir(parents=True)
    assert _resolve_host_path("../..", sub) == tmp_path.resolve()


def test_resolver_errors_map_to_bad_path(tmp_path, monkeypatch):
    # A resolver blow-up (symlink loop, bad drive) surfaces as ValueError so the
    # endpoint returns a clean bad_path, never a 500.
    import webterm.broker.app as app_mod

    def boom(self, *a, **k):
        raise OSError("symlink loop")

    monkeypatch.setattr(app_mod.Path, "resolve", boom)
    with pytest.raises(ValueError):
        _resolve_host_path("anything", tmp_path)


@pytest.mark.skipif(not WIN, reason="NTFS alternate-data-stream semantics")
def test_windows_ads_rejected(tmp_path):
    # Leaf, mid-component, and bare-relative ADS spellings all rejected...
    for bad in (r"C:\dir\file:ads", r"C:\dir:ads\file", "file:ads"):
        with pytest.raises(ValueError):
            _resolve_host_path(bad, tmp_path)
    # ...but the drive anchor's own colon must NOT false-reject a normal path.
    assert _resolve_host_path(r"C:\dir\file", tmp_path) == \
        Path(r"C:\dir\file").resolve()


@pytest.mark.skipif(not WIN, reason="windows drive/root semantics")
def test_windows_half_absolute_rejected(tmp_path):
    # Drive-relative (C:foo) and rooted-relative (\foo) would jump to a drive
    # root if joined onto default_dir — reject them outright.
    for bad in ("C:foo", "\\foo"):
        with pytest.raises(ValueError):
            _resolve_host_path(bad, tmp_path)


@pytest.mark.skipif(not WIN, reason="windows absolute semantics")
def test_windows_absolute_kept(tmp_path):
    assert _resolve_host_path(r"C:\Windows", tmp_path) == \
        Path(r"C:\Windows").resolve()
    # forward slashes (a browser may send them) normalise to the same path
    assert _resolve_host_path("C:/Windows", tmp_path) == \
        Path(r"C:\Windows").resolve()


@pytest.mark.skipif(WIN, reason="POSIX absolute + legal-colon semantics")
def test_posix_absolute_and_colon_filenames(tmp_path):
    assert _resolve_host_path("/etc", tmp_path) == Path("/etc").resolve()
    # ':' is a legal POSIX filename char — must NOT be rejected as ADS.
    assert _resolve_host_path("weird:name", tmp_path) == \
        (tmp_path / "weird:name").resolve()


# --------------------------------------------------------------------------- #
# link-safe resolver (#72, follow_leaf)
# --------------------------------------------------------------------------- #

def _symlink_or_skip(target: Path, link: Path):
    try:
        os.symlink(str(target), str(link),
                   target_is_directory=target.is_dir())
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"symlinks not supported in this environment: {exc}")


def test_follow_leaf_preserves_symlink_leaf(tmp_path):
    target = tmp_path / "target_dir"
    target.mkdir()
    link = tmp_path / "link"
    _symlink_or_skip(target, link)
    # Default (follow_leaf=True) dereferences the leaf -> the real target.
    assert _resolve_host_path("link", tmp_path) == target.resolve()
    # Link-safe (follow_leaf=False) preserves the link entry itself.
    safe = _resolve_host_path("link", tmp_path, follow_leaf=False)
    assert os.path.islink(str(safe))
    assert safe == (tmp_path.resolve() / "link")


# --------------------------------------------------------------------------- #
# /file/copy (#72)
# --------------------------------------------------------------------------- #

def test_copy_file_ok(tmp_path, monkeypatch):
    (tmp_path / "a.txt").write_text("hello", encoding="utf-8")
    app = _make_file_app(tmp_path, monkeypatch)
    _, r = app.test_client.post("/file/copy",
                                json={"src": "a.txt", "dst": "b.txt"})
    assert r.status == 200 and r.json["ok"] is True
    assert (tmp_path / "b.txt").read_text(encoding="utf-8") == "hello"
    assert (tmp_path / "a.txt").exists()          # source untouched


def test_copy_dir_ok(tmp_path, monkeypatch):
    src = tmp_path / "tree"
    (src / "sub").mkdir(parents=True)
    (src / "sub" / "f.txt").write_text("x", encoding="utf-8")
    app = _make_file_app(tmp_path, monkeypatch)
    _, r = app.test_client.post("/file/copy",
                                json={"src": "tree", "dst": "tree2"})
    assert r.status == 200 and r.json["ok"] is True
    assert (tmp_path / "tree2" / "sub" / "f.txt").read_text(encoding="utf-8") == "x"


def test_copy_exists_without_overwrite_409(tmp_path, monkeypatch):
    (tmp_path / "a.txt").write_text("a", encoding="utf-8")
    (tmp_path / "b.txt").write_text("b", encoding="utf-8")
    app = _make_file_app(tmp_path, monkeypatch)
    _, r = app.test_client.post("/file/copy",
                                json={"src": "a.txt", "dst": "b.txt"})
    assert r.status == 409 and r.json["error"] == "exists"
    assert (tmp_path / "b.txt").read_text(encoding="utf-8") == "b"   # untouched


def test_copy_overwrite_ok(tmp_path, monkeypatch):
    (tmp_path / "a.txt").write_text("a", encoding="utf-8")
    (tmp_path / "b.txt").write_text("b", encoding="utf-8")
    app = _make_file_app(tmp_path, monkeypatch)
    _, r = app.test_client.post(
        "/file/copy", json={"src": "a.txt", "dst": "b.txt", "overwrite": True})
    assert r.status == 200 and r.json["ok"] is True
    assert (tmp_path / "b.txt").read_text(encoding="utf-8") == "a"


def test_copy_dest_in_source_rejected_no_500(tmp_path, monkeypatch):
    # Copying a dir into its own subtree would recurse forever — must be a clean
    # 400, never a 500 / RecursionError + half-built litter.
    (tmp_path / "tree").mkdir()
    app = _make_file_app(tmp_path, monkeypatch)
    _, r = app.test_client.post("/file/copy",
                                json={"src": "tree", "dst": "tree/inner"})
    assert r.status == 400 and r.json["error"] == "dest_in_source"


def test_copy_same_rejected(tmp_path, monkeypatch):
    (tmp_path / "a.txt").write_text("a", encoding="utf-8")
    app = _make_file_app(tmp_path, monkeypatch)
    _, r = app.test_client.post("/file/copy",
                                json={"src": "a.txt", "dst": "a.txt"})
    assert r.status == 400 and r.json["error"] == "same"


def test_copy_type_mismatch(tmp_path, monkeypatch):
    (tmp_path / "a.txt").write_text("a", encoding="utf-8")
    (tmp_path / "d").mkdir()
    app = _make_file_app(tmp_path, monkeypatch)
    _, r = app.test_client.post(
        "/file/copy", json={"src": "a.txt", "dst": "d", "overwrite": True})
    assert r.status == 400 and r.json["error"] == "type_mismatch"


def test_copy_source_missing_404(tmp_path, monkeypatch):
    app = _make_file_app(tmp_path, monkeypatch)
    _, r = app.test_client.post("/file/copy",
                                json={"src": "nope", "dst": "x"})
    assert r.status == 404 and r.json["error"] == "not_found"


# --------------------------------------------------------------------------- #
# /file/move (#72)
# --------------------------------------------------------------------------- #

def test_move_file_ok(tmp_path, monkeypatch):
    (tmp_path / "a.txt").write_text("hi", encoding="utf-8")
    app = _make_file_app(tmp_path, monkeypatch)
    _, r = app.test_client.post("/file/move",
                                json={"src": "a.txt", "dst": "b.txt"})
    assert r.status == 200 and r.json["ok"] is True
    assert not (tmp_path / "a.txt").exists()
    assert (tmp_path / "b.txt").read_text(encoding="utf-8") == "hi"


def test_move_dir_ok(tmp_path, monkeypatch):
    src = tmp_path / "tree"
    (src / "sub").mkdir(parents=True)
    (src / "sub" / "f.txt").write_text("x", encoding="utf-8")
    app = _make_file_app(tmp_path, monkeypatch)
    _, r = app.test_client.post("/file/move",
                                json={"src": "tree", "dst": "tree2"})
    assert r.status == 200 and r.json["ok"] is True
    assert not (tmp_path / "tree").exists()
    assert (tmp_path / "tree2" / "sub" / "f.txt").read_text(encoding="utf-8") == "x"


def test_move_exists_without_overwrite_409(tmp_path, monkeypatch):
    (tmp_path / "a.txt").write_text("a", encoding="utf-8")
    (tmp_path / "b.txt").write_text("b", encoding="utf-8")
    app = _make_file_app(tmp_path, monkeypatch)
    _, r = app.test_client.post("/file/move",
                                json={"src": "a.txt", "dst": "b.txt"})
    assert r.status == 409 and r.json["error"] == "exists"
    assert (tmp_path / "a.txt").exists()          # source untouched


def test_move_overwrite_file_ok(tmp_path, monkeypatch):
    (tmp_path / "a.txt").write_text("a", encoding="utf-8")
    (tmp_path / "b.txt").write_text("b", encoding="utf-8")
    app = _make_file_app(tmp_path, monkeypatch)
    _, r = app.test_client.post(
        "/file/move", json={"src": "a.txt", "dst": "b.txt", "overwrite": True})
    assert r.status == 200 and r.json["ok"] is True
    assert not (tmp_path / "a.txt").exists()
    assert (tmp_path / "b.txt").read_text(encoding="utf-8") == "a"


def test_move_overwrite_dir_ok(tmp_path, monkeypatch):
    # The backup-and-restore path (no atomic dir-over-dir replace on Windows).
    src = tmp_path / "tree"
    src.mkdir()
    (src / "f.txt").write_text("new", encoding="utf-8")
    dst = tmp_path / "dest"
    dst.mkdir()
    (dst / "old.txt").write_text("old", encoding="utf-8")
    app = _make_file_app(tmp_path, monkeypatch)
    _, r = app.test_client.post(
        "/file/move", json={"src": "tree", "dst": "dest", "overwrite": True})
    assert r.status == 200 and r.json["ok"] is True
    assert not (tmp_path / "tree").exists()
    assert (tmp_path / "dest" / "f.txt").read_text(encoding="utf-8") == "new"
    assert not (tmp_path / "dest" / "old.txt").exists()   # replaced, not merged
    # no stray backup left behind
    assert not list(tmp_path.glob("dest.webterm-bak-*"))


def test_move_source_missing_404(tmp_path, monkeypatch):
    app = _make_file_app(tmp_path, monkeypatch)
    _, r = app.test_client.post("/file/move",
                                json={"src": "nope", "dst": "x"})
    assert r.status == 404 and r.json["error"] == "not_found"


def test_move_symlink_to_dir_moves_link_not_target(tmp_path, monkeypatch):
    # The headline data-loss guard for move: relocating a symlink-to-dir moves
    # the LINK entry; the target tree (and its contents) stays put.
    target = tmp_path / "target"
    target.mkdir()
    (target / "keep.txt").write_text("precious", encoding="utf-8")
    link = tmp_path / "link"
    _symlink_or_skip(target, link)
    app = _make_file_app(tmp_path, monkeypatch)
    _, r = app.test_client.post("/file/move",
                                json={"src": "link", "dst": "moved"})
    assert r.status == 200 and r.json["ok"] is True
    moved = tmp_path / "moved"
    assert os.path.islink(str(moved))             # the link itself moved
    assert not os.path.lexists(str(link))         # old link gone
    assert target.is_dir()                        # target tree intact
    assert (target / "keep.txt").read_text(encoding="utf-8") == "precious"


# --------------------------------------------------------------------------- #
# /file/delete (#72 — recursive + reparse-safe)
# --------------------------------------------------------------------------- #

def test_delete_file_ok(tmp_path, monkeypatch):
    (tmp_path / "a.txt").write_text("a", encoding="utf-8")
    app = _make_file_app(tmp_path, monkeypatch)
    _, r = app.test_client.post("/file/delete", json={"path": "a.txt"})
    assert r.status == 200 and r.json["ok"] is True
    assert not (tmp_path / "a.txt").exists()


def test_delete_dir_without_recursive_is_400(tmp_path, monkeypatch):
    d = tmp_path / "d"
    (d / "child.txt").parent.mkdir()
    (d / "child.txt").write_text("x", encoding="utf-8")
    app = _make_file_app(tmp_path, monkeypatch)
    _, r = app.test_client.post("/file/delete", json={"path": "d"})
    assert r.status == 400 and r.json["error"] == "is_a_directory"
    assert d.is_dir()                             # untouched without recursive


def test_delete_dir_recursive_ok(tmp_path, monkeypatch):
    d = tmp_path / "d"
    (d / "sub").mkdir(parents=True)
    (d / "sub" / "f.txt").write_text("x", encoding="utf-8")
    app = _make_file_app(tmp_path, monkeypatch)
    _, r = app.test_client.post("/file/delete",
                                json={"path": "d", "recursive": True})
    assert r.status == 200 and r.json["ok"] is True
    assert not d.exists()


def test_delete_missing_404(tmp_path, monkeypatch):
    app = _make_file_app(tmp_path, monkeypatch)
    _, r = app.test_client.post("/file/delete", json={"path": "nope"})
    assert r.status == 404 and r.json["error"] == "not_found"


def test_delete_symlink_to_dir_unlinks_link_keeps_target(tmp_path, monkeypatch):
    # THE headline data-loss guard: deleting a symlink-to-dir (even recursive)
    # removes only the link; the target tree and its files survive.
    target = tmp_path / "target"
    target.mkdir()
    (target / "keep.txt").write_text("precious", encoding="utf-8")
    link = tmp_path / "link"
    _symlink_or_skip(target, link)
    app = _make_file_app(tmp_path, monkeypatch)
    _, r = app.test_client.post("/file/delete",
                                json={"path": "link", "recursive": True})
    assert r.status == 200 and r.json["ok"] is True
    assert not os.path.lexists(str(link))         # link gone
    assert target.is_dir()                        # target intact
    assert (target / "keep.txt").read_text(encoding="utf-8") == "precious"


def test_delete_symlink_to_file_unlinks_link_keeps_target(tmp_path, monkeypatch):
    target = tmp_path / "real.txt"
    target.write_text("precious", encoding="utf-8")
    link = tmp_path / "link.txt"
    _symlink_or_skip(target, link)
    app = _make_file_app(tmp_path, monkeypatch)
    _, r = app.test_client.post("/file/delete", json={"path": "link.txt"})
    assert r.status == 200 and r.json["ok"] is True
    assert not os.path.lexists(str(link))
    assert target.read_text(encoding="utf-8") == "precious"


# --------------------------------------------------------------------------- #
# /file/zip + /file/unzip (#72)
# --------------------------------------------------------------------------- #

def test_zip_file_then_unzip_roundtrip(tmp_path, monkeypatch):
    (tmp_path / "a.txt").write_text("hello", encoding="utf-8")
    app = _make_file_app(tmp_path, monkeypatch)
    _, r = app.test_client.post("/file/zip",
                                json={"src": "a.txt", "dest": "a.zip"})
    assert r.status == 200 and r.json["ok"] is True
    assert (tmp_path / "a.zip").is_file()
    with zipfile.ZipFile(tmp_path / "a.zip") as zf:
        assert zf.namelist() == ["a.txt"]
    _, r = app.test_client.post("/file/unzip",
                                json={"path": "a.zip", "dest": "out"})
    assert r.status == 200 and r.json["ok"] is True
    assert (tmp_path / "out" / "a.txt").read_text(encoding="utf-8") == "hello"


def test_zip_dir_then_unzip_roundtrip(tmp_path, monkeypatch):
    src = tmp_path / "tree"
    (src / "sub").mkdir(parents=True)
    (src / "sub" / "f.txt").write_text("x", encoding="utf-8")
    (src / "top.txt").write_text("t", encoding="utf-8")
    app = _make_file_app(tmp_path, monkeypatch)
    _, r = app.test_client.post("/file/zip",
                                json={"src": "tree", "dest": "tree.zip"})
    assert r.status == 200 and r.json["ok"] is True
    # the archive carries the top folder name
    with zipfile.ZipFile(tmp_path / "tree.zip") as zf:
        names = set(zf.namelist())
    assert any(n.startswith("tree/") for n in names)
    _, r = app.test_client.post("/file/unzip",
                                json={"path": "tree.zip", "dest": "out"})
    assert r.status == 200 and r.json["ok"] is True
    assert (tmp_path / "out" / "tree" / "sub" / "f.txt").read_text(
        encoding="utf-8") == "x"
    assert (tmp_path / "out" / "tree" / "top.txt").read_text(
        encoding="utf-8") == "t"


def test_zip_dest_exists_409(tmp_path, monkeypatch):
    (tmp_path / "a.txt").write_text("a", encoding="utf-8")
    (tmp_path / "a.zip").write_text("not really a zip", encoding="utf-8")
    app = _make_file_app(tmp_path, monkeypatch)
    _, r = app.test_client.post("/file/zip",
                                json={"src": "a.txt", "dest": "a.zip"})
    assert r.status == 409 and r.json["error"] == "exists"


def test_zip_dest_overwrite_ok(tmp_path, monkeypatch):
    (tmp_path / "a.txt").write_text("a", encoding="utf-8")
    (tmp_path / "a.zip").write_text("stale", encoding="utf-8")
    app = _make_file_app(tmp_path, monkeypatch)
    _, r = app.test_client.post(
        "/file/zip", json={"src": "a.txt", "dest": "a.zip", "overwrite": True})
    assert r.status == 200 and r.json["ok"] is True
    with zipfile.ZipFile(tmp_path / "a.zip") as zf:   # a real archive now
        assert zf.namelist() == ["a.txt"]


def test_zip_too_many_entries_rejected(tmp_path, monkeypatch):
    src = tmp_path / "tree"
    src.mkdir()
    (src / "a.txt").write_text("a", encoding="utf-8")
    (src / "b.txt").write_text("b", encoding="utf-8")
    monkeypatch.setattr(app_mod, "MAX_ARCHIVE_ENTRIES", 1)
    app = _make_file_app(tmp_path, monkeypatch)
    _, r = app.test_client.post("/file/zip",
                                json={"src": "tree", "dest": "out.zip"})
    assert r.status == 400 and r.json["error"] == "too_many_entries"
    assert not (tmp_path / "out.zip").exists()       # no partial archive


def test_zip_dest_in_source_rejected(tmp_path, monkeypatch):
    (tmp_path / "tree").mkdir()
    app = _make_file_app(tmp_path, monkeypatch)
    _, r = app.test_client.post("/file/zip",
                                json={"src": "tree", "dest": "tree/inner.zip"})
    assert r.status == 400 and r.json["error"] == "dest_in_source"


def test_unzip_dest_exists_409(tmp_path, monkeypatch):
    with zipfile.ZipFile(tmp_path / "a.zip", "w") as zf:
        zf.writestr("x.txt", "x")
    (tmp_path / "out").mkdir()
    app = _make_file_app(tmp_path, monkeypatch)
    _, r = app.test_client.post("/file/unzip",
                                json={"path": "a.zip", "dest": "out"})
    assert r.status == 409 and r.json["error"] == "exists"


def test_unzip_bad_zip(tmp_path, monkeypatch):
    (tmp_path / "a.zip").write_text("not a zip at all", encoding="utf-8")
    app = _make_file_app(tmp_path, monkeypatch)
    _, r = app.test_client.post("/file/unzip",
                                json={"path": "a.zip", "dest": "out"})
    assert r.status == 400 and r.json["error"] == "bad_zip"
    assert not (tmp_path / "out").exists()


def test_unzip_zip_bomb_rejected(tmp_path, monkeypatch):
    with zipfile.ZipFile(tmp_path / "big.zip", "w") as zf:
        zf.writestr("a.txt", "x" * 1000)
    monkeypatch.setattr(app_mod, "MAX_ARCHIVE_BYTES", 10)
    app = _make_file_app(tmp_path, monkeypatch)
    _, r = app.test_client.post("/file/unzip",
                                json={"path": "big.zip", "dest": "out"})
    assert r.status == 400 and r.json["error"] == "archive_too_large"
    assert not (tmp_path / "out").exists()           # nothing extracted


def test_unzip_traversal_member_stays_in_dest(tmp_path, monkeypatch):
    # A malicious '../escape.txt' member must be sanitised to land UNDER dest by
    # CPython's extractall — it must NOT escape to dest's parent.
    with zipfile.ZipFile(tmp_path / "evil.zip", "w") as zf:
        zf.writestr("../escape.txt", "pwned")
    app = _make_file_app(tmp_path, monkeypatch)
    _, r = app.test_client.post("/file/unzip",
                                json={"path": "evil.zip", "dest": "out"})
    assert r.status == 200 and r.json["ok"] is True
    assert not (tmp_path / "escape.txt").exists()    # did NOT escape
    assert (tmp_path / "out" / "escape.txt").read_text(
        encoding="utf-8") == "pwned"


# --------------------------------------------------------------------------- #
# /file/stat (#72)
# --------------------------------------------------------------------------- #

def test_stat_file(tmp_path, monkeypatch):
    (tmp_path / "a.txt").write_text("hello", encoding="utf-8")
    app = _make_file_app(tmp_path, monkeypatch)
    _, r = app.test_client.post("/file/stat", json={"path": "a.txt"})
    assert r.status == 200 and r.json["ok"] is True
    assert r.json["type"] == "file"
    assert r.json["size"] == 5
    assert isinstance(r.json["mtime"], (int, float))
    assert isinstance(r.json["mode"], int)
    assert "children" not in r.json


def test_stat_dir_has_child_count(tmp_path, monkeypatch):
    d = tmp_path / "d"
    d.mkdir()
    (d / "a").write_text("a", encoding="utf-8")
    (d / "b").write_text("b", encoding="utf-8")
    (d / "sub").mkdir()
    app = _make_file_app(tmp_path, monkeypatch)
    _, r = app.test_client.post("/file/stat", json={"path": "d"})
    assert r.status == 200 and r.json["ok"] is True
    assert r.json["type"] == "dir"
    assert r.json["children"] == 3


def test_stat_missing_404(tmp_path, monkeypatch):
    app = _make_file_app(tmp_path, monkeypatch)
    _, r = app.test_client.post("/file/stat", json={"path": "nope"})
    assert r.status == 404 and r.json["error"] == "not_found"


def test_stat_reports_os(tmp_path, monkeypatch):
    # #96: the platform discriminator that picks the dialog's editor branch.
    (tmp_path / "a.txt").write_text("hi", encoding="utf-8")
    app = _make_file_app(tmp_path, monkeypatch)
    _, r = app.test_client.post("/file/stat", json={"path": "a.txt"})
    assert r.status == 200 and r.json["ok"] is True
    assert r.json["os"] == ("windows" if WIN else "posix")


@pytest.mark.skipif(not WIN, reason="windows-only attribute breakdown")
def test_stat_windows_attributes(tmp_path, monkeypatch):
    # #96: stat exposes the READONLY/HIDDEN/ARCHIVE booleans the dialog pre-checks.
    (tmp_path / "a.txt").write_text("hi", encoding="utf-8")
    app = _make_file_app(tmp_path, monkeypatch)
    _, r = app.test_client.post("/file/stat", json={"path": "a.txt"})
    assert r.status == 200 and r.json["ok"] is True
    attrs = r.json["attributes"]
    assert set(attrs) == {"readonly", "hidden", "archive"}
    assert all(isinstance(v, bool) for v in attrs.values())


# --------------------------------------------------------------------------- #
# /file/setattr (#96)
# --------------------------------------------------------------------------- #

@pytest.mark.skipif(WIN, reason="POSIX rwx semantics")
def test_setattr_chmod_posix(tmp_path, monkeypatch):
    f = tmp_path / "s.sh"
    f.write_text("#!/bin/sh\n", encoding="utf-8")
    app = _make_file_app(tmp_path, monkeypatch)
    _, r = app.test_client.post("/file/setattr",
                                json={"path": "s.sh", "mode": 0o640})
    assert r.status == 200 and r.json["ok"] is True
    assert stat.S_IMODE(f.stat().st_mode) == 0o640
    # The headline +x case: flip the execute bits on.
    _, r = app.test_client.post("/file/setattr",
                                json={"path": "s.sh", "mode": 0o755})
    assert r.status == 200 and r.json["ok"] is True
    m = f.stat().st_mode
    assert stat.S_IMODE(m) == 0o755
    assert m & stat.S_IXUSR and m & stat.S_IXGRP and m & stat.S_IXOTH


@pytest.mark.skipif(WIN, reason="POSIX special-mode bits")
def test_setattr_posix_preserves_special_bits(tmp_path, monkeypatch):
    # C3: the client sends only the low 9 perm bits; setgid/setuid/sticky are
    # preserved server-side from a live re-stat.
    f = tmp_path / "g.sh"
    f.write_text("x", encoding="utf-8")
    os.chmod(str(f), 0o2755)                       # setgid + rwxr-xr-x
    app = _make_file_app(tmp_path, monkeypatch)
    _, r = app.test_client.post("/file/setattr",
                                json={"path": "g.sh", "mode": 0o644})
    assert r.status == 200 and r.json["ok"] is True
    assert stat.S_IMODE(f.stat().st_mode) == 0o2644   # setgid survived


@pytest.mark.skipif(WIN, reason="POSIX mode validation")
def test_setattr_posix_requires_mode(tmp_path, monkeypatch):
    f = tmp_path / "m.txt"
    f.write_text("x", encoding="utf-8")
    app = _make_file_app(tmp_path, monkeypatch)
    # Missing mode.
    _, r = app.test_client.post("/file/setattr", json={"path": "m.txt"})
    assert r.status == 400 and r.json["error"] == "bad_mode"
    # Non-int mode (would make os.chmod raise TypeError -> 500 if not guarded).
    _, r = app.test_client.post("/file/setattr",
                                json={"path": "m.txt", "mode": "755"})
    assert r.status == 400 and r.json["error"] == "bad_mode"
    # A bool is an int subclass — still rejected.
    _, r = app.test_client.post("/file/setattr",
                                json={"path": "m.txt", "mode": True})
    assert r.status == 400 and r.json["error"] == "bad_mode"


@pytest.mark.skipif(not WIN, reason="windows attribute semantics")
def test_setattr_windows_readonly(tmp_path, monkeypatch):
    f = tmp_path / "w.txt"
    f.write_text("x", encoding="utf-8")
    app = _make_file_app(tmp_path, monkeypatch)
    _, r = app.test_client.post(
        "/file/setattr",
        json={"path": "w.txt", "attributes": {"readonly": True}})
    assert r.status == 200 and r.json["ok"] is True
    assert f.stat().st_file_attributes & stat.FILE_ATTRIBUTE_READONLY
    # Clear it again — also lets the tmp_path teardown delete the file.
    _, r = app.test_client.post(
        "/file/setattr",
        json={"path": "w.txt", "attributes": {"readonly": False}})
    assert r.status == 200 and r.json["ok"] is True
    assert not (f.stat().st_file_attributes & stat.FILE_ATTRIBUTE_READONLY)


@pytest.mark.skipif(not WIN, reason="windows attribute validation")
def test_setattr_windows_requires_attributes(tmp_path, monkeypatch):
    f = tmp_path / "w.txt"
    f.write_text("x", encoding="utf-8")
    app = _make_file_app(tmp_path, monkeypatch)
    _, r = app.test_client.post("/file/setattr", json={"path": "w.txt"})
    assert r.status == 400 and r.json["error"] == "bad_attrs"


def test_setattr_bad_path(tmp_path, monkeypatch):
    app = _make_file_app(tmp_path, monkeypatch)
    _, r = app.test_client.post("/file/setattr", json={"path": ""})
    assert r.status == 400 and r.json["error"] == "bad_path"


def test_setattr_missing_404(tmp_path, monkeypatch):
    app = _make_file_app(tmp_path, monkeypatch)
    body = {"path": "nope", "attributes": {"readonly": True}} if WIN \
        else {"path": "nope", "mode": 0o644}
    _, r = app.test_client.post("/file/setattr", json=body)
    assert r.status == 404 and r.json["error"] == "not_found"
