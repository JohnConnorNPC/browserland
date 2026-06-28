"""Agent integration against the fake broker, with a scripted backend.

Covers the protocol/agent logic deterministically: hello-first, output
relay, input/resize round-trips, snapshot framing, title sniffing into
re-hello, and reconnect with backoff. The real-ConPTY path is in
test_conpty.py.
"""

from __future__ import annotations

import asyncio
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
