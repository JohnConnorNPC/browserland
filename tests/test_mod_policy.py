"""Per-broker MOD POLICY: the served-mod catalog on /info + POST /mods/policy (#157).

The Control Panel grew a "Mods on this broker" section per host tab, so a browser
has to be able to ask ANY broker which mods it serves and which of them that
broker PINS on/off for every browser that loads its page. Both halves ride
surfaces a browser already talks to:

* ``GET /info`` gained ``mods`` (the catalog, from ui.mod_catalog()) and
  ``mod_policy`` (the pins). It is /info and not a new ``GET /mods`` on purpose:
  a token-bearing cross-origin GET is preceded by OPTIONS, and a broker predating
  this feature has no route for a NEW path -- its preflight fails and the browser
  reports an opaque network error, indistinguishable from "that machine is
  asleep". On /info an older broker answers normally and simply lacks the keys.

* ``POST /mods/policy`` is the only writer, token-gated and deliberately NOT
  lease-gated -- the test below pins that, because it is the whole reason the
  policy is a broker sidecar instead of a key in the /state settings blob (a
  /state PUT from a non-active browser is refused 409 not_active forever, i.e.
  exactly on the brokers an operator most wants to administer remotely).

Route tests use the in-process Sanic test client (each create_app needs a UNIQUE
name) with state_path in a tmp dir, so neither the identity file nor the policy
sidecar ever lands in the repo.
"""

from __future__ import annotations

import json
from pathlib import Path

from .auth_helpers import TEST_TOKEN, authed
from webterm.broker import ui
from webterm.broker.app import (MAX_MOD_POLICY_KEYS, _INSTALLED_ID_PREFIX,
                                _MODSTORE_ID_RE, _is_reserved_mod_id,
                                _load_mod_policy, _sanitize_mod_policy,
                                create_app)

SIDECAR = "webterm_mod_policy.json"

_app_seq = 0


def _make_app(tmp_path, monkeypatch, **cfg):
    global _app_seq
    _app_seq += 1
    monkeypatch.delenv("WEB_TERMINAL_TOKEN", raising=False)
    conf = {"state_path": str(tmp_path / "webterm_state.json"),
            "auth_token": TEST_TOKEN}
    conf.update(cfg)
    return create_app(conf, name=f"webterm-modpolicy-test-{_app_seq}")


# ---- the mod-id shape itself (#172) ---------------------------------------

def test_mod_id_regex_is_anchored_at_both_ends():
    """#172: the pattern is ANCHORED, so a future ``.match()`` cannot silently
    accept a traversal.

    Every call site today uses ``.fullmatch()`` and is therefore already safe --
    which is exactly why the hazard is invisible: ``.match()`` is the natural
    thing to write, and on the old unanchored pattern it would have accepted
    ``clock/../../etc`` as a /mod-store path segment and a pin key. Also \\A..\\Z
    rather than ^..$, because ``$`` matches before a trailing newline too."""
    for bad in ("clock/../../etc", "clock/x", "clock\n", "clock\nx",
                "../etc", "clock:stream", "clock\\x"):
        assert _MODSTORE_ID_RE.match(bad) is None, \
            f"{bad!r} must not even .match() the mod-id shape"
        assert _MODSTORE_ID_RE.fullmatch(bad) is None
    for good in ("clock", "mod-sync", "x-notes", "a", "0", "a" * 64):
        assert _MODSTORE_ID_RE.match(good) is not None
        assert _MODSTORE_ID_RE.fullmatch(good) is not None
    assert _MODSTORE_ID_RE.fullmatch("a" * 65) is None      # cap still holds


def test_installed_ids_are_exactly_the_x_prefixed_ones():
    """#172's namespace split, as one lexical test. An id is RESERVED for
    first-party mods iff it does not carry the prefix; a runtime-installed mod
    must. Both regex twins stay unchanged because "x-foo" already fullmatches."""
    assert _INSTALLED_ID_PREFIX == "x-"
    for shipped in ("clock", "git", "mod-sync", "x", "xylophone", "xy-lo"):
        assert _is_reserved_mod_id(shipped), shipped
    for installed in ("x-notes", "x-johnconnornpc-notes", "x-a"):
        assert not _is_reserved_mod_id(installed), installed
        # ...and it is still a valid /mod-store segment + pin key, unchanged.
        assert _MODSTORE_ID_RE.fullmatch(installed)
        assert _sanitize_mod_policy({installed: True}) == {installed: True}
    # Junk is RESERVED, never "installable": the caller's next step is to refuse.
    for junk in (None, 7, b"x-notes", [], True):
        assert _is_reserved_mod_id(junk)


# ---- _sanitize_mod_policy / _load_mod_policy (pure) -----------------------

def test_sanitize_keeps_only_real_bools_on_mod_id_keys():
    dirty = {
        "git": True, "clock": False,          # kept
        "Git": True,                          # uppercase is not a mod-id shape
        "../etc": True, "a" * 65: True,       # traversal / over-long
        "": True, "-lead": True,              # empty / bad first char
        "aistatus": "true", "editor": 1,      # truthy but NOT a bool
        "help": None,
    }
    assert _sanitize_mod_policy(dirty) == {"clock": False, "git": True}


def test_sanitize_rejects_non_dicts_and_caps_key_count():
    for junk in (None, [], "git", 7, True):
        assert _sanitize_mod_policy(junk) == {}
    flood = {f"m{i:04d}": True for i in range(MAX_MOD_POLICY_KEYS + 50)}
    out = _sanitize_mod_policy(flood)
    assert len(out) == MAX_MOD_POLICY_KEYS
    # Deterministic (sorted) truncation, so two readers agree on the same subset.
    assert sorted(out) == sorted(flood)[:MAX_MOD_POLICY_KEYS]


def test_load_mod_policy_self_heals_and_accepts_both_shapes(tmp_path):
    missing = tmp_path / SIDECAR
    assert _load_mod_policy(missing) == {}           # never written yet
    missing.write_text("{ not valid json", encoding="utf-8")
    assert _load_mod_policy(missing) == {}           # truncated / hand-mangled
    missing.write_text(json.dumps({"policy": {"git": True, "x!": True}}),
                       encoding="utf-8")
    assert _load_mod_policy(missing) == {"git": True}
    # A bare map (no "policy" wrapper) is accepted too, so a hand-written file
    # in the obvious shape works.
    missing.write_text(json.dumps({"clock": False}), encoding="utf-8")
    assert _load_mod_policy(missing) == {"clock": False}


# ---- GET /info: the catalog + the pins ------------------------------------

def test_info_reports_full_catalog_and_empty_policy_by_default(tmp_path, monkeypatch):
    app = _make_app(tmp_path, monkeypatch)
    _, r = authed(app).get("/info")
    assert r.status == 200
    body = r.json
    assert body["mod_policy"] == {}                  # nothing pinned out of the box
    ids = [m["id"] for m in body["mods"]]
    # Every served mod dir is advertised, in served order, with the fields the
    # remote policy editor needs to label its rows.
    assert ids == [m["id"] for m in ui.mod_catalog()]
    assert len(ids) == len(ui._mod_dirs(ui._MODS)) == len(set(ids))
    for m in body["mods"]:
        assert isinstance(m["title"], str) and m["title"]
        assert isinstance(m["default_enabled"], bool)
        assert isinstance(m["requires"], list)
        # #163: the catalog has TWO sources and every row says which. With
        # nothing installed (this broker's mods_dir does not exist) the list is
        # shipped-only, so this asserts what it USED to hold implicitly. Its
        # companion -- ordering, provenance and default_enabled with a fixture
        # mod actually installed -- is
        # tests/test_mod_install.py::test_info_reports_shipped_then_installed_with_provenance.
        assert m["source"] == "shipped"
    by_id = {m["id"]: m for m in body["mods"]}
    assert by_id["git"]["default_enabled"] is False      # ships off (#116)
    assert by_id["clock"]["default_enabled"] is True
    assert by_id["scratchpad"]["requires"] == ["editor"]


def test_info_reports_persisted_policy_and_hides_junk(tmp_path, monkeypatch):
    (tmp_path / SIDECAR).write_text(
        json.dumps({"policy": {"git": True, "clipboard": False,
                               "not a mod id": True, "clock": "yes"}}),
        encoding="utf-8")
    app = _make_app(tmp_path, monkeypatch)
    _, r = authed(app).get("/info")
    assert r.status == 200
    # Sidecar is truth, sanitized: the bogus key and the non-bool are gone.
    assert r.json["mod_policy"] == {"clipboard": False, "git": True}


def test_headless_broker_serves_no_mods(tmp_path, monkeypatch):
    # #87: a headless broker never imports .ui, so it has no catalog -- and that
    # is the literal truth (it serves no page, so nothing loads its mods). The
    # client tells this empty apart from an older broker's MISSING key via
    # serve_ui, so both must be present.
    app = _make_app(tmp_path, monkeypatch, serve_ui=False)
    _, r = authed(app).get("/info")
    assert r.status == 200
    assert r.json["serve_ui"] is False
    assert r.json["mods"] == []
    assert r.json["mod_policy"] == {}


# ---- POST /mods/policy ----------------------------------------------------

def test_policy_post_sets_clears_and_persists(tmp_path, monkeypatch):
    app = _make_app(tmp_path, monkeypatch)
    client = authed(app)
    _, r = client.post("/mods/policy", json={"set": {"git": True}})
    assert r.status == 200
    # The response is the AUTHORITATIVE stored policy -- the editor repaints from
    # it rather than from the value it hoped would land.
    assert r.json == {"ok": True, "policy": {"git": True}}
    assert json.loads((tmp_path / SIDECAR).read_text())["policy"] == {"git": True}
    _, r = client.get("/info")
    assert r.json["mod_policy"] == {"git": True}

    # PATCH semantics: a second mod is added, the first is untouched.
    _, r = client.post("/mods/policy", json={"set": {"clipboard": False}})
    assert r.json["policy"] == {"clipboard": False, "git": True}
    # null CLEARS one pin without resending the map.
    _, r = client.post("/mods/policy", json={"set": {"git": None}})
    assert r.json["policy"] == {"clipboard": False}
    assert json.loads((tmp_path / SIDECAR).read_text())["policy"] == \
        {"clipboard": False}


def test_policy_survives_a_restart(tmp_path, monkeypatch):
    app = _make_app(tmp_path, monkeypatch)
    _, r = authed(app).post("/mods/policy", json={"set": {"termfont": True}})
    assert r.status == 200
    # A "restart": a fresh app over the same state dir reads the sidecar.
    again = _make_app(tmp_path, monkeypatch)
    assert again.ctx.mod_policy == {"termfont": True}
    _, r = authed(again).get("/info")
    assert r.json["mod_policy"] == {"termfont": True}


def test_policy_post_needs_the_token(tmp_path, monkeypatch):
    app = _make_app(tmp_path, monkeypatch)
    # Raw client (no token appended): a policy write is a broker mutation.
    _, r = app.test_client.post("/mods/policy", json={"set": {"git": True}})
    assert r.status == 401
    assert app.ctx.mod_policy == {}
    assert not (tmp_path / SIDECAR).exists()


def test_policy_post_is_not_lease_gated(tmp_path, monkeypatch):
    # THE reason the policy is a broker sidecar and not a /state key. With another
    # browser holding this broker's single-active lease, a /state PUT is refused
    # 409 not_active -- permanently, since the client's adopt-and-retry cannot fix
    # it. A policy write must still land: administering a broker that somebody is
    # using is the case the feature exists for.
    app = _make_app(tmp_path, monkeypatch)
    app.ctx.active_client_id = "some-other-browser"
    client = authed(app)
    _, denied = client.put("/state", json={"baseRev": 0, "settings": {},
                                          "layout": {}, "clientId": "me"})
    assert denied.status == 409
    assert denied.json["error"] == "not_active"
    _, allowed = client.post("/mods/policy", json={"set": {"git": True}})
    assert allowed.status == 200
    assert allowed.json["policy"] == {"git": True}


def test_policy_post_rejects_bad_payloads_without_changing_anything(tmp_path,
                                                                   monkeypatch):
    app = _make_app(tmp_path, monkeypatch)
    client = authed(app)
    _, r = client.post("/mods/policy", json={"set": {"clock": False}})
    assert r.status == 200                          # a known-good baseline
    bad = [
        ({}, 400, "bad_set"),                       # no `set`
        ({"set": {}}, 400, "bad_set"),              # empty `set`
        ({"set": []}, 400, "bad_set"),              # wrong type
        ({"set": {"Git": True}}, 400, "bad_mod_id"),        # not a mod-id shape
        ({"set": {"../x": True}}, 400, "bad_mod_id"),       # traversal
        ({"set": {"git": "true"}}, 400, "bad_pin"),         # truthy, not a bool
        ({"set": {"git": 1}}, 400, "bad_pin"),              # int, not a bool
        ({"set": {f"m{i:04d}": True
                  for i in range(MAX_MOD_POLICY_KEYS + 1)}}, 413, "too_many"),
    ]
    for payload, status, error in bad:
        _, r = client.post("/mods/policy", json=payload)
        assert r.status == status, payload
        assert r.json["error"] == error, payload
    # Validation happens BEFORE the lock/write, so not one of them moved anything.
    assert app.ctx.mod_policy == {"clock": False}
    assert json.loads((tmp_path / SIDECAR).read_text())["policy"] == {"clock": False}


def test_policy_post_rejects_non_json_body(tmp_path, monkeypatch):
    app = _make_app(tmp_path, monkeypatch)
    _, r = authed(app).post("/mods/policy", data="not json")
    assert r.status == 400
    assert r.json["error"] == "bad_json"


def test_policy_options_preflight_cors(tmp_path, monkeypatch):
    # The write is cross-origin from another broker's Control Panel tab, and a
    # JSON POST is never a "simple" request -- so its preflight must be answered.
    app = _make_app(tmp_path, monkeypatch)
    _, r = authed(app).options("/mods/policy")
    assert r.status == 204
    assert r.headers.get("Access-Control-Allow-Origin") == "*"
    assert "POST" in r.headers.get("Access-Control-Allow-Methods", "")


def test_policy_sidecar_path_is_overridable(tmp_path, monkeypatch):
    # Same knob shape as mcp_state_path, so a deploy can place it deliberately.
    where = tmp_path / "nested" / "pins.json"
    where.parent.mkdir()
    app = _make_app(tmp_path, monkeypatch, mod_policy_path=str(where))
    assert app.ctx.mod_policy_path == Path(where).resolve()
    _, r = authed(app).post("/mods/policy", json={"set": {"git": True}})
    assert r.status == 200
    assert json.loads(where.read_text())["policy"] == {"git": True}
    assert not (tmp_path / SIDECAR).exists()
