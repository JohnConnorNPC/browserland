"""The MCP ADMIN credential (#245): its sidecar config, env pin, mint/rotate
and clear, and the refusal ladder it adds to ``_mcp_auth_error``.

Sections land per atom (A1: config; A2: ladder; A3: reach; A4: knobs; A5: the
admin_set badge). The app factory mirrors tests/test_mcp_scope.py's ``_make_app``
(unique app names, everything under tmp_path) with one extra knob: ``admin``
seeds ``mcp_admin_token``.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

import webterm.broker.app as app_mod
from webterm.broker.auth import MCP_ADMIN_TOKEN_ENV
from webterm.protocol import MCP_ADMIN_HEADER

from .auth_helpers import TEST_TOKEN, authed

MCP_TOKEN = "scope-store-token"
ADMIN_TOKEN = "admin-token-0123456789abcdef"
_URLSAFE_43 = re.compile(r"[A-Za-z0-9_-]{43}")
_app_seq = 0


def _make_app(tmp_path, monkeypatch, *, admin=None, port=None,
              keep_admin_env=False, **extra):
    """A broker app on the test_mcp_scope template. ``admin`` seeds the
    configured admin token; the env pins are cleared so the cfg values are
    honored — unless ``keep_admin_env`` (the pin tests set the env first and
    need create_app to see it)."""
    global _app_seq
    _app_seq += 1
    monkeypatch.delenv("WEB_TERMINAL_TOKEN", raising=False)
    monkeypatch.delenv("WEB_TERMINAL_MCP_TOKEN", raising=False)
    if not keep_admin_env:
        monkeypatch.delenv(MCP_ADMIN_TOKEN_ENV, raising=False)
    cfg = {
        "state_path": str(tmp_path / "webterm_state.json"),
        "mcp_state_path": str(tmp_path / "webterm_mcp.json"),
        "auth_token": TEST_TOKEN,
        "mcp_enabled": True,
        "mcp_token": MCP_TOKEN,
        "mcp_default_mode": "off",
    }
    if admin is not None:
        cfg["mcp_admin_token"] = admin
    cfg.update(extra)
    app = app_mod.create_app(cfg, name=f"webterm-mcp-admin-{_app_seq}",
                             **({} if port is None else {"port": port}))
    return app


def _sidecar(tmp_path) -> Path:
    return tmp_path / "webterm_mcp.json"


# ---- A1: load, self-heal, env pin -----------------------------------------


def test_admin_token_absent_by_default(tmp_path, monkeypatch):
    """#245: with nothing configured there is no admin credential — off out of
    the box — and the browser view reports the empty string, not the None the
    route would serialize as null."""
    app = _make_app(tmp_path, monkeypatch)
    assert app.ctx.mcp_cfg["admin_token"] is None
    _, resp = authed(app).get("/mcp/config")
    assert resp.status == 200
    assert resp.json["admin_token"] == ""
    assert resp.json["admin_token_env_pinned"] is False


def test_admin_token_load_selfheals_and_env_pin_wins(tmp_path, monkeypatch):
    """#245: the sidecar is the durable source (a valid string loads), a
    hand-edited non-string self-heals to None, and the env pin
    WEB_TERMINAL_MCP_ADMIN_TOKEN wins over the sidecar exactly like the token
    pin does."""
    path = _sidecar(tmp_path)
    path.write_text(json.dumps({"admin_token": "sidecar-secret"}), encoding="utf-8")
    cfg = app_mod._load_mcp_cfg(path, {"mcp_enabled": True})
    assert cfg["admin_token"] == "sidecar-secret"

    path.write_text(json.dumps({"admin_token": 3}), encoding="utf-8")
    cfg = app_mod._load_mcp_cfg(path, {"mcp_enabled": True})
    assert cfg["admin_token"] is None

    path.write_text(json.dumps({"admin_token": "sidecar-secret"}), encoding="utf-8")
    monkeypatch.setenv(MCP_ADMIN_TOKEN_ENV, "pinned-admin-secret")
    cfg = app_mod._load_mcp_cfg(path, {"mcp_enabled": True})
    assert cfg["admin_token"] == "pinned-admin-secret"

    # App level: a configured token rides create_app into the live cfg (the
    # sidecar written above must not shadow it, so it is removed first).
    path.unlink()
    app = _make_app(tmp_path, monkeypatch, admin=ADMIN_TOKEN)
    assert app.ctx.mcp_cfg["admin_token"] == ADMIN_TOKEN


def test_config_generate_and_clear_admin_token(tmp_path, monkeypatch):
    """#245: admin_generate mints a token_urlsafe(32) secret (never a
    client-chosen value), it lands in the sidecar, admin_token null clears it,
    and a second generate rotates to a fresh value."""
    app = _make_app(tmp_path, monkeypatch)

    _, resp = authed(app).post("/mcp/config", json={"admin_generate": True})
    assert resp.status == 200
    minted = resp.json["admin_token"]
    assert _URLSAFE_43.fullmatch(minted), minted
    assert json.loads(_sidecar(tmp_path).read_text(encoding="utf-8"))[
        "admin_token"] == minted

    _, resp = authed(app).post("/mcp/config", json={"admin_generate": True})
    assert resp.json["admin_token"] != minted      # rotation mints fresh

    _, resp = authed(app).post("/mcp/config", json={"admin_token": None})
    assert resp.status == 200
    assert resp.json["admin_token"] == ""
    assert app.ctx.mcp_cfg["admin_token"] is None
    assert json.loads(_sidecar(tmp_path).read_text(encoding="utf-8"))[
        "admin_token"] is None


def test_admin_env_pinned_ignores_ui_mint_and_writes_none(tmp_path,
                                                          monkeypatch):
    """#245: while the env pins the admin token the Control Panel can neither
    mint nor clear it (the live value stays the env one) and the sidecar holds
    None, keeping the env secret off disk — the token rule, mirrored."""
    monkeypatch.setenv(MCP_ADMIN_TOKEN_ENV, "pinned-admin-secret")
    app = _make_app(tmp_path, monkeypatch, admin="ignored-config-seed",
                    keep_admin_env=True)
    assert app.ctx.mcp_cfg["admin_token"] == "pinned-admin-secret"
    _, resp = authed(app).get("/mcp/config")
    assert resp.json["admin_token_env_pinned"] is True

    _, resp = authed(app).post("/mcp/config", json={"admin_generate": True})
    assert resp.status == 200
    assert resp.json["admin_token"] == "pinned-admin-secret"
    _, resp = authed(app).post("/mcp/config", json={"admin_token": None})
    assert resp.json["admin_token"] == "pinned-admin-secret"
    assert app.ctx.mcp_cfg["admin_token"] == "pinned-admin-secret"
    assert json.loads(_sidecar(tmp_path).read_text(encoding="utf-8"))[
        "admin_token"] is None


def test_config_rejects_non_string_admin_token(tmp_path, monkeypatch):
    """#245: an admin_token that is neither null nor a string is a 400 before
    any write, mirroring the token rule (a client may CLEAR the secret but
    never SET it to a chosen value)."""
    app = _make_app(tmp_path, monkeypatch)
    _, resp = authed(app).post("/mcp/config", json={"admin_token": 3})
    assert (resp.status, resp.json["error"]) == (400, "bad_token")
    assert not _sidecar(tmp_path).exists()
