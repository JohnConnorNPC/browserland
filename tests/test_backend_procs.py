"""enumerate_procs + scoped kill_proc on the shared PtyBackend base
(todo2 task 3, the task manager). These exercise the real psutil walk against
a throwaway child process; they must NEVER touch a pid outside the session
tree (notably the test runner itself)."""

import os
import shutil
import subprocess
import sys
import tempfile
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


# -- agent-aware cwd (issue #47) ---------------------------------------------
# The session shell can sit a level *above* where the agent (Claude Code etc.)
# actually runs; cwd() must report the AGENT's working dir, not the shell's, so
# the "edit AGENTS.md for this folder" button opens the right place.


def _kill_tree(pid):
    """Best-effort: kill `pid` and every descendant so no sleeper outlives the
    test (and so a child's cwd no longer pins a temp dir on Windows)."""
    try:
        proc = psutil.Process(pid)
    except Exception:
        return
    for child in (proc.children(recursive=True) or []):
        try:
            child.kill()
        except Exception:
            pass
    try:
        proc.kill()
    except Exception:
        pass


def _spawn_shell_with_agent(shell_dir, agent_dir):
    """A 'shell' sleeper running in `shell_dir` that spawns a child whose argv[0]
    is 'claude' (so classify_proc tags it the agent) running in `agent_dir`.
    Mirrors the #47 repro: shell in the parent, agent in a subdir."""
    shell_code = (
        "import subprocess, sys, time\n"
        "subprocess.Popen(['claude', '-c', 'import time; time.sleep(60)'], "
        f"executable=sys.executable, cwd={agent_dir!r})\n"
        "time.sleep(60)\n"
    )
    return subprocess.Popen([sys.executable, "-c", shell_code], cwd=shell_dir)


def test_cwd_prefers_foreground_agent_subdir():
    shell_dir = tempfile.mkdtemp()
    agent_dir = tempfile.mkdtemp(dir=shell_dir)  # a subdir of the shell's cwd
    shell = None
    try:
        shell = _spawn_shell_with_agent(shell_dir, agent_dir)
        be = _StubBackend()
        be.pid = shell.pid
        # Wait for the 'claude' grandchild to come up and be classified; until
        # then cwd() correctly falls back to the shell dir.
        got = None
        deadline = time.time() + 10
        while time.time() < deadline:
            got = be.cwd()
            if got and os.path.exists(got) and os.path.samefile(got, agent_dir):
                break
            time.sleep(0.1)
        assert got is not None, "cwd() returned None for a live session"
        assert os.path.samefile(got, agent_dir), (
            f"cwd() = {got!r}; expected the agent's dir {agent_dir!r}, "
            f"not the shell's {shell_dir!r}"
        )
        assert not os.path.samefile(got, shell_dir)
    finally:
        if shell is not None:
            _kill_tree(shell.pid)
        shutil.rmtree(agent_dir, ignore_errors=True)
        shutil.rmtree(shell_dir, ignore_errors=True)


def test_cwd_falls_back_to_shell_when_no_agent():
    shell_dir = tempfile.mkdtemp()
    shell = None
    try:
        # A plain sleeper -> classify_proc finds no agent -> shell's cwd.
        shell = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(60)"], cwd=shell_dir)
        be = _StubBackend()
        be.pid = shell.pid
        got = be.cwd()
        assert got is not None and os.path.samefile(got, shell_dir), (
            f"cwd() = {got!r}; expected the shell dir {shell_dir!r}")
    finally:
        if shell is not None:
            _kill_tree(shell.pid)
        shutil.rmtree(shell_dir, ignore_errors=True)


def test_cwd_none_without_session():
    be = _StubBackend()
    be.pid = None
    assert be.cwd() is None


def test_cwd_respects_session_root_identity_guard():
    """A recycled session-root PID (create_time mismatch) makes cwd() return
    None, just like enumerate/kill — it never reads a stranger's cwd."""
    child = _spawn_sleeper()
    try:
        be = _StubBackend()
        be.pid = child.pid
        be.note_session_started()
        assert be.cwd() is not None  # resolves normally
        be._root_ctime = be._root_ctime + 10000.0  # simulate PID reuse
        assert be.cwd() is None
    finally:
        child.kill()


def test_cwd_picks_shallowest_agent():
    """With agents at two depths, the one CLOSEST to the shell (the session's
    primary) wins — a deeper nested agent must not hijack the reported cwd."""
    base = tempfile.mkdtemp()
    dir_shallow = tempfile.mkdtemp(dir=base)
    dir_deep = tempfile.mkdtemp(dir=base)
    shell = None
    try:
        # A non-agent intermediate that owns the DEEP agent (grandchild of shell).
        inter_code = (
            "import subprocess, sys, time\n"
            "subprocess.Popen(['claude', '-c', 'import time; time.sleep(60)'], "
            f"executable=sys.executable, cwd={dir_deep!r})\n"
            "time.sleep(60)\n"
        )
        shell_code = (
            "import subprocess, sys, time\n"
            # SHALLOW agent: a direct child of the shell (depth 1).
            "subprocess.Popen(['claude', '-c', 'import time; time.sleep(60)'], "
            f"executable=sys.executable, cwd={dir_shallow!r})\n"
            # DEEP agent: under a non-agent intermediate (depth 2).
            f"subprocess.Popen([sys.executable, '-c', {inter_code!r}])\n"
            "time.sleep(60)\n"
        )
        shell = subprocess.Popen([sys.executable, "-c", shell_code], cwd=base)
        be = _StubBackend()
        be.pid = shell.pid
        got = None
        deadline = time.time() + 10
        while time.time() < deadline:
            got = be.cwd()
            if got and os.path.exists(got) and os.path.samefile(got, dir_shallow):
                break
            time.sleep(0.1)
        assert got is not None and os.path.samefile(got, dir_shallow), (
            f"cwd() = {got!r}; expected the shallow agent dir {dir_shallow!r}, "
            f"not the deep one {dir_deep!r}")
    finally:
        if shell is not None:
            _kill_tree(shell.pid)
        shutil.rmtree(base, ignore_errors=True)


def test_cwd_none_when_detected_agent_cwd_unreadable(monkeypatch):
    """If an agent IS detected but its cwd can't be read (denied / mid-exit
    race), cwd() returns None to preserve the last-known dir — it must NOT fall
    back to the shell's cwd, which is the known-wrong parent in the #47 case."""
    shell_dir = tempfile.mkdtemp()
    agent_dir = tempfile.mkdtemp(dir=shell_dir)
    shell = None
    try:
        shell = _spawn_shell_with_agent(shell_dir, agent_dir)
        be = _StubBackend()
        be.pid = shell.pid
        # Wait until the agent is up and normally resolvable.
        deadline = time.time() + 10
        while time.time() < deadline:
            c = be.cwd()
            if c and os.path.exists(c) and os.path.samefile(c, agent_dir):
                break
            time.sleep(0.1)
        # Locate the 'claude' grandchild so we can deny only ITS cwd.
        agent_pid = None
        for p in (psutil.Process(shell.pid).children(recursive=True) or []):
            try:
                cl = p.cmdline()
            except Exception:
                continue
            if cl and os.path.basename(cl[0]).lower().startswith("claude"):
                agent_pid = p.pid
                break
        assert agent_pid is not None, "agent grandchild never appeared"
        real_cwd = psutil.Process.cwd

        def fake_cwd(self):
            if self.pid == agent_pid:
                raise psutil.AccessDenied(agent_pid)
            return real_cwd(self)

        monkeypatch.setattr(psutil.Process, "cwd", fake_cwd)
        # Agent detected, agent.cwd() denied -> None, NOT the shell's parent dir.
        assert be.cwd() is None
    finally:
        if shell is not None:
            _kill_tree(shell.pid)
        shutil.rmtree(agent_dir, ignore_errors=True)
        shutil.rmtree(shell_dir, ignore_errors=True)
