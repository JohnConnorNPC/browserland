"""The status_fetch_enabled operator gate on GET /status/fetch (#189).

/status/fetch is the broker's first egress route, and until this gate it was
token-only: any token holder could make the broker dial four provider hosts
with no operator consent -- the aistatus mod shipping default-off is cosmetic
as egress protection (#182, on the record for exactly this route). The gate
clones #182's three-gate machinery for a fourth key, default OFF.

What this file pins, clause by clause:

* **Default is off, and off means ZERO upstream calls** -- 503
  ``status_fetch_disabled`` with an instrumented fetch counter at zero, on a
  COLD cache and on a WARM one. Cache is not consent: a warm row served while
  disabled would misreport this broker's egress posture.
* **A config key that is present wins, both directions.** ``false`` in
  broker_config beats a sidecar grant (edit-config-and-restart is the
  emergency response to unwanted egress and must keep working), and ``true``
  survives a corrupt sidecar.
* **A corrupt sidecar fails CLOSED** -- unreadable JSON, and a non-bool
  ``status_fetch_enabled`` (one malformed key condemns the whole file).
* **A revoke beats a worker already queued** -- the re-check inside
  ``_status_one``, before the cache lookup and before the executor dispatch.
  Revocation stays prospective: a fetch already handed to the executor
  completes; no new one starts.
* **The refusal ladder is 401 (token) -> 503 (gate) -> 400 (shape)** -- auth
  first, capability second, request shape last, matching /update/check.

No network anywhere: the module-level _fetch_status_blocking is replaced with
a counting fake (``_status_one`` resolves it as a module global at call time).
The proxy's own behavior with the gate OPEN (allowlist, normalization, cache,
CORS) stays in test_status_fetch.py.
"""

from __future__ import annotations

import asyncio
import json

from .auth_helpers import TEST_TOKEN, authed
from webterm.broker import app as broker_app
from webterm.broker.app import (STATUS_ALLOWLIST, _load_update_policy,
                                create_app)


_app_seq = 0


def _make_app(tmp_path, monkeypatch, token=None, **cfg_extra):
    """A broker whose state and consent sidecar land in tmp_path.
    ``status_fetch_enabled`` is deliberately ABSENT unless a test passes it:
    absence is the shipped posture, and absence is what the default-off
    assertions are about."""
    global _app_seq
    _app_seq += 1
    monkeypatch.delenv("WEB_TERMINAL_TOKEN", raising=False)
    tmp_path.mkdir(parents=True, exist_ok=True)
    cfg = {"state_path": str(tmp_path / "webterm_state.json"),
           "auth_token": token or TEST_TOKEN}
    cfg.update(cfg_extra)
    return create_app(cfg, name=f"webterm-status-gate-{_app_seq}")


def _patch_fetch(monkeypatch):
    """Replace the blocking upstream fetch with a no-network fake that records
    each call -- the instrument behind every zero-egress assertion here."""
    calls = []

    def fake(pid):
        calls.append(pid)
        return {"id": pid, "label": STATUS_ALLOWLIST[pid]["label"],
                "indicator": "none", "description": "All Systems Operational",
                "incidents": [], "components": []}

    monkeypatch.setattr(broker_app, "_fetch_status_blocking", fake)
    return calls


def _sidecar(tmp_path, **gates):
    """A readable consent sidecar carrying the given gates. check_enabled is
    mandatory in the file format (its absence reads as corruption), so it is
    always present -- False unless a test says otherwise."""
    path = tmp_path / "webterm_update_policy.json"
    doc = {"check_enabled": False}
    doc.update(gates)
    path.write_text(json.dumps(doc), encoding="utf-8")
    return path


# ---- default off: 503, zero egress, cold and warm ---------------------------

def test_default_is_off_and_makes_zero_upstream_calls_cold(tmp_path,
                                                           monkeypatch):
    calls = _patch_fetch(monkeypatch)
    app = _make_app(tmp_path, monkeypatch)
    assert app.ctx.status_fetch_enabled is False
    assert app.ctx.status_fetch_source == "default"
    _, r = authed(app).get("/status/fetch")
    assert r.status == 503
    assert r.json == {"ok": False, "error": "status_fetch_disabled"}
    # A cached 503 would hide the next-poll recovery a grant promises.
    assert r.headers.get("Cache-Control") == "no-store"
    assert calls == [], "a disabled broker made an upstream status call"
    # The refusal sits before the cache: nothing was fetched, nothing stored.
    assert app.ctx.status_cache == {}


def test_gate_off_refuses_on_a_warm_cache_too(tmp_path, monkeypatch):
    """Cache is not consent. A row cached while the gate was open must not be
    served after a revoke -- the 503 comes first, matching /update/check."""
    calls = _patch_fetch(monkeypatch)
    _sidecar(tmp_path, status_fetch_enabled=True)
    app = _make_app(tmp_path, monkeypatch)
    assert app.ctx.status_fetch_source == "stored"
    _, r = authed(app).get("/status/fetch?provider=anthropic")
    assert r.status == 200
    assert calls == ["anthropic"]            # the cache is now warm
    # The revoke lands live. Flipped directly on ctx ON PURPOSE even though
    # the consent wire exists now (#189 A6): this pins the ROUTE's per-request
    # ctx read independent of how the flip arrives. The wire's own
    # convergence is pinned in the view-and-wire section below.
    app.ctx.status_fetch_enabled = False
    _, r = authed(app).get("/status/fetch?provider=anthropic")
    assert r.status == 503
    assert r.json["error"] == "status_fetch_disabled"
    assert calls == ["anthropic"], "the warm-cache refusal still dialed out"


# ---- who decides: config presence, sidecar, corruption ----------------------

def test_config_false_beats_a_sidecar_grant(tmp_path, monkeypatch):
    """The emergency stop: an operator who wrote false and restarted must not
    find a stored grant had quietly overridden it."""
    calls = _patch_fetch(monkeypatch)
    _sidecar(tmp_path, status_fetch_enabled=True)
    app = _make_app(tmp_path, monkeypatch, status_fetch_enabled=False)
    assert app.ctx.status_fetch_enabled is False
    assert app.ctx.status_fetch_source == "config"
    _, r = authed(app).get("/status/fetch")
    assert r.status == 503
    assert r.json["error"] == "status_fetch_disabled"
    assert calls == []


def test_config_true_survives_a_corrupt_sidecar(tmp_path, monkeypatch):
    """A present config key decides its gate whatever state the sidecar is in;
    corruption fails only UNPINNED gates closed."""
    _patch_fetch(monkeypatch)
    (tmp_path / "webterm_update_policy.json").write_text("{not json",
                                                         encoding="utf-8")
    app = _make_app(tmp_path, monkeypatch, status_fetch_enabled=True)
    assert app.ctx.status_fetch_enabled is True
    assert app.ctx.status_fetch_source == "config"


def test_corrupt_sidecar_fails_the_gate_closed(tmp_path, monkeypatch):
    calls = _patch_fetch(monkeypatch)
    (tmp_path / "webterm_update_policy.json").write_text("{not json",
                                                         encoding="utf-8")
    app = _make_app(tmp_path, monkeypatch)
    assert app.ctx.status_fetch_enabled is False
    assert app.ctx.status_fetch_source == "corrupt"
    _, r = authed(app).get("/status/fetch")
    assert r.status == 503
    assert calls == []


def test_a_non_bool_stored_grant_condemns_the_whole_file(tmp_path,
                                                         monkeypatch):
    """"true" is a string and strings are truthy -- the worst possible place
    to coerce. One malformed key makes the whole sidecar corrupt, so even its
    well-formed siblings stay off."""
    _patch_fetch(monkeypatch)
    _sidecar(tmp_path, status_fetch_enabled="true")
    app = _make_app(tmp_path, monkeypatch)
    assert app.ctx.status_fetch_enabled is False
    assert app.ctx.status_fetch_source == "corrupt"


def test_loader_recognizes_the_fourth_key(tmp_path):
    path = _sidecar(tmp_path, status_fetch_enabled=True)
    stored, corrupt = _load_update_policy(path)
    assert corrupt is False
    assert stored["status_fetch_enabled"] is True
    # ...and truly unknown keys are still ignored, not corruption: a future
    # field must not brick the file on this build.
    path.write_text(json.dumps({"check_enabled": False,
                                "some_future_gate": "whatever"}),
                    encoding="utf-8")
    stored, corrupt = _load_update_policy(path)
    assert corrupt is False
    assert "some_future_gate" not in stored


# ---- gate on: the route works, from either source ---------------------------

def test_config_true_opens_the_gate(tmp_path, monkeypatch):
    calls = _patch_fetch(monkeypatch)
    app = _make_app(tmp_path, monkeypatch, status_fetch_enabled=True)
    assert app.ctx.status_fetch_source == "config"
    _, r = authed(app).get("/status/fetch")
    assert r.status == 200
    assert r.json["ok"] is True
    assert [p["id"] for p in r.json["providers"]] == list(STATUS_ALLOWLIST)
    # The response order above is deterministic (gather order); the CALLS run
    # on executor threads, so their order is not -- compare as a multiset.
    assert sorted(calls) == sorted(STATUS_ALLOWLIST)
    # Live-policy answers must never outlive the policy in an HTTP cache.
    assert r.headers.get("Cache-Control") == "no-store"


def test_a_stored_sidecar_grant_opens_the_gate(tmp_path, monkeypatch):
    """The GUI grant path: config-file-only fails fleets (#182), so a stored
    real-bool grant in the consent sidecar must open the gate at boot."""
    _patch_fetch(monkeypatch)
    _sidecar(tmp_path, status_fetch_enabled=True)
    app = _make_app(tmp_path, monkeypatch)
    assert app.ctx.status_fetch_enabled is True
    assert app.ctx.status_fetch_source == "stored"
    _, r = authed(app).get("/status/fetch?provider=openai")
    assert r.status == 200
    assert [p["id"] for p in r.json["providers"]] == ["openai"]


# ---- revoke while queued: the _status_one re-check --------------------------

def test_a_revoke_beats_a_worker_already_queued(tmp_path, monkeypatch):
    """Driven through app.ctx.status_fetch_one rather than the HTTP client on
    purpose -- the test client serializes requests, which would pass this test
    without the re-check. The cache for the pid is deliberately WARM: if the
    re-check ever slipped behind the cache lookup, this would catch the warm
    row leaking past a revoke."""
    calls = _patch_fetch(monkeypatch)
    _sidecar(tmp_path, status_fetch_enabled=True)
    app = _make_app(tmp_path, monkeypatch)
    assert app.ctx.status_fetch_enabled is True

    async def drive():
        loop = asyncio.get_running_loop()
        app.ctx.status_cache["anthropic"] = {
            "at": loop.time(),
            "data": {"id": "anthropic", "label": "Anthropic",
                     "indicator": "none", "description": "",
                     "incidents": [], "components": []}}
        # The revoke lands after admission, before the worker runs its turn.
        app.ctx.status_fetch_enabled = False
        return await app.ctx.status_fetch_one("anthropic")

    result = asyncio.run(drive())
    assert result is None, "a revoked worker must not return a result"
    assert calls == [], (
        "the revoke was granted and then the forbidden request was made anyway")


class _RevokingCache(dict):
    """A status_cache whose first get() flips the gate off -- the in-loop,
    deterministic stand-in for a revoke landing between the handler's
    admission check and a sibling worker's turn. It runs on the event loop
    thread, so the interleaving is fixed: worker 1 passes its gate check,
    fires the revoke on its cache lookup and dispatches its (already-admitted)
    fetch; worker 2 then runs its gate check and must refuse."""

    def __init__(self, app):
        super().__init__()
        self._app = app
        self._fired = False

    def get(self, key, default=None):
        if not self._fired:
            self._fired = True
            self._app.ctx.status_fetch_enabled = False
        return super().get(key, default)


def test_a_mid_request_revoke_answers_503_not_partial_results(tmp_path,
                                                              monkeypatch):
    """End to end through the route: revocation is prospective (worker 1's
    already-dispatched fetch completes), but no NEW upstream call starts and
    the response is the same 503 the admission check gives -- never a partial
    provider list fetched half before, half after the revoke."""
    calls = _patch_fetch(monkeypatch)
    _sidecar(tmp_path, status_fetch_enabled=True)
    app = _make_app(tmp_path, monkeypatch)
    app.ctx.status_cache = _RevokingCache(app)
    _, r = authed(app).get("/status/fetch?provider=anthropic,openai")
    assert r.status == 503
    assert r.json["error"] == "status_fetch_disabled"
    assert calls == ["anthropic"], (
        "worker 2 dialed out after the revoke it was required to see")


# ---- the ladder: 401 (token) -> 503 (gate) -> 400 (shape) -------------------

def test_the_ladder_is_token_then_gate_then_shape(tmp_path, monkeypatch):
    calls = _patch_fetch(monkeypatch)
    app = _make_app(tmp_path, monkeypatch, token="sekrit")
    # Auth answers first, gate off or not. RAW client on purpose: authed()
    # would append the very token these need to be missing/wrong.
    _, r = app.test_client.get("/status/fetch")
    assert r.status == 401
    _, r = app.test_client.get("/status/fetch?token=nope")
    assert r.status == 401
    # Capability answers second -- even for a request whose provider list is
    # entirely invalid. A disabled broker does not discuss request shape.
    _, r = authed(app).get("/status/fetch?provider=evil")
    assert r.status == 503
    assert r.json["error"] == "status_fetch_disabled"
    assert calls == []
    # Shape answers last, once the capability exists.
    app.ctx.status_fetch_enabled = True
    _, r = authed(app).get("/status/fetch?provider=evil")
    assert r.status == 400
    assert r.json["error"] == "no_valid_provider"
    _, r = authed(app).get("/status/fetch?provider=anthropic")
    assert r.status == 200
    assert calls == ["anthropic"]


def test_preflight_is_not_gated(tmp_path, monkeypatch):
    # A CORS preflight is not egress, and it must keep answering with the
    # gate off or the browser could never even LEARN the 503.
    app = _make_app(tmp_path, monkeypatch, token="sekrit")
    assert app.ctx.status_fetch_enabled is False
    _, r = authed(app).options("/status/fetch")
    assert r.status == 204
    assert r.headers.get("Access-Control-Allow-Origin") == "*"


# ---- the view and the wire (#189, atom A6) ----------------------------------

def test_info_publishes_the_gate_view_with_source(tmp_path, monkeypatch):
    """The feature-detect contract: `update.policy.status_fetch` exists ONLY
    on a build that gates the route, so a client reads its absence as "old
    build, the route works ungated" and never probes (#157). Same _gate_view
    shape as the three update gates it sits beside."""
    _patch_fetch(monkeypatch)
    app = _make_app(tmp_path, monkeypatch)
    _, r = authed(app).get("/info")
    assert r.json["update"]["policy"]["status_fetch"] == {
        "enabled": False, "source": "default", "mutable": True}
    # A present config key owns the gate and says so: immutable in the view,
    # whichever direction it points.
    pinned = _make_app(tmp_path / "b", monkeypatch, status_fetch_enabled=True)
    _, r = authed(pinned).get("/info")
    assert r.json["update"]["policy"]["status_fetch"] == {
        "enabled": True, "source": "config", "mutable": False}


def test_a_policy_grant_converges_the_route_with_no_restart(tmp_path,
                                                            monkeypatch):
    """End to end on one process: 503, grant via POST /update/policy with the
    direction NAMED, and the very next /status/fetch dials out -- then the
    NAMED revoke refuses the next request even on the now-warm cache."""
    calls = _patch_fetch(monkeypatch)
    app = _make_app(tmp_path, monkeypatch)
    _, r = authed(app).get("/status/fetch?provider=anthropic")
    assert r.status == 503
    assert calls == []

    _, w = authed(app).post("/update/policy",
                            json={"status_fetch_enabled": True})
    assert w.status == 200, w.json
    assert w.json["update"]["policy"]["status_fetch"] == {
        "enabled": True, "source": "stored", "mutable": True}

    _, r = authed(app).get("/status/fetch?provider=anthropic")
    assert r.status == 200
    assert calls == ["anthropic"]

    _, w = authed(app).post("/update/policy",
                            json={"status_fetch_enabled": False})
    assert w.status == 200
    _, r = authed(app).get("/status/fetch?provider=anthropic")
    assert r.status == 503
    assert r.json["error"] == "status_fetch_disabled"
    assert calls == ["anthropic"], "the revoked route still dialed out"
