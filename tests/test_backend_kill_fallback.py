"""kill_proc_fallback: the psutil-free destroy-window path (issue #12).

Two layers:

* Delegation (cross-platform, runs on Windows too): with psutil import
  blocked, ``PtyBackend.kill_proc`` must route to ``kill_proc_fallback`` AFTER
  the existing pid validation. The base hook still returns
  ``psutil_unavailable`` (today's behavior), an override is reached.
* Linux integration (POSIX + ``/proc`` only): a real ``LinuxPtyBackend`` shell
  is torn down by a SID-scoped session kill — including a job-control child in
  its OWN process group — without psutil, while a pid outside the session is
  refused.

NOTE: no module-level ``importorskip("psutil")`` — these tests must run with
psutil absent (and several actively block its import). ``LinuxPtyBackend`` is
imported lazily inside the POSIX tests because ``linux_pty`` pulls in POSIX-only
modules (``pty``/``fcntl``/``termios``) that don't import on Windows."""

import builtins
import os
import subprocess
import sys
import time

import pytest

from webterm.agent.backends.base import PtyBackend

posix_only = pytest.mark.skipif(
    os.name != "posix" or not os.path.isdir("/proc/self"),
    reason="needs POSIX with /proc",
)


def _block_psutil(monkeypatch):
    """Make ``import psutil`` raise ImportError for the duration of the test,
    even if psutil is installed — exercises the no-psutil code path."""
    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "psutil" or name.startswith("psutil."):
            raise ImportError("psutil blocked for test")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)


# --------------------------------------------------------------------------
# Delegation (cross-platform: this also runs on the Windows dev box)
# --------------------------------------------------------------------------

class _PlainStub(PtyBackend):
    """Concrete shell with no fallback override -> inherits the base hook."""

    def spawn(self, *a, **k): ...
    def start(self, *a, **k): ...
    def write(self, data): ...
    def resize(self, cols, rows): ...
    def kill(self): ...
    def exitcode(self): return None


class _FallbackStub(_PlainStub):
    """Records that the override was reached and returns a sentinel distinct
    from the base hook's ``psutil_unavailable``."""

    fallback_pid = None

    def kill_proc_fallback(self, pid):
        self.fallback_pid = pid
        return True, "stub_fallback"


def test_kill_proc_delegates_to_fallback_without_psutil(monkeypatch):
    be = _FallbackStub()
    be.pid = 4242
    _block_psutil(monkeypatch)
    assert be.kill_proc(be.pid) == (True, "stub_fallback")
    assert be.fallback_pid == 4242


def test_default_fallback_preserves_psutil_unavailable(monkeypatch):
    be = _PlainStub()
    be.pid = 4242
    _block_psutil(monkeypatch)
    # Backends without a psutil-free path keep today's exact behavior.
    assert be.kill_proc(be.pid) == (False, "psutil_unavailable")


def test_validation_runs_before_fallback(monkeypatch):
    be = _FallbackStub()
    _block_psutil(monkeypatch)
    # no_session is decided before the psutil import -> fallback NOT reached.
    be.pid = None
    assert be.kill_proc(123) == (False, "no_session")
    assert be.fallback_pid is None
    # bad_pid likewise short-circuits above the import.
    be.pid = 4242
    assert be.kill_proc(-1) == (False, "bad_pid")
    assert be.kill_proc(0) == (False, "bad_pid")
    assert be.fallback_pid is None


# --------------------------------------------------------------------------
# Linux integration (real LinuxPtyBackend, psutil blocked)
# --------------------------------------------------------------------------

def _spawn_linux(argv):
    from webterm.agent.backends.linux_pty import LinuxPtyBackend
    be = LinuxPtyBackend()
    be.spawn(list(argv), 80, 24)
    return be


def _cleanup(be):
    try:
        be.kill()
    except Exception:
        pass


def _proc_alive(pid):
    """True iff ``pid`` exists and is not a zombie, via ``/proc``. Robust to
    process names containing ')'."""
    try:
        with open("/proc/%d/stat" % pid, "rb") as fh:
            data = fh.read()
    except OSError:
        return False
    try:
        state = data[data.rindex(b")") + 2:].split(b" ", 1)[0]
    except ValueError:
        return False
    return state not in (b"Z", b"X", b"x")


def _wait_until(pred, timeout=3.0, step=0.05):
    deadline = timeout
    elapsed = 0.0
    while elapsed < deadline:
        if pred():
            return True
        time.sleep(step)
        elapsed += step
    return pred()


@posix_only
def test_fallback_kills_shell_session(monkeypatch):
    _block_psutil(monkeypatch)
    be = _spawn_linux(["/bin/sh", "-c", "sleep 60"])
    try:
        assert be.kill_proc(be.pid) == (True, None)
        # The shell (our live child) is reaped via poll() once SIGKILL lands.
        assert _wait_until(lambda: be._proc.poll() is not None)
    finally:
        _cleanup(be)


@posix_only
def test_fallback_kills_job_control_child(tmp_path, monkeypatch):
    """A descendant in its OWN process group but the SAME session (exactly what
    shell job control does for a foreground app) is caught by the SID scan —
    a plain killpg(shell_pgrp) would miss it."""
    _block_psutil(monkeypatch)
    pidfile = tmp_path / "child.pid"
    script = (
        "import os, sys, time, subprocess\n"
        "child = subprocess.Popen(\n"
        "    [sys.executable, '-c', 'import time; time.sleep(120)'],\n"
        "    preexec_fn=lambda: os.setpgid(0, 0))\n"  # own pgroup, same session
        "open(sys.argv[1], 'w').write(str(child.pid))\n"
        "time.sleep(120)\n"
    )
    be = _spawn_linux([sys.executable, "-c", script, str(pidfile)])
    try:
        assert _wait_until(lambda: pidfile.exists() and pidfile.read_text())
        child_pid = int(pidfile.read_text())
        # Precondition: same session as the shell, but a DIFFERENT process
        # group (so killpg of the shell's group could never reach it).
        assert os.getsid(child_pid) == os.getsid(be.pid)
        assert os.getpgid(child_pid) != os.getpgid(be.pid)

        assert be.kill_proc(be.pid) == (True, None)
        assert _wait_until(lambda: be._proc.poll() is not None)
        assert _wait_until(lambda: not _proc_alive(child_pid))
    finally:
        _cleanup(be)
        try:
            os.kill(child_pid, 9)  # belt-and-suspenders if the assert path bailed
        except Exception:
            pass


@posix_only
def test_fallback_refuses_pid_other_than_shell(monkeypatch):
    """Without psutil there is no descendant list, so any pid != self.pid is
    refused (no arbitrary-PID kill) and survives untouched."""
    _block_psutil(monkeypatch)
    be = _spawn_linux(["/bin/sh", "-c", "sleep 60"])
    other = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        assert be.kill_proc(other.pid) == (False, "psutil_unavailable")
        assert other.poll() is None  # untouched
    finally:
        other.kill()
        _cleanup(be)


@posix_only
def test_fallback_session_gone_when_shell_exited(monkeypatch):
    _block_psutil(monkeypatch)
    be = _spawn_linux(["/bin/sh", "-c", "exit 7"])
    be._proc.wait(timeout=5)  # let the shell exit + reap it
    try:
        assert be.kill_proc(be.pid) == (False, "session_gone")
    finally:
        _cleanup(be)


@posix_only
def test_fallback_uses_pidfd_path(tmp_path, monkeypatch):
    """Exercise the reuse-proof pidfd path end-to-end for BOTH the leader and a
    non-leader job-control member. Some builds expose os.pidfd_open but not
    os.pidfd_send_signal, so inject a shim that resolves the pidfd back to its
    pid (via /proc fdinfo) and signals it — the real ``_pin_proc`` ->
    ``os.pidfd_send_signal`` branch then runs and actually tears the tree down."""
    if not hasattr(os, "pidfd_open"):
        pytest.skip("kernel/Python lacks pidfd_open")

    seen = []

    def _pidfd_pid(fd):
        with open("/proc/self/fdinfo/%d" % fd) as fh:
            for line in fh:
                if line.startswith("Pid:"):
                    return int(line.split()[1])
        return -1

    def _shim_send(fd, sig, *a, **k):
        pid = _pidfd_pid(fd)
        # A reaped pidfd reports -1; os.kill(-1, ...) would signal EVERY
        # process we can reach. Refuse anything non-positive.
        if pid <= 0:
            return
        seen.append(pid)
        os.kill(pid, sig)

    monkeypatch.setattr(os, "pidfd_send_signal", _shim_send, raising=False)
    _block_psutil(monkeypatch)
    pidfile = tmp_path / "child.pid"
    script = (
        "import os, sys, time, subprocess\n"
        "child = subprocess.Popen(\n"
        "    [sys.executable, '-c', 'import time; time.sleep(120)'],\n"
        "    preexec_fn=lambda: os.setpgid(0, 0))\n"  # own pgroup, same session
        "open(sys.argv[1], 'w').write(str(child.pid))\n"
        "time.sleep(120)\n"
    )
    be = _spawn_linux([sys.executable, "-c", script, str(pidfile)])
    try:
        assert _wait_until(lambda: pidfile.exists() and pidfile.read_text())
        child_pid = int(pidfile.read_text())
        assert be.kill_proc(be.pid) == (True, None)
        assert _wait_until(lambda: be._proc.poll() is not None)
        assert _wait_until(lambda: not _proc_alive(child_pid))
        # BOTH the leader and the non-leader member were reached THROUGH a pidfd.
        assert be.pid in seen      # leader  -> _signal_pid(fd=leader_fd)
        assert child_pid in seen   # member  -> _signal_pid(fd=None -> _pin_proc)
    finally:
        _cleanup(be)
        try:
            os.kill(child_pid, 9)
        except Exception:
            pass


@posix_only
def test_agent_session_is_disjoint_from_shell(monkeypatch):
    """Invariant the whole fallback rests on: the agent (this test process)
    is NEVER in the shell's session, so a session-scoped kill can't hit it."""
    be = _spawn_linux(["/bin/sh", "-c", "sleep 60"])
    try:
        assert os.getsid(os.getpid()) != os.getsid(be.pid)
    finally:
        _cleanup(be)
