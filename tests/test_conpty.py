"""Windows ConPTY end-to-end: real cmd.exe through the full Agent against
the fake broker (the automatable part of the M3 smoke)."""

from __future__ import annotations

import asyncio
import os

import pytest

from webterm.agent.agent import Agent
from webterm.agent.config import AgentConfig

from .fake_broker import FakeBroker

pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows ConPTY only")


@pytest.fixture
async def broker():
    fb = FakeBroker()
    await fb.start()
    yield fb
    await fb.stop()


async def test_cmd_exe_round_trip(broker):
    config = AgentConfig(
        command=(os.environ.get("COMSPEC", "cmd.exe"),),
        broker_url=broker.url,
        cols=100,
        rows=30,
        title="cmd",
        window_id=777002,
    )
    agent = Agent(config)
    task = asyncio.create_task(agent.run())
    try:
        await broker.wait_connected(timeout=15)
        hello = broker.hellos[0]
        assert hello["cols"] == 100 and hello["rows"] == 30
        assert hello["pid"] > 0

        # Wait for the cmd banner/prompt, then run a marker command.
        await broker.wait_binary(lambda b: b">" in b, timeout=15)
        await broker.send_input("echo marker_12345\r\n")
        await broker.wait_binary(lambda b: b"marker_12345" in b, timeout=15)

        # Resize round-trips through ConPTY.
        await broker.send_resize(90, 28)
        frame = await broker.wait_text(
            lambda f: f.get("type") == "resized", timeout=15)
        assert frame["cols"] == 90 and frame["rows"] == 28

        # Snapshot has the preamble and reflects the screen.
        await broker.request_snapshot()
        snap = await broker.wait_binary(
            lambda b: b.startswith(b"\x1b[0m\x1b[2J\x1b[H"), timeout=15)
        assert b"marker_12345" in snap

        # title sniffing through a real ConPTY (cmd's `title` emits OSC 0).
        await broker.send_input("title conpty_title_test\r\n")
        frame = await broker.wait_text(
            lambda f: f.get("type") == "title" and
            "conpty_title_test" in f.get("data", ""), timeout=15)

        # Clean exit propagates the child's code.
        await broker.send_input("exit\r\n")
        code = await asyncio.wait_for(task, 20)
        assert code == 0
    finally:
        if not task.done():
            agent.backend.kill()
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


async def test_conpty_with_console_ctrl_c_interrupts(broker):
    """#25: when a hidden console can be acquired, the auto/ConPTY backend must
    still interrupt a running child. Skips where the mechanism is unavailable
    (e.g. a ConPTY-hosted test host where AllocConsole is denied) so the suite
    never hangs — the detached-agent spike carries the full proof."""
    from webterm.agent.backends.win_conpty import _ensure_hidden_console

    if not _ensure_hidden_console():
        pytest.skip("hidden console unavailable here (already console-attached)")

    config = AgentConfig(
        command=(os.environ.get("COMSPEC", "cmd.exe"),),
        broker_url=broker.url,
        cols=80,
        rows=24,
        title="cmd",
        window_id=777004,
        pty_backend="conpty",
    )
    agent = Agent(config)
    assert agent.backend._backend_name == "ConPTY"
    task = asyncio.create_task(agent.run())
    try:
        await broker.wait_connected(timeout=15)
        await broker.wait_binary(lambda b: b">" in b, timeout=15)
        await broker.send_input("ping -t 127.0.0.1\r\n")
        await broker.wait_binary(
            lambda b: b"Reply from 127.0.0.1" in b, timeout=15)
        await broker.send_input("\x03")
        await broker.wait_binary(lambda b: b"Control-C" in b, timeout=15)
        await broker.send_input("exit 0\r\n")
        code = await asyncio.wait_for(task, 20)
        assert code == 0
    finally:
        if not task.done():
            agent.backend.kill()
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


async def test_winpty_backend_ctrl_c_interrupts(broker):
    """WinPTY is the auto-selected backend for headless agents (ConPTY drops
    the ^C translation without a console window) — verify the wiring and
    that 0x03 actually interrupts a foreground command."""
    config = AgentConfig(
        command=(os.environ.get("COMSPEC", "cmd.exe"),),
        broker_url=broker.url,
        cols=80,
        rows=24,
        title="cmd",
        window_id=777003,
        pty_backend="winpty",
    )
    agent = Agent(config)
    task = asyncio.create_task(agent.run())
    try:
        await broker.wait_connected(timeout=15)
        await broker.wait_binary(lambda b: b">" in b, timeout=15)
        await broker.send_input("ping -t 127.0.0.1\r\n")
        await broker.wait_binary(
            lambda b: b"Reply from 127.0.0.1" in b, timeout=15)
        await broker.send_input("\x03")
        await broker.wait_binary(lambda b: b"Control-C" in b, timeout=15)
        # `exit` alone would propagate ping's STATUS_CONTROL_C_EXIT
        # errorlevel — pin the code to prove clean exit plumbing.
        await broker.send_input("exit 0\r\n")
        code = await asyncio.wait_for(task, 20)
        assert code == 0
    finally:
        if not task.done():
            agent.backend.kill()
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
