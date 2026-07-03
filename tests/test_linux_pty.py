"""POSIX PTY end-to-end: real bash/sh/cat through the full Agent against
the fake broker (the Linux counterpart of test_conpty.py).

Written blind on a Windows dev box — every assertion avoids prompt
content (root # vs $ vs themes), matches only strings that cannot appear
in the typed input echo, uses \n line endings (ICRNL not assumed), and
keeps paste lines under MAX_CANON. See LINUX_VERIFICATION.md.
"""

from __future__ import annotations

import asyncio
import os
import shutil

import pytest

from webterm.agent.agent import Agent
from webterm.agent.config import AgentConfig

from .fake_broker import FakeBroker

pytestmark = pytest.mark.skipif(os.name == "nt", reason="POSIX PTY only")
requires_bash = pytest.mark.skipif(
    shutil.which("bash") is None, reason="bash not installed")

# --norc/--noprofile kill rc/login noise deterministically; the agent's
# default TERM=xterm-256color covers the rest.
BASH = ("bash", "--norc", "--noprofile")


@pytest.fixture
async def broker():
    fb = FakeBroker()
    await fb.start()
    yield fb
    await fb.stop()


@requires_bash
async def test_bash_round_trip(broker):
    config = AgentConfig(
        command=BASH,
        broker_url=broker.url,
        cols=100,
        rows=30,
        title="bash",
        window_id=777101,
    )
    agent = Agent(config)
    task = asyncio.create_task(agent.run())
    try:
        await broker.wait_connected(timeout=15)
        hello = broker.hellos[0]
        assert hello["cols"] == 100 and hello["rows"] == 30
        assert hello["pid"] > 0

        # Don't wait for the prompt: output produced before the client
        # connects goes to the ring only (by design — snapshot heals), and
        # bash wins that race on a fast box. PTY input buffers until bash
        # reads it, so the first command doubles as the readiness probe.
        # Arithmetic expansion: output says linux_41005, the typed echo
        # only ever contains linux_$((41000+5)).
        await broker.send_input("echo linux_$((41000+5))\n")
        await broker.wait_binary(lambda b: b"linux_41005" in b, timeout=15)

        # Resize round-trips through TIOCSWINSZ.
        await broker.send_resize(90, 28)
        frame = await broker.wait_text(
            lambda f: f.get("type") == "resized", timeout=15)
        assert frame["cols"] == 90 and frame["rows"] == 28

        # ...and the kernel actually has the new winsize (rows cols).
        await broker.send_input("stty size\n")
        await broker.wait_binary(lambda b: b"28 90" in b, timeout=15)

        # Snapshot has the preamble and reflects the screen.
        await broker.request_snapshot()
        snap = await broker.wait_binary(
            lambda b: b.startswith(b"\x1b[0m\x1b[2J\x1b[H"), timeout=15)
        assert b"linux_41005" in snap

        # Title sniffing through a real PTY: the printf builtin emits a
        # real OSC 0 (ESC ] 0 ; ... BEL); the typed echo only carries the
        # literal backslash sequences, so a title frame proves PTY output.
        await broker.send_input("printf '\\033]0;linux_title_test\\007'\n")
        await broker.wait_text(
            lambda f: f.get("type") == "title" and
            "linux_title_test" in f.get("data", ""), timeout=15)

        # Nonzero exit proves the WEXITSTATUS decode in _reap.
        await broker.send_input("exit 7\n")
        code = await asyncio.wait_for(task, 20)
        assert code == 7
    finally:
        if not task.done():
            agent.backend.kill()
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


@requires_bash
async def test_ctrl_c_interrupts_foreground(broker):
    """0x03 must interrupt a foreground job — proves TIOCSCTTY took effect
    (without a controlling TTY the kernel never delivers SIGINT)."""
    config = AgentConfig(
        command=BASH,
        broker_url=broker.url,
        cols=80,
        rows=24,
        title="bash",
        window_id=777102,
    )
    agent = Agent(config)
    task = asyncio.create_task(agent.run())
    try:
        await broker.wait_connected(timeout=15)
        # Readiness handshake (the pre-connect prompt is never streamed —
        # see test_bash_round_trip); output go_102 differs from the echo.
        await broker.send_input("echo go_$((100+2))\n")
        await broker.wait_binary(lambda b: b"go_102" in b, timeout=15)

        await broker.send_input("sleep 100\n")
        # sleep must own the foreground pgrp before ^C arrives.
        await asyncio.sleep(1.0)
        await broker.send_input("\x03")
        # No ^C-echo assertion (ECHOCTL-dependent); 128+SIGINT=130 is the
        # proof, and the typed echo only says intr:$?.
        await broker.send_input("echo intr:$?\n")
        await broker.wait_binary(lambda b: b"intr:130" in b, timeout=15)

        await broker.send_input("exit 0\n")
        code = await asyncio.wait_for(task, 20)
        assert code == 0
    finally:
        if not task.done():
            agent.backend.kill()
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


async def test_exit_code_without_interaction(broker):
    """Spawn -> child exits -> EIO -> EOF -> reap, no input at all.
    Isolates the backend lifecycle; high diagnostic value if the
    interactive tests fail. The child may beat the hello, so no
    wait_connected — by design."""
    config = AgentConfig(
        command=("sh", "-c", "exit 7"),
        broker_url=broker.url,
        cols=80,
        rows=24,
        title="sh",
        window_id=777103,
    )
    agent = Agent(config)
    task = asyncio.create_task(agent.run())
    try:
        code = await asyncio.wait_for(task, 15)
        assert code == 7
    finally:
        if not task.done():
            agent.backend.kill()
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


async def test_flush_input_clears_pending_write_buffer():
    """#133: flush_input drops our queued write bytes AND disarms the writer, then
    flushes the slave's unread input via a transient reopen — on a REAL openpty.

    We seed the state _flush_writes leaves behind when the PTY input buffer is
    full (bytes still buffered, writer armed on the master fd) and assert
    flush_input clears both. The transient tcflush(TCIFLUSH) on the reopened
    slave must run without raising on a live pty (cat keeps the child alive)."""
    from webterm.agent.backends.linux_pty import LinuxPtyBackend

    loop = asyncio.get_running_loop()
    backend = LinuxPtyBackend()
    # cat blocks reading stdin, so the child stays alive (not EOF) through flush.
    backend.spawn(("cat",), 80, 24)
    backend.start(loop, lambda d: None, lambda c: None)
    master = backend._master
    assert master is not None
    try:
        # No await between arming and flush, so the writer callback can't fire
        # first — the seeded backpressure state survives into flush_input.
        backend._write_buf += b"unconsumed keystrokes"
        loop.add_writer(master, backend._on_writable)
        backend._writer_armed = True

        backend.flush_input()

        assert bytes(backend._write_buf) == b""        # our queue dropped
        assert backend._writer_armed is False          # writer disarmed
    finally:
        try:
            loop.remove_reader(master)
        except Exception:
            pass
        backend.kill()
        try:
            os.close(master)
        except OSError:
            pass
        if backend._proc is not None:
            await loop.run_in_executor(None, backend._proc.wait)


async def test_huge_paste_backpressure(broker):
    """One ~404 KB send_input overflows the PTY input buffer and exercises
    BlockingIOError -> add_writer -> _on_writable. cat (no shell, raw-ish
    pipe through the line discipline) keeps it deterministic; lines stay
    well under MAX_CANON (4096) so canonical mode can't truncate them."""
    config = AgentConfig(
        command=("cat",),
        broker_url=broker.url,
        cols=80,
        rows=24,
        title="cat",
        window_id=777104,
    )
    agent = Agent(config)
    task = asyncio.create_task(agent.run())
    try:
        await broker.wait_connected(timeout=15)

        payload = ("x" * 100 + "\n") * 4000 + "END_OF_PASTE_77\n"
        await broker.send_input(payload)
        await broker.wait_binary(
            lambda b: b"END_OF_PASTE_77" in b, timeout=30)

        # VEOF at line start: cat sees EOF and exits cleanly.
        await broker.send_input("\x04")
        code = await asyncio.wait_for(task, 20)
        assert code == 0
    finally:
        if not task.done():
            agent.backend.kill()
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
