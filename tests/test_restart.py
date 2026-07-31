"""The operator gate, the drain state machine, and the worker half of the
restart intent (#183).

Three things are pinned here, and they are pinned separately because they fail
separately:

  * **the gate** (``restart_enabled``) defaults OFF and is read from the broker
    config alone. It is NOT an authentication question. Holding the browser
    token already means shell-level access to the box, so gating a restart on
    "is this caller logged in" would give every logged-in session the power to
    bounce a broker that is hosting other people's live terminals.

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
import threading

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
            "recordings_dir": str(tmp_path / "recs")}
    base.update(cfg)
    return create_app(base, name=f"webterm-restart-test-{_app_seq}")


def _no_supervisor(monkeypatch):
    """Scrub the three supervisor variables. The test process inherits whatever
    the developer's shell has, and a stray BROWSERLAND_RUN_DIR would let the
    'no supervisor' tests arm a real sentinel."""
    for name in (supervise.ENV_RUN_DIR, supervise.ENV_SUPERVISOR_NONCE,
                 supervise.ENV_SUPERVISOR_PID):
        monkeypatch.delenv(name, raising=False)


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
        "an authenticated broker must not be a restartable one: the config key "
        "is the only thing that may turn this on")


def test_the_example_configs_carry_the_key():
    """Both shipped examples must mention it, or the switch is undiscoverable.

    Strict JSON has no comments, so the repo's convention is a sibling
    ``_<key>_note`` string (see ``_auth_token_note``)."""
    import json
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    for name in ("broker_config.example.json",
                 "broker_config.linux.example.json"):
        cfg = json.loads((root / name).read_text(encoding="utf-8"))
        assert cfg.get("restart_enabled") is False, \
            f"{name} must ship the key, defaulting to false"
        assert "_restart_enabled_note" in cfg, \
            f"{name} needs the note explaining what the key does"


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
