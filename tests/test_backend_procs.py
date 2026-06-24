"""enumerate_procs + scoped kill_proc on the shared PtyBackend base
(todo2 task 3, the task manager). These exercise the real psutil walk against
a throwaway child process; they must NEVER touch a pid outside the session
tree (notably the test runner itself)."""

import os
import subprocess
import sys
import time

import pytest

psutil = pytest.importorskip("psutil")

from webterm.agent.backends.base import PtyBackend


class _StubBackend(PtyBackend):
    """Concrete shell so we can instantiate the base; only ``pid`` matters for
    enumerate_procs/kill_proc."""

    def spawn(self, *a, **k): ...
    def start(self, *a, **k): ...
    def write(self, data): ...
    def resize(self, cols, rows): ...
    def kill(self): ...
    def exitcode(self): return None


def _spawn_sleeper():
    return subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])


def test_enumerate_includes_session_shell():
    child = _spawn_sleeper()
    try:
        be = _StubBackend()
        be.pid = child.pid
        procs = be.enumerate_procs()
        assert isinstance(procs, list) and procs
        pids = {p["pid"] for p in procs}
        assert child.pid in pids
        row = next(p for p in procs if p["pid"] == child.pid)
        assert "name" in row and "cmdline" in row and "mem_mb" in row
    finally:
        child.kill()


def test_kill_rejects_pid_outside_tree():
    child = _spawn_sleeper()
    try:
        be = _StubBackend()
        be.pid = child.pid
        # The test runner is the PARENT of `child`, so it is NOT in child's
        # tree — kill_proc must refuse it (and absolutely not kill us).
        ok, err = be.kill_proc(os.getpid())
        assert ok is False and err == "not_in_session"
        # Bad pids.
        assert be.kill_proc(-1) == (False, "bad_pid")
        assert be.kill_proc(0) == (False, "bad_pid")
        # Nonexistent high pid -> not in the session set.
        ok, err = be.kill_proc(2_000_000_000)
        assert ok is False and err == "not_in_session"
        # Still alive after all the refusals.
        assert child.poll() is None
    finally:
        child.kill()


def test_kill_terminates_session_member():
    child = _spawn_sleeper()
    try:
        be = _StubBackend()
        be.pid = child.pid
        ok, err = be.kill_proc(child.pid)
        assert ok is True and err is None
        # The sleeper should be gone shortly.
        for _ in range(50):
            if child.poll() is not None:
                break
            time.sleep(0.1)
        assert child.poll() is not None
    finally:
        if child.poll() is None:
            child.kill()


def test_no_session_pid():
    be = _StubBackend()
    be.pid = None
    assert be.enumerate_procs() == []
    assert be.kill_proc(123) == (False, "no_session")


def test_session_root_identity_guard():
    """note_session_started pins the shell's create_time; a mismatch (a
    recycled session-root PID) makes enumerate/kill refuse the whole tree."""
    child = _spawn_sleeper()
    try:
        be = _StubBackend()
        be.pid = child.pid
        be.note_session_started()
        assert be._root_ctime is not None
        # Sanity: with the real identity, the root resolves and lists.
        assert any(p["pid"] == child.pid for p in be.enumerate_procs())
        # Tamper with the pinned identity to simulate PID reuse -> rejected.
        be._root_ctime = be._root_ctime + 10000.0
        assert be.enumerate_procs() == []
        assert be.kill_proc(child.pid) == (False, "session_gone")
        # The child must be untouched by the refused kill.
        assert child.poll() is None
    finally:
        child.kill()
