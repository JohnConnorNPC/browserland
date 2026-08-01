"""Turning update checking on from the GUI (#182).

Opting a broker in used to mean editing ``broker_config.json`` and restarting
the process. ``POST /update/policy`` takes that decision live, and this file is
about the parts of that which are easy to get wrong rather than the happy path.

The rules it pins down, each of which came out of adversarial review of the
design and would otherwise be a comment nobody checks:

* **A config key that is PRESENT wins, always.** Editing the config and
  bouncing the broker is the standard response to egress you did not want, and
  a sidecar that could override it would make that a silent no-op -- the file
  saying ``false`` while the process cheerfully checks.
* **A corrupt sidecar fails CLOSED.** Treating unreadable as absent would fall
  through to the config seed, so a deliberate stored revoke that later got
  truncated would come back from a restart as the seed's "enabled": an egress
  permission resurrected by a damaged file.
* **The value must be a real bool, in the file and on the wire.** ``"false"``
  is truthy, and this is the worst possible place to guess.
* **A revoke that returns success must actually stop the next request**, even
  one already queued on the single-flight lock -- otherwise the grant is
  withdrawn and then honoured anyway.

No network anywhere: every test that could reach GitHub patches ``run_check``
with a fake that raises if it is called at all.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from .auth_helpers import TEST_TOKEN, authed
from webterm.broker import app as broker_app
from webterm.broker import update
from webterm.broker.app import create_app


_app_seq = 0


def _make_app(tmp_path, monkeypatch, **cfg_extra):
    """A broker whose sidecar lands in tmp_path. `update_check_enabled` is
    deliberately ABSENT unless a test passes it: absence is what hands the
    decision to the GUI, and it is what every shipped example config has."""
    global _app_seq
    _app_seq += 1
    monkeypatch.delenv("WEB_TERMINAL_TOKEN", raising=False)
    cfg = {"state_path": str(tmp_path / "webterm_state.json"),
           "auth_token": TEST_TOKEN}
    cfg.update(cfg_extra)
    return create_app(cfg, name=f"webterm-update-policy-{_app_seq}")


def _no_network(monkeypatch):
    def fake(**kwargs):
        raise AssertionError(
            "the broker made an upstream call it was not permitted to make")
    monkeypatch.setattr(broker_app.update_check, "run_check", fake)


def _allow_network(monkeypatch):
    calls = []

    def fake(**kwargs):
        calls.append(kwargs)
        return {"state": update.STATE_CURRENT, "reason": None, "mode": "commit",
                "local": {"version": "0.8.0", "sha": "a" * 40},
                "repo": update.UPSTREAM_REPO, "checkedAt": 1,
                "upstream": {"sha": "a" * 40}, "treeVerified": False}

    monkeypatch.setattr(broker_app.update_check, "run_check", fake)
    return calls


def _sidecar(app):
    return app.ctx.update_policy_path


# ---- who decides ------------------------------------------------------------

def test_absent_config_leaves_the_decision_to_the_gui(tmp_path, monkeypatch):
    app = _make_app(tmp_path, monkeypatch)
    assert app.ctx.update_check_enabled is False
    assert app.ctx.update_policy_source == "default"
    _, r = authed(app).get("/info")
    assert r.json["update"]["mutable"] is True
    assert r.json["update"]["source"] == "default"


def test_a_config_key_that_is_present_owns_the_setting(tmp_path, monkeypatch):
    """Both directions. The `false` case is the one that matters: it is the
    emergency stop, and it must not be overridable from a browser."""
    app = _make_app(tmp_path, monkeypatch, update_check_enabled=False)
    assert app.ctx.update_policy_source == "config"
    _, r = authed(app).get("/info")
    assert r.json["update"]["mutable"] is False

    app = _make_app(tmp_path, monkeypatch, update_check_enabled=True)
    assert app.ctx.update_check_enabled is True
    assert app.ctx.update_policy_source == "config"


def test_the_route_refuses_when_the_config_owns_it(tmp_path, monkeypatch):
    _no_network(monkeypatch)
    app = _make_app(tmp_path, monkeypatch, update_check_enabled=False)
    _, r = authed(app).post("/update/policy", json={"check_enabled": True})
    assert r.status == 409
    assert r.json["error"] == "policy_locked"
    assert r.json["source"] == "config"
    # And nothing moved: not the live flag, not the file.
    assert app.ctx.update_check_enabled is False
    assert not _sidecar(app).exists()
    _, r = authed(app).get("/update/check")
    assert r.status == 503


def test_a_stored_grant_outlives_the_process(tmp_path, monkeypatch):
    """The whole point of a sidecar rather than a runtime flag: the operator
    turns it on once, not once per restart."""
    _no_network(monkeypatch)
    app = _make_app(tmp_path, monkeypatch)
    _, r = authed(app).post("/update/policy", json={"check_enabled": True})
    assert r.status == 200
    assert r.json["update"]["check_enabled"] is True
    assert r.json["update"]["source"] == "stored"

    fresh = _make_app(tmp_path, monkeypatch)
    assert fresh.ctx.update_check_enabled is True
    assert fresh.ctx.update_policy_source == "stored"


def test_a_revoke_also_outlives_the_process(tmp_path, monkeypatch):
    _no_network(monkeypatch)
    app = _make_app(tmp_path, monkeypatch)
    authed(app).post("/update/policy", json={"check_enabled": True})
    _, r = authed(app).post("/update/policy", json={"check_enabled": False})
    assert r.status == 200
    assert r.json["update"]["check_enabled"] is False
    fresh = _make_app(tmp_path, monkeypatch)
    assert fresh.ctx.update_check_enabled is False


# ---- failing closed ---------------------------------------------------------

@pytest.mark.parametrize("blob", ["{ not json", "[]", '{"check_enabled": 1}',
                                  '{"check_enabled": "true"}', '{}'])
def test_a_corrupt_sidecar_disables_checking(tmp_path, monkeypatch, blob):
    """Unreadable must NOT degrade to "absent". If it did, a stored revoke that
    got truncated would fall through to a config seed of `true` and come back
    enabled -- a permission granted by file damage."""
    _no_network(monkeypatch)
    path = tmp_path / "webterm_update_policy.json"
    path.write_text(blob, encoding="utf-8")
    app = _make_app(tmp_path, monkeypatch,
                    update_policy_path=str(path))
    assert app.ctx.update_check_enabled is False
    assert app.ctx.update_policy_source == "corrupt"
    _, r = authed(app).get("/update/check")
    assert r.status == 503


def test_corruption_does_not_break_boot(tmp_path, monkeypatch):
    """Degrade, never refuse to start: an unreadable optional sidecar must not
    take the whole desktop down with it."""
    path = tmp_path / "webterm_update_policy.json"
    path.write_text("<<<garbage>>>", encoding="utf-8")
    app = _make_app(tmp_path, monkeypatch, update_policy_path=str(path))
    _, r = authed(app).get("/info")
    assert r.status == 200
    assert r.json["update"]["source"] == "corrupt"
    # Still mutable: the fix for a broken file is being able to write a good
    # one, so the route must not be locked by the damage.
    assert r.json["update"]["mutable"] is True
    _, r = authed(app).post("/update/policy", json={"check_enabled": True})
    assert r.status == 200
    assert app.ctx.update_check_enabled is True


def test_a_corrupt_sidecar_cannot_be_read_as_a_grant(tmp_path, monkeypatch):
    """The sharpest version of the rule: config says ON, the sidecar is
    damaged. Config is absent from this app, so nothing may grant."""
    _no_network(monkeypatch)
    path = tmp_path / "webterm_update_policy.json"
    path.write_text('{"check_enabled": "yes"}', encoding="utf-8")
    app = _make_app(tmp_path, monkeypatch, update_policy_path=str(path))
    assert app.ctx.update_check_enabled is False


# ---- the wire ---------------------------------------------------------------

def test_the_route_requires_a_token(tmp_path, monkeypatch):
    app = _make_app(tmp_path, monkeypatch)
    _, r = app.test_client.post("/update/policy", json={"check_enabled": True})
    assert r.status == 401
    assert app.ctx.update_check_enabled is False


@pytest.mark.parametrize("body", [{}, {"check_enabled": "true"},
                                  {"check_enabled": 1}, {"check_enabled": None},
                                  {"check_enabled": "false"}])
def test_only_a_real_bool_is_accepted(tmp_path, monkeypatch, body):
    """`"false"` is truthy in Python as it is in JavaScript. Coercing here
    would grant egress to a client that asked for the opposite."""
    _no_network(monkeypatch)
    app = _make_app(tmp_path, monkeypatch)
    _, r = authed(app).post("/update/policy", json=body)
    assert r.status == 400
    assert r.json["error"] == "bad_check_enabled"
    assert app.ctx.update_check_enabled is False
    assert not _sidecar(app).exists()


def test_a_cross_origin_write_is_refused(tmp_path, monkeypatch):
    """Origin-gated unlike /mods/policy, which it otherwise copies: a mod pin is
    recoverable, and a disclosed address is not. Enabling is local-only at the
    UI too, so nothing legitimate needs this door."""
    _no_network(monkeypatch)
    app = _make_app(tmp_path, monkeypatch)
    _, r = authed(app).post("/update/policy", json={"check_enabled": True},
                            headers={"Origin": "https://elsewhere.example"})
    assert r.status == 403
    assert r.json["error"] == "forbidden_origin"
    assert app.ctx.update_check_enabled is False


def test_the_preflight_exists_so_the_route_is_not_invisible(tmp_path,
                                                            monkeypatch):
    """A new path with no OPTIONS route 405s before any middleware, which a
    browser reports as an opaque network error. The entry exists so that
    enabling a PEER stays a policy decision rather than a compat break."""
    app = _make_app(tmp_path, monkeypatch)
    _, r = app.test_client.options("/update/policy")
    assert r.status in (200, 204)


def test_the_write_is_idempotent(tmp_path, monkeypatch):
    """N tabs asking for the state it is already in must not rewrite the file N
    times."""
    _no_network(monkeypatch)
    app = _make_app(tmp_path, monkeypatch)
    authed(app).post("/update/policy", json={"check_enabled": True})
    # Counted, not mtime'd: three writes inside one filesystem timestamp tick
    # would leave mtime unchanged and pass this test without the guard.
    writes = []
    real = broker_app._write_state_atomic

    def counting(path, state):
        writes.append(path)
        return real(path, state)

    monkeypatch.setattr(broker_app, "_write_state_atomic", counting)
    for _ in range(3):
        _, r = authed(app).post("/update/policy", json={"check_enabled": True})
        assert r.status == 200
        assert r.json["update"]["check_enabled"] is True
    assert writes == [], f"re-asserting the current state rewrote the file: {writes}"
    # ...and a genuine change still writes, so the guard is not just "never".
    authed(app).post("/update/policy", json={"check_enabled": False})
    assert len(writes) == 1


def test_what_landed_on_disk_is_what_info_reports(tmp_path, monkeypatch):
    _no_network(monkeypatch)
    app = _make_app(tmp_path, monkeypatch)
    _, w = authed(app).post("/update/policy", json={"check_enabled": True})
    _, i = authed(app).get("/info")
    assert w.json["update"] == i.json["update"], (
        "the write's response is what a client repaints from; it must equal "
        "what re-fetching /info would have given")
    stored = json.loads(_sidecar(app).read_text(encoding="utf-8"))
    assert stored["check_enabled"] is True


# ---- the gate it governs ----------------------------------------------------

def test_enabling_takes_effect_with_no_restart(tmp_path, monkeypatch):
    """The reason this is a route and not a config key: the very next request
    goes through."""
    calls = _allow_network(monkeypatch)
    app = _make_app(tmp_path, monkeypatch)
    client = authed(app)
    _, r = client.get("/update/check")
    assert r.status == 503 and not calls

    _, r = client.post("/update/policy", json={"check_enabled": True})
    assert r.status == 200

    _, r = client.get("/update/check")
    assert r.status == 200
    assert r.json["check"]["state"] == update.STATE_CURRENT
    assert len(calls) == 1


def test_revoking_stops_the_next_check(tmp_path, monkeypatch):
    _allow_network(monkeypatch)
    app = _make_app(tmp_path, monkeypatch)
    client = authed(app)
    client.post("/update/policy", json={"check_enabled": True})
    client.get("/update/check")
    client.post("/update/policy", json={"check_enabled": False})
    _, r = client.get("/update/check")
    assert r.status == 503
    assert r.json["error"] == "update_check_disabled"


def test_a_revoke_beats_a_check_already_queued_on_the_lock(tmp_path,
                                                           monkeypatch):
    """The TOCTOU the gate re-read inside update_lock exists for.

    A check that passed the handler's gate test and then queued behind another
    caller must NOT make its outbound request if the grant was withdrawn while
    it waited. Driven through app.ctx.update_check_run rather than the HTTP
    client on purpose -- the test client serializes requests, which would pass
    this test without the fix.
    """
    calls = []
    started = asyncio.Event()
    release = asyncio.Event()

    def fake(**kwargs):
        calls.append(kwargs)
        return {"state": update.STATE_CURRENT, "reason": None, "mode": "commit",
                "local": {"version": "0.8.0", "sha": "a" * 40},
                "repo": update.UPSTREAM_REPO, "checkedAt": 1,
                "upstream": {"sha": "a" * 40}, "treeVerified": False}

    monkeypatch.setattr(broker_app.update_check, "run_check", fake)
    app = _make_app(tmp_path, monkeypatch)
    app.ctx.update_check_enabled = True
    app.ctx.update_policy_source = "stored"

    async def drive():
        # Hold update_lock so the check below queues behind it, exactly as a
        # second caller would during a real upstream request.
        async def holder():
            async with app.ctx.update_lock:
                started.set()
                await release.wait()

        h = asyncio.ensure_future(holder())
        await started.wait()
        queued = asyncio.ensure_future(app.ctx.update_check_run())
        # Spin the loop until it is genuinely parked ON the lock. A single
        # sleep(0) happens to be enough today; a bare one would silently stop
        # testing anything if the path ahead of the lock ever gained an await.
        for _ in range(50):
            if app.ctx.update_lock._waiters:
                break
            await asyncio.sleep(0)
        else:
            raise AssertionError("the check never reached update_lock")
        # The revoke lands while that check is waiting its turn.
        app.ctx.update_check_enabled = False
        release.set()
        result = await queued
        await h
        return result

    result = asyncio.run(drive())
    assert result is None, "a revoked check must not return a result"
    assert not calls, (
        "the revoke was granted and then the forbidden request was made anyway")
