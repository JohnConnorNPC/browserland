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


# ---- A2: the refusal ladder's admin step ----------------------------------

_ABSENT_ADMIN = object()
MCP_TOKEN_ROUTES = {
    "/mcp/info": ("GET", None),
    "/mcp/terminals": ("GET", None),
    "/mcp/read": ("POST", {"id": 5}),
    "/mcp/input": ("POST", {"id": 5, "data": "x"}),
    "/mcp/reset": ("POST", {"id": 5}),
    "/mcp/flush": ("POST", {"id": 5}),
    "/mcp/pace": ("POST", {"id": 5, "pace_ms": 5}),
    "/mcp/profiles": ("GET", None),
    "/mcp/launch": ("POST", {}),
}
BAD_ADMIN = [("Authorization", f"Bearer {MCP_TOKEN}"),
             (MCP_ADMIN_HEADER, "wrong-admin-credential")]


def _mcp(app, path, *, token=MCP_TOKEN, admin=_ABSENT_ADMIN, params=None,
         extra_headers=()):
    headers = [("Authorization", f"Bearer {token}")]
    if admin is not _ABSENT_ADMIN:
        headers.append((MCP_ADMIN_HEADER, admin))
    headers.extend(extra_headers)
    method, body = MCP_TOKEN_ROUTES[path]
    if method == "GET":
        return app.test_client.get(path, headers=headers, params=params)
    return app.test_client.post(path, json=body, headers=headers, params=params)


def test_ladder_disabled_token_scope_admin_order(tmp_path, monkeypatch):
    """#245: the ladder's order is pinned — a broker with MCP off answers
    mcp_disabled whatever the admin header says; a bad MCP token answers 401
    (the admin credential is ADDITIONAL, never a substitute); a bad scope
    answers 400 before the admin step; only then does a present-but-wrong
    admin header 403."""
    disabled = _make_app(tmp_path, monkeypatch, admin=ADMIN_TOKEN,
                         mcp_enabled=False)
    _, resp = _mcp(disabled, "/mcp/info", admin="wrong-admin-credential")
    assert (resp.status, resp.json["error"]) == (403, "mcp_disabled")

    app = _make_app(tmp_path, monkeypatch, admin=ADMIN_TOKEN)
    _, resp = _mcp(app, "/mcp/info", token="wrong-token",
                   admin="wrong-admin-credential")
    assert (resp.status, resp.json["error"]) == (401, "auth_required")

    _, resp = _mcp(app, "/mcp/info", extra_headers=[
        ("X-Browserland-Scope", "a:b"), (MCP_ADMIN_HEADER, "nope")])
    assert (resp.status, resp.json["error"]) == (400, "bad_scope")

    _, resp = _mcp(app, "/mcp/info", admin="wrong-admin-credential")
    assert (resp.status, resp.json["error"]) == (403, "admin_required")


def test_wrong_admin_header_403_and_nothing_written(tmp_path, monkeypatch):
    """#245: a present-but-wrong admin header is 403 on a plain route, and
    the refusal happens before anything is written: the MCP sidecar is
    byte-identical afterwards."""
    app = _make_app(tmp_path, monkeypatch, admin=ADMIN_TOKEN)
    before = _sidecar(tmp_path).read_bytes() if _sidecar(tmp_path).exists() \
        else None
    for path in ("/mcp/info", "/mcp/terminals", "/mcp/launch"):
        _, resp = _mcp(app, path, admin="wrong-admin-credential")
        assert (resp.status, resp.json["error"]) == (403, "admin_required")
    after = _sidecar(tmp_path).read_bytes() if _sidecar(tmp_path).exists() \
        else None
    assert after == before


def test_absent_header_never_errors_on_plain_routes(tmp_path, monkeypatch):
    """#245: the three states — absent is a plain non-admin caller and every
    ordinary route answers 200 with admin: false; present + matching is
    admin; present + wrong is 403 (pinned above). Configuring the admin
    credential must not break a non-admin client."""
    app = _make_app(tmp_path, monkeypatch, admin=ADMIN_TOKEN)
    _, resp = _mcp(app, "/mcp/info")
    assert resp.status == 200
    assert resp.json["admin"] is False


def test_admin_token_in_query_refused_even_with_correct_header(
        tmp_path, monkeypatch):
    """#245: the admin credential in the URL — as a value or as a KEY — is
    refused with 400 admin_token_in_url even when the header is also
    correct: the rule is about where the secret has BEEN (#191)."""
    app = _make_app(tmp_path, monkeypatch, admin=ADMIN_TOKEN)
    _, resp = _mcp(app, "/mcp/info", admin=ADMIN_TOKEN,
                   params={"admin": ADMIN_TOKEN})
    assert (resp.status, resp.json["error"]) == (400, "admin_token_in_url")
    _, resp = _mcp(app, "/mcp/info", admin=ADMIN_TOKEN,
                   params={ADMIN_TOKEN: "1"})
    assert (resp.status, resp.json["error"]) == (400, "admin_token_in_url")


def test_admin_header_ignored_when_unconfigured(tmp_path, monkeypatch):
    """#245: with no admin token configured the header is ignored entirely —
    the pre-#245 wire, byte for byte — and /mcp/info says admin: false so a
    client fails closed."""
    app = _make_app(tmp_path, monkeypatch)
    _, plain = _mcp(app, "/mcp/info")
    _, with_header = _mcp(app, "/mcp/info", admin="anything-at-all")
    assert with_header.status == 200
    assert with_header.body == plain.body
    assert plain.json["admin"] is False


def test_info_echoes_admin_true_false(tmp_path, monkeypatch):
    """#245: /mcp/info answers admin: true only for a caller that presented
    the configured credential."""
    app = _make_app(tmp_path, monkeypatch, admin=ADMIN_TOKEN)
    _, resp = _mcp(app, "/mcp/info", admin=ADMIN_TOKEN)
    assert resp.json["admin"] is True


def test_mcp_route_ladder_inventory(tmp_path, monkeypatch):
    """#245: derived from the RUNNING router, not a hand list: every /mcp/*
    token route (the browser-realm /mcp/config and OPTIONS preflights
    exempt) refuses an unauthenticated caller 401, a bad scope 400 and a
    present-but-wrong admin header 403 — all from the shared ladder, before
    the route's own checks."""
    app = _make_app(tmp_path, monkeypatch, admin=ADMIN_TOKEN)
    served = sorted(
        ("/" + r.path, method) for r in app.router.routes
        for method in r.methods
        if r.path.startswith("mcp/") and method != "OPTIONS")
    served = [entry for entry in served
              if entry not in (("/mcp/config", "GET"), ("/mcp/config",
                                                        "POST"))]
    assert set(served) == {(path, method) for path, (method, _body)
                           in MCP_TOKEN_ROUTES.items()}
    for path, method in served:
        _, resp = _mcp(app, path, token="wrong")
        assert (resp.status, resp.json["error"]) == (401, "auth_required"), \
            (path, method)
        _, resp = _mcp(app, path, extra_headers=[
            ("X-Browserland-Scope", "a:b"), (MCP_ADMIN_HEADER, "nope")])
        assert (resp.status, resp.json["error"]) == (400, "bad_scope"), \
            (path, method)
        _, resp = _mcp(app, path, admin="wrong-admin-credential")
        assert (resp.status, resp.json["error"]) == (403, "admin_required"), \
            (path, method)


def test_preflight_advertises_admin_header_unconditionally(tmp_path,
                                                            monkeypatch):
    """#245: MCP_ADMIN_HEADER is in Access-Control-Allow-Headers on every
    /mcp/* preflight even with NO admin token configured (the realm is
    runtime-mutable, so a conditional list would stale behind the Max-Age),
    and never on a non-/mcp preflight."""
    app = _make_app(tmp_path, monkeypatch)
    assert app.ctx.mcp_cfg["admin_token"] is None
    _, resp = app.test_client.options(
        "/mcp/terminals",
        headers=[("Origin", "https://elsewhere.example"),
                 ("Access-Control-Request-Method", "GET")])
    assert resp.status == 204
    allowed = [t.strip() for t in
               resp.headers["Access-Control-Allow-Headers"].split(",")]
    assert MCP_ADMIN_HEADER in allowed
    _, resp = app.test_client.options(
        "/sessions",
        headers=[("Origin", "https://elsewhere.example"),
                 ("Access-Control-Request-Method", "GET")])
    assert MCP_ADMIN_HEADER not in \
        resp.headers["Access-Control-Allow-Headers"]


def test_mcp_responses_vary_on_admin_header(tmp_path, monkeypatch):
    """#245: an /mcp/* answer depends on the admin header (the listing, the
    echo, the 403), so it rides Vary beside the scope header and Cookie."""
    app = _make_app(tmp_path, monkeypatch)
    _, resp = _mcp(app, "/mcp/info")
    vary = resp.headers.get("Vary", "")
    assert MCP_ADMIN_HEADER in vary
    assert "X-Browserland-Scope" in vary
