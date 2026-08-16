"""The ADMIN CREDENTIAL CLASS on the broker's most dangerous writes (#191).

The page token is a STANDING capability: every page and every running mod holds
it, so with it alone gating /mods/install, one installed mod can install the
next. When ``admin_token`` is configured, the five ADMIN_ROUTES additionally
require the ``X-Webterm-Admin`` header; when it is absent, the wire must be
byte-identical to a build without the class, plus exactly one loud boot line.

What this file pins, and why each pin exists:

* **The ladder order.** 401 ``auth_required`` (the existing page realm) outranks
  403 ``admin_required`` outranks every route-own gate — a caller with no page
  token learns nothing about the admin class, and a refusal writes nothing.
* **Header-only transport.** The admin token in a query string is refused
  outright (400), even when the header is ALSO correct: a token in a URL is a
  credential in every access log downstream, and accepting it once teaches a
  client the transport works.
* **Boot refuses a collapsible credential.** Empty, short, or page-token-equal
  admin_token means the operator believes the routes are gated while they are
  not (or the two classes are one) — failing to start is the only honest answer.
* **Redaction.** The credential value never appears in any log record at any
  level, on refusal or success, at boot or per-request.
* **Byte-identity when unconfigured.** No config key -> no behavior change
  anywhere, pinned as literal response bytes on /mods/policy.
"""

from __future__ import annotations

import json
import logging

import pytest

import webterm.broker.app as app_mod
from .auth_helpers import TEST_TOKEN, authed
from webterm.broker.app import (ADMIN_HEADER, ADMIN_ROUTES,
                                ADMIN_TOKEN_MIN_CHARS, create_app)

#: >= ADMIN_TOKEN_MIN_CHARS, distinct from TEST_TOKEN, and DISTINCTIVE — the
#: redaction scan greps captured logs for this exact value.
ADMIN = "admin-secret-0123456789abcdef"
AHDR = {ADMIN_HEADER: ADMIN}
SET_GIT = {"set": {"git": True}}

_app_seq = 0


def _make_app(tmp_path, monkeypatch, **cfg):
    global _app_seq
    _app_seq += 1
    monkeypatch.delenv("WEB_TERMINAL_TOKEN", raising=False)
    conf = {"state_path": str(tmp_path / "webterm_state.json"),
            "auth_token": TEST_TOKEN,
            "mods_dir": str(tmp_path / "mods")}
    conf.update(cfg)
    return create_app(conf, name=f"webterm-admin-gate-{_app_seq}")


def _admin_app(tmp_path, monkeypatch, **cfg):
    return _make_app(tmp_path, monkeypatch, admin_token=ADMIN, **cfg)


def _install_payload(mod_id="x-notes"):
    return {"manifest": {"id": mod_id, "version": "1.0.0", "ctxVersion": 1,
                         "title": "Notes", "description": "a note pad",
                         "scripts": [f"{mod_id}.js"]},
            "files": {f"{mod_id}.js": f"registerMod({{ id: '{mod_id}' }});\n"}}


# ---- boot-time validation ---------------------------------------------------

@pytest.mark.parametrize("bad, fragment", [
    ("", "empty"),
    ("a" * (ADMIN_TOKEN_MIN_CHARS - 1), "too short"),
    (None, "must be a string"),
    (1234, "must be a string"),
])
def test_boot_refuses_an_unusable_admin_token(tmp_path, monkeypatch, bad,
                                              fragment):
    with pytest.raises(SystemExit) as exc:
        _make_app(tmp_path, monkeypatch, admin_token=bad)
    assert fragment in str(exc.value)
    assert "admin_token" in str(exc.value)


def test_boot_refuses_an_admin_token_equal_to_the_page_token(tmp_path,
                                                             monkeypatch):
    # A page token long enough that the length check cannot mask the equality
    # check — equal values collapse the two credential classes into one.
    page = "page-token-0123456789abcdef"
    with pytest.raises(SystemExit) as exc:
        _make_app(tmp_path, monkeypatch, auth_token=page, admin_token=page)
    assert "auth_token" in str(exc.value)


def test_boot_accepts_the_minimum_length_exactly(tmp_path, monkeypatch):
    app = _make_app(tmp_path, monkeypatch,
                    admin_token="a" * ADMIN_TOKEN_MIN_CHARS)
    assert app.ctx.admin_token == "a" * ADMIN_TOKEN_MIN_CHARS


# ---- the ladder, on each of the five routes ---------------------------------

def _routes_and_bodies():
    return [("/mods/install", _install_payload()),
            ("/mods/uninstall", {"id": "x-notes"}),
            ("/mods/rescan", {}),
            ("/mods/policy", SET_GIT),
            ("/update/policy", {"check_enabled": True})]


def test_the_gated_route_list_is_exactly_admin_routes():
    """The seam /info's advertisement (#191's capability report) will read is
    ADMIN_ROUTES; this pins that the tests below cover exactly that set, so
    the constant cannot drift from what is actually enforced."""
    assert sorted(p for p, _ in _routes_and_bodies()) == sorted(ADMIN_ROUTES)


@pytest.mark.parametrize("route, body", _routes_and_bodies())
def test_the_page_token_alone_answers_admin_required_and_writes_nothing(
        tmp_path, monkeypatch, route, body):
    app = _admin_app(tmp_path, monkeypatch)
    _, r = authed(app).post(route, json=body)
    assert r.status == 403, (route, r.json)
    assert r.json == {"ok": False, "error": "admin_required"}
    # Nothing was written: no sidecar, no store dir, no live-ctx change.
    assert not app.ctx.mod_policy_path.exists()
    assert app.ctx.mod_policy == {}
    assert not app.ctx.update_policy_path.exists()
    assert app.ctx.update_check_enabled is False
    assert app.ctx.mods_index["mods"] == {}
    assert not (tmp_path / "mods" / "x-notes").exists()


@pytest.mark.parametrize("route, body", _routes_and_bodies())
def test_a_wrong_admin_header_is_admin_required_too(tmp_path, monkeypatch,
                                                    route, body):
    app = _admin_app(tmp_path, monkeypatch)
    _, r = authed(app).post(route, json=body,
                            headers={ADMIN_HEADER: "wrong-" + "x" * 16})
    assert r.status == 403 and r.json["error"] == "admin_required"


def test_401_outranks_403(tmp_path, monkeypatch):
    """No/bad page token answers the EXISTING realm's 401 even when the admin
    header is correct — an unauthenticated caller learns nothing about the
    admin class."""
    app = _admin_app(tmp_path, monkeypatch)
    _, r = app.test_client.post("/mods/policy?token=wrong", json=SET_GIT,
                                headers=AHDR)
    assert r.status == 401 and r.json["error"] == "auth_required"
    _, r = app.test_client.post("/mods/policy", json=SET_GIT, headers=AHDR)
    assert r.status == 401 and r.json["error"] == "auth_required"


def test_the_admin_header_lets_each_route_proceed_to_its_own_gates(
        tmp_path, monkeypatch):
    """With page token + correct header, every route runs its OWN logic: the
    install lands, rescan rescans, a policy write persists, and an uninstall
    of a never-installed id reaches the route's own 404 (proof the gate is
    behind us, not a blanket 200)."""
    app = _admin_app(tmp_path, monkeypatch)
    client = authed(app)

    _, r = client.post("/mods/install", json=_install_payload(), headers=AHDR)
    assert r.status == 200, r.json
    assert "x-notes" in app.ctx.mods_index["mods"]

    _, r = client.post("/mods/rescan", json={}, headers=AHDR)
    assert r.status == 200, r.json

    _, r = client.post("/mods/policy", json=SET_GIT, headers=AHDR)
    assert r.status == 200 and r.json["policy"] == {"git": True}
    assert app.ctx.mod_policy_path.exists()

    _, r = client.post("/update/policy", json={"check_enabled": False},
                       headers=AHDR)
    assert r.status == 200, r.json
    assert app.ctx.update_policy_path.exists()

    _, r = client.post("/mods/uninstall", json={"id": "x-gone"}, headers=AHDR)
    assert r.status == 404 and r.json["error"] == "not_installed"

    # A page-token-only uninstall of the REAL mod still refuses and removes
    # nothing; the header then succeeds.
    _, r = client.post("/mods/uninstall", json={"id": "x-notes"})
    assert r.status == 403 and "x-notes" in app.ctx.mods_index["mods"]
    _, r = client.post("/mods/uninstall", json={"id": "x-notes"}, headers=AHDR)
    assert r.status == 200 and "x-notes" not in app.ctx.mods_index["mods"]


# ---- header-only transport --------------------------------------------------

def test_an_admin_token_in_a_query_string_is_refused_outright(tmp_path,
                                                              monkeypatch):
    app = _admin_app(tmp_path, monkeypatch)
    client = authed(app)
    # ...whatever the parameter is called,
    _, r = client.post(f"/mods/policy?admin={ADMIN}", json=SET_GIT)
    assert r.status == 400 and r.json["error"] == "admin_token_in_url"
    _, r = client.post(f"/mods/policy?cb={ADMIN}", json=SET_GIT)
    assert r.status == 400 and r.json["error"] == "admin_token_in_url"
    # ...and even when the header is ALSO correct — the URL already leaked the
    # credential into every access log downstream, and accepting it once
    # teaches a client the transport works.
    _, r = client.post(f"/update/policy?admin={ADMIN}",
                       json={"check_enabled": True}, headers=AHDR)
    assert r.status == 400 and r.json["error"] == "admin_token_in_url"
    # Nothing was written by any of the refusals.
    assert not app.ctx.mod_policy_path.exists()
    assert not app.ctx.update_policy_path.exists()


def test_the_admin_token_in_the_page_token_slot_is_never_accepted(tmp_path,
                                                                  monkeypatch):
    """``?token=<admin>`` reads as a (wrong) PAGE token and dies in the 401
    realm — the admin credential is not accepted from the query string under
    any spelling."""
    app = _admin_app(tmp_path, monkeypatch)
    _, r = app.test_client.post(f"/mods/policy?token={ADMIN}", json=SET_GIT)
    assert r.status == 401 and r.json["error"] == "auth_required"


# ---- absent config: byte-identity + the one boot line -----------------------

def test_absent_admin_config_is_byte_identical_on_the_wire(tmp_path,
                                                           monkeypatch):
    """No ``admin_token`` key -> no behavior change anywhere, pinned as the
    LITERAL response bytes (status line + body) /mods/policy served before the
    class existed. A stray admin header and an admin-shaped query param are
    both inert — no new 403, no new 400."""
    app = _make_app(tmp_path, monkeypatch)
    client = authed(app)
    _, ok = client.post("/mods/policy", json=SET_GIT)
    assert ok.status == 200
    assert ok.body == b'{"ok":true,"policy":{"git":true}}'
    _, hdr = client.post("/mods/policy", json=SET_GIT, headers=AHDR)
    assert hdr.status == 200 and hdr.body == ok.body
    _, qry = client.post(f"/mods/policy?admin={ADMIN}", json=SET_GIT)
    assert qry.status == 200 and qry.body == ok.body
    # The 401 realm is exactly what it always was.
    _, anon = app.test_client.post("/mods/policy", json=SET_GIT)
    assert anon.status == 401
    assert anon.body == b'{"ok":false,"error":"auth_required"}'
    # And a second route's error shape is untouched too.
    _, bad = client.post("/update/policy", json={})
    assert bad.status == 400
    assert bad.body == b'{"ok":false,"error":"bad_check_enabled"}'


def test_absent_admin_config_logs_exactly_one_boot_line(tmp_path, monkeypatch,
                                                        caplog):
    with caplog.at_level(logging.INFO, logger=app_mod.LOGGER.name):
        _make_app(tmp_path, monkeypatch)
    lines = [rec for rec in caplog.records
             if "admin credential class NOT configured" in rec.getMessage()]
    assert len(lines) == 1
    msg = lines[0].getMessage()
    assert "admin_token" in msg and ADMIN_HEADER in msg


def test_a_configured_admin_token_boots_quietly_and_redacted(tmp_path,
                                                             monkeypatch,
                                                             caplog):
    with caplog.at_level(logging.DEBUG):
        _admin_app(tmp_path, monkeypatch)
    assert not any("NOT configured" in rec.getMessage()
                   for rec in caplog.records)
    assert any("admin credential class enabled" in rec.getMessage()
               for rec in caplog.records)
    assert ADMIN not in caplog.text


# ---- redaction --------------------------------------------------------------

def test_the_admin_value_never_appears_in_any_log_line(tmp_path, monkeypatch,
                                                       caplog):
    """Boot, a 403, a wrong header and a success — the credential value (sent
    only in headers) is in NONE of the captured records, at any level, from
    ANY logger."""
    with caplog.at_level(logging.DEBUG):
        app = _admin_app(tmp_path, monkeypatch)
        client = authed(app)
        _, r1 = client.post("/mods/policy", json=SET_GIT)
        _, r2 = client.post("/mods/policy", json=SET_GIT,
                            headers={ADMIN_HEADER: "wrong-" + "x" * 16})
        _, r4 = client.post("/mods/policy", json=SET_GIT, headers=AHDR)
    assert (r1.status, r2.status, r4.status) == (403, 403, 200)
    assert ADMIN not in caplog.text
    # The 403 DID log (route + peer, value-free) — redaction is not silence.
    assert any("rejected non-admin /mods/policy" in rec.getMessage()
               for rec in caplog.records)


# ---- cross-origin preflight (#191) ------------------------------------------

@pytest.mark.parametrize("route", ["/mods/policy", "/update/policy"])
def test_admin_route_preflight_allows_the_admin_header(tmp_path, monkeypatch,
                                                        route):
    """The OPTIONS preflight for an ADMIN_ROUTES path lists ADMIN_HEADER in
    Access-Control-Allow-Headers -- otherwise a cross-origin admin write dies
    in preflight before the browser ever sends the POST. ONLY on an enforcing
    broker: a broker with no admin_token has no realm to advertise, and its
    preflight must stay byte-identical to the build before this class existed
    (the absent-config invariant; the plain shape is pinned in
    tests/test_broker_e2e.py::test_cors_preflight). No client sends the header
    to a broker whose /info never advertised `admin`, so the narrower list
    costs nothing."""
    _, r = _admin_app(tmp_path, monkeypatch).test_client.options(route)
    assert r.status == 204
    allow = [h.strip() for h in
            r.headers.get("Access-Control-Allow-Headers", "").split(",")]
    assert ADMIN_HEADER in allow
    _, r2 = _make_app(tmp_path, monkeypatch).test_client.options(route)
    assert r2.status == 204
    assert (r2.headers.get("Access-Control-Allow-Headers")
            == "Authorization, Content-Type")


def test_a_cross_origin_admin_write_survives_preflight_and_lands(tmp_path,
                                                                  monkeypatch):
    """The full sequence a browser performs for a cross-origin POST carrying a
    non-simple header: OPTIONS preflight (must allow the header, and must NOT
    require any auth of its own -- it carries no credentials), then the real
    POST with the page token + the admin header, which must land."""
    app = _admin_app(tmp_path, monkeypatch)
    client = authed(app)
    _, preflight = app.test_client.options(
        "/mods/policy",
        headers={"Origin": "https://elsewhere.example",
                 "Access-Control-Request-Method": "POST",
                 "Access-Control-Request-Headers": ADMIN_HEADER})
    assert preflight.status == 204
    allow = [h.strip() for h in
            preflight.headers.get("Access-Control-Allow-Headers", "").split(",")]
    assert ADMIN_HEADER in allow
    _, r = client.post("/mods/policy", json=SET_GIT,
                       headers={"Origin": "https://elsewhere.example", **AHDR})
    assert r.status == 200 and r.json["policy"] == {"git": True}


def test_a_cross_origin_write_with_a_wrong_admin_header_403s_not_blocked_by_preflight(
        tmp_path, monkeypatch):
    """The preflight never blocks the request -- a WRONG admin header still
    reaches the route and is refused there (403 admin_required), the same
    ladder position as the same-origin case."""
    app = _admin_app(tmp_path, monkeypatch)
    client = authed(app)
    _, preflight = app.test_client.options(
        "/mods/policy",
        headers={"Origin": "https://elsewhere.example",
                 "Access-Control-Request-Method": "POST",
                 "Access-Control-Request-Headers": ADMIN_HEADER})
    assert preflight.status == 204
    _, r = client.post("/mods/policy", json=SET_GIT,
                       headers={"Origin": "https://elsewhere.example",
                                ADMIN_HEADER: "wrong-" + "x" * 16})
    assert r.status == 403 and r.json["error"] == "admin_required"


def test_the_query_refusal_log_is_value_free(tmp_path, monkeypatch, caplog):
    """The query-string refusal path, scanned separately: the TEST harness
    (httpx / sanic_testing) echoes the URL the test itself poisoned at INFO,
    so the full-text scan above cannot apply here. The redaction surface is
    the BROKER's records — none may carry the value, and auth.py's "never log
    request URLs" rule is what keeps the poisoned URL out of them."""
    app = _admin_app(tmp_path, monkeypatch)
    client = authed(app)
    with caplog.at_level(logging.DEBUG):
        _, r = client.post(f"/mods/policy?admin={ADMIN}", json=SET_GIT)
    assert r.status == 400
    broker_records = [rec for rec in caplog.records
                      if rec.name.startswith("webterm")]
    assert not any(ADMIN in rec.getMessage() for rec in broker_records)
    assert any("rejected admin token in query string" in rec.getMessage()
               for rec in broker_records)
