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
* **A subset write must not erase its siblings.** The route accepts any
  subset of the four recognized keys and merges over what is on disk,
  re-read under its own lock, so granting one gate never clobbers a stored
  grant for another -- and never copies a config-owned value into the
  sidecar. Since #189 the fourth key is ``status_fetch_enabled``, riding the
  same seam: its direction is always NAMED in the body, never derived from
  absence.

No network anywhere: every test that could reach GitHub patches ``run_check``
with a fake that raises if it is called at all.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from .auth_helpers import TEST_TOKEN, authed, authed_reusable, with_token
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
    tmp_path.mkdir(parents=True, exist_ok=True)
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


def test_a_cross_origin_write_is_the_point(tmp_path, monkeypatch):
    """This route briefly WAS origin-gated, and that was wrong twice over.

    An operator administers a fleet from one desktop, so the broker they need to
    switch on is the one they have no local session on — the gate refused the
    only case that mattered. And it protected nothing: this broker authenticates
    by an explicit token, never a cookie, so a page on another origin has no
    ambient credential to ride, and a caller holding the token can already POST
    /launch for a shell. POST /restart keeps its origin check; this does not."""
    _no_network(monkeypatch)
    app = _make_app(tmp_path, monkeypatch)
    _, r = authed(app).post("/update/policy", json={"check_enabled": True},
                            headers={"Origin": "https://elsewhere.example"})
    assert r.status == 200, "a peer's desktop must be able to make this change"
    assert app.ctx.update_check_enabled is True
    # Still worthless without the token, whatever the Origin says.
    app2 = _make_app(tmp_path / "b", monkeypatch)
    _, r = app2.test_client.post("/update/policy",
                                 json={"check_enabled": True},
                                 headers={"Origin": "https://elsewhere.example"})
    assert r.status == 401
    assert app2.ctx.update_check_enabled is False


def test_remote_writable_is_published_so_an_older_peer_is_not_offered_one(
        tmp_path, monkeypatch):
    """A separate key from `mutable`, and the separation is what keeps a rolling
    upgrade honest: the first build to ship this route origin-gated it, so it
    reports mutable:true and refuses the write. Only a build that accepts the
    write says so."""
    app = _make_app(tmp_path, monkeypatch)
    _, r = authed(app).get("/info")
    assert r.json["update"]["remote_writable"] is True
    # And it stays true even where the setting itself cannot be changed: the two
    # keys answer different questions, and collapsing them is the bug.
    locked = _make_app(tmp_path / "c", monkeypatch, update_check_enabled=False)
    _, r = authed(locked).get("/info")
    assert r.json["update"]["mutable"] is False
    assert r.json["update"]["remote_writable"] is True


def test_remote_applyable_is_published_so_an_older_peer_is_not_offered_a_button(
        tmp_path, monkeypatch):
    """Same pattern as `remote_writable` (#205): only a build whose POST
    /update/apply accepts a cross-origin call may say so, so an older peer's
    silence is what keeps a remote UI from offering a button it can only
    misdescribe when it 403s. Pinned on both wires the shared view feeds."""
    _no_network(monkeypatch)
    app = _make_app(tmp_path, monkeypatch)
    _, w = authed(app).post("/update/policy", json={"check_enabled": True})
    assert w.json["update"]["remote_applyable"] is True
    _, r = authed(app).get("/info")
    assert r.json["update"]["remote_applyable"] is True
    # Independent of whether apply itself is currently grantable: the
    # capability key describes the code, `policy.apply` the live gate.
    locked = _make_app(tmp_path / "c", monkeypatch, update_apply_enabled=False)
    _, r = authed(locked).get("/info")
    assert r.json["update"]["policy"]["apply"]["enabled"] is False
    assert r.json["update"]["remote_applyable"] is True


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
    _, w = authed(app).post("/update/policy",
                            json={"check_enabled": True,
                                  "apply_enabled": True,
                                  "restart_enabled": True})
    _, i = authed(app).get("/info")
    assert w.json["update"] == i.json["update"], (
        "the write's response is what a client repaints from; it must equal "
        "what re-fetching /info would have given")
    stored = json.loads(_sidecar(app).read_text(encoding="utf-8"))
    assert stored == {"check_enabled": True, "apply_enabled": True,
                      "restart_enabled": True}


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


# ---- the apply and restart gates: same seam, never coupled to the check -----
#
# REVERSED from the first build, which held `update_apply_enabled` (and
# `restart_enabled`) to config-file-only. All three gates now resolve through
# the same per-key layering -- config-key presence > corrupt-fails-closed >
# stored sidecar grant > default off -- each key INDEPENDENTLY. What these
# tests pin instead is the part that must survive the reversal: turning the
# CHECK on, by config key or by stored grant, must never turn the APPLY or the
# RESTART on; a malformed key condemns the whole sidecar; and a PRESENT config
# key still outranks anything the sidecar says, which is what keeps a
# hands-off deployment (keys pinned in broker_config) un-grantable from the
# GUI.

def test_apply_gate_defaults_false_when_key_absent(tmp_path, monkeypatch):
    app = _make_app(tmp_path, monkeypatch)
    assert app.ctx.update_apply_enabled is False


def test_a_present_apply_config_key_owns_the_gate(tmp_path, monkeypatch):
    """Either value: presence is ownership. The `false` case is the emergency
    stop -- source "config", immutable in the view, and no browser write can
    move it. The `true` case is the same ownership pointing the other way."""
    off = _make_app(tmp_path, monkeypatch, update_apply_enabled=False)
    assert off.ctx.update_apply_enabled is False
    assert off.ctx.update_apply_source == "config"
    _, r = authed(off).get("/info")
    assert r.json["update"]["policy"]["apply"] == {
        "enabled": False, "source": "config", "mutable": False}

    on = _make_app(tmp_path / "b", monkeypatch, update_apply_enabled=True)
    assert on.ctx.update_apply_enabled is True
    assert on.ctx.update_apply_source == "config"
    _, r = authed(on).get("/info")
    assert r.json["update"]["policy"]["apply"] == {
        "enabled": True, "source": "config", "mutable": False}


def test_a_stored_check_grant_does_not_leak_into_the_apply_gate(
        tmp_path, monkeypatch):
    """Independence, direction 1 -- the no-escalation pin: a sidecar that flips
    the CHECK gate on carries no apply key, and an absent key must resolve to
    default-off, never to a sibling's value."""
    path = tmp_path / "webterm_update_policy.json"
    path.write_text(json.dumps({"check_enabled": True}), encoding="utf-8")
    app = _make_app(tmp_path, monkeypatch, update_policy_path=str(path))
    assert app.ctx.update_check_enabled is True
    assert app.ctx.update_policy_source == "stored"
    assert app.ctx.update_apply_enabled is False, (
        "a stored grant for the CHECK must not also grant the APPLY")


def test_an_old_sidecar_never_escalates_into_apply_or_restart(tmp_path,
                                                              monkeypatch):
    """A sidecar written before apply/restart joined the seam carries only
    check_enabled. Its grant is honoured for the check and for NOTHING else,
    and /info's `policy` block is where a client reads all three verdicts."""
    path = tmp_path / "webterm_update_policy.json"
    path.write_text(json.dumps({"check_enabled": True}), encoding="utf-8")
    app = _make_app(tmp_path, monkeypatch, update_policy_path=str(path))
    assert app.ctx.update_check_enabled is True
    assert app.ctx.update_policy_source == "stored"
    assert app.ctx.update_apply_enabled is False
    assert app.ctx.update_apply_source == "default"
    assert app.ctx.restart_enabled is False
    assert app.ctx.restart_source == "default"
    _, r = authed(app).get("/info")
    assert r.json["update"]["policy"] == {
        "check": {"enabled": True, "source": "stored", "mutable": True},
        "apply": {"enabled": False, "source": "default", "mutable": True},
        "restart": {"enabled": False, "source": "default", "mutable": True},
        "status_fetch": {"enabled": False, "source": "default",
                         "mutable": True}}


def test_a_present_but_malformed_key_corrupts_the_whole_sidecar(tmp_path,
                                                                monkeypatch):
    """All-or-nothing: `apply_enabled: 1` is not one bad key, it is an
    untrustworthy file. Even the check -- whose own key here is a perfectly
    fine bool -- fails closed, because half-trusting a damaged consent file
    is how a permission gets granted by accident."""
    path = tmp_path / "webterm_update_policy.json"
    path.write_text(json.dumps({"check_enabled": True, "apply_enabled": 1}),
                    encoding="utf-8")
    app = _make_app(tmp_path, monkeypatch, update_policy_path=str(path))
    verdicts = [
        (app.ctx.update_check_enabled, app.ctx.update_policy_source),
        (app.ctx.update_apply_enabled, app.ctx.update_apply_source),
        (app.ctx.restart_enabled, app.ctx.restart_source)]
    assert verdicts == [(False, "corrupt")] * 3, verdicts


def test_a_corrupt_sidecar_fails_every_gate_closed(tmp_path, monkeypatch):
    """The check's fail-closed rule, extended verbatim to its two siblings:
    unreadable is never "absent", for any of the three gates."""
    path = tmp_path / "webterm_update_policy.json"
    path.write_text("{ not json", encoding="utf-8")
    app = _make_app(tmp_path, monkeypatch, update_policy_path=str(path))
    verdicts = [
        (app.ctx.update_check_enabled, app.ctx.update_policy_source),
        (app.ctx.update_apply_enabled, app.ctx.update_apply_source),
        (app.ctx.restart_enabled, app.ctx.restart_source)]
    assert verdicts == [(False, "corrupt")] * 3, verdicts


def test_a_config_key_still_wins_over_a_corrupt_sidecar(tmp_path, monkeypatch):
    """Corruption fails closed only for UNPINNED gates. A present config key
    decides its gate whatever state the sidecar is in -- restart comes up TRUE
    here, from the operator's own file -- while apply, unpinned, stays
    corrupt-closed beside it."""
    path = tmp_path / "webterm_update_policy.json"
    path.write_text("<<<garbage>>>", encoding="utf-8")
    app = _make_app(tmp_path, monkeypatch, update_policy_path=str(path),
                    restart_enabled=True)
    assert app.ctx.restart_enabled is True
    assert app.ctx.restart_source == "config"
    assert app.ctx.update_apply_enabled is False
    assert app.ctx.update_apply_source == "corrupt"
    assert app.ctx.update_check_enabled is False
    assert app.ctx.update_policy_source == "corrupt"


def test_restart_resolves_from_the_sidecar_after_state_path_is_known(
        tmp_path, monkeypatch):
    """The boot-order move: restart_enabled used to be assigned long before
    state_path was resolved, where a sidecar grant could not possibly be seen.
    A custom state dir whose sidecar grants restart proves the resolution now
    runs after the path is known -- and the sidecar's own check:false is
    honoured beside the grant, so this is per-key reading, not a blanket."""
    custom = tmp_path / "elsewhere"
    custom.mkdir()
    (custom / "webterm_update_policy.json").write_text(
        json.dumps({"check_enabled": False, "restart_enabled": True}),
        encoding="utf-8")
    app = _make_app(tmp_path, monkeypatch,
                    state_path=str(custom / "webterm_state.json"))
    assert app.ctx.restart_enabled is True
    assert app.ctx.restart_source == "stored"
    assert app.ctx.update_check_enabled is False
    assert app.ctx.update_policy_source == "stored"


def test_enabling_the_check_via_config_does_not_enable_apply(
        tmp_path, monkeypatch):
    """Independence, direction 2: `update_check_enabled: true` in the config,
    with `update_apply_enabled` absent, must leave the apply gate off. This is
    the exact coupling bug the atom exists to rule out."""
    app = _make_app(tmp_path, monkeypatch, update_check_enabled=True)
    assert app.ctx.update_check_enabled is True
    assert app.ctx.update_apply_enabled is False


def test_info_reports_the_real_apply_gate(tmp_path, monkeypatch):
    off = _make_app(tmp_path, monkeypatch)
    _, r = authed(off).get("/info")
    assert r.json["update"]["apply_enabled"] is False

    on = _make_app(tmp_path / "b", monkeypatch, update_apply_enabled=True)
    _, r = authed(on).get("/info")
    assert r.json["update"]["apply_enabled"] is True
    # And still independent of the check on the wire, not just in ctx.
    both = _make_app(tmp_path / "c", monkeypatch, update_check_enabled=True)
    _, r = authed(both).get("/info")
    assert r.json["update"]["check_enabled"] is True
    assert r.json["update"]["apply_enabled"] is False


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


# ---- the multi-key write: one route, three gates, subset bodies -------------
#
# POST /update/policy grew from a single-key (check) write into the one wire
# for all three gates. The rules a subset write must hold: unknown keys are
# ignored, a body with NO recognized key keeps the historical bad_check_enabled
# answer, types answer before ownership (first offender in fixed
# check/apply/restart order), a config-owned REQUESTED key locks the whole
# write and is named in `locked`, and the merge under the lock preserves every
# sibling the body did not mention -- synthesizing check_enabled: False (never
# the config's value) when nothing else supplies the one key the loader
# demands.

SHA = "a" * 40
TARGET = "b" * 40


def _apply_snap(**over):
    """A snapshot that passes every apply precondition unless a test flips a
    field -- the injected-verdict idiom test_update_apply.py drives the route
    with, so nothing here touches the real checkout or the network."""
    base = dict(local_sha=SHA, dirty=False, check_state=update.STATE_BEHIND,
                target_sha=TARGET, ahead_by=0, behind_by=5,
                restart_available=True, restart_reason=None,
                apply_enabled=True, writable=True, dependency_delta=None)
    base.update(over)
    return update.ApplySnapshot(**base)


def _stub_apply_machinery(monkeypatch, snap):
    """collect_apply_snapshot answers ``snap`` verbatim; perform_apply raises
    if the route ever reaches the git phase, so no test below can mutate the
    real checkout even by accident."""
    monkeypatch.setattr(update, "collect_apply_snapshot", lambda **kw: snap)

    def explode(**kwargs):
        raise AssertionError("the git phase was reached when it must not be")

    monkeypatch.setattr(update, "perform_apply", explode)


def test_one_request_can_grant_all_three_gates(tmp_path, monkeypatch):
    _no_network(monkeypatch)
    app = _make_app(tmp_path, monkeypatch)
    _, r = authed(app).post("/update/policy", json={
        "check_enabled": True, "apply_enabled": True, "restart_enabled": True})
    assert r.status == 200, r.json
    policy = r.json["update"]["policy"]
    for gate in ("check", "apply", "restart"):
        assert policy[gate] == {"enabled": True, "source": "stored",
                                "mutable": True}, gate
    stored = json.loads(_sidecar(app).read_text(encoding="utf-8"))
    assert stored == {"check_enabled": True, "apply_enabled": True,
                      "restart_enabled": True}


def test_a_subset_write_leaves_the_other_gates_alone(tmp_path, monkeypatch):
    """The merge is re-read from disk under the lock, so revoking ONE gate
    must leave the other two exactly as their grants were written."""
    _no_network(monkeypatch)
    app = _make_app(tmp_path, monkeypatch)
    client = authed(app)
    _, r = client.post("/update/policy", json={
        "check_enabled": True, "apply_enabled": True, "restart_enabled": True})
    assert r.status == 200
    _, r = client.post("/update/policy", json={"apply_enabled": False})
    assert r.status == 200, r.json
    stored = json.loads(_sidecar(app).read_text(encoding="utf-8"))
    assert stored == {"check_enabled": True, "apply_enabled": False,
                      "restart_enabled": True}
    policy = r.json["update"]["policy"]
    assert policy["apply"] == {"enabled": False, "source": "stored",
                               "mutable": True}
    assert policy["check"] == {"enabled": True, "source": "stored",
                               "mutable": True}
    assert policy["restart"] == {"enabled": True, "source": "stored",
                                 "mutable": True}


def test_a_config_owned_key_locks_the_whole_write_and_names_it(tmp_path,
                                                               monkeypatch):
    """All-or-nothing: one requested key the config owns refuses the WHOLE
    write -- a partial landing would tell the client half its request holds
    -- and `locked` names exactly which keys, so the UI can name the file."""
    _no_network(monkeypatch)
    app = _make_app(tmp_path, monkeypatch, restart_enabled=True)
    _, r = authed(app).post("/update/policy", json={
        "apply_enabled": True, "restart_enabled": True})
    assert r.status == 409, r.json
    assert r.json["error"] == "policy_locked"
    assert r.json["source"] == "config"
    assert r.json["locked"] == ["restart_enabled"]
    assert app.ctx.update_apply_enabled is False, \
        "the grantable half of a locked write must not land"
    assert not _sidecar(app).exists()
    # Ownership answers before idempotence: asking the config-owned key for
    # the very value it already has still 409s -- the setting is not this
    # interface's to confirm, either.
    _, r = authed(app).post("/update/policy", json={"restart_enabled": True})
    assert r.status == 409, r.json
    assert r.json["locked"] == ["restart_enabled"]


def test_only_a_real_bool_is_accepted_for_each_key(tmp_path, monkeypatch):
    """Each gate has its own 400 code, and mixed offenders answer the FIRST
    in the fixed check/apply/restart order -- deterministic, never dict-order
    luck."""
    _no_network(monkeypatch)
    app = _make_app(tmp_path, monkeypatch)
    client = authed(app)
    _, r = client.post("/update/policy", json={"apply_enabled": 1})
    assert (r.status, r.json["error"]) == (400, "bad_apply_enabled")
    _, r = client.post("/update/policy",
                       json={"check_enabled": True, "restart_enabled": "true"})
    assert (r.status, r.json["error"]) == (400, "bad_restart_enabled")
    _, r = client.post("/update/policy",
                       json={"check_enabled": 1, "restart_enabled": "x"})
    assert (r.status, r.json["error"]) == (400, "bad_check_enabled")
    # Nothing moved for any of them: the well-formed half of a half-bad body
    # must not land.
    assert app.ctx.update_check_enabled is False
    assert not _sidecar(app).exists()


def test_an_empty_body_still_answers_bad_check_enabled(tmp_path, monkeypatch):
    """The historical empty-body answer, kept deliberately: {} and a body of
    only unknown keys carry no recognized key, and an old client that knows
    only check_enabled keeps seeing the error it always saw."""
    _no_network(monkeypatch)
    app = _make_app(tmp_path, monkeypatch)
    for body in ({}, {"unknown_key": True}):
        _, r = authed(app).post("/update/policy", json=body)
        assert r.status == 400, body
        assert r.json["error"] == "bad_check_enabled", body
    assert not _sidecar(app).exists()


def test_a_grant_of_apply_alone_records_check_as_off_not_as_the_config_value(
        tmp_path, monkeypatch):
    """The no-minting rule. The loader demands check_enabled be present, so a
    write that carries no check key synthesizes False -- NEVER the config's
    live True, because deleting the config key later must not uncover a
    stored check grant nobody clicked."""
    _no_network(monkeypatch)
    app = _make_app(tmp_path, monkeypatch, update_check_enabled=True)
    _, r = authed(app).post("/update/policy", json={"apply_enabled": True})
    assert r.status == 200, r.json
    stored = json.loads(_sidecar(app).read_text(encoding="utf-8"))
    assert stored == {"check_enabled": False, "apply_enabled": True}
    # The view still shows the check enabled, owned by the config: the
    # sidecar's synthesized False is layered UNDER a present config key.
    policy = r.json["update"]["policy"]
    assert policy["check"] == {"enabled": True, "source": "config",
                               "mutable": False}
    assert policy["apply"] == {"enabled": True, "source": "stored",
                               "mutable": True}


def test_a_stored_three_gate_grant_outlives_the_process(tmp_path, monkeypatch):
    _no_network(monkeypatch)
    app = _make_app(tmp_path, monkeypatch)
    _, r = authed(app).post("/update/policy", json={
        "check_enabled": True, "apply_enabled": True, "restart_enabled": True})
    assert r.status == 200
    fresh = _make_app(tmp_path, monkeypatch)
    assert fresh.ctx.update_check_enabled is True
    assert fresh.ctx.update_policy_source == "stored"
    assert fresh.ctx.update_apply_enabled is True
    assert fresh.ctx.update_apply_source == "stored"
    assert fresh.ctx.restart_enabled is True
    assert fresh.ctx.restart_source == "stored"


def test_a_repeated_grant_performs_no_second_write(tmp_path, monkeypatch):
    """The multi-key no-op proof: a second identical POST -- and a subset
    asking for what already holds -- answers ok without touching the disk.
    Counted, not mtime'd, same as the single-key idempotency test."""
    _no_network(monkeypatch)
    app = _make_app(tmp_path, monkeypatch)
    client = authed(app)
    everything = {"check_enabled": True, "apply_enabled": True,
                  "restart_enabled": True}
    _, r = client.post("/update/policy", json=everything)
    assert r.status == 200
    writes = []
    real = broker_app._write_state_atomic

    def counting(path, state):
        writes.append(dict(state))
        return real(path, state)

    monkeypatch.setattr(broker_app, "_write_state_atomic", counting)
    _, r = client.post("/update/policy", json=everything)
    assert r.status == 200
    _, r = client.post("/update/policy", json={"apply_enabled": True})
    assert r.status == 200
    assert writes == [], \
        f"re-asserting the on-disk state rewrote the file: {writes}"
    # ...and a genuine change still writes, once, preserving its siblings.
    _, r = client.post("/update/policy", json={"apply_enabled": False})
    assert r.status == 200
    assert writes == [{"check_enabled": True, "apply_enabled": False,
                       "restart_enabled": True}]


def test_a_policy_revoke_of_restart_flips_restart_status_live(tmp_path,
                                                              monkeypatch):
    """restart_status reads app.ctx per call, so a stored-grant revoke lands
    on the very next /info -- same process, no reboot anywhere."""
    _no_network(monkeypatch)
    app = _make_app(tmp_path, monkeypatch)
    client = authed(app)
    _, r = client.post("/update/policy", json={"restart_enabled": True})
    assert r.status == 200
    _, r = client.get("/info")
    assert r.json["restart"]["configured"] is True
    assert r.json["restart"]["reason_code"] != \
        broker_app.REASON_RESTART_DISABLED

    _, r = client.post("/update/policy", json={"restart_enabled": False})
    assert r.status == 200
    # The flip IS the ctx assignment: provable without another request.
    assert app.ctx.restart_enabled is False
    assert app.ctx.restart_source == "stored"
    _, r = client.get("/info")
    block = r.json["restart"]
    assert block["configured"] is False
    assert block["available"] is False
    assert block["reason_code"] == broker_app.REASON_RESTART_DISABLED
    assert r.json["update"]["policy"]["restart"] == {
        "enabled": False, "source": "stored", "mutable": True}


def test_a_stored_grant_opens_the_real_apply_gate(tmp_path, monkeypatch):
    """The positive integration proof: with no config key the apply route
    refuses at the gate; a grant via THIS route gets the SAME request past
    that refusal, into the controlled precondition refusal the stubbed
    snapshot arranges. perform_apply raises if reached, so nothing real can
    happen past the seam."""
    _no_network(monkeypatch)
    _stub_apply_machinery(monkeypatch, _apply_snap(dirty=True))
    app = _make_app(tmp_path, monkeypatch)
    client = authed(app)
    body = {"target_sha": TARGET}
    _, r = client.post("/update/apply", json=body)
    assert r.status == 503, r.json
    assert r.json["error"] == "update_apply_disabled"

    _, r = client.post("/update/policy", json={"apply_enabled": True})
    assert r.status == 200

    _, r = client.post("/update/apply", json=body)
    assert r.status == 409, r.json
    assert r.json["error"] == "apply_refused", \
        "the stored grant must reach the real gate, not just the view"
    assert r.json["reason_codes"] == [update.APPLY_DIRTY_TREE]


def test_a_policy_revoke_beats_an_apply_already_queued_on_the_lock(
        tmp_path, monkeypatch):
    """The apply twin of the queued-check revoke above, driven end to end
    through the real routes: an apply that claimed its operation and queued
    on update_lock is refused at admission when a POST /update/policy revoke
    (which takes update_policy_lock, NOT update_lock, so it can land while
    the apply waits) got there first. The control arm runs the SAME setup
    without the revoke and gets PAST the gate to the controlled precondition
    refusal -- without it, a gate that refuses everyone would pass the revoke
    arm vacuously."""
    _no_network(monkeypatch)
    _stub_apply_machinery(monkeypatch, _apply_snap(dirty=True))
    app = _make_app(tmp_path, monkeypatch)
    with authed_reusable(app) as client:
        apply_url = with_token(
            f"http://{client.host}:{client.port}/update/apply",
            app.ctx.auth_token)
        policy_url = with_token(
            f"http://{client.host}:{client.port}/update/policy",
            app.ctx.auth_token)
        _, r = client.post("/update/policy", json={"apply_enabled": True})
        assert r.status == 200

        async def _drive(revoke):
            # Hold update_lock so the apply below queues behind it, exactly
            # as it would behind a concurrent check's upstream request.
            await app.ctx.update_lock.acquire()
            req = asyncio.ensure_future(
                client._session.post(apply_url, json={"target_sha": TARGET}))
            for _ in range(500):
                if app.ctx.update_apply_claim is not None:
                    break
                await asyncio.sleep(0.01)
            assert app.ctx.update_apply_claim is not None, \
                "the apply never passed the top-of-handler gate"
            if revoke:
                resp = await client._session.post(
                    policy_url, json={"apply_enabled": False})
                assert resp.status_code == 200, resp.text
            app.ctx.update_lock.release()
            return await req

        control = client._loop.run_until_complete(_drive(revoke=False))
        assert control.status_code == 409, control.text
        assert control.json()["error"] == "apply_refused"
        assert update.APPLY_DIRTY_TREE in control.json()["reason_codes"]

        revoked = client._loop.run_until_complete(_drive(revoke=True))
        assert revoked.status_code == 503, revoked.text
        assert revoked.json()["error"] == "update_apply_disabled"
    assert app.ctx.update_apply_claim is None, \
        "a refused admission must release the claim"


# ---- the fourth key (#189): status_fetch_enabled on the same wire -----------
#
# GET /status/fetch's operator gate rides the SAME consent seam as the three
# update gates: same sidecar, same route, same per-key layering. What these
# tests pin is the write surface the enforcement atom deliberately left open:
# the key is writable with its direction NAMED in the body (absence means
# "leave it alone", never a direction to infer -- #187's lesson), a config pin
# locks it with the byte-same 409 shape its siblings answer, a malformed value
# gets its own bad_%s code, and a subset write no longer erases a stored
# status grant -- the fail-closed state loss the three-key write surface used
# to accept. Enforcement on the route itself stays in test_status_fetch_gate.


def test_the_status_key_grants_and_revokes_live(tmp_path, monkeypatch):
    """Direction NAMED both ways, converging on ctx in-request: the write's
    response already reflects the flip, and no restart sits in between."""
    _no_network(monkeypatch)
    app = _make_app(tmp_path, monkeypatch)
    assert app.ctx.status_fetch_enabled is False
    _, r = authed(app).post("/update/policy",
                            json={"status_fetch_enabled": True})
    assert r.status == 200, r.json
    assert r.json["update"]["policy"]["status_fetch"] == {
        "enabled": True, "source": "stored", "mutable": True}
    assert app.ctx.status_fetch_enabled is True
    assert app.ctx.status_fetch_source == "stored"
    # The loader demands check_enabled be present, so the write synthesizes
    # False -- never a value nobody clicked (the no-minting rule).
    stored = json.loads(_sidecar(app).read_text(encoding="utf-8"))
    assert stored == {"check_enabled": False, "status_fetch_enabled": True}
    # The revoke is a NAMED False, and it lands just as live.
    _, r = authed(app).post("/update/policy",
                            json={"status_fetch_enabled": False})
    assert r.status == 200
    assert app.ctx.status_fetch_enabled is False
    stored = json.loads(_sidecar(app).read_text(encoding="utf-8"))
    assert stored == {"check_enabled": False, "status_fetch_enabled": False}


def test_a_stored_status_grant_outlives_the_process(tmp_path, monkeypatch):
    _no_network(monkeypatch)
    app = _make_app(tmp_path, monkeypatch)
    _, r = authed(app).post("/update/policy",
                            json={"status_fetch_enabled": True})
    assert r.status == 200
    fresh = _make_app(tmp_path, monkeypatch)
    assert fresh.ctx.status_fetch_enabled is True
    assert fresh.ctx.status_fetch_source == "stored"


def test_a_config_pinned_status_gate_answers_policy_locked(tmp_path,
                                                           monkeypatch):
    """Byte-consistent with its siblings' refusal: 409 policy_locked naming
    the key, nothing moved -- and ownership answers even for the very value
    the gate already has."""
    _no_network(monkeypatch)
    app = _make_app(tmp_path, monkeypatch, status_fetch_enabled=False)
    _, r = authed(app).post("/update/policy",
                            json={"status_fetch_enabled": True})
    assert r.status == 409, r.json
    assert r.json["error"] == "policy_locked"
    assert r.json["source"] == "config"
    assert r.json["locked"] == ["status_fetch_enabled"]
    assert app.ctx.status_fetch_enabled is False
    assert not _sidecar(app).exists()
    _, r = authed(app).post("/update/policy",
                            json={"status_fetch_enabled": False})
    assert r.status == 409
    assert r.json["locked"] == ["status_fetch_enabled"]


def test_only_a_real_bool_is_accepted_for_the_status_key(tmp_path,
                                                         monkeypatch):
    """Its own bad_%s code, last in the fixed validation order."""
    _no_network(monkeypatch)
    app = _make_app(tmp_path, monkeypatch)
    _, r = authed(app).post("/update/policy",
                            json={"status_fetch_enabled": "true"})
    assert (r.status, r.json["error"]) == (400, "bad_status_fetch_enabled")
    # Mixed offenders still answer the FIRST in the fixed order, so the new
    # key sits at the END: a malformed sibling ahead of it answers.
    _, r = authed(app).post("/update/policy",
                            json={"restart_enabled": 1,
                                  "status_fetch_enabled": "x"})
    assert (r.status, r.json["error"]) == (400, "bad_restart_enabled")
    assert app.ctx.status_fetch_enabled is False
    assert not _sidecar(app).exists()


def test_a_subset_write_no_longer_erases_a_stored_status_grant(
        tmp_path, monkeypatch):
    """THE state-loss fix this atom exists for. Before the write surface grew
    the fourth key, any policy write rewrote the sidecar WITHOUT a stored
    status_fetch_enabled -- and did not re-resolve the ctx, so the file and
    the live gate silently disagreed until the next boot."""
    _no_network(monkeypatch)
    path = tmp_path / "webterm_update_policy.json"
    path.write_text(json.dumps({"check_enabled": False,
                                "status_fetch_enabled": True}),
                    encoding="utf-8")
    app = _make_app(tmp_path, monkeypatch)
    assert app.ctx.status_fetch_enabled is True
    assert app.ctx.status_fetch_source == "stored"
    _, r = authed(app).post("/update/policy", json={"apply_enabled": True})
    assert r.status == 200, r.json
    stored = json.loads(_sidecar(app).read_text(encoding="utf-8"))
    assert stored == {"check_enabled": False, "apply_enabled": True,
                      "status_fetch_enabled": True}
    # The gate an ABSENT key left alone is exactly where it was: absence is
    # "leave it", never a direction (#187).
    assert app.ctx.status_fetch_enabled is True
    assert app.ctx.status_fetch_source == "stored"
