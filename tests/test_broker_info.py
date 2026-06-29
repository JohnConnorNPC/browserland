"""Stable broker identity + /info endpoint (#64).

The same broker reached through several URLs (127.0.0.1 / localhost / Tailscale
100.x) was getting added as multiple browser-local host records, spawning
duplicate taskbar chips and making Terminate silently fail against a stale twin.
``/info`` exposes a stable, non-secret ``broker_id`` so the UI can detect "same
broker, different URL" and gate the terminate fallback. These cover the
persisted-id contract and the token-or-loopback gate.

The route tests use the in-process Sanic test client (each create_app needs a
UNIQUE name — Sanic refuses two apps sharing a name in one process) and point
state_path at a tmp dir so the identity file never lands in the repo.
"""

from __future__ import annotations

import json

import pytest

from webterm.broker.app import _load_or_create_broker_id, create_app

IDENTITY = "webterm_identity.json"


# ---- _load_or_create_broker_id (persisted-id contract) -------------------

def test_broker_id_stable_same_dir(tmp_path):
    p = tmp_path / IDENTITY
    first = _load_or_create_broker_id(p)
    second = _load_or_create_broker_id(p)
    assert isinstance(first, str) and first
    assert first == second                         # immutable across "restarts"
    assert json.loads(p.read_text())["broker_id"] == first   # persisted on disk


def test_broker_id_differs_across_dirs(tmp_path):
    d1, d2 = tmp_path / "one", tmp_path / "two"
    d1.mkdir()
    d2.mkdir()
    assert _load_or_create_broker_id(d1 / IDENTITY) != \
        _load_or_create_broker_id(d2 / IDENTITY)


def test_broker_id_self_heals_corrupt_file(tmp_path):
    # A hand-edited / truncated identity file must re-mint, never break startup.
    p = tmp_path / IDENTITY
    p.write_text("{ not valid json")
    minted = _load_or_create_broker_id(p)
    assert isinstance(minted, str) and minted
    assert json.loads(p.read_text())["broker_id"] == minted   # rewritten clean
    assert _load_or_create_broker_id(p) == minted             # now stable


# ---- /info route gating + CORS -------------------------------------------

_app_seq = 0


def _make_app(tmp_path, monkeypatch, token=None):
    """Build a broker app with state_path in tmp_path (identity file lands there,
    not the repo) and a UNIQUE Sanic name. Env token would override config, so
    clear it and set auth_token explicitly when a token is wanted."""
    global _app_seq
    _app_seq += 1
    monkeypatch.delenv("WEB_TERMINAL_TOKEN", raising=False)
    cfg = {"state_path": str(tmp_path / "webterm_state.json")}
    if token:
        cfg["auth_token"] = token
    return create_app(cfg, name=f"webterm-info-test-{_app_seq}")


def test_info_loopback_ok_no_token(tmp_path, monkeypatch):
    # No token configured: a loopback request (the test client dials 127.0.0.1)
    # passes the gate and gets the broker_id + version.
    app = _make_app(tmp_path, monkeypatch, token=None)
    _, response = app.test_client.get("/info")
    assert response.status == 200
    body = response.json
    assert body["ok"] is True
    assert isinstance(body["broker_id"], str) and body["broker_id"]
    assert body["broker_id"] == app.ctx.broker_id
    assert body["version"] == app.ctx.version


def test_info_reports_mods_enabled_default_true(tmp_path, monkeypatch):
    # #71: the mod-system master switch defaults ON and is surfaced via /info so
    # the frontend loader can gate at runtime (fail-open / default-on).
    app = _make_app(tmp_path, monkeypatch, token=None)
    assert app.ctx.mods_enabled is True
    _, response = app.test_client.get("/info")
    assert response.status == 200
    assert response.json["mods_enabled"] is True


def test_info_reports_mods_enabled_false_when_configured(tmp_path, monkeypatch):
    global _app_seq
    _app_seq += 1
    monkeypatch.delenv("WEB_TERMINAL_TOKEN", raising=False)
    cfg = {"state_path": str(tmp_path / "webterm_state.json"),
           "mods_enabled": False}
    app = create_app(cfg, name=f"webterm-info-test-{_app_seq}")
    assert app.ctx.mods_enabled is False
    _, response = app.test_client.get("/info")
    assert response.status == 200
    assert response.json["mods_enabled"] is False


def test_info_requires_token_when_configured(tmp_path, monkeypatch):
    app = _make_app(tmp_path, monkeypatch, token="sekrit")
    # No / wrong token -> 401.
    _, response = app.test_client.get("/info")
    assert response.status == 401
    _, response = app.test_client.get("/info?token=nope")
    assert response.status == 401
    # Correct token (the path appendHostToken uses) -> 200 with the id.
    _, response = app.test_client.get("/info?token=sekrit")
    assert response.status == 200
    assert response.json["broker_id"] == app.ctx.broker_id


def test_info_options_preflight_cors(tmp_path, monkeypatch):
    # Even with a token configured, the preflight is answered with ACAO:* (the
    # cross-origin add-time probe depends on it).
    app = _make_app(tmp_path, monkeypatch, token="sekrit")
    _, response = app.test_client.options("/info")
    assert response.status == 204
    assert response.headers.get("Access-Control-Allow-Origin") == "*"
    assert response.headers.get("Access-Control-Allow-Methods") == \
        "GET, POST, PUT, OPTIONS"
