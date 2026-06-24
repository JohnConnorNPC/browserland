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
