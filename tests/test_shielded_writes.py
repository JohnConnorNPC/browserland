"""The ``_shielded_region`` contract itself (#160).

Every mutating handler that holds a lock across an ``_off_loop`` hop runs its
critical section through ``webterm.broker.app._shielded_region``. Sanic cancels
the handler task on ``connection_lost`` (and on the 60 s RESPONSE_TIMEOUT), and
a RUNNING executor future cannot be cancelled — so an unshielded region would
release its lock while the worker was still writing, and the accounting after
the await would never run.

The per-endpoint consequences are tested next to their own families
(``test_file_api.py`` for /file/upload_chunk, ``test_recording.py`` for
/recording/chunk and /recording/commit). What lives HERE is the shared
mechanism, which no per-endpoint test pins:

  - lock acquisition is OUTSIDE the shield, so a cancelled waiter leaves nothing
    behind (shielding the acquire would turn one slow worker into an unbounded
    pile of immortal waiters, route-wide on the process-global locks);
  - a section that fails after its request is gone is LOGGED, because CPython's
    ``shield`` deliberately marks the orphaned exception retrieved and would
    otherwise drop the traceback silently;
  - the config-writer class (/state and its three identical twins) keeps disk
    and memory in step.

Driven with ``ReusableClient`` (one server across requests) and a
``threading.Event`` handshake rather than sleep guesses, so the cancel always
lands inside the worker hop.
"""

import asyncio
import json
import threading
import time

import webterm.broker.app as app_mod
from .auth_helpers import TEST_TOKEN, authed_reusable, with_token
from webterm.broker.app import create_app

_app_seq = 0


def _make_app(tmp_path, monkeypatch):
    global _app_seq
    _app_seq += 1
    monkeypatch.delenv("WEB_TERMINAL_TOKEN", raising=False)
    cfg = {"auth_token": TEST_TOKEN,
           "editor_root": str(tmp_path),
           "state_path": str(tmp_path / "webterm_state.json"),
           "recordings_dir": str(tmp_path / "recs")}
    return create_app(cfg, name=f"webterm-shield-test-{_app_seq}")


def test_cancelled_lock_waiter_leaves_no_immortal_task(tmp_path, monkeypatch):
    """A request cancelled while QUEUED on the lock must vanish completely.

    This is the failure mode that argues against the obvious "wrap the whole
    locked region, acquire included" shape. The default executor is SHARED with
    /status/fetch, /profiles/detect and every _write_state_atomic, so one slow
    worker stalls the lock holder; if acquisition were shielded too, every
    request that then queued and hit the 60 s RESPONSE_TIMEOUT would leave a
    cancellation-immune task behind, each still retaining its decoded body. On
    the process-global locks that is route-wide.

    The observable is ``_CRITICAL_TASKS``, which exists to hold a strong
    reference to exactly the sections that have ALREADY acquired their lock. A
    queued waiter must not be in it — measured as a delta, since the set is
    module-global and shared with anything else in flight.
    """
    app = _make_app(tmp_path, monkeypatch)
    real_append = app_mod._append_chunk_gz
    inside = threading.Event()
    release = threading.Event()

    def blocking_append(tmp, data):
        inside.set()                       # the holder is in the worker hop
        release.wait(10)                   # ...and stays until the test says so
        real_append(tmp, data)

    monkeypatch.setattr(app_mod, "_append_chunk_gz", blocking_append)
    with authed_reusable(app) as client:
        _, r = client.post("/recording/begin", json={})
        assert r.status == 200, r.json
        rec_id = r.json["recording_id"]
        url = with_token(
            f"http://{client.host}:{client.port}/recording/chunk",
            app.ctx.auth_token)
        body = {"recording_id": rec_id, "offset": 0, "content_b64": "e30K"}
        lock = app.ctx.rec_uploads[rec_id]["lock"]
        before = set(app_mod._CRITICAL_TASKS)

        async def _drive():
            holder = asyncio.ensure_future(client._session.post(url, json=body))
            for _ in range(500):           # holder now owns the lock
                if inside.is_set():
                    break
                await asyncio.sleep(0.01)
            assert inside.is_set(), "the holder never entered its worker hop"
            waiter = asyncio.ensure_future(client._session.post(url, json=body))
            # Handshake, not a sleep guess: asyncio.Lock parks contenders in
            # _waiters, so a non-empty deque IS "the second request reached the
            # acquire". Without this the sampling below could run before the
            # waiter ever got there, and `queued == 1` would pass trivially —
            # even for an implementation that shields acquisition.
            for _ in range(500):
                if lock._waiters:
                    break
                await asyncio.sleep(0.01)
            assert lock._waiters, "the second request never reached the lock"
            queued = len(set(app_mod._CRITICAL_TASKS) - before)
            waiter.cancel()
            try:
                await waiter
            except BaseException:  # noqa: BLE001 - cancellation is the point
                pass
            after_cancel = len(set(app_mod._CRITICAL_TASKS) - before)
            release.set()
            try:
                await holder
            except BaseException:  # noqa: BLE001 - holder may also be torn down
                pass
            for _ in range(400):           # holder's section drains
                if not set(app_mod._CRITICAL_TASKS) - before:
                    break
                await asyncio.sleep(0.01)
            return queued, after_cancel

        queued, after_cancel = client._loop.run_until_complete(_drive())
        assert queued == 1, (
            f"{queued} shielded tasks while one holder ran: a request still "
            "queued on the lock must not own a task — shielding the acquire "
            "makes every timed-out waiter immortal")
        assert after_cancel == 1, \
            "cancelling a queued waiter left a task behind"
        assert not set(app_mod._CRITICAL_TASKS) - before, \
            "the holder's task outlived its section"
    release.set()


def test_section_failing_after_cancellation_is_logged(tmp_path, monkeypatch,
                                                      caplog):
    """A shielded section that blows up post-disconnect must not fail silently.

    CPython's ``shield`` calls ``inner.exception()`` from its done callback when
    the outer future was cancelled, deliberately marking the exception retrieved
    so the "Task exception was never retrieved" warning never fires. The client
    is already gone, so there is no 500 and no access-log line either: without
    an explicit callback an unexpected exception inside a critical section is
    invisible everywhere, having possibly already mutated state.

    A non-OSError is used on purpose — /recording/chunk catches (OSError,
    zlib.error), so this is the class of bug the handler does NOT convert into a
    response.
    """
    app = _make_app(tmp_path, monkeypatch)
    entered = threading.Event()

    def exploding_append(tmp, data):
        entered.set()
        time.sleep(0.40)                   # long enough to be cancelled inside
        raise RuntimeError("boom from the worker")

    monkeypatch.setattr(app_mod, "_append_chunk_gz", exploding_append)
    with caplog.at_level("ERROR", logger="webterm.broker.app"), \
            authed_reusable(app) as client:
        _, r = client.post("/recording/begin", json={})
        assert r.status == 200, r.json
        rec_id = r.json["recording_id"]
        url = with_token(
            f"http://{client.host}:{client.port}/recording/chunk",
            app.ctx.auth_token)
        body = {"recording_id": rec_id, "offset": 0, "content_b64": "e30K"}

        async def _drive():
            task = asyncio.ensure_future(client._session.post(url, json=body))
            for _ in range(500):       # deadlined: an inert patch must FAIL,
                if entered.is_set():   # not hang the suite forever
                    break
                await asyncio.sleep(0.01)
            assert entered.is_set(), "the worker never entered its hop"
            task.cancel()
            try:
                await task
            except BaseException:  # noqa: BLE001 - cancellation is the point
                pass
            assert task.cancelled(), \
                "the request completed instead of being cancelled mid-hop"
            for _ in range(400):
                if any("shielded critical section failed" in rec.message
                       for rec in caplog.records):
                    break
                await asyncio.sleep(0.01)

        client._loop.run_until_complete(_drive())
        hits = [rec for rec in caplog.records
                if "shielded critical section failed" in rec.message]
        assert hits, (
            "a section that raised after its request was cancelled produced no "
            "log line at all: shield consumed the exception and the client was "
            "already gone")
        assert hits[0].exc_info and \
            isinstance(hits[0].exc_info[1], RuntimeError), \
            "the log line must carry the original traceback"


def test_cancelled_state_put_leaves_disk_and_memory_in_step(tmp_path,
                                                            monkeypatch):
    """The config-writer class: disk must never end up ahead of memory.

    /state, /mod-store/<id>, /mcp/config and /profiles/config are the same
    shape — a lock held across ``_write_state_atomic`` with the live in-memory
    swap AFTER it — and all four go through the same helper, so /state stands in
    for the class. ``_write_state_atomic`` is temp + ``os.replace``, so a
    partial file is impossible; the damage is divergence. It self-heals on the
    next successful write, but a broker restart inside that window silently
    adopts a value no client was ever told landed, and rev 2 ends up describing
    two different states.
    """
    app = _make_app(tmp_path, monkeypatch)
    state_path = tmp_path / "webterm_state.json"
    real_write = app_mod._write_state_atomic
    entered = threading.Event()

    def slow_write(path, state):
        entered.set()
        time.sleep(0.40)
        real_write(path, state)

    monkeypatch.setattr(app_mod, "_write_state_atomic", slow_write)
    with authed_reusable(app) as client:
        url = with_token(f"http://{client.host}:{client.port}/state",
                         app.ctx.auth_token)
        body = {"baseRev": 0, "settings": {"a": 2}, "layout": {"cols": []}}

        async def _drive():
            task = asyncio.ensure_future(client._session.put(url, json=body))
            for _ in range(500):       # deadlined: an inert patch must FAIL,
                if entered.is_set():   # not hang the suite forever
                    break
                await asyncio.sleep(0.01)
            assert entered.is_set(), "the writer never entered its hop"
            task.cancel()
            try:
                await task
            except BaseException:  # noqa: BLE001 - cancellation is the point
                pass
            assert task.cancelled(), (
                "the PUT completed instead of being cancelled mid-write, so "
                "this run proves nothing about the shield")
            for _ in range(400):
                if app.ctx.state["rev"] != 0:
                    break
                await asyncio.sleep(0.01)

        client._loop.run_until_complete(_drive())

        on_disk = json.loads(state_path.read_text(encoding="utf-8"))
        assert app.ctx.state["rev"] == on_disk["rev"], (
            f"memory rev {app.ctx.state['rev']} vs disk rev {on_disk['rev']}: a "
            "restart in this window adopts a value no client was told landed")
        assert app.ctx.state["settings"] == on_disk["settings"]
