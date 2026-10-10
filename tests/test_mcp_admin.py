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
    "/mcp/knobs": ("POST", {}),
    "/mcp/window": ("POST", {"id": 5}),
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


# ---- A3: admin reach on the existing surface -------------------------------

from webterm.broker.registry import WindowEntry  # noqa: E402


class _Producer:
    """Producer double: an entry holder that records nothing (the A3 cells
    exercise broker-local gates only — the chokepoint's 404 fires before any
    RPC, and /mcp/pace is broker-local)."""

    def __init__(self):
        self.entry = None


def _window(app, wid, scope=None, mcp_mode=None):
    """A live entry tagged ``scope``/``mcp_mode``, injected straight into the
    registry the way tests/test_mcp_scope.py's ``_window`` does."""
    ws = _Producer()
    entry = WindowEntry(wid, 111, "t", 80, 24, ws, kind="agent")
    entry.mcp_scope = scope
    entry.mcp_mode = mcp_mode
    ws.entry = entry
    app.ctx.registry._entries[wid] = entry
    return entry


def test_admin_lists_every_window_including_off_and_foreign_scope(
        tmp_path, monkeypatch):
    """#245: an admin lists every window whatever its scope or mode, each
    with its RAW scope tag and effective mode — including an off window and
    one tagged with a scope the admin did NOT declare. A declared scope on
    the admin call changes nothing (ignored for visibility)."""
    app = _make_app(tmp_path, monkeypatch, admin=ADMIN_TOKEN)
    _window(app, 5, scope="a", mcp_mode="read")
    _window(app, 6, scope="other", mcp_mode=None)      # effective: default off
    _window(app, 7, mcp_mode="readwrite")              # untagged
    for admin_scope in (None, "a"):
        headers = [("Authorization", f"Bearer {MCP_TOKEN}")]
        if admin_scope:
            headers.append(("X-Browserland-Scope", admin_scope))
        headers.append((MCP_ADMIN_HEADER, ADMIN_TOKEN))
        _, resp = app.test_client.get("/mcp/terminals", headers=headers)
        assert resp.status == 200
        rows = {r["id"]: r for r in resp.json}
        assert sorted(rows) == [5, 6, 7]
        assert (rows[5]["scope"], rows[5]["mode"]) == ("a", "read")
        assert (rows[6]["scope"], rows[6]["mode"]) == ("other", "off")
        assert (rows[7]["scope"], rows[7]["mode"]) == (None, "readwrite")


def test_admin_read_off_window_still_404_unknown_or_off(tmp_path,
                                                         monkeypatch):
    """#245: mode gates terminal I/O even for the admin — reading an off
    window is the chokepoint's 404 until the admin sets the window's mode,
    and the bytes are the shared construction a missing id answers."""
    app = _make_app(tmp_path, monkeypatch, admin=ADMIN_TOKEN)
    _window(app, 5, scope="a", mcp_mode="off")
    _, resp = _mcp(app, "/mcp/read", admin=ADMIN_TOKEN)
    assert (resp.status, resp.json["error"]) == (404, "unknown_or_off")
    _, gone = app.test_client.post(
        "/mcp/read", json={"id": 999},
        headers=[("Authorization", f"Bearer {MCP_TOKEN}"),
                 (MCP_ADMIN_HEADER, ADMIN_TOKEN)])
    assert (gone.status, gone.body) == (404, resp.body)


def test_admin_reaches_a_foreign_scope_window(tmp_path, monkeypatch):
    """#245: the chokepoint's scope clause is skipped for an admin — a window
    tagged with a scope the admin never declared is reachable (/mcp/pace is
    broker-local, so this proves the filter, not an RPC), while the same
    window is the usual 404 for a scoped non-admin."""
    app = _make_app(tmp_path, monkeypatch, admin=ADMIN_TOKEN)
    _window(app, 5, scope="theirs", mcp_mode="readwrite")
    _, resp = _mcp(app, "/mcp/pace", admin=ADMIN_TOKEN,
                   extra_headers=[("X-Browserland-Scope", "mine")])
    assert (resp.status, resp.json.get("pace_ms")) == (200, 5)
    _, resp = _mcp(app, "/mcp/pace",
                   extra_headers=[("X-Browserland-Scope", "mine")])
    assert (resp.status, resp.json["error"]) == (404, "unknown_or_off")


def test_non_admin_sees_unchanged_listing_and_404_bytes(tmp_path,
                                                         monkeypatch):
    """#245: a non-admin's world is byte-identical to before — the off
    window is still hidden from the listing, and the admin credential's
    existence changes nothing about the 404s it gets."""
    app = _make_app(tmp_path, monkeypatch, admin=ADMIN_TOKEN)
    _window(app, 5, scope="a", mcp_mode="off")
    _window(app, 6, scope="a", mcp_mode="read")
    _, resp = _mcp(app, "/mcp/terminals",
                   extra_headers=[("X-Browserland-Scope", "a")])
    assert [r["id"] for r in resp.json] == [6]
    _, off = _mcp(app, "/mcp/read", extra_headers=[("X-Browserland-Scope",
                                                    "a")])
    _, foreign = _mcp(app, "/mcp/read",
                      extra_headers=[("X-Browserland-Scope", "zzz")])
    assert off.body == foreign.body
    assert (off.status, off.json["error"]) == (404, "unknown_or_off")


# ---- A4: POST /mcp/knobs ----------------------------------------------------


def test_knobs_admin_sets_default_mode_and_allow_launch(tmp_path, monkeypatch):
    """#245: an admin flips default_mode and allow_launch over the MCP wire;
    the change is live and durable (sidecar), and the answer carries the new
    knob values."""
    app = _make_app(tmp_path, monkeypatch, admin=ADMIN_TOKEN)
    _, resp = _mcp(app, "/mcp/knobs", admin=ADMIN_TOKEN)
    assert (resp.status, resp.json) == (200, {"ok": True,
                                              "default_mode": "off",
                                              "allow_launch": False,
                                              "allow_desktop": False})
    body = MCP_TOKEN_ROUTES["/mcp/knobs"][1]
    _, resp = app.test_client.post(
        "/mcp/knobs", json={"default_mode": "read", "allow_launch": True},
        headers=[("Authorization", f"Bearer {MCP_TOKEN}"),
                 (MCP_ADMIN_HEADER, ADMIN_TOKEN)])
    assert (resp.status, resp.json["default_mode"],
            resp.json["allow_launch"]) == (200, "read", True)
    assert app.ctx.mcp_cfg["default_mode"] == "read"
    assert app.ctx.mcp_cfg["allow_launch"] is True
    sidecar = json.loads(_sidecar(tmp_path).read_text(encoding="utf-8"))
    assert (sidecar["default_mode"], sidecar["allow_launch"]) == ("read",
                                                                  True)


def test_knobs_rejects_token_admin_token_enabled_400_bad_key_no_write(
        tmp_path, monkeypatch):
    """#245: the admin may never change the MCP token, the admin token or
    enabled — rotating a credential or disabling MCP could lock out every
    agent, the admin included — so those keys are 400 bad_key before any
    write."""
    app = _make_app(tmp_path, monkeypatch, admin=ADMIN_TOKEN)
    for keys in ({"token": "x"}, {"admin_token": None},
                 {"admin_generate": True}, {"enabled": False},
                 {"generate": True}):
        _, resp = app.test_client.post(
            "/mcp/knobs", json=keys,
            headers=[("Authorization", f"Bearer {MCP_TOKEN}"),
                     (MCP_ADMIN_HEADER, ADMIN_TOKEN)])
        assert (resp.status, resp.json["error"]) == (400, "bad_key"), keys
    assert not _sidecar(tmp_path).exists()
    assert app.ctx.mcp_cfg["enabled"] is True


def test_knobs_rejects_non_bool_allow_launch_400(tmp_path, monkeypatch):
    """#245: the admin API takes REAL booleans only — the string "false" is
    truthy junk and enabling-when-meant-disabling is not a value to coerce."""
    app = _make_app(tmp_path, monkeypatch, admin=ADMIN_TOKEN)
    _, resp = app.test_client.post(
        "/mcp/knobs", json={"allow_launch": "false"},
        headers=[("Authorization", f"Bearer {MCP_TOKEN}"),
                 (MCP_ADMIN_HEADER, ADMIN_TOKEN)])
    assert (resp.status, resp.json["error"]) == (400, "bad_value")
    assert app.ctx.mcp_cfg["allow_launch"] is False
    assert not _sidecar(tmp_path).exists()


def test_knobs_non_admin_403_admin_required(tmp_path, monkeypatch):
    """#245: no admin header is 403 admin_required (an admin-only op), and a
    wrong one is 403 from the ladder before it; nothing is written either
    way."""
    app = _make_app(tmp_path, monkeypatch, admin=ADMIN_TOKEN)
    _, resp = _mcp(app, "/mcp/knobs")
    assert (resp.status, resp.json["error"]) == (403, "admin_required")
    _, resp = _mcp(app, "/mcp/knobs", admin="wrong-admin-credential")
    assert (resp.status, resp.json["error"]) == (403, "admin_required")
    assert not _sidecar(tmp_path).exists()


def test_knobs_bad_mode_400(tmp_path, monkeypatch):
    """#245: a default_mode outside MCP_MODES is 400 bad_mode before any
    write, mirroring /mcp/config."""
    app = _make_app(tmp_path, monkeypatch, admin=ADMIN_TOKEN)
    _, resp = app.test_client.post(
        "/mcp/knobs", json={"default_mode": "sometimes"},
        headers=[("Authorization", f"Bearer {MCP_TOKEN}"),
                 (MCP_ADMIN_HEADER, ADMIN_TOKEN)])
    assert (resp.status, resp.json["error"]) == (400, "bad_mode")
    assert app.ctx.mcp_cfg["default_mode"] == "off"


def test_knobs_write_serializes_with_config_post(tmp_path, monkeypatch):
    """#245: both routes share the ONE locked writer and copy from the LIVE
    cfg, so a knobs write cannot clobber a value the Control Panel just set
    (and vice versa): each write carries the other's changes forward."""
    app = _make_app(tmp_path, monkeypatch, admin=ADMIN_TOKEN)
    _, resp = authed(app).post("/mcp/config",
                               json={"default_mode": "read"})
    assert resp.status == 200
    _, resp = app.test_client.post(
        "/mcp/knobs", json={"allow_launch": True},
        headers=[("Authorization", f"Bearer {MCP_TOKEN}"),
                 (MCP_ADMIN_HEADER, ADMIN_TOKEN)])
    assert resp.status == 200
    sidecar = json.loads(_sidecar(tmp_path).read_text(encoding="utf-8"))
    assert (sidecar["default_mode"], sidecar["allow_launch"],
            sidecar["enabled"]) == ("read", True, True)


# ---- A5: the admin_set badge ------------------------------------------------

LAUNCHED = (1 << 52) | 77


class _WS:
    """Producer WS double: register() only needs send/close."""

    async def send(self, payload):
        pass

    async def close(self, *a, **k):
        pass


def _hello(wid, pid, host="hostA"):
    return {"type": "hello", "window_id": wid, "pid": pid, "title": "t",
            "cols": 80, "rows": 24, "host": host, "kind": "agent"}


class _FakeLauncher:
    """Stands in for app.ctx.launcher (the test_mcp_scope shape): awaits
    pre_spawn, reads the sidecar where the real one would spawn, registers
    the hello, answers 200."""

    def __init__(self, app, *, pid=4242):
        self.app, self.pid = app, pid
        self.row_at_spawn = None

    async def launch(self, profile, **kwargs):
        pre_spawn = kwargs.get("pre_spawn")
        if pre_spawn is not None:
            await pre_spawn(LAUNCHED)
        path = self.app.ctx.mcp_windows_path
        if path.exists():
            self.row_at_spawn = json.loads(
                path.read_text(encoding="utf-8"))["windows"].get(
                    str(LAUNCHED))
        await self.app.ctx.registry.register(_WS(), _hello(LAUNCHED,
                                                           self.pid))
        return 200, {"ok": True, "id": LAUNCHED, "registered": True,
                     "agent_pid": self.pid}


def _launch(app):
    fake = _FakeLauncher(app)
    app.ctx.launcher = fake
    return fake


def _sessions(app):
    _, resp = authed(app).get("/sessions")
    assert resp.status == 200
    return {row["id"]: row for row in resp.json}


def _launch_call(app, *, admin=_ABSENT_ADMIN, scope=_ABSENT_ADMIN):
    headers = [("Authorization", f"Bearer {MCP_TOKEN}")]
    if admin is not _ABSENT_ADMIN:
        headers.append((MCP_ADMIN_HEADER, admin))
    if scope is not _ABSENT_ADMIN:
        headers.append(("X-Browserland-Scope", scope))
    return app.test_client.post("/mcp/launch", json={}, headers=headers)


def test_admin_launch_marks_row_and_summary_scoped(tmp_path, monkeypatch):
    """#245: an admin-authenticated scoped launch stamps the pre-spawn row
    (admin_set True on disk before the spawn), reapply carries it onto the
    live entry, and /sessions surfaces mcp_admin for the chip badge."""
    app = _make_app(tmp_path, monkeypatch, admin=ADMIN_TOKEN,
                    mcp_allow_launch=True)
    fake = _launch(app)
    _, resp = _launch_call(app, admin=ADMIN_TOKEN, scope="teamA")
    assert resp.status == 200
    assert fake.row_at_spawn["admin_set"] is True
    assert app.ctx.registry.get(LAUNCHED).mcp_admin_launch is True
    assert _sessions(app)[LAUNCHED]["mcp_admin"] is True


def test_non_admin_launch_unmarked(tmp_path, monkeypatch):
    """#245: a plain scoped launch (no admin header) stores admin_set None
    and the entry stays unbadged."""
    app = _make_app(tmp_path, monkeypatch, admin=ADMIN_TOKEN,
                    mcp_allow_launch=True)
    fake = _launch(app)
    _, resp = _launch_call(app, scope="teamA")
    assert resp.status == 200
    assert fake.row_at_spawn["admin_set"] is None
    assert app.ctx.registry.get(LAUNCHED).mcp_admin_launch is False
    assert _sessions(app)[LAUNCHED]["mcp_admin"] is False


def test_session_mcp_edit_clears_admin_set_including_row_deletion_fallback(
        tmp_path, monkeypatch):
    """#245: the browser edit means a human took the window back: the row's
    admin_set is cleared, and when the edit deletes the row outright (both
    fields null) the DIRECT-SET FALLBACK clears the live badge too, so it
    never outlives its row."""
    app = _make_app(tmp_path, monkeypatch, admin=ADMIN_TOKEN,
                    mcp_allow_launch=True)
    _launch(app)
    _launch_call(app, admin=ADMIN_TOKEN, scope="teamA")
    assert app.ctx.registry.get(LAUNCHED).mcp_admin_launch is True

    _, resp = authed(app).post("/session/mcp",
                               json={"id": LAUNCHED, "mode": "read"})
    assert resp.status == 200
    assert app.ctx.mcp_windows.get(LAUNCHED)["admin_set"] is None
    assert app.ctx.registry.get(LAUNCHED).mcp_admin_launch is False

    # stamp again (the durable write reapply mirrors onto the live entry),
    # then clear BOTH fields: the row dies, the fallback runs. The launch
    # minted an owner (#243) which now keeps rows alive, so drop it first —
    # from #243's B3 on, the /session/mcp edit itself clears the owner and
    # this manual step disappears.
    import asyncio

    def _stamp(work):
        work.set(LAUNCHED, admin_set=True, pid=4242, host="hostA",
                 now=work.clock())

    asyncio.run(app.ctx.persist_mcp_windows(_stamp))
    live = app.ctx.registry.get(LAUNCHED)
    app.ctx.mcp_windows.reapply(live)
    assert live.mcp_admin_launch is True

    def _drop_owner(work):
        work.set(LAUNCHED, owner=None, pid=4242, host="hostA",
                 now=work.clock())

    asyncio.run(app.ctx.persist_mcp_windows(_drop_owner))

    _, resp = authed(app).post(
        "/session/mcp", json={"id": LAUNCHED, "mode": None, "scope": None})
    assert resp.status == 200
    assert app.ctx.mcp_windows.get(LAUNCHED) is None
    assert app.ctx.registry.get(LAUNCHED).mcp_admin_launch is False


def test_admin_set_survives_restart_and_reconnect(tmp_path, monkeypatch):
    """#245: the badge is durable — a fresh broker on the same sidecar
    re-applies the row (same producer hello), badge included."""
    app = _make_app(tmp_path, monkeypatch, admin=ADMIN_TOKEN,
                    mcp_allow_launch=True)
    _launch(app)
    _, resp = _launch_call(app, admin=ADMIN_TOKEN, scope="teamA")
    assert resp.status == 200

    restarted = _make_app(tmp_path, monkeypatch, admin=ADMIN_TOKEN)
    import asyncio
    entry = asyncio.run(restarted.ctx.registry.register(
        _WS(), _hello(LAUNCHED, 4242)))
    assert (entry.mcp_scope, entry.mcp_mode) == ("teamA", "readwrite")
    assert entry.mcp_admin_launch is True
    assert _sessions(restarted)[LAUNCHED]["mcp_admin"] is True
