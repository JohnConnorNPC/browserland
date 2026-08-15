"""The operator gate, the drain state machine, and the worker half of the
restart intent (#183).

Three things are pinned here, and they are pinned separately because they fail
separately:

  * **the gate** (``restart_enabled``) defaults OFF and resolves like the
    update gates: a PRESENT config key owns it outright, else a stored grant
    in the update-policy sidecar, else off. It is NOT an authentication
    question. Holding the browser token already means shell-level access to
    the box, so gating a restart on "is this caller logged in" would give
    every logged-in session the power to bounce a broker that is hosting
    other people's live terminals.

  * **the drain**: while it runs, the three entry points that CREATE new work
    (``/file/upload_begin``, ``/recording/begin``, ``/launch``) are refused,
    the in-flight shielded critical sections are awaited to a BOUNDED deadline,
    and whatever is left incomplete is aborted *and named* rather than silently
    unlinked by ``before_server_stop`` a moment later.

  * **the intent**: ``supervise.arm_restart`` is reached only by a drain that
    succeeded, and a False from it stops the whole thing — because with no
    supervisor to honour exit 75, exiting 75 does not restart the broker, it
    ends it.

The in-flight cases are driven the way ``test_shielded_writes.py`` drives them:
``ReusableClient`` (one server across requests) plus a ``threading.Event``
handshake, so the drain always starts while the worker hop is genuinely inside
the critical section rather than at some sleep-guessed moment.
"""

import asyncio
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

import webterm.broker.app as app_mod
from webterm.broker import supervise
from webterm.broker.app import create_app
from .auth_helpers import TEST_TOKEN, authed, authed_reusable, with_token

_app_seq = 0


def _make_app(tmp_path, monkeypatch, **cfg):
    global _app_seq
    _app_seq += 1
    monkeypatch.delenv("WEB_TERMINAL_TOKEN", raising=False)
    base = {"auth_token": TEST_TOKEN,
            "editor_root": str(tmp_path),
            "state_path": str(tmp_path / "webterm_state.json"),
            "recordings_dir": str(tmp_path / "recs"),
            # The post-boot cooldown (#183, R6) defaults to 90s, which would
            # make every fresh test app refuse a restart for its whole life.
            # Pinned OFF here; the cooldown's own tests below override this
            # key (base.update(cfg) lets them) and patch BOOT_TIME.
            "restart_cooldown_seconds": 0}
    base.update(cfg)
    return create_app(base, name=f"webterm-restart-test-{_app_seq}")


def _no_supervisor(monkeypatch):
    """Scrub the three supervisor variables. The test process inherits whatever
    the developer's shell has, and a stray BROWSERLAND_RUN_DIR would let the
    'no supervisor' tests arm a real sentinel."""
    for name in (supervise.ENV_RUN_DIR, supervise.ENV_SUPERVISOR_NONCE,
                 supervise.ENV_SUPERVISOR_PID):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture(autouse=True)
def _pinned_capability(monkeypatch):
    """Pin the PROCESS capability memo to "nothing supervises us", for every
    test in this file.

    The memo is per process by design — it describes this interpreter, and
    ``worker_capability`` can block for 5 s on ``systemctl show``, so probing it
    per app (let alone per request) is not on the table. That design has one
    cost in a test suite: the answer would otherwise depend on the developer's
    shell (a broker-launched terminal inherits the three variables) and on which
    test happened to create the first app. Pinning it here makes every test
    below start from the same known-unsupported state; the ones that need
    another mechanism say so explicitly with ``_capability``."""
    monkeypatch.setattr(app_mod, "_RESTART_CAPABILITY",
                        {"supported": False,
                         "mechanism": supervise.MECHANISM_NONE,
                         "reason_code": supervise.REASON_NO_SUPERVISOR})


def _capability(app, mechanism, reason_code=None):
    """Pin ONE app's view of the process capability.

    ``app.ctx.restart_capability`` is the documented override seam: it exists so
    the systemd branch — which cannot be reached on a Windows box or on a CI
    runner without systemd — is testable at all."""
    app.ctx.restart_capability = {
        "supported": mechanism != supervise.MECHANISM_NONE,
        "mechanism": mechanism, "reason_code": reason_code}
    return app


# ---- E14: the operator gate -------------------------------------------------

def test_the_gate_defaults_to_off(tmp_path, monkeypatch):
    """Absent config must mean "this broker will not restart itself"."""
    app = _make_app(tmp_path, monkeypatch)
    assert app.ctx.restart_enabled is False


def test_the_gate_is_read_from_config(tmp_path, monkeypatch):
    app = _make_app(tmp_path, monkeypatch, restart_enabled=True)
    assert app.ctx.restart_enabled is True
    # bool(), like update_check_enabled: a config hand-edited to a string or a
    # number resolves to a definite yes/no rather than a truthy surprise.
    app = _make_app(tmp_path, monkeypatch, restart_enabled=0)
    assert app.ctx.restart_enabled is False


def test_the_gate_is_independent_of_authentication(tmp_path, monkeypatch):
    """Being logged in is not the same as being allowed to restart.

    The token is fully valid here — the request below is authenticated and
    answered — and the gate is still off. If the gate ever became a function of
    auth state, this is the assertion that would break."""
    app = _make_app(tmp_path, monkeypatch)
    _, r = authed(app).get("/info")
    assert r.status == 200, "the token used here must genuinely authenticate"
    assert app.ctx.restart_enabled is False, (
        "an authenticated broker must not be a restartable one: only an "
        "explicit operator grant (config key or stored sidecar) may turn "
        "this on")


def test_a_stored_grant_opens_the_gate_and_a_config_key_still_closes_it(
        tmp_path, monkeypatch):
    """restart_enabled rides the three-key update-policy sidecar now: a stored
    grant opens the gate with no config key at all -- and a PRESENT config key
    still outranks it, because the config file remains the operator override a
    sidecar can never outvote. The shipped examples pin the key false, which
    is exactly what keeps the hands-off deploy un-grantable from the GUI."""
    (tmp_path / "webterm_update_policy.json").write_text(
        json.dumps({"check_enabled": False, "restart_enabled": True}),
        encoding="utf-8")
    app = _make_app(tmp_path, monkeypatch)
    assert app.ctx.restart_enabled is True
    assert app.ctx.restart_source == "stored"
    _, r = authed(app).get("/info")
    # The same surface the gate tests above read: /info. `configured` is the
    # gate; only this file's pinned no-supervisor capability keeps
    # `available` false here.
    assert r.json["restart"]["configured"] is True
    assert r.json["update"]["policy"]["restart"] == {
        "enabled": True, "source": "stored", "mutable": True}
    # And the sidecar's own check:false was honoured beside the grant.
    assert app.ctx.update_check_enabled is False

    closed = _make_app(tmp_path, monkeypatch, restart_enabled=False)
    assert closed.ctx.restart_enabled is False
    assert closed.ctx.restart_source == "config"
    _, r = authed(closed).get("/info")
    assert r.json["restart"]["configured"] is False
    assert r.json["update"]["policy"]["restart"] == {
        "enabled": False, "source": "config", "mutable": False}


def test_the_example_configs_document_the_override_but_do_not_carry_it():
    """Both shipped examples must mention the two gates, but must NOT ship
    either key: a PRESENT key (either value) owns the gate and locks it away
    from the GUI, and the new default hands that decision to the desktop's
    "Allow this broker to update itself" row in the Update window instead.

    Strict JSON has no comments, so the repo's convention is a sibling
    ``_<key>_note`` string (see ``_auth_token_note``) that survives even
    though the key itself is gone."""
    import json
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    for name in ("broker_config.example.json",
                 "broker_config.linux.example.json"):
        raw = (root / name).read_text(encoding="utf-8")
        cfg = json.loads(raw)          # must parse as JSON
        assert "restart_enabled" not in cfg, \
            f"{name} must not ship restart_enabled: a present key (either " \
            "value) would pin this install away from the GUI row"
        assert "update_apply_enabled" not in cfg, \
            f"{name} must not ship update_apply_enabled: a present key " \
            "(either value) would pin this install away from the GUI row"
        # The exact key-with-colon form: the note keys legitimately contain
        # these substrings (`_restart_enabled_note`, ...), so the check must
        # be for the real key, not just the substring.
        assert '"restart_enabled":' not in raw, name
        assert '"update_apply_enabled":' not in raw, name

        assert "_restart_enabled_note" in cfg, \
            f"{name} needs the note explaining what restart_enabled does"
        assert "_update_apply_enabled_note" in cfg, \
            f"{name} needs the note explaining what update_apply_enabled does"
        for key in ("_restart_enabled_note", "_update_apply_enabled_note"):
            note = cfg[key]
            # A reader must be able to find the override this note documents.
            gated_key = key[1:-len("_note")]
            assert gated_key in note, \
                f"{name}'s {key} does not name {gated_key}"
            assert "Allow this broker to update itself" in note, \
                f"{name}'s {key} does not name the desktop row that decides " \
                "when the key is absent"

        # The cooldown (#183, R6) ships beside it, at its real default.
        assert cfg.get("restart_cooldown_seconds") == 90, \
            f"{name} must ship restart_cooldown_seconds at the 90s default"
        assert "_restart_cooldown_seconds_note" in cfg, \
            f"{name} needs the note explaining the cooldown"


# ---- E16: refusing new work while quiescing ---------------------------------

@pytest.mark.parametrize("path,body", [
    ("/file/upload_begin", {"path": "x.txt"}),
    ("/recording/begin", {}),
    ("/launch", {"profile": "cmd"}),
])
def test_new_work_is_refused_while_quiescing(tmp_path, monkeypatch, path, body):
    """The three entry points that CREATE something a drain would have to wait
    for, or throw away. A clear 503 — not a crash, and not a success that the
    process then fails to honour."""
    app = _make_app(tmp_path, monkeypatch)
    app.ctx.lifecycle = app_mod.LIFECYCLE_QUIESCING
    _, r = authed(app).post(path, json=body)
    assert r.status == 503, f"{path} answered {r.status} while quiescing"
    assert r.json["ok"] is False
    assert r.json["error"] == "restarting"
    assert r.json["lifecycle"] == "quiescing"


def test_refusal_never_precedes_the_auth_gate(tmp_path, monkeypatch):
    """A quiescing broker still 401s an unauthenticated caller.

    Answering 503 first would leak this broker's lifecycle to anyone who can
    reach the port, and would break test_auth_mandatory's live-router walk the
    moment a restart is under way."""
    app = _make_app(tmp_path, monkeypatch)
    app.ctx.lifecycle = app_mod.LIFECYCLE_QUIESCING
    # RAW client on purpose: this request must NOT carry a token.
    for path in ("/file/upload_begin", "/recording/begin", "/launch"):
        _, r = app.test_client.post(path, json={})
        assert r.status == 401, f"{path} answered {r.status} unauthenticated"
        assert r.json["error"] == "auth_required"


def test_an_in_flight_session_can_still_finish_while_quiescing(tmp_path,
                                                               monkeypatch):
    """Only the BEGIN half is refused. If /recording/chunk were refused too, the
    drain would be waiting for sessions it had itself made impossible to
    complete."""
    app = _make_app(tmp_path, monkeypatch)
    with authed_reusable(app) as client:
        _, r = client.post("/recording/begin", json={})
        assert r.status == 200, r.json
        rec_id = r.json["recording_id"]
        app.ctx.lifecycle = app_mod.LIFECYCLE_QUIESCING
        _, r = client.post("/recording/chunk",
                           json={"recording_id": rec_id, "offset": 0,
                                 "content_b64": "e30K"})
        assert r.status == 200, r.json
        _, r = client.post("/recording/begin", json={})
        assert r.status == 503, "a NEW save must still be refused"


# ---- E16: the drain itself --------------------------------------------------

def test_the_drain_is_a_no_op_when_nothing_is_in_flight(tmp_path, monkeypatch):
    """An idle broker drains instantly, reports nothing, and reaches
    restart_ready. This is the common case and it must not need a timeout."""
    app = _make_app(tmp_path, monkeypatch)
    report = asyncio.run(app_mod.drain_for_restart(app))
    assert report["ok"] is True
    assert report["waited_for"] == 0
    assert report["timed_out"] == 0
    assert report["aborted_uploads"] == []
    assert report["aborted_recordings"] == []
    assert report["lifecycle"] == app_mod.LIFECYCLE_RESTART_READY
    assert app.ctx.lifecycle == app_mod.LIFECYCLE_RESTART_READY


def test_the_drain_waits_for_a_real_in_flight_section(tmp_path, monkeypatch):
    """The whole point: a shielded critical section that is mid-write when the
    restart is asked for gets to finish.

    The handshake is what makes this real — the drain starts only once the
    worker thread is genuinely inside ``_append_chunk_gz``, i.e. once the
    section holds its lock and is past the point where a cancel would be free.
    """
    app = _make_app(tmp_path, monkeypatch)
    real_append = app_mod._append_chunk_gz
    inside = threading.Event()
    release = threading.Event()
    finished = threading.Event()

    def blocking_append(tmp, data):
        inside.set()
        release.wait(10)
        real_append(tmp, data)
        finished.set()

    monkeypatch.setattr(app_mod, "_append_chunk_gz", blocking_append)
    with authed_reusable(app) as client:
        _, r = client.post("/recording/begin", json={})
        assert r.status == 200, r.json
        rec_id = r.json["recording_id"]
        url = with_token(
            f"http://{client.host}:{client.port}/recording/chunk",
            app.ctx.auth_token)
        body = {"recording_id": rec_id, "offset": 0, "content_b64": "e30K"}
        before = set(app_mod._CRITICAL_TASKS)

        async def _drive():
            holder = asyncio.ensure_future(client._session.post(url, json=body))
            for _ in range(500):
                if inside.is_set():
                    break
                await asyncio.sleep(0.01)
            assert inside.is_set(), "the holder never entered its worker hop"
            # Unblock from the loop a little AFTER the drain has started, so a
            # drain that did not actually wait would come back with
            # finished == 0 instead of passing by luck.
            asyncio.get_running_loop().call_later(0.30, release.set)
            report = await app_mod.drain_for_restart(app, timeout=10)
            try:
                await holder
            except BaseException:  # noqa: BLE001
                pass
            return report

        report = client._loop.run_until_complete(_drive())

    release.set()
    assert report["ok"] is True, report
    assert report["waited_for"] == 1, (
        f"the drain saw {report['waited_for']} in-flight sections; the "
        "handshake says there was exactly one")
    assert report["finished"] == 1
    assert report["timed_out"] == 0
    assert finished.is_set(), \
        "the drain returned before the shielded append had finished"
    # Stated policy: an incomplete session is ABORTED and NAMED, never left for
    # before_server_stop to unlink without telling anyone.
    assert report["aborted_recordings"] == [rec_id], report
    assert app.ctx.rec_uploads == {}
    assert report["lifecycle"] == app_mod.LIFECYCLE_RESTART_READY
    assert not set(app_mod._CRITICAL_TASKS) - before


def test_the_drain_is_bounded_and_destroys_nothing_when_it_times_out(
        tmp_path, monkeypatch):
    """A section that never finishes must not hang the drain — and must not be
    tidied up around, either.

    The deadline is the easy half. The load-bearing half is what does NOT
    happen: a worker thread may still be inside ``open(tmp, 'ab')`` (a running
    executor future cannot be cancelled), so unlinking its temp here would
    recreate exactly the disk/memory divergence the shield exists to prevent.
    """
    app = _make_app(tmp_path, monkeypatch)
    inside = threading.Event()
    release = threading.Event()
    real_append = app_mod._append_chunk_gz

    def wedged_append(tmp, data):
        inside.set()
        release.wait(10)
        real_append(tmp, data)

    monkeypatch.setattr(app_mod, "_append_chunk_gz", wedged_append)
    with authed_reusable(app) as client:
        _, r = client.post("/recording/begin", json={})
        assert r.status == 200, r.json
        rec_id = r.json["recording_id"]
        url = with_token(
            f"http://{client.host}:{client.port}/recording/chunk",
            app.ctx.auth_token)
        body = {"recording_id": rec_id, "offset": 0, "content_b64": "e30K"}

        async def _drive():
            holder = asyncio.ensure_future(client._session.post(url, json=body))
            for _ in range(500):
                if inside.is_set():
                    break
                await asyncio.sleep(0.01)
            assert inside.is_set(), "the holder never entered its worker hop"
            report = await app_mod.drain_for_restart(app, timeout=0.3)
            # Sampled HERE, not after the client context exits: leaving the
            # ReusableClient stops the server, and before_server_stop clears
            # rec_uploads — which would make this assertion pass for the wrong
            # reason (or, worse, fail for one).
            survived = rec_id in app.ctx.rec_uploads
            release.set()                  # let the wedged section land
            try:
                await holder
            except BaseException:  # noqa: BLE001
                pass
            for _ in range(400):           # and drain out of the global set
                if not [t for t in app_mod._CRITICAL_TASKS if not t.done()]:
                    break
                await asyncio.sleep(0.01)
            return report, survived

        report, survived = client._loop.run_until_complete(_drive())

    release.set()
    assert report["ok"] is False
    assert report["timed_out"] == 1, report
    assert report["reason"] == "critical_sections_timed_out"
    assert report["elapsed"] < 5, \
        f"a 0.3s deadline took {report['elapsed']}s: the wait is not bounded"
    assert report["aborted_recordings"] == [], (
        "a timed-out drain destroyed a session anyway: its worker thread may "
        "still be writing to that very temp file")
    assert survived, "the timed-out drain popped the session it could not wait for"


def test_the_drain_reports_and_aborts_an_idle_upload_session(tmp_path,
                                                             monkeypatch):
    """The idle-session half of the policy. Nothing is in flight, so nothing is
    waited for — but the session cannot survive the restart (the table is
    in-memory, and the id means nothing to the next process), so it is aborted
    and its temp is gone."""
    from pathlib import Path

    app = _make_app(tmp_path, monkeypatch)
    with authed_reusable(app) as client:
        _, r = client.post("/file/upload_begin",
                           json={"path": str(tmp_path / "landing.bin")})
        assert r.status == 200, r.json
        upload_id = r.json["upload_id"]
        tmp_file = app.ctx.uploads[upload_id]["tmp"]

        async def _drive():
            report = await app_mod.drain_for_restart(app)
            # Sampled before the client context exits: before_server_stop does
            # the same unlink, and would mask a drain that did nothing.
            return report, dict(app.ctx.uploads), Path(tmp_file).exists()

        report, left, temp_exists = client._loop.run_until_complete(_drive())

    assert report["ok"] is True
    assert report["waited_for"] == 0
    assert report["aborted_uploads"] == [upload_id], report
    assert left == {}
    assert not temp_exists, "the aborted session's .part temp was left behind"


def test_the_drain_never_raises(tmp_path, monkeypatch):
    """A drain that blows up must still return a report. The caller has a
    request to answer and a restart to call off; an exception here would do
    neither and would leave the broker quiesced."""
    app = _make_app(tmp_path, monkeypatch)

    class _Exploding(dict):
        def values(self):
            raise RuntimeError("boom from the session table")

    app.ctx.uploads = _Exploding()
    report = asyncio.run(app_mod.drain_for_restart(app))
    assert report["ok"] is False
    assert "drain_error" in (report["reason"] or "")


# ---- E11: the worker half of the intent -------------------------------------

def test_arming_is_skipped_when_there_is_no_supervisor(tmp_path, monkeypatch):
    """No supervisor means exit 75 STOPS the broker rather than restarting it.

    So nothing may be set up for that exit: no exit code, no stop, and the
    quiesce is undone so the broker carries on serving."""
    _no_supervisor(monkeypatch)
    app = _make_app(tmp_path, monkeypatch)
    stops = []

    result = asyncio.run(app_mod.request_restart(
        app, stop=lambda: stops.append(True)))

    assert result["armed"] is False
    assert result["stopping"] is False
    assert result["ok"] is False
    assert result["reason"] == "not_supervised"
    assert stops == [], "the broker was stopped with nothing to bring it back"
    assert getattr(app.ctx, "exit_code", None) is None, \
        "exit 75 was set up with no supervisor to honour it"
    assert app.ctx.lifecycle == app_mod.LIFECYCLE_RUNNING, \
        "an abandoned restart left the broker refusing new work forever"


def test_the_intent_is_not_armed_when_the_drain_fails(tmp_path, monkeypatch):
    """Arming before (or despite) the drain would leave a sentinel on disk
    authorizing a relaunch this broker has just decided against — and the next
    accidental exit 75 would be honoured as a deliberate restart."""
    _no_supervisor(monkeypatch)
    app = _make_app(tmp_path, monkeypatch)
    armed = []
    stops = []
    inside = threading.Event()
    release = threading.Event()
    real_append = app_mod._append_chunk_gz

    def wedged_append(tmp, data):
        inside.set()
        release.wait(10)
        real_append(tmp, data)

    monkeypatch.setattr(app_mod, "_append_chunk_gz", wedged_append)
    with authed_reusable(app) as client:
        _, r = client.post("/recording/begin", json={})
        assert r.status == 200, r.json
        url = with_token(
            f"http://{client.host}:{client.port}/recording/chunk",
            app.ctx.auth_token)
        body = {"recording_id": r.json["recording_id"], "offset": 0,
                "content_b64": "e30K"}

        async def _drive():
            holder = asyncio.ensure_future(client._session.post(url, json=body))
            for _ in range(500):
                if inside.is_set():
                    break
                await asyncio.sleep(0.01)
            assert inside.is_set(), "the holder never entered its worker hop"
            result = await app_mod.request_restart(
                app, timeout=0.3,
                arm=lambda: armed.append(True) or True,
                stop=lambda: stops.append(True))
            release.set()
            try:
                await holder
            except BaseException:  # noqa: BLE001
                pass
            for _ in range(400):
                if not [t for t in app_mod._CRITICAL_TASKS if not t.done()]:
                    break
                await asyncio.sleep(0.01)
            return result

        result = client._loop.run_until_complete(_drive())

    release.set()
    assert armed == [], "the restart intent was armed by a drain that FAILED"
    assert stops == []
    assert result["ok"] is False
    assert result["reason"] == "critical_sections_timed_out"
    assert getattr(app.ctx, "exit_code", None) is None
    assert app.ctx.lifecycle == app_mod.LIFECYCLE_RUNNING


def test_a_cancelled_restart_request_does_not_strand_the_broker(tmp_path,
                                                                monkeypatch):
    """The client that asked for the restart can disconnect mid-drain (Sanic
    cancels the handler on connection_lost). The quiesce must be undone, or the
    broker refuses new work for the rest of its life while looking healthy."""
    _no_supervisor(monkeypatch)
    app = _make_app(tmp_path, monkeypatch)
    inside = threading.Event()
    release = threading.Event()
    real_append = app_mod._append_chunk_gz

    def wedged_append(tmp, data):
        inside.set()
        release.wait(10)
        real_append(tmp, data)

    monkeypatch.setattr(app_mod, "_append_chunk_gz", wedged_append)
    with authed_reusable(app) as client:
        _, r = client.post("/recording/begin", json={})
        assert r.status == 200, r.json
        url = with_token(
            f"http://{client.host}:{client.port}/recording/chunk",
            app.ctx.auth_token)
        body = {"recording_id": r.json["recording_id"], "offset": 0,
                "content_b64": "e30K"}

        async def _drive():
            holder = asyncio.ensure_future(client._session.post(url, json=body))
            for _ in range(500):
                if inside.is_set():
                    break
                await asyncio.sleep(0.01)
            assert inside.is_set(), "the holder never entered its worker hop"
            restarting = asyncio.ensure_future(
                app_mod.request_restart(app, timeout=10,
                                        arm=lambda: True, stop=lambda: None))
            for _ in range(500):           # wait until it is actually draining
                if app.ctx.lifecycle == app_mod.LIFECYCLE_DRAINING:
                    break
                await asyncio.sleep(0.01)
            restarting.cancel()
            try:
                await restarting
            except BaseException:  # noqa: BLE001 - cancellation is the point
                pass
            stranded = app.ctx.lifecycle
            release.set()
            try:
                await holder
            except BaseException:  # noqa: BLE001
                pass
            for _ in range(400):
                if not [t for t in app_mod._CRITICAL_TASKS if not t.done()]:
                    break
                await asyncio.sleep(0.01)
            return stranded

        stranded = client._loop.run_until_complete(_drive())

    release.set()
    assert stranded == app_mod.LIFECYCLE_RUNNING, (
        f"a cancelled restart left the broker in {stranded}: it would refuse "
        "every new upload, recording and launch from here on")
    assert getattr(app.ctx, "exit_code", None) is None


def test_a_successful_drain_arms_the_real_sentinel_and_stops(tmp_path,
                                                             monkeypatch):
    """The happy path, end to end against the REAL ``supervise.arm_restart``:
    drain, sentinel on disk, exit code 75, and a stop deferred just long enough
    for the 202 to reach the wire."""
    run_dir = tmp_path / "run"
    monkeypatch.setenv(supervise.ENV_RUN_DIR, str(run_dir))
    monkeypatch.setenv(supervise.ENV_SUPERVISOR_NONCE, "nonce-for-the-test")
    app = _make_app(tmp_path, monkeypatch)
    # Arming is now per-MECHANISM: only the "supervisor" mechanism has a
    # sentinel to write. The env above is what arm_restart actually reads; this
    # is what says the sentinel is the right thing to write (the real capability
    # probe would report ppid-mismatch here, since our parent is pytest).
    _capability(app, supervise.MECHANISM_SUPERVISOR)
    stops = []

    async def _drive():
        result = await app_mod.request_restart(
            app, delay=0.01, stop=lambda: stops.append(True))
        # The stop is DEFERRED: it must not have fired by the time the caller
        # gets its report, or the response could never be flushed.
        assert stops == [], "the server was stopped before the response existed"
        for _ in range(200):
            if stops:
                break
            await asyncio.sleep(0.01)
        return result

    result = asyncio.run(_drive())

    assert result["ok"] is True, result
    assert result["armed"] is True
    assert result["stopping"] is True
    assert supervise.restart_armed(str(run_dir), "nonce-for-the-test"), \
        "no valid intent sentinel: the supervisor would treat exit 75 as a crash"
    assert app.ctx.exit_code == supervise.EXIT_RESTART
    assert stops == [True], "the deferred stop never fired"
    assert app.ctx.lifecycle == app_mod.LIFECYCLE_RESTART_READY


# ---- the mechanism decides whether there is anything to arm -----------------

def test_systemd_does_not_arm_and_does_not_need_to(tmp_path, monkeypatch):
    """The seam this checkpoint closes.

    Under a unit with Restart=always/on-failure there is no supervisor, no nonce
    and no run dir, so ``arm_restart`` CANNOT succeed — while systemd respawns
    any non-zero exit, so exiting 75 is the whole mechanism. Arming
    unconditionally (as this did) refused a genuine systemd install the one
    restart it could actually perform."""
    _no_supervisor(monkeypatch)
    app = _make_app(tmp_path, monkeypatch)
    _capability(app, supervise.MECHANISM_SYSTEMD)
    armed = []
    stops = []

    async def _drive():
        result = await app_mod.request_restart(
            app, delay=0.01, arm=lambda: armed.append(True) or True,
            stop=lambda: stops.append(True))
        for _ in range(200):
            if stops:
                break
            await asyncio.sleep(0.01)
        return result

    result = asyncio.run(_drive())

    assert armed == [], \
        "systemd was handed a sentinel it will never read (and cannot write)"
    assert result["ok"] is True, result
    assert result["armed"] is True, "a systemd restart was refused"
    assert result["mechanism"] == supervise.MECHANISM_SYSTEMD
    assert app.ctx.exit_code == supervise.EXIT_RESTART, \
        "exit 75 is the ONLY thing that restarts us under systemd"
    assert stops == [True]
    assert app.ctx.lifecycle == app_mod.LIFECYCLE_RESTART_READY


def test_mechanism_none_never_even_tries_to_arm(tmp_path, monkeypatch):
    """"Refuse outright", not "try and see".

    A sentinel written by an unsupervised broker sits on disk authorizing the
    NEXT accidental exit 75 — and the mechanism is already known, so trying is
    pure downside."""
    _no_supervisor(monkeypatch)
    app = _make_app(tmp_path, monkeypatch)
    _capability(app, supervise.MECHANISM_NONE, supervise.REASON_PPID_MISMATCH)
    armed = []
    stops = []

    result = asyncio.run(app_mod.request_restart(
        app, arm=lambda: armed.append(True) or True,
        stop=lambda: stops.append(True)))

    assert armed == [], "an unsupervised broker armed a sentinel anyway"
    assert result["ok"] is False
    assert result["reason"] == "not_supervised"
    assert stops == []
    assert getattr(app.ctx, "exit_code", None) is None
    assert app.ctx.lifecycle == app_mod.LIFECYCLE_RUNNING


# ---- E15: honest capability reporting on /info ------------------------------

def test_info_carries_the_restart_object(tmp_path, monkeypatch):
    """Every field a client needs to render the control AND explain it."""
    app = _make_app(tmp_path, monkeypatch)
    _, r = authed(app).get("/info")
    assert r.status == 200
    block = r.json["restart"]
    assert set(block) == {"configured", "available", "mechanism", "reason_code",
                          "continuity", "lifecycle", "bootId"}
    assert block["configured"] is False
    assert block["available"] is False
    assert block["mechanism"] == supervise.MECHANISM_NONE
    assert block["reason_code"] == app_mod.REASON_RESTART_DISABLED
    assert block["lifecycle"] == app_mod.LIFECYCLE_RUNNING
    assert set(block["continuity"]) == {"guaranteed", "at_risk", "unknown"}
    assert block["bootId"] == app_mod.BOOT_ID
    # Additive: the keys other clients already read must be untouched.
    for key in ("ok", "broker_id", "version", "mods", "mod_policy", "update"):
        assert key in r.json


@pytest.mark.parametrize("enabled,mechanism,reason,stage,expected", [
    # The operator gate wins: it is the one an operator can actually change.
    (False, supervise.MECHANISM_SUPERVISOR, None, app_mod.LIFECYCLE_RUNNING,
     app_mod.REASON_RESTART_DISABLED),
    (True, supervise.MECHANISM_NONE, supervise.REASON_NO_SUPERVISOR,
     app_mod.LIFECYCLE_RUNNING, supervise.REASON_NO_SUPERVISOR),
    # Inherited variables, not ours — a DIFFERENT fix from "no supervisor".
    (True, supervise.MECHANISM_NONE, supervise.REASON_PPID_MISMATCH,
     app_mod.LIFECYCLE_RUNNING, supervise.REASON_PPID_MISMATCH),
    # systemd started us but will not bring us back.
    (True, supervise.MECHANISM_NONE, supervise.REASON_SYSTEMD_NO_RESTART,
     app_mod.LIFECYCLE_RUNNING, supervise.REASON_SYSTEMD_NO_RESTART),
    # ...and "we could not read the policy" is not the same as "it says no".
    (True, supervise.MECHANISM_NONE, supervise.REASON_SYSTEMD_POLICY_UNKNOWN,
     app_mod.LIFECYCLE_RUNNING, supervise.REASON_SYSTEMD_POLICY_UNKNOWN),
    (True, supervise.MECHANISM_SUPERVISOR, None, app_mod.LIFECYCLE_QUIESCING,
     app_mod.REASON_RESTART_IN_PROGRESS),
    (True, supervise.MECHANISM_SUPERVISOR, None, app_mod.LIFECYCLE_RESTART_READY,
     app_mod.REASON_RESTART_IN_PROGRESS),
    (True, supervise.MECHANISM_SUPERVISOR, None, app_mod.LIFECYCLE_RUNNING, None),
    (True, supervise.MECHANISM_SYSTEMD, None, app_mod.LIFECYCLE_RUNNING, None),
])
def test_every_unavailable_cause_has_its_own_reason_code(
        tmp_path, monkeypatch, enabled, mechanism, reason, stage, expected):
    """A bare supported/none collapses states an operator needs told apart —
    policy off, no supervisor, inherited variables, a unit that will not respawn
    us, and one already running all read as "greyed out, no idea why"."""
    app = _make_app(tmp_path, monkeypatch, restart_enabled=enabled)
    _capability(app, mechanism, reason)
    app.ctx.lifecycle = stage
    _, r = authed(app).get("/info")
    block = r.json["restart"]
    assert block["reason_code"] == expected, block
    assert block["available"] is (expected is None), block
    assert block["configured"] is enabled


def test_the_reason_codes_are_all_distinct():
    """They are API — the UI renders them — so a collision would silently show
    one cause's text for another's problem."""
    codes = [app_mod.REASON_RESTART_DISABLED,
             app_mod.REASON_RESTART_IN_PROGRESS,
             app_mod.REASON_RESTART_COOLDOWN,
             app_mod.REASON_RESTART_ERROR,
             supervise.REASON_NO_SUPERVISOR,
             supervise.REASON_PPID_MISMATCH,
             supervise.REASON_SYSTEMD_NO_RESTART,
             supervise.REASON_SYSTEMD_POLICY_UNKNOWN,
             supervise.REASON_PROBE_FAILED]
    assert len(set(codes)) == len(codes), codes


# ---- E15/E17 (R6): the post-boot restart cooldown ---------------------------

def _cooled(tmp_path, monkeypatch, seconds=90, uptime=0.0, **cfg):
    """A restartable app with a real cooldown and a PINNED uptime.

    BOOT_TIME is module-level (one process, one boot), so without pinning it
    these tests would depend on how long ago this test session imported
    app.py — a suite that ran longer than the cooldown would quietly flip
    every expectation."""
    monkeypatch.setattr(app_mod, "BOOT_TIME", time.monotonic() - uptime)
    app = _make_app(tmp_path, monkeypatch, restart_enabled=True,
                    restart_cooldown_seconds=seconds, **cfg)
    return _capability(app, supervise.MECHANISM_SUPERVISOR)


def test_the_capability_reports_cooldown_while_it_is_fresh(tmp_path,
                                                           monkeypatch):
    """configured:true / available:false / reason "cooldown", plus the one
    number no other reason gets: WHEN to come back."""
    app = _cooled(tmp_path, monkeypatch)
    _, r = authed(app).get("/info")
    block = r.json["restart"]
    assert block["configured"] is True, block
    assert block["available"] is False, block
    assert block["reason_code"] == app_mod.REASON_RESTART_COOLDOWN, block
    assert isinstance(block["retry_after_s"], int), block
    assert 0 < block["retry_after_s"] <= 90, block


def test_the_capability_recovers_once_the_cooldown_has_passed(tmp_path,
                                                              monkeypatch):
    """The one reason that clears by ITSELF — no operator action, no request."""
    app = _cooled(tmp_path, monkeypatch, uptime=90.5)
    _, r = authed(app).get("/info")
    block = r.json["restart"]
    assert block["available"] is True, block
    assert block["reason_code"] is None, block
    assert "retry_after_s" not in block, \
        "retry_after_s is the cooldown's key alone; absent means available"


def test_cooldown_zero_disables_and_a_negative_clamps_to_zero(tmp_path,
                                                              monkeypatch):
    """0 is the documented off switch (the fixtures and the e2e need it), and
    a negative hand-edit clamps rather than erroring or going truthy."""
    app = _cooled(tmp_path, monkeypatch, seconds=0)
    assert app.ctx.restart_cooldown_seconds == 0
    _, r = authed(app).get("/info")
    assert r.json["restart"]["available"] is True, r.json["restart"]
    app = _cooled(tmp_path, monkeypatch, seconds=-30)
    assert app.ctx.restart_cooldown_seconds == 0
    _, r = authed(app).get("/info")
    assert r.json["restart"]["available"] is True, r.json["restart"]


def test_the_cooldown_defaults_to_90_when_the_config_is_silent(tmp_path,
                                                               monkeypatch):
    """create_app directly — _make_app pins the key to 0 for every other test
    in this file, precisely because this default would otherwise refuse each
    fresh test app. And unreadable garbage falls back to the DEFAULT, never to
    off: a typo must not silently remove the guard."""
    monkeypatch.delenv("WEB_TERMINAL_TOKEN", raising=False)
    app = create_app({"auth_token": TEST_TOKEN,
                      "state_path": str(tmp_path / "s.json")},
                     name="webterm-restart-cooldown-default")
    assert app.ctx.restart_cooldown_seconds == 90
    # The arithmetic the default exists for: strictly longer than the
    # supervisor's budget window (5 per 60s), so deliberate restarts land at
    # most one per window and can never exhaust the budget on their own.
    assert app.ctx.restart_cooldown_seconds > supervise.RESTART_WINDOW
    app = create_app({"auth_token": TEST_TOKEN,
                      "state_path": str(tmp_path / "s2.json"),
                      "restart_cooldown_seconds": "soon"},
                     name="webterm-restart-cooldown-garbage")
    assert app.ctx.restart_cooldown_seconds == 90


def test_the_gate_and_mechanism_and_in_progress_outrank_the_cooldown(
        tmp_path, monkeypatch):
    """Precedence: an operator must be told about a missing gate or mechanism
    FIRST — those need a human fix, while the cooldown clears by itself, so
    "cooldown" over either would invite waiting out a refusal that waiting
    cannot fix. And an in-flight restart is the truth of the moment."""
    monkeypatch.setattr(app_mod, "BOOT_TIME", time.monotonic())
    app = _make_app(tmp_path, monkeypatch,        # the gate is off
                    restart_cooldown_seconds=90)
    _capability(app, supervise.MECHANISM_SUPERVISOR)
    _, r = authed(app).get("/info")
    assert r.json["restart"]["reason_code"] == app_mod.REASON_RESTART_DISABLED
    assert "retry_after_s" not in r.json["restart"]
    app = _make_app(tmp_path, monkeypatch, restart_enabled=True,
                    restart_cooldown_seconds=90)   # nothing to relaunch us
    _capability(app, supervise.MECHANISM_NONE, supervise.REASON_NO_SUPERVISOR)
    _, r = authed(app).get("/info")
    assert r.json["restart"]["reason_code"] == supervise.REASON_NO_SUPERVISOR
    app = _cooled(tmp_path, monkeypatch)           # one already under way
    app.ctx.lifecycle = app_mod.LIFECYCLE_DRAINING
    _, r = authed(app).get("/info")
    assert r.json["restart"]["reason_code"] == \
        app_mod.REASON_RESTART_IN_PROGRESS


def test_the_capability_is_probed_once_per_process_not_per_request(
        tmp_path, monkeypatch):
    """The hard requirement: ``worker_capability`` can shell out to ``systemctl
    show`` with a 5 s timeout, synchronously, and /info is POLLED."""
    calls = []

    def counting_probe(**kwargs):
        calls.append(True)
        return {"supported": True, "mechanism": supervise.MECHANISM_SUPERVISOR,
                "reason_code": None}

    monkeypatch.setattr(app_mod, "_RESTART_CAPABILITY", None)
    monkeypatch.setattr(supervise, "worker_capability", counting_probe)
    app = _make_app(tmp_path, monkeypatch, restart_enabled=True)
    for _ in range(3):
        _, r = authed(app).get("/info")
        assert r.json["restart"]["mechanism"] == supervise.MECHANISM_SUPERVISOR
    assert calls == [True], f"{len(calls)} probes for one app and 3 requests"
    # And per PROCESS, not per app: it describes this interpreter, so a second
    # app must not pay for a second systemctl call either.
    other = _make_app(tmp_path, monkeypatch)
    _, r = authed(other).get("/info")
    assert calls == [True], "a second app re-probed the process capability"


def test_a_probe_that_raises_degrades_to_unsupported(tmp_path, monkeypatch):
    """A capability probe that throws is strictly worse than one that says no:
    /info is how the whole desktop boots, and it must not 500 because a
    subprocess call went wrong."""
    def exploding_probe(**kwargs):
        raise RuntimeError("boom from the capability probe")

    monkeypatch.setattr(app_mod, "_RESTART_CAPABILITY", None)
    monkeypatch.setattr(supervise, "worker_capability", exploding_probe)
    app = _make_app(tmp_path, monkeypatch, restart_enabled=True)
    _, r = authed(app).get("/info")
    assert r.status == 200, "a failed probe took /info down"
    block = r.json["restart"]
    assert block["available"] is False
    assert block["mechanism"] == supervise.MECHANISM_NONE
    assert block["reason_code"] == supervise.REASON_PROBE_FAILED


def test_a_probe_that_answers_nonsense_degrades_to_unsupported(
        tmp_path, monkeypatch):
    """Same rule for a probe that returns something unusable rather than
    raising — "we could not tell" must never render as "yes"."""
    monkeypatch.setattr(app_mod, "_RESTART_CAPABILITY", None)
    monkeypatch.setattr(supervise, "worker_capability", lambda **k: None)
    app = _make_app(tmp_path, monkeypatch, restart_enabled=True)
    _, r = authed(app).get("/info")
    assert r.status == 200
    assert r.json["restart"]["mechanism"] == supervise.MECHANISM_NONE
    assert r.json["restart"]["reason_code"] == supervise.REASON_PROBE_FAILED


def test_an_unreadable_launcher_still_reports_a_continuity_summary(
        tmp_path, monkeypatch):
    """Bookkeeping must never be the reason the capability cannot be reported.
    Zeros, and the restart is still describable."""
    app = _make_app(tmp_path, monkeypatch, restart_enabled=True)
    app.ctx.launcher = None                   # no launcher at all
    _, r = authed(app).get("/info")
    assert r.status == 200
    assert r.json["restart"]["continuity"] == {"guaranteed": 0, "at_risk": 0,
                                               "unknown": 0}


def test_info_is_never_served_from_a_cache(tmp_path, monkeypatch):
    """A restart capability read out of a shared cache (tailscale serve, a
    corporate MITM) could show a quiescing broker as available — or a stale
    bootId as proof of a restart nobody performed."""
    app = _make_app(tmp_path, monkeypatch)
    _, r = authed(app).get("/info")
    assert r.headers.get("Cache-Control") == "no-store"


def test_the_boot_id_is_stable_within_the_process(tmp_path, monkeypatch):
    """Stable, or every poll would look like a restart."""
    app = _make_app(tmp_path, monkeypatch)
    _, first = authed(app).get("/info")
    _, second = authed(app).get("/info")
    assert first.json["restart"]["bootId"] == second.json["restart"]["bootId"]
    # Two apps ARE one process: a per-app id would report a restart that never
    # happened the moment anything created a second app.
    other = _make_app(tmp_path, monkeypatch)
    _, third = authed(other).get("/info")
    assert third.json["restart"]["bootId"] == first.json["restart"]["bootId"]
    # And it is NOT broker_id, which is durable across restarts by design (#64)
    # — watching that one could never confirm anything.
    assert first.json["restart"]["bootId"] != first.json["broker_id"]


def test_the_boot_id_changes_when_the_process_changes():
    """The whole point of it. The HTTP response to POST /restart cannot prove a
    restart happened (the process answering is the one being replaced), so the
    client confirms by polling /info until this value differs — which only works
    if a NEW process really does mint a new one."""
    root = Path(__file__).resolve().parent.parent
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(
        [str(root)] + [p for p in sys.path if p])
    out = subprocess.run(
        [sys.executable, "-c",
         "import webterm.broker.app as a; print(a.BOOT_ID)"],
        cwd=str(root), env=env, capture_output=True, text=True, timeout=180)
    assert out.returncode == 0, out.stderr
    other = out.stdout.strip()
    assert other, out.stderr
    assert other != app_mod.BOOT_ID, \
        "a fresh interpreter reported the same bootId: a client could never " \
        "tell a restart from a broker that never went away"


# ---- E17: POST /restart -----------------------------------------------------

#: What a successful ``request_restart`` hands back, minus the part that stops
#: the server. Route tests inject this: the route's job is the status codes and
#: the body, and the machinery underneath has its own tests above.
_OK_RESULT = {"ok": True, "lifecycle": app_mod.LIFECYCLE_RESTART_READY,
              "reason": None, "waited_for": 0, "finished": 0, "timed_out": 0,
              "aborted_uploads": [], "aborted_recordings": [], "elapsed": 0.01,
              "armed": True, "stopping": True,
              "mechanism": supervise.MECHANISM_SUPERVISOR}


def _spy_drain(monkeypatch):
    """Record every drain — and make sure a refusal that reaches one cannot
    quietly succeed instead of failing the test."""
    calls = []

    async def _drain(app, timeout=None):
        calls.append(timeout)
        return dict(_OK_RESULT)

    monkeypatch.setattr(app_mod, "drain_for_restart", _drain)
    return calls


def _spy_restart(monkeypatch, result=None, hold=None):
    """Replace the machinery with a recorder.

    The route calls ``request_restart(app)`` with no injectable seams of its
    own — by design, the seams live one layer down — so this is where a route
    test stops short of actually stopping the server."""
    calls = []
    payload = dict(_OK_RESULT if result is None else result)

    async def _restart(app, **kwargs):
        calls.append(kwargs)
        if hold is not None:
            await hold["event"].wait()
        if payload.get("ok"):
            app.ctx.lifecycle = app_mod.LIFECYCLE_RESTART_READY
        else:
            app_mod.resume_from_quiesce(app)   # what the real one does
        return dict(payload)

    monkeypatch.setattr(app_mod, "request_restart", _restart)
    return calls


def _restartable(tmp_path, monkeypatch, **cfg):
    app = _make_app(tmp_path, monkeypatch, restart_enabled=True, **cfg)
    return _capability(app, supervise.MECHANISM_SUPERVISOR)


def test_restart_401s_unauthenticated(tmp_path, monkeypatch):
    """The shared gate, first and always — and the live-router walk in
    test_auth_mandatory asserts exactly this for every non-public route.

    Sent with a FOREIGN Origin on purpose: since #205 dropped the origin
    gate, the token is the whole admission story, and a caller without one
    gets 401 whatever its Origin says."""
    drains = _spy_drain(monkeypatch)
    restarts = _spy_restart(monkeypatch)
    app = _restartable(tmp_path, monkeypatch)
    # RAW client on purpose: no token.
    _, r = app.test_client.post("/restart", json={},
                                headers={"Origin": "https://elsewhere.example"})
    assert r.status == 401
    assert r.json["error"] == "auth_required"
    assert drains == [] and restarts == [], \
        "an unauthenticated request reached the drain"


def test_restart_is_post_only(tmp_path, monkeypatch):
    """A GET that bounces the process would be followed by every prefetcher and
    link scanner on the network."""
    app = _restartable(tmp_path, monkeypatch)
    _, r = authed(app).get("/restart")
    assert r.status == 405, r.status


def test_restart_answers_its_preflight(tmp_path, monkeypatch):
    """A JSON POST is never a simple request, so a cross-origin caller sends
    OPTIONS first; route resolution precedes middleware, so an unrouted OPTIONS
    405s and the browser reports an opaque network error."""
    app = _restartable(tmp_path, monkeypatch)
    _, r = app.test_client.options("/restart")
    assert r.status == 204
    assert r.headers.get("Access-Control-Allow-Origin") == "*"
    assert r.headers.get("Access-Control-Allow-Methods") == \
        "GET, POST, PUT, OPTIONS"


def test_restart_503s_when_the_gate_is_off_and_never_drains(tmp_path,
                                                            monkeypatch):
    """503, mirroring /update/check's disabled shape: an ABSENT capability is
    not the caller being unauthorized."""
    drains = _spy_drain(monkeypatch)
    restarts = _spy_restart(monkeypatch)
    app = _make_app(tmp_path, monkeypatch)       # gate off (the default)
    _capability(app, supervise.MECHANISM_SUPERVISOR)
    _, r = authed(app).post("/restart", json={})
    assert r.status == 503, r.json
    assert r.json["error"] == "restart_disabled"
    assert r.json["reason_code"] == app_mod.REASON_RESTART_DISABLED
    assert drains == [] and restarts == [], "a refused restart drained anyway"
    assert app.ctx.lifecycle == app_mod.LIFECYCLE_RUNNING


@pytest.mark.parametrize("reason", [supervise.REASON_NO_SUPERVISOR,
                                    supervise.REASON_PPID_MISMATCH,
                                    supervise.REASON_SYSTEMD_NO_RESTART])
def test_restart_409s_when_nothing_would_relaunch_us(tmp_path, monkeypatch,
                                                     reason):
    """Refusing to try is a correct outcome; trying and dying is not. The body
    carries the reason_code so the UI can say WHICH."""
    drains = _spy_drain(monkeypatch)
    restarts = _spy_restart(monkeypatch)
    app = _make_app(tmp_path, monkeypatch, restart_enabled=True)
    _capability(app, supervise.MECHANISM_NONE, reason)
    _, r = authed(app).post("/restart", json={})
    assert r.status == 409, r.json
    assert r.json["error"] == "restart_unsupported"
    assert r.json["reason_code"] == reason
    assert drains == [] and restarts == []
    assert app.ctx.lifecycle == app_mod.LIFECYCLE_RUNNING


def test_restart_409s_when_one_is_already_in_progress(tmp_path, monkeypatch):
    drains = _spy_drain(monkeypatch)
    restarts = _spy_restart(monkeypatch)
    app = _restartable(tmp_path, monkeypatch)
    app.ctx.lifecycle = app_mod.LIFECYCLE_DRAINING
    _, r = authed(app).post("/restart", json={})
    assert r.status == 409, r.json
    assert r.json["error"] == "restart_in_progress"
    assert r.json["reason_code"] == app_mod.REASON_RESTART_IN_PROGRESS
    assert drains == [] and restarts == [], \
        "a second restart drained a broker that was already draining"


def test_restart_409s_during_the_cooldown_and_consumes_no_claim(tmp_path,
                                                                monkeypatch):
    """R6 at the route: refused BEFORE the CAS, with the same graded-409 shape
    as the mechanism refusals plus the retry_after_s the reason earns. The
    load-bearing half is what does NOT happen — no drain, no claim — so a
    retry after the window starts from a clean RUNNING broker and wins."""
    drains = _spy_drain(monkeypatch)
    restarts = _spy_restart(monkeypatch)
    app = _cooled(tmp_path, monkeypatch)
    _, r = authed(app).post("/restart", json={})
    assert r.status == 409, r.json
    assert r.json["error"] == "restart_cooldown"
    assert r.json["reason_code"] == app_mod.REASON_RESTART_COOLDOWN
    assert isinstance(r.json["retry_after_s"], int), r.json
    assert r.json["retry_after_s"] > 0, r.json
    assert drains == [] and restarts == [], "a cooled-down restart drained"
    assert app.ctx.lifecycle == app_mod.LIFECYCLE_RUNNING, \
        "the cooldown refusal consumed the claim"
    # The window passes (the boot recedes); the SAME app now restarts.
    monkeypatch.setattr(app_mod, "BOOT_TIME", time.monotonic() - 91)
    _, r = authed(app).post("/restart", json={})
    assert r.status == 202, r.json
    assert len(restarts) == 1


def test_a_202_carries_the_operation_id_the_claim_minted(tmp_path,
                                                         monkeypatch):
    """R16: the claim mints the id (the same shape /update/apply mints at its
    claim), and the 202 hands it back."""
    _spy_restart(monkeypatch)
    app = _restartable(tmp_path, monkeypatch)
    _, r = authed(app).post("/restart", json={})
    assert r.status == 202, r.json
    op = r.json["operation_id"]
    assert isinstance(op, str) and op.startswith("restart-"), r.json
    assert op == app.ctx.restart_operation_id


def test_a_foreign_origin_post_reaches_the_operator_gate(tmp_path,
                                                         monkeypatch):
    """#205: the origin gate is GONE, and this pins the new admission. A POST
    with a valid token and a foreign Origin is never 403 forbidden_origin —
    it walks straight to the NEXT gate, proved here with the operator gate
    off: the answer is the gate's own 503, for every Origin the old check
    used to refuse ("null" included)."""
    drains = _spy_drain(monkeypatch)
    restarts = _spy_restart(monkeypatch)
    app = _make_app(tmp_path, monkeypatch)       # gate off (the default)
    _capability(app, supervise.MECHANISM_SUPERVISOR)
    for origin in ("http://evil.example", "https://evil.example:8443",
                   "https://elsewhere.example", "null"):
        _, r = authed(app).post("/restart", json={},
                                headers={"Origin": origin})
        assert r.status == 503, f"{origin} answered {r.status}: {r.json}"
        assert r.json["error"] == "restart_disabled"
        assert r.json["reason_code"] == app_mod.REASON_RESTART_DISABLED
    assert drains == [] and restarts == [], "a gated restart drained anyway"
    assert app.ctx.lifecycle == app_mod.LIFECYCLE_RUNNING


def test_a_foreign_origin_post_with_the_gate_open_restarts(tmp_path,
                                                           monkeypatch):
    """The door IS the feature (#205): a fleet operator's desktop is a page
    on ANOTHER origin, and with a valid token and the operator gate on it
    must run the whole route — no origin layer anywhere on the path."""
    restarts = _spy_restart(monkeypatch)
    app = _restartable(tmp_path, monkeypatch)
    _, r = authed(app).post("/restart", json={},
                            headers={"Origin": "https://elsewhere.example"})
    assert r.status == 202, r.json
    assert len(restarts) == 1


def test_a_successful_restart_answers_202_with_a_boot_id(tmp_path, monkeypatch):
    """202, never 200: "restarted" is not a claim this response can make. The
    client confirms by watching bootId change."""
    restarts = _spy_restart(monkeypatch)
    app = _restartable(tmp_path, monkeypatch)
    _, r = authed(app).post("/restart", json={})
    assert r.status == 202, r.json
    assert r.json["ok"] is True
    assert r.json["bootId"] == app_mod.BOOT_ID
    assert r.json["mechanism"] == supervise.MECHANISM_SUPERVISOR
    # The drain report: what it waited for and what it cost.
    assert r.json["drain"]["armed"] is True
    assert r.json["drain"]["stopping"] is True
    assert r.json["drain"]["aborted_uploads"] == []
    assert set(r.json["continuity"]) == {"guaranteed", "at_risk", "unknown"}
    # And the capability is honest about itself immediately afterwards.
    assert r.json["restart"]["available"] is False
    assert r.json["restart"]["reason_code"] == \
        app_mod.REASON_RESTART_IN_PROGRESS
    assert restarts == [{}], "the route did not drive request_restart exactly once"


def test_a_restart_that_cannot_be_armed_is_not_a_202(tmp_path, monkeypatch):
    """The mechanism said supervisor, the arming said otherwise (a run dir that
    could not be written). Same class of fact as the 409 above — and NOT a
    success, because nothing will bring this process back."""
    _spy_restart(monkeypatch, result=dict(_OK_RESULT, ok=False,
                                          reason="not_supervised",
                                          armed=False, stopping=False))
    app = _restartable(tmp_path, monkeypatch)
    _, r = authed(app).post("/restart", json={})
    assert r.status == 409, r.json
    assert r.json["ok"] is False
    assert r.json["reason_code"] == "not_supervised"
    assert app.ctx.lifecycle == app_mod.LIFECYCLE_RUNNING, \
        "a refused restart left the broker refusing new work"


def test_a_drain_that_times_out_is_a_503_not_a_202(tmp_path, monkeypatch):
    """"Come back shortly" — the broker is still up, still serving, and the
    in-flight write it could not wait for is still running."""
    _spy_restart(monkeypatch,
                 result=dict(_OK_RESULT, ok=False, armed=False, stopping=False,
                             reason="critical_sections_timed_out",
                             timed_out=1))
    app = _restartable(tmp_path, monkeypatch)
    _, r = authed(app).post("/restart", json={})
    assert r.status == 503, r.json
    assert r.json["reason_code"] == "critical_sections_timed_out"
    assert r.json["drain"]["timed_out"] == 1
    assert app.ctx.lifecycle == app_mod.LIFECYCLE_RUNNING


def test_a_restart_that_blows_up_does_not_strand_the_broker(tmp_path,
                                                            monkeypatch):
    """Should be unreachable — request_restart swallows the drain's own errors.
    What matters is that a bug there cannot leave the broker QUIESCING, refusing
    every upload, recording and launch for the rest of its life while looking
    perfectly healthy."""
    async def _boom(app, **kwargs):
        raise RuntimeError("boom from the restart machinery")

    monkeypatch.setattr(app_mod, "request_restart", _boom)
    app = _restartable(tmp_path, monkeypatch)
    _, r = authed(app).post("/restart", json={})
    assert r.status == 503, r.json
    assert r.json["reason_code"] == app_mod.REASON_RESTART_ERROR
    assert app.ctx.lifecycle == app_mod.LIFECYCLE_RUNNING


def test_two_simultaneous_requests_produce_one_restart(tmp_path, monkeypatch):
    """Two operators clicking at once, or one double-submit, must not schedule
    two drains and two shutdowns.

    Both requests are genuinely in flight: the first is held inside the
    machinery until the second has been answered, so the loser is refused by the
    compare-and-swap rather than by having arrived after everything was over.
    """
    hold = {}
    restarts = _spy_restart(monkeypatch, hold=hold)
    app = _restartable(tmp_path, monkeypatch)

    with authed_reusable(app) as client:
        url = with_token(f"http://{client.host}:{client.port}/restart",
                         app.ctx.auth_token)

        async def _drive():
            hold["event"] = asyncio.Event()
            first = asyncio.ensure_future(client._session.post(url, json={}))
            second = asyncio.ensure_future(client._session.post(url, json={}))
            for _ in range(500):        # until one of them holds the claim
                if restarts:
                    break
                await asyncio.sleep(0.01)
            assert restarts, "neither request reached the machinery"
            # Give the loser time to be refused while the winner is still held.
            for _ in range(200):
                if second.done() or first.done():
                    break
                await asyncio.sleep(0.01)
            hold["event"].set()
            return await asyncio.gather(first, second)

        responses = client._loop.run_until_complete(_drive())

    statuses = sorted(r.status_code for r in responses)
    assert statuses == [202, 409], statuses
    assert len(restarts) == 1, \
        f"{len(restarts)} restarts were started for two simultaneous requests"
    loser = [r for r in responses if r.status_code == 409][0]
    assert loser.json()["error"] == "restart_in_progress"
    assert loser.json()["reason_code"] == app_mod.REASON_RESTART_IN_PROGRESS
    # R16: the loser is told WHICH restart beat it — the winner's own
    # operation id, so a client retrying a request it thought had timed out
    # can recognise its work already running rather than assuming a stranger's.
    winner = [r for r in responses if r.status_code == 202][0]
    op = winner.json()["operation_id"]
    assert isinstance(op, str) and op.startswith("restart-"), winner.json()
    assert loser.json()["operation_id"] == op, (
        "the concurrent loser's 409 did not carry the winner's operation id")


def test_the_claim_is_a_compare_and_swap(tmp_path, monkeypatch):
    """The primitive underneath, on its own: exactly one caller wins, and the
    claim is what makes the broker refuse new work from that instant rather than
    an await later."""
    app = _make_app(tmp_path, monkeypatch)
    assert app_mod._claim_restart(app) is True
    assert app.ctx.lifecycle == app_mod.LIFECYCLE_QUIESCING
    assert app_mod._claim_restart(app) is False, "two callers claimed one restart"
    app_mod.resume_from_quiesce(app)
    assert app_mod._claim_restart(app) is True, \
        "an abandoned restart left the claim held forever"
