"""Event-loop liveness: a /file/* request must not freeze the broker.

This is the test that asserts on the DEFECT rather than on a response shape.
The broker runs ``single_process=True`` — ONE event loop for every HTTP handler,
every producer WebSocket and every browser relay — so a blocking filesystem call
inside a handler freezes the WHOLE broker, live terminals included, for its full
duration. The file API is deliberately host-wide and accepts UNC paths, so a
single ``/file/list`` against a dead SMB share used to wedge everything for the
SMB timeout.

Method: patch ``_classify_path`` to ``time.sleep(BLOCK_S)``, fire ONE request,
and count how many ``asyncio.sleep(TICK_S)`` ticks the loop services while that
request is in flight. A loop that stayed free services tens of them; a loop that
blocked services none. Counting only starts once the patched stub signals it has
actually entered the stall, so ticks serviced before the blocking call began can
never inflate the score. ``test_a_blocked_loop_really_scores_zero`` is the
negative control: it blocks this very loop with ``time.sleep`` and asserts the
same counter reports ~0, so a broken counter cannot silently pass everything.

CAVEAT — use ``app.asgi_client`` ONLY for single-request checks like these.
``SanicASGITestClient.request`` fires a full startup AND shutdown event pair
around EVERY request, and this app's ``before_server_stop`` listeners tear down
in-memory state (``_drain_upload_sessions`` unlinks every in-flight upload temp).
Any multi-request flow that depends on ``app.ctx`` state surviving between calls
must keep using ``ReusableClient`` (see ``authed_reusable``).
"""

import asyncio
import threading
import time

import webterm.broker.app as app_mod
from .auth_helpers import TEST_TOKEN, with_token

#: How long the patched _classify_path blocks for.
BLOCK_S = 0.6
#: Tick granularity of the liveness counter.
TICK_S = 0.01
#: A free loop services ~60 ticks in BLOCK_S (fewer on Windows, whose default
#: timer resolution is ~15 ms); a blocked one services 0-1. The threshold sits
#: far from both so a loaded CI box cannot flip the verdict.
MIN_TICKS = 20

_app_seq = 0


def _make_app(tmp_path, monkeypatch):
    """A file-API app whose editor_root and state file live in the sandbox.
    Sanic refuses two apps with the same name in one process, hence the seq."""
    global _app_seq
    _app_seq += 1
    monkeypatch.delenv("WEB_TERMINAL_TOKEN", raising=False)
    return app_mod.create_app(
        {"editor_root": str(tmp_path),
         "state_path": str(tmp_path / "webterm_state.json"),
         "auth_token": TEST_TOKEN},
        name=f"webterm-loop-test-{_app_seq}")


def _block_classify(monkeypatch, entered):
    """Make _classify_path take BLOCK_S, announcing (thread-safely — post-fix it
    runs in a worker) the moment it entered."""
    real = app_mod._classify_path

    def slow(p):
        entered.set()
        time.sleep(BLOCK_S)
        return real(p)

    monkeypatch.setattr(app_mod, "_classify_path", slow)


async def _ticks_during(make_request, entered):
    """Run ``make_request()`` and count loop ticks serviced from the moment the
    stall begins until the request completes. Returns ``(ticks, response)``."""
    done = asyncio.Event()
    box = {}

    async def _run():
        try:
            box["response"] = await make_request()
        finally:
            done.set()

    task = asyncio.create_task(_run())
    # Do not start counting until the handler has actually reached the blocking
    # call. If the loop IS blocked this poll cannot run either, so it falls
    # through on `done` instead of hanging.
    while not entered.is_set() and not done.is_set():
        await asyncio.sleep(TICK_S / 2)
    ticks = 0
    while not done.is_set():
        await asyncio.sleep(TICK_S)
        ticks += 1
    await task
    return ticks, box.get("response")


async def test_a_blocked_loop_really_scores_zero():
    """Negative control for the counter itself: block THIS loop with time.sleep
    and the tick count must collapse. Without this, a counter that always
    returned a big number would 'prove' liveness for free."""
    entered = threading.Event()

    async def blocking():
        entered.set()
        time.sleep(BLOCK_S)          # deliberate: freezes the running loop
        return "blocked"

    ticks, response = await _ticks_during(blocking, entered)
    assert response == "blocked"
    assert ticks <= 2, f"counter scored {ticks} ticks on a KNOWN-blocked loop"


async def test_file_list_keeps_the_loop_responsive(tmp_path, monkeypatch):
    """/file/list must service its resolve + classify + scan off the loop."""
    (tmp_path / "a.txt").write_text("x", encoding="utf-8")
    (tmp_path / "sub").mkdir()
    app = _make_app(tmp_path, monkeypatch)

    # Prove the handler and this counter share ONE event loop — otherwise the
    # test would measure a loop nothing was blocking and pass on broken code.
    seen = {}

    @app.on_request
    async def _capture_loop(request):
        seen["loop"] = asyncio.get_running_loop()

    entered = threading.Event()
    _block_classify(monkeypatch, entered)

    url = with_token("/file/list", TEST_TOKEN)

    async def fire():
        _, resp = await app.asgi_client.post(url, json={"path": str(tmp_path)})
        return resp

    ticks, resp = await _ticks_during(fire, entered)

    assert seen.get("loop") is asyncio.get_running_loop()
    # The liveness assertion comes FIRST: it is the point of this module, and a
    # payload assertion failing ahead of it would hide the real regression.
    assert ticks > MIN_TICKS, (
        f"only {ticks} loop ticks serviced during a {BLOCK_S}s /file/list — the "
        "event loop was blocked by the handler's filesystem calls")
    assert resp.status == 200 and resp.json["ok"] is True
    names = {e["name"] for e in resp.json["entries"]}
    assert {"a.txt", "sub"} <= names, names
    assert resp.json["truncated"] is False


async def test_file_stat_keeps_the_loop_responsive(tmp_path, monkeypatch):
    """The same guarantee through _probe_path, on a handler that is not
    /file/list — the batched probe is what every other /file/* route relies on."""
    (tmp_path / "a.txt").write_text("hello", encoding="utf-8")
    app = _make_app(tmp_path, monkeypatch)
    entered = threading.Event()
    _block_classify(monkeypatch, entered)

    url = with_token("/file/stat", TEST_TOKEN)

    async def fire():
        _, resp = await app.asgi_client.post(url, json={"path": "a.txt"})
        return resp

    ticks, resp = await _ticks_during(fire, entered)

    assert ticks > MIN_TICKS, (
        f"only {ticks} loop ticks serviced during a {BLOCK_S}s /file/stat — the "
        "event loop was blocked by _probe_path's filesystem calls")
    assert resp.status == 200 and resp.json["type"] == "file"
