"""Agent integration against the fake broker, with a scripted backend.

Covers the protocol/agent logic deterministically: hello-first, output
relay, input/resize round-trips, snapshot framing, title sniffing into
re-hello, and reconnect with backoff. The real-ConPTY path is in
test_conpty.py.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

import pytest

from webterm.agent.agent import Agent
from webterm.agent.backends.base import PtyBackend
from webterm.agent.config import AgentConfig

from .fake_broker import FakeBroker


class FakeBackend(PtyBackend):
    """Test double: output is injected with feed(); exit with exit_child()."""

    def __init__(self) -> None:
        self.pid = 4242
        self.written = bytearray()
        self.size = None
        self.killed = False
        self.fail_resize = False
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._on_data = None
        self._on_exit = None

    def spawn(self, argv, cols, rows, cwd=None, env=None):
        self.argv = list(argv)
        self.size = (cols, rows)

    def start(self, loop, on_data, on_exit):
        self._loop = loop
        self._on_data = on_data
        self._on_exit = on_exit

    def write(self, data: bytes) -> None:
        self.written += data

    def resize(self, cols: int, rows: int) -> None:
        if self.fail_resize:
            raise OSError("resize refused")
        self.size = (cols, rows)

    def kill(self) -> None:
        self.killed = True

    def exitcode(self):
        return None

    # test hooks
    def feed(self, data: bytes) -> None:
        self._loop.call_soon(self._on_data, data)

    def exit_child(self, code: int) -> None:
        self._loop.call_soon(self._on_exit, code)


def make_config(broker: FakeBroker, **overrides) -> AgentConfig:
    defaults = dict(
        command=("fake-shell",),
        broker_url=broker.url,
        cols=80,
        rows=24,
        title="fake-shell",
        window_id=777001,
        ring_bytes=4096,
    )
    defaults.update(overrides)
    return AgentConfig(**defaults)


@pytest.fixture
async def broker():
    fb = FakeBroker()
    await fb.start()
    yield fb
    await fb.stop()


@pytest.fixture
async def running_agent(broker):
    backend = FakeBackend()
    agent = Agent(make_config(broker), backend=backend)
    task = asyncio.create_task(agent.run())
    await broker.wait_connected()
    yield agent, backend, task
    if not task.done():
        backend.exit_child(0)
        await asyncio.wait_for(task, 5)


async def test_hello_first_with_identity(running_agent, broker):
    hello = broker.hellos[0]
    assert hello["type"] == "hello"
    assert hello["window_id"] == 777001
    assert hello["pid"] == 4242
    assert hello["title"] == "fake-shell"
    assert hello["cols"] == 80 and hello["rows"] == 24
    assert hello["kind"] == "agent"
    assert hello["host"]  # gethostname is always non-empty


async def test_hello_carries_pyte_flag(running_agent, broker):
    # #134: the hello reports pyte availability so the broker / MCP list_terminals
    # can flag the no-pyte textgrid fallback. The flag mirrors pyte_snap.available()
    # (True in the test env, where pyte is installed).
    from webterm.agent.snapshot import pyte_snap
    agent, _, _ = running_agent
    hello = broker.hellos[0]
    assert "pyte" in hello
    assert hello["pyte"] is pyte_snap.available()
    assert agent.state.pyte is pyte_snap.available()


async def test_startup_warns_when_pyte_absent(broker, monkeypatch, caplog):
    # #134: a pyte-less agent WARNs once at spawn so the read_screen degradation is
    # visible (the deployed DF agent runs without pyte), and its hello reports the
    # flag False so the broker/MCP surface it too.
    import webterm.agent.agent as agent_mod
    monkeypatch.setattr(agent_mod.pyte_snap, "available", lambda: False)
    backend = FakeBackend()
    agent = Agent(make_config(broker, window_id=777334), backend=backend)
    assert agent.state.pyte is False              # __init__ read the probe
    with caplog.at_level(logging.WARNING):
        task = asyncio.create_task(agent.run())
        await broker.wait_connected()
    backend.exit_child(0)
    await asyncio.wait_for(task, 5)
    assert broker.hellos[0]["pyte"] is False       # reported on the wire
    assert any("pyte not installed" in r.getMessage() for r in caplog.records)


async def test_output_relayed_as_binary(running_agent, broker):
    _, backend, _ = running_agent
    backend.feed(b"hello from pty\r\n")
    frame = await broker.wait_binary(lambda b: b"hello from pty" in b)
    assert isinstance(frame, bytes)


async def test_input_written_to_pty(running_agent, broker):
    _, backend, _ = running_agent
    await broker.send_input("ls -la\r")
    await asyncio.wait_for(_poll_until(
        lambda: b"ls -la\r" in backend.written), 5)


async def test_resize_applies_and_echoes_resized(running_agent, broker):
    _, backend, _ = running_agent
    await broker.send_resize(120, 32)
    frame = await broker.wait_text(lambda f: f.get("type") == "resized")
    assert frame == {"type": "resized", "cols": 120, "rows": 32}
    assert backend.size == (120, 32)


async def test_failed_resize_echoes_current_dims(running_agent, broker):
    _, backend, _ = running_agent
    backend.fail_resize = True
    await broker.send_resize(999, 999)
    frame = await broker.wait_text(lambda f: f.get("type") == "resized")
    assert frame == {"type": "resized", "cols": 80, "rows": 24}


async def test_snapshot_is_single_binary_with_preamble(running_agent, broker):
    _, backend, _ = running_agent
    backend.feed(b"screen content here")
    await broker.wait_binary(lambda b: b"screen content here" in b)
    await broker.request_snapshot()
    snap = await broker.wait_binary(
        lambda b: b.startswith(b"\x1b[0m\x1b[2J\x1b[H"))
    assert b"screen content here" in snap


async def test_snapshot_pyte_mode_renders_settled_screen(broker):
    pytest.importorskip("pyte")
    backend = FakeBackend()
    agent = Agent(make_config(broker, snapshot_mode="pyte", cols=40, rows=5),
                  backend=backend)
    task = asyncio.create_task(agent.run())
    try:
        await broker.wait_connected()
        backend.feed(b"".join(b"line %d\r\n" % i for i in range(20)))
        await broker.wait_binary(lambda b: b"line 19" in b)
        await broker.request_snapshot()
        snap = await broker.wait_binary(
            lambda b: b.startswith(b"\x1b[0m\x1b[2J\x1b[H"))
        # Settled screen only: the scrolled-away head is gone.
        assert b"line 16" in snap and b"line 1\r" not in snap
    finally:
        backend.exit_child(0)
        await asyncio.wait_for(task, 5)


async def test_title_sniffed_and_forwarded(running_agent, broker):
    _, backend, _ = running_agent
    backend.feed(b"\x1b]0;my new title\x07")
    frame = await broker.wait_text(lambda f: f.get("type") == "title")
    assert frame == {"type": "title", "data": "my new title"}


async def test_reconnect_fresh_hello_with_updated_state(running_agent, broker):
    agent, backend, _ = running_agent
    # Mutate state: resize + title change.
    await broker.send_resize(100, 30)
    await broker.wait_text(lambda f: f.get("type") == "resized")
    backend.feed(b"\x1b]2;renamed\x07")
    await broker.wait_text(lambda f: f.get("type") == "title")

    await broker.kill_producer()
    # Backoff starts at 0.5 s; the fresh hello must carry the new state.
    await broker.wait_hello_count(2, timeout=10)
    hello = broker.hellos[1]
    assert hello["cols"] == 100 and hello["rows"] == 30
    assert hello["title"] == "renamed"
    # PTY kept running while disconnected: output still flows after re-hello.
    backend.feed(b"alive after reconnect")
    await broker.wait_binary(lambda b: b"alive after reconnect" in b)


async def test_child_exit_code_propagates(broker):
    backend = FakeBackend()
    agent = Agent(make_config(broker), backend=backend)
    task = asyncio.create_task(agent.run())
    await broker.wait_connected()
    backend.feed(b"bye\r\n")
    backend.exit_child(42)
    assert await asyncio.wait_for(task, 5) == 42
    # Final output was flushed before the client shut down.
    assert any(b"bye" in frame for frame in broker.binary)


async def test_child_exit_pushes_exit_frame(broker):
    """On real child exit the agent pushes an explicit 'exit' event carrying the
    code, flushed before shutdown — so the broker can tear browsers down at once
    instead of waiting on the poll grace cycle. Issue #1."""
    backend = FakeBackend()
    agent = Agent(make_config(broker), backend=backend)
    task = asyncio.create_task(agent.run())
    await broker.wait_connected()
    backend.feed(b"bye\r\n")
    backend.exit_child(7)
    assert await asyncio.wait_for(task, 5) == 7
    # Exactly one exit frame, carrying the child's code, and the final output
    # also made it out (the exit frame rides the same ordered queue, last).
    assert [t for t in broker.texts if t.get("type") == "exit"] == [
        {"type": "exit", "code": 7}]
    assert any(b"bye" in frame for frame in broker.binary)


async def test_binary_from_broker_is_raw_input(running_agent, broker):
    _, backend, _ = running_agent
    await broker.producer.send(b"\x03")  # binary fallback = raw input
    await asyncio.wait_for(_poll_until(
        lambda: b"\x03" in backend.written), 5)


async def _poll_until(predicate):
    while not predicate():
        await asyncio.sleep(0.02)


# ---- #26: read_screen content_hash + wait-for-change ----------------------

async def _read_screen(broker, req, **kw):
    """Drive one screen_text_please RPC and return the matching screen_text."""
    frame = {"type": "screen_text_please", "req": req, "view": "screen",
             "lines": 0}
    frame.update(kw)
    await broker.send(frame)
    return await broker.wait_text(
        lambda f: f.get("type") == "screen_text" and f.get("req") == req)


def test_content_hash_is_stable_and_sensitive():
    from webterm.agent.agent import _content_hash
    assert _content_hash("hello world") == _content_hash("hello world")
    assert _content_hash("hello world") != _content_hash("hello worle")
    assert len(_content_hash("x")) == 32          # blake2b digest_size=16


async def test_read_screen_returns_content_hash(running_agent, broker):
    _, backend, _ = running_agent
    backend.feed(b"first screen\r\n")
    await broker.wait_binary(lambda b: b"first screen" in b)
    r1 = await _read_screen(broker, 1)
    assert r1["content_hash"]                      # non-empty digest
    assert "first screen" in r1["text"]
    backend.feed(b"second screen\r\n")
    await broker.wait_binary(lambda b: b"second screen" in b)
    r2 = await _read_screen(broker, 2)
    assert r2["content_hash"] != r1["content_hash"]   # changed -> new hash


async def test_wait_for_change_returns_promptly_on_change(running_agent, broker):
    _, backend, _ = running_agent
    backend.feed(b"baseline\r\n")
    await broker.wait_binary(lambda b: b"baseline" in b)
    base = await _read_screen(broker, 1)
    h0 = base["content_hash"]
    # Park a wait on the baseline hash, let it settle, then change the screen.
    waiter = asyncio.create_task(
        _read_screen(broker, 2, wait_for_change=h0, timeout_ms=5000))
    await asyncio.sleep(0.15)                       # let the agent render + park
    assert not waiter.done()                        # still waiting (no change yet)
    backend.feed(b"CHANGED!\r\n")
    r = await asyncio.wait_for(waiter, 5)
    assert r["content_hash"] != h0
    assert "CHANGED!" in r["text"]


async def test_wait_for_change_times_out_returns_current(running_agent, broker):
    _, backend, _ = running_agent
    backend.feed(b"static\r\n")
    await broker.wait_binary(lambda b: b"static" in b)
    base = await _read_screen(broker, 1)
    h0 = base["content_hash"]
    # No further output: the wait must return the current screen at timeout,
    # not hang and not error.
    loop = asyncio.get_running_loop()
    t0 = loop.time()
    r = await asyncio.wait_for(
        _read_screen(broker, 2, wait_for_change=h0, timeout_ms=400), 5)
    elapsed = loop.time() - t0
    assert r["content_hash"] == h0                  # unchanged screen
    assert "static" in r["text"]
    assert elapsed >= 0.3                           # actually waited ~timeout_ms


async def test_wait_for_change_clamps_huge_timeout(running_agent, broker):
    # timeout_ms beyond the cap must not let the agent hold forever; with a
    # change fed in, it still returns promptly (sanity that clamping is benign).
    _, backend, _ = running_agent
    backend.feed(b"start\r\n")
    await broker.wait_binary(lambda b: b"start" in b)
    h0 = (await _read_screen(broker, 1))["content_hash"]
    waiter = asyncio.create_task(
        _read_screen(broker, 2, wait_for_change=h0, timeout_ms=10**9))
    await asyncio.sleep(0.1)
    backend.feed(b"go\r\n")
    r = await asyncio.wait_for(waiter, 5)
    assert "go" in r["text"] and r["content_hash"] != h0


# ---- #51: read_screen wait-for-content (text / regex predicate) ------------

async def test_wait_for_text_returns_on_appear(running_agent, broker):
    _, backend, _ = running_agent
    backend.feed(b"booting\r\n")
    await broker.wait_binary(lambda b: b"booting" in b)
    waiter = asyncio.create_task(
        _read_screen(broker, 2, wait_for_text="Ready", timeout_ms=5000))
    await asyncio.sleep(0.15)
    assert not waiter.done()                 # awaited text not on screen yet
    backend.feed(b"status: Ready\r\n")
    r = await asyncio.wait_for(waiter, 5)
    assert r.get("matched") is True
    assert "Ready" in r["text"]


async def test_wait_for_text_times_out_matched_false(running_agent, broker):
    _, backend, _ = running_agent
    backend.feed(b"idle\r\n")
    await broker.wait_binary(lambda b: b"idle" in b)
    loop = asyncio.get_running_loop()
    t0 = loop.time()
    # The text never appears: the wait returns the CURRENT screen at timeout
    # with matched=false (not a hang, not an error).
    r = await asyncio.wait_for(
        _read_screen(broker, 2, wait_for_text="NEVER", timeout_ms=400), 5)
    assert r.get("matched") is False
    assert loop.time() - t0 >= 0.3           # actually waited ~timeout_ms
    assert "idle" in r["text"]


async def test_wait_for_text_already_present_is_immediate(running_agent, broker):
    _, backend, _ = running_agent
    backend.feed(b"DONE here\r\n")
    await broker.wait_binary(lambda b: b"DONE" in b)
    loop = asyncio.get_running_loop()
    t0 = loop.time()
    r = await asyncio.wait_for(
        _read_screen(broker, 2, wait_for_text="DONE", timeout_ms=5000), 5)
    assert r.get("matched") is True
    assert loop.time() - t0 < 1.0            # matched on pass one, didn't wait out


async def test_wait_for_regex_returns_on_match(running_agent, broker):
    _, backend, _ = running_agent
    backend.feed(b"progress 5\r\n")
    await broker.wait_binary(lambda b: b"progress 5" in b)
    waiter = asyncio.create_task(
        _read_screen(broker, 2, wait_for_regex=r"\d+%", timeout_ms=5000))
    await asyncio.sleep(0.15)
    assert not waiter.done()
    backend.feed(b"progress 100%\r\n")
    r = await asyncio.wait_for(waiter, 5)
    assert r.get("matched") is True
    assert "100%" in r["text"]


async def test_wait_absent_returns_when_text_disappears(running_agent, broker):
    _, backend, _ = running_agent
    backend.feed(b"loading spinner\r\n")
    await broker.wait_binary(lambda b: b"spinner" in b)
    waiter = asyncio.create_task(
        _read_screen(broker, 2, wait_for_text="spinner",
                     wait_absent=True, timeout_ms=5000))
    await asyncio.sleep(0.15)
    assert not waiter.done()                 # still present -> keep waiting
    backend.feed(b"\x1b[2J\x1b[H")           # clear screen -> the text is gone
    r = await asyncio.wait_for(waiter, 5)
    assert r.get("matched") is True
    assert "spinner" not in r["text"]


async def test_wait_for_regex_invalid_is_safe(running_agent, broker):
    # The broker validates the regex (bad_regex 400), but if a bad pattern ever
    # reaches the agent (e.g. an older broker), it must NOT hang or error —
    # treat it as no predicate and reply immediately.
    _, backend, _ = running_agent
    backend.feed(b"hello\r\n")
    await broker.wait_binary(lambda b: b"hello" in b)
    loop = asyncio.get_running_loop()
    t0 = loop.time()
    r = await asyncio.wait_for(
        _read_screen(broker, 2, wait_for_regex="(unclosed", timeout_ms=5000), 5)
    assert loop.time() - t0 < 1.0            # didn't wait out the timeout
    assert "hello" in r["text"]
    assert "matched" not in r                # no predicate was in effect


# ---- #52: read_screen delta mode (changed rows since a prior hash) ----------

async def test_delta_returns_only_changed_rows(running_agent, broker):
    _, backend, _ = running_agent
    backend.feed(b"\x1b[2J\x1b[H")                 # clear + home
    backend.feed(b"line one\r\nline two\r\nline three\r\n")
    await broker.wait_binary(lambda b: b"line three" in b)
    r1 = await _read_screen(broker, 1)             # baseline (full grid)
    h1 = r1["content_hash"]
    assert not r1.get("delta")                     # first read is never a delta
    assert "line two" in r1["text"]
    # Overwrite exactly ONE row (row 2, 1-based) in place.
    backend.feed(b"\x1b[2;1Hline TWO changed\x1b[K")
    await broker.wait_binary(lambda b: b"TWO changed" in b)
    r2 = await _read_screen(broker, 2, since=h1)
    assert r2.get("delta") is True
    assert r2["text"] == ""                        # full grid omitted
    texts = [c["text"] for c in r2["changed_rows"]]
    assert any("TWO changed" in t for t in texts)
    assert all("line one" not in t for t in texts)  # unchanged rows not resent
    assert r2["content_hash"] != h1
    # Reconstruction: patching changed_rows onto the baseline MUST reproduce the
    # exact screen a full read returns (proves the diff is complete + correct).
    full = await _read_screen(broker, 3)
    patched = r1["text"].split("\n")
    for c in r2["changed_rows"]:
        patched[c["row"]] = c["text"]
    assert "\n".join(patched) == full["text"]
    assert full["content_hash"] == r2["content_hash"]


async def test_delta_full_fallback_on_unknown_since(running_agent, broker):
    _, backend, _ = running_agent
    backend.feed(b"hello world\r\n")
    await broker.wait_binary(lambda b: b"hello world" in b)
    # `since` references a frame the agent never produced -> full grid fallback.
    r = await _read_screen(broker, 2, since="deadbeef" * 4)
    assert not r.get("delta")
    assert "hello world" in r["text"]
    assert r["content_hash"]


async def test_delta_unchanged_screen_is_empty_diff(running_agent, broker):
    _, backend, _ = running_agent
    backend.feed(b"\x1b[2J\x1b[Hstatic content\r\n")
    await broker.wait_binary(lambda b: b"static content" in b)
    h1 = (await _read_screen(broker, 1))["content_hash"]
    # No change between reads -> a delta with zero changed rows (still cheaper
    # than re-sending the whole grid).
    r2 = await _read_screen(broker, 2, since=h1)
    assert r2.get("delta") is True
    assert r2["changed_rows"] == []
    assert r2["content_hash"] == h1


# ---- #27: reset_terminal clears the agent's render buffer -----------------

async def _reset(broker, req):
    """Drive one reset_please RPC and return the matching reset_done."""
    await broker.send({"type": "reset_please", "req": req})
    return await broker.wait_text(
        lambda f: f.get("type") == "reset_done" and f.get("req") == req)


async def test_reset_clears_ring_and_acks(running_agent, broker):
    agent, backend, _ = running_agent
    # Two chunks past the 4096-byte ring force an eviction (evicted=True), so
    # the reset must also clear that flag for a clean-slate next render.
    backend.feed(b"A" * 3000)
    backend.feed(b"some screen content here\r\n" + b"B" * 3000)
    await broker.wait_binary(lambda b: b"some screen content" in b)
    await asyncio.wait_for(_poll_until(lambda: agent.ring.evicted), 5)
    assert len(agent.ring) > 0
    gen_before = agent._output_gen

    done = await _reset(broker, 2)
    assert done["ok"] is True
    assert len(agent.ring) == 0                 # ring emptied
    assert agent.ring.evicted is False          # evicted flag reset
    assert agent._output_gen > gen_before       # waiters were woken

    # Next read renders a blank grid — the prior content is gone, but the
    # reply is still well-formed (bounded grid + a content_hash, #26).
    r = await _read_screen(broker, 3)
    assert "some screen content" not in r["text"]
    assert r["text"].strip() == ""
    assert r["content_hash"]


async def test_reset_wakes_wait_for_change(running_agent, broker):
    # A parked wait-for-change must wake when the screen is reset to blank.
    agent, backend, _ = running_agent
    backend.feed(b"before reset\r\n")
    await broker.wait_binary(lambda b: b"before reset" in b)
    h0 = (await _read_screen(broker, 1))["content_hash"]
    waiter = asyncio.create_task(
        _read_screen(broker, 2, wait_for_change=h0, timeout_ms=5000))
    await asyncio.sleep(0.15)
    assert not waiter.done()
    await _reset(broker, 3)
    r = await asyncio.wait_for(waiter, 5)
    assert r["content_hash"] != h0
    assert "before reset" not in r["text"]


# ---- #133: flush_input drops pending INPUT (mirror of reset for output) -----

async def _flush(broker, req):
    """Drive one flush_input_please RPC and return the matching flush_input_done."""
    await broker.send({"type": "flush_input_please", "req": req})
    return await broker.wait_text(
        lambda f: f.get("type") == "flush_input_done" and f.get("req") == req)


async def test_flush_input_acks_and_leaves_output_untouched(running_agent,
                                                            broker):
    # flush_input is the INPUT-side mirror of reset: the agent acks, but because
    # flushing input changes no OUTPUT it must NOT clear the ring, NOT wake
    # wait_for_change waiters, and NOT touch the keyframe (unlike _reset). The
    # FakeBackend has no input-flush primitive, so it inherits the base no-op —
    # this test pins the agent-side wiring + the "output is untouched" contract.
    agent, backend, _ = running_agent
    backend.feed(b"on the screen\r\n")
    await broker.wait_binary(lambda b: b"on the screen" in b)
    len_before = len(agent.ring)
    gen_before = agent._output_gen

    done = await _flush(broker, 5)
    assert done["ok"] is True
    # OUTPUT state is entirely undisturbed: ring kept, no waiter wakeup.
    assert len(agent.ring) == len_before
    assert agent._output_gen == gen_before
    # The next read still renders the pre-flush screen (nothing was cleared).
    r = await _read_screen(broker, 6)
    assert "on the screen" in r["text"]


async def test_flush_input_forwards_to_backend(broker):
    # The RPC reaches the backend's flush_input(): a backend that records the
    # call proves the client dispatch + agent handler wiring end to end.
    class FlushRecordingBackend(FakeBackend):
        def __init__(self):
            super().__init__()
            self.flushed = 0

        def flush_input(self):
            self.flushed += 1

    backend = FlushRecordingBackend()
    agent = Agent(make_config(broker), backend=backend)
    task = asyncio.create_task(agent.run())
    try:
        await broker.wait_connected()
        done = await _flush(broker, 1)
        assert done["ok"] is True
        assert backend.flushed == 1
    finally:
        backend.exit_child(0)
        await asyncio.wait_for(task, 5)


async def test_flush_input_reports_backend_failure(broker):
    # A backend whose flush_input raises must still be acked — with ok=False and
    # the error string — so the broker never waits out the RPC timeout.
    class BoomBackend(FakeBackend):
        def flush_input(self):
            raise RuntimeError("flush boom")

    backend = BoomBackend()
    agent = Agent(make_config(broker), backend=backend)
    task = asyncio.create_task(agent.run())
    try:
        await broker.wait_connected()
        done = await _flush(broker, 1)
        assert done["ok"] is False
        assert "flush boom" in done["error"]
    finally:
        backend.exit_child(0)
        await asyncio.wait_for(task, 5)


# ---- #128: read_screen attrs -> styled fg/bg/reverse run map ---------------

async def test_read_screen_attrs_surfaces_reverse_video(running_agent, broker):
    # A reverse-video run (a color-only selection marker) is invisible in the
    # plain text but comes back in attr_runs when the read asks for attrs.
    _, backend, _ = running_agent
    backend.feed(b"\x1b[7mABANDON\x1b[0m\r\nkeep\r\n")
    await broker.wait_binary(lambda b: b"ABANDON" in b)
    r = await _read_screen(broker, 1, attrs=True)
    assert "ABANDON" in r["text"]
    assert {"row": 0, "col": 0, "len": 7, "fg": "default",
            "bg": "default", "reverse": True} in r["attr_runs"]
    # A default read (no attrs) omits the key entirely — unchanged behavior.
    plain = await _read_screen(broker, 2)
    assert "attr_runs" not in plain


# ---- #133: read_screen idle_ms (ms since the last PTY output) --------------

async def test_read_screen_reports_idle_ms(running_agent, broker, monkeypatch):
    # _on_pty_data stamps the last-output time and the read reply reports the ms
    # since it. Drive the agent's time.monotonic through a controlled clock (patch
    # the module attribute, not the global module, so only the agent's calls are
    # affected) so idle_ms is exact and the test stays deterministic.
    import types
    from webterm.agent import agent as agent_mod
    agent, backend, _ = running_agent
    clock = [1000.0]
    monkeypatch.setattr(agent_mod, "time",
                        types.SimpleNamespace(monotonic=lambda: clock[0]))
    # Output lands "now": _on_pty_data stamps _last_output_ts to the clock value.
    backend.feed(b"tick\r\n")
    await broker.wait_binary(lambda b: b"tick" in b)
    assert agent._last_output_ts == 1000.0            # _on_pty_data updated it
    # 0.5 s of silence, then a read: idle_ms is the exact gap.
    clock[0] = 1000.5
    r = await _read_screen(broker, 1)
    assert r["idle_ms"] == 500
    # A read right after fresh output reports ~0 (output just landed).
    backend.feed(b"tock\r\n")
    await broker.wait_binary(lambda b: b"tock" in b)
    r2 = await _read_screen(broker, 2)
    assert r2["idle_ms"] == 0


# ---- #130: alt-screen keyframe survives ring eviction ----------------------

try:
    import pyte as _pyte_probe  # noqa: F401
    _HAS_PYTE = True
except ImportError:
    _HAS_PYTE = False

requires_pyte = pytest.mark.skipif(
    not _HAS_PYTE, reason="keyframe reconstruction needs pyte")

# Alt-screen enter + a one-time full paint carrying a unique panel token. A real
# TUI paints this once, then streams only diffs (which never repaint the panel).
_ALT_PAINT = b"\x1b[?1049h\x1b[2J\x1b[1;1HLEGENDPANEL"
# A diff chunk that overwrites the SAME spot on row 3 (no wrap, no scroll, so the
# panel on row 1 is never disturbed). ~1440 bytes -> a few chunks evict the 4096
# ring, dropping the initial paint + its ?1049h/2J markers.
_DIFF_CHUNK = b"\x1b[3;1Hstatus-update-line" * 60


@requires_pyte
async def test_alt_screen_panel_survives_eviction(running_agent, broker):
    # The load-bearing #130 repro: a panel painted once must still read back
    # after >256 KiB (here >4 KiB) of diff-only output evicts the original paint.
    agent, backend, _ = running_agent
    req = 1
    backend.feed(_ALT_PAINT)
    r = await _read_screen(broker, req); req += 1
    assert "LEGENDPANEL" in r["text"]
    assert r["alt_screen"] is True                 # ?1049h sniffed live
    last = None
    for _ in range(20):                            # feed diffs, reading between
        backend.feed(_DIFF_CHUNK)
        r = await _read_screen(broker, req); req += 1
        if agent.ring.evicted:
            break
        last = r                                   # last pre-eviction read
    assert agent.ring.evicted                      # the paint chunk is gone
    # The statically-painted panel SURVIVES the eviction (reconstructed from the
    # immutable keyframe chain), and the read is a full frame, not partial.
    assert "LEGENDPANEL" in r["text"]
    assert "status-update-line" in r["text"]
    assert not r.get("partial")
    assert r.get("degraded") is not True
    # content_hash is stable across the eviction boundary (same settled screen).
    assert last is not None
    assert r["content_hash"] == last["content_hash"]


@requires_pyte
async def test_partial_flag_fires_for_sparse_read_under_flood(running_agent,
                                                              broker):
    # Sparse reads: the client floods WITHOUT reading, so no keyframe is ever
    # seeded and eviction overtakes the paint. The one read after the flood can't
    # reconstruct -> honest partial (a valid grid, just possibly incomplete).
    agent, backend, _ = running_agent
    backend.feed(_ALT_PAINT)
    for _ in range(6):
        backend.feed(_DIFF_CHUNK)                  # ~8.6 KiB, no reads between
    r = await _read_screen(broker, 1)
    assert agent.ring.evicted
    assert r["alt_screen"] is True
    assert r.get("partial") is True                # could not reconstruct
    assert r.get("degraded") is not True           # still a real grid + cursor


async def test_primary_screen_flood_never_seeds_keyframe(running_agent, broker):
    # A non-alt shell flood keeps the hardened off-loop path untouched: no
    # keyframe is ever seeded (so nothing can be reconstructed or flagged
    # partial), regardless of pyte.
    agent, backend, _ = running_agent
    backend.feed(b"\x1b[2J\x1b[H")                  # primary screen, no alt-enter
    backend.feed(b"".join(b"shell-line-%04d\r\n" % i for i in range(600)))
    await asyncio.wait_for(_poll_until(lambda: agent.ring.evicted), 5)
    r = await _read_screen(broker, 1)
    assert r["alt_screen"] is False
    assert r["partial"] is False                    # never flagged on the shell path
    assert agent._screen_keyframe is None          # non-alt never seeds it


@requires_pyte
async def test_reset_nulls_the_keyframe(running_agent, broker):
    # reset_terminal must drop the keyframe alongside the ring, or the next read
    # would reconstruct a stale grid onto the clean slate reset promises.
    agent, backend, _ = running_agent
    backend.feed(_ALT_PAINT)
    await _read_screen(broker, 1)                   # seeds the keyframe
    assert agent._screen_keyframe is not None
    done = await _reset(broker, 2)
    assert done["ok"] is True
    assert agent._screen_keyframe is None
    r = await _read_screen(broker, 3)
    assert "LEGENDPANEL" not in r["text"]           # no stale bleed
    assert r["text"].strip() == ""


@requires_pyte
async def test_resize_nulls_the_keyframe(running_agent, broker):
    # A resize invalidates the keyframe (its per-row CUP would misposition at the
    # new dims); the chain re-seeds from the next trustworthy read.
    agent, backend, _ = running_agent
    backend.feed(_ALT_PAINT)
    await _read_screen(broker, 1)
    assert agent._screen_keyframe is not None
    await broker.send_resize(100, 30)
    await broker.wait_text(lambda f: f.get("type") == "resized")
    assert agent._screen_keyframe is None


# App A's one-time paint carrying a token that only App A ever writes, so any
# read that surfaces it after App B took over is proof of stale cross-app bleed.
_APP_A_PAINT = b"\x1b[?1049h\x1b[2J\x1b[1;1HAPP_A_ONLY_PANEL"
# App B enters alt via DEC private mode 47 (CSI ?47h). The live sniffer counts 47
# as an alt mode (state.alt_screen -> True), but ?47h is NOT one of textgrid's
# _RESTART_MARKERS, so App B's enter can't anchor a trim. App B then streams only
# a row-3 diff (no 2J), so once App A's ?1049h/2J head has evicted, NO restart
# marker survives (the bug_condition) while App A's keyframe_k still sits in the
# ring -- exactly the window in which a STALE App A keyframe would reconstruct
# onto App B's brand-new session unless the transition invalidated it.
_APP_B_ENTER = b"\x1b[?47h"


@requires_pyte
async def test_alt_transition_invalidates_stale_keyframe(running_agent, broker):
    # #130 cross-app corruption: App A's keyframe must never reconstruct onto a
    # DIFFERENT alt session. App A seeds+keeps a keyframe carrying a unique panel;
    # App A leaves alt and App B enters a new alt session streaming only diffs. A
    # read after eviction must NOT bleed App A's panel through the stale keyframe.
    agent, backend, _ = running_agent
    req = 1
    # App A: paint the unique panel on row 1, then flood row-3 diffs (reading
    # between) so the head paint + its ?1049h/2J evict while the keyframe chain
    # keeps App A's panel alive -- mirroring test_alt_screen_panel_survives_evict.
    backend.feed(_APP_A_PAINT)
    r = await _read_screen(broker, req); req += 1
    assert "APP_A_ONLY_PANEL" in r["text"]
    for _ in range(20):
        backend.feed(_DIFF_CHUNK)
        r = await _read_screen(broker, req); req += 1
        if agent.ring.evicted:
            break
    assert agent.ring.evicted                       # App A's head paint is gone
    assert "APP_A_ONLY_PANEL" in r["text"]          # keyframe kept it alive...
    assert agent._screen_keyframe is not None       # ...via a live App A keyframe
    # App A exits its alt session; App B enters a NEW one (mode 47) and paints a
    # single diff-only frame. No read happens in between, so nothing re-seeds a
    # keyframe for App B before the post-transition read below.
    backend.feed(b"\x1b[?1049l")                     # App A leaves alt (True->False)
    backend.feed(_APP_B_ENTER)                       # App B enters alt (False->True)
    backend.feed(b"\x1b[3;1HAPP_B_status_line")      # App B: diff only, no repaint
    r = await _read_screen(broker, req); req += 1
    assert r["alt_screen"] is True                   # App B's ?47h sniffed live
    # The load-bearing assertion: App A's panel is ABSENT. Before the fix the
    # stale keyframe reconstructs and returns a CONFIDENT frame still showing
    # APP_A_ONLY_PANEL; after the fix the transition dropped the keyframe, so this
    # read honestly returns App B content (a partial flag is fine) with no bleed.
    assert "APP_A_ONLY_PANEL" not in r["text"]
