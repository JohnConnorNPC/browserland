"""Unit tests for the host-wide file-API path resolver (#35).

``_resolve_host_path`` replaced the ``editor_root`` sandbox: the file tools now
browse the whole host (gated by the same auth as ``/launch``, which already
grants shell-level filesystem access). These cover the resolution rules and the
half-absolute / ADS rejections an adversarial review (codex) flagged."""

import sys
from pathlib import Path

import pytest

from webterm.broker.app import _resolve_host_path, create_app

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
