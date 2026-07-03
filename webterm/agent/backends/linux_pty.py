"""Linux/POSIX PTY backend: openpty + Popen + loop.add_reader.

Deliberately not ``pty.fork()``: Popen avoids forking the interpreter, gives
clean child reaping, and surfaces exec errors as exceptions. The child gets
its own session (``start_new_session``) and adopts the slave as controlling
TTY via ``ioctl(0, TIOCSCTTY)`` in preexec_fn — without that, Ctrl-C in the
browser would not interrupt foreground jobs.
"""

from __future__ import annotations

import asyncio
import errno
import fcntl
import os
import pty
import signal
import struct
import subprocess
import termios
import time
from typing import Callable, Mapping, Optional, Sequence, Tuple

from ..detect import _safe_exe, classify_proc
from .base import PtyBackend

_READ_SIZE = 64 * 1024


def _proc_state(pid: int) -> Optional[str]:
    """The single-char state field from ``/proc/<pid>/stat`` ('R'/'S'/'Z'/...),
    or None if the pid is gone/unreadable. Robust to comm strings containing
    ')'. Used to treat zombies as already-dead when waiting for a session to
    drain. Never raises."""
    try:
        with open("/proc/%d/stat" % pid, "rb") as fh:
            data = fh.read()
    except OSError:
        return None
    try:
        rest = data[data.rindex(b")") + 2:]
    except ValueError:
        return None
    return rest[:1].decode("ascii", "replace") if rest else None


def _pin_proc(pid: int):
    """A pidfd pinning ``pid`` (reuse-proof signal target, Linux 5.3+), as
    ``(fd_or_None, already_reaped)``. ``already_reaped`` is True only when the
    pid is provably gone; a None fd with ``already_reaped`` False means pidfd
    is unusable here, so the caller must fall back to numeric pid signalling.
    Requires BOTH ``os.pidfd_open`` and ``os.pidfd_send_signal`` — some builds
    expose one without the other (observed on Ubuntu 24.04: open present,
    send_signal absent), and a fd we can't signal through is useless. A pidfd
    pins the process that occupied ``pid`` at open time, so a later
    ``pidfd_send_signal`` either reaches that exact process or fails with ESRCH
    once it's reaped — it can never hit a recycled pid. Never raises."""
    open_fn = getattr(os, "pidfd_open", None)
    if open_fn is None or not hasattr(os, "pidfd_send_signal"):
        return None, False
    try:
        return open_fn(pid), False
    except ProcessLookupError:
        return None, True
    except OSError:
        return None, False


class LinuxPtyBackend(PtyBackend):
    def __init__(self) -> None:
        self.pid: Optional[int] = None
        self._proc: Optional[subprocess.Popen] = None
        self._master: Optional[int] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._on_data: Optional[Callable[[bytes], None]] = None
        self._on_exit: Optional[Callable[[int], None]] = None
        self._write_buf = bytearray()
        self._writer_armed = False
        self._eof = False

    def spawn(
        self,
        argv: Sequence[str],
        cols: int,
        rows: int,
        cwd: Optional[str] = None,
        env: Optional[Mapping[str, str]] = None,
    ) -> None:
        master, slave = pty.openpty()
        try:
            self._set_winsize(master, cols, rows)

            def _become_controlling_tty() -> None:
                # Runs in the child after setsid() (start_new_session) and
                # after stdio is wired to the slave.
                fcntl.ioctl(0, termios.TIOCSCTTY, 0)

            child_env = dict(env) if env is not None else dict(os.environ)
            child_env.setdefault("TERM", "xterm-256color")

            self._proc = subprocess.Popen(
                list(argv),
                stdin=slave,
                stdout=slave,
                stderr=slave,
                cwd=cwd,
                env=child_env,
                start_new_session=True,
                preexec_fn=_become_controlling_tty,
                close_fds=True,
            )
        except Exception:
            os.close(master)
            raise
        finally:
            # Whether spawn succeeded or not, the parent must not keep the
            # slave open — a lingering parent-side slave fd means EOF (EIO on
            # the master) never arrives after the child exits.
            try:
                os.close(slave)
            except OSError:
                pass

        os.set_blocking(master, False)
        self._master = master
        self.pid = self._proc.pid

    def start(
        self,
        loop: asyncio.AbstractEventLoop,
        on_data: Callable[[bytes], None],
        on_exit: Callable[[int], None],
    ) -> None:
        assert self._master is not None and self._proc is not None
        self._loop = loop
        self._on_data = on_data
        self._on_exit = on_exit
        loop.add_reader(self._master, self._on_readable)

    def _on_readable(self) -> None:
        assert self._master is not None
        try:
            data = os.read(self._master, _READ_SIZE)
        except BlockingIOError:
            return
        except OSError as exc:
            if exc.errno == errno.EIO:
                # All slave fds closed: stream EOF.
                self._handle_eof()
                return
            self._handle_eof()
            return
        if not data:
            self._handle_eof()
            return
        if self._on_data is not None:
            self._on_data(data)

    def _handle_eof(self) -> None:
        if self._eof:
            return
        self._eof = True
        assert self._loop is not None and self._master is not None
        self._loop.remove_reader(self._master)
        if self._writer_armed:
            self._loop.remove_writer(self._master)
            self._writer_armed = False
        try:
            os.close(self._master)
        except OSError:
            pass
        self._master = None
        # proc.wait() can block if a grandchild inherited the PTY; reap off
        # the loop. on_exit still fires after the final on_data because both
        # are sequenced through the loop from here.
        proc = self._proc

        async def _reap() -> None:
            code = await self._loop.run_in_executor(None, proc.wait)
            if self._on_exit is not None:
                self._on_exit(code)

        self._loop.create_task(_reap())

    # -- input ------------------------------------------------------------

    def write(self, data: bytes) -> None:
        if self._master is None or self._eof:
            return
        self._write_buf += data
        self._flush_writes()

    def _flush_writes(self) -> None:
        if self._master is None:
            return
        while self._write_buf:
            try:
                n = os.write(self._master, self._write_buf)
            except BlockingIOError:
                # PTY input buffer full (huge paste): finish via add_writer.
                if not self._writer_armed:
                    self._loop.add_writer(self._master, self._on_writable)
                    self._writer_armed = True
                return
            except OSError:
                self._write_buf.clear()
                return
            del self._write_buf[:n]
        if self._writer_armed:
            self._loop.remove_writer(self._master)
            self._writer_armed = False

    def _on_writable(self) -> None:
        self._flush_writes()

    def flush_input(self) -> None:
        """Discard keystrokes queued toward the child but not yet read (#133).

        The INPUT-direction mirror of reset_terminal: reset clears our OUTPUT
        ring, this drops the pending INPUT backlog — e.g. a runaway send_keys
        burst a frame-polling TUI hasn't drained yet. Two queues hold that
        backlog, cleared in order:

        1. **Our own write buffer** — bytes we accepted but have not handed to
           the kernel yet (a backpressured huge paste parked for ``add_writer``).
           We clear it and disarm the writer: the buffer is now empty, so a
           still-armed writer would only wake to do nothing.
        2. **The slave's kernel input queue** — bytes the kernel holds that the
           app has not ``read()`` yet. ``tcflush(fd, TCIFLUSH)`` is the
           POSIX-defined "discard data received but not read", so it targets the
           keystroke backlog the app hasn't consumed. It is best-effort by
           nature: bytes the app has ALREADY read into its own event queue are
           out of the kernel and cannot be recalled. It must be issued on a
           SLAVE fd, but we deliberately closed the slave at spawn so EOF (EIO on
           the master) arrives after the child exits — so we reopen it transiently
           BY NAME. This stays EOF-safe: EOF needs child-exit AND zero open slave
           fds, and we open then immediately close, so the microsecond overlap at
           most defers EOF until that close. ``O_NOCTTY`` so the reopen never
           steals a controlling tty; ``O_NONBLOCK`` so the open can't stall.

        Never raises (backend contract): if the ptsname/open path fails we fall
        back to a best-effort ``tcflush(master, TCOFLUSH)``. TCOFLUSH on the
        master flushes the master's OUTPUT queue — the bytes WE wrote heading to
        the slave (keystrokes), not the app's output back to us — so even this
        fallback only ever discards input and never loses screen content. Any
        error is swallowed.
        """
        # Step 1: drop our own queued bytes and disarm the now-pointless writer.
        self._write_buf.clear()
        if (self._writer_armed and self._loop is not None
                and self._master is not None):
            try:
                self._loop.remove_writer(self._master)
            except Exception:
                pass          # loop torn down: honor the never-raises contract
            self._writer_armed = False
        # Nothing more to flush on a closed / already-EOF'd PTY.
        if self._master is None or self._eof:
            return
        # Step 2: flush the slave's unread input via a transient reopen by name.
        master = self._master
        try:
            slave = os.ptsname(master)
            fd = os.open(slave, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
            try:
                termios.tcflush(fd, termios.TCIFLUSH)
            finally:
                os.close(fd)
        except (OSError, termios.error):
            # ptsname/open/tcflush unusable (rare): best-effort flush on the
            # master. termios.error is NOT an OSError subclass, so it must be
            # named explicitly or the "never raises" contract above would leak.
            try:
                termios.tcflush(master, termios.TCOFLUSH)
            except (OSError, termios.error):
                pass

    # -- control ----------------------------------------------------------

    def resize(self, cols: int, rows: int) -> None:
        if self._master is None:
            raise OSError("pty closed")
        # The kernel delivers SIGWINCH to the foreground process group itself.
        self._set_winsize(self._master, cols, rows)

    @staticmethod
    def _set_winsize(fd: int, cols: int, rows: int) -> None:
        fcntl.ioctl(fd, termios.TIOCSWINSZ,
                    struct.pack("HHHH", rows, cols, 0, 0))

    def kill(self) -> None:
        if self._proc is not None and self._proc.poll() is None:
            try:
                self._proc.kill()
            except OSError:
                pass

    def exitcode(self) -> Optional[int]:
        if self._proc is None:
            return None
        return self._proc.returncode

    # -- foreground-agent detection --------------------------------------

    def foreground_command(self) -> Optional[str]:
        """The PTY's true foreground process group is the honest signal here:
        when the user runs ``claude``, the shell puts it in its own process
        group and hands it the terminal, so ``tcgetpgrp`` names it. We match
        only processes in that group. If ``tcgetpgrp`` is unavailable (master
        closed mid-EOF, etc.) we fall back to a descendant walk. Never raises."""
        if self.pid is None:
            return None
        try:
            import psutil
        except Exception:
            return None

        # Snapshot candidates: the shell plus every descendant. One denied or
        # vanished process must not blank the whole result.
        try:
            shell = psutil.Process(self.pid)
        except Exception:
            return None
        candidates = [shell]
        try:
            candidates.extend(shell.children(recursive=True))
        except Exception:
            pass

        # Read self._master defensively — it is set to None on EOF.
        master = self._master
        fg_pgid: Optional[int] = None
        if master is not None:
            try:
                fg_pgid = os.tcgetpgrp(master)
            except OSError:
                fg_pgid = None

        if fg_pgid is not None and fg_pgid > 0:
            # Primary signal: only processes in the foreground process group.
            for proc in candidates:
                try:
                    if os.getpgid(proc.pid) != fg_pgid:
                        continue
                    agent = classify_proc(proc.name(), proc.cmdline(),
                                          _safe_exe(proc))
                except Exception:
                    continue
                if agent:
                    return agent
            return None

        # Fallback (tcgetpgrp failed): any agent descendant counts.
        for proc in candidates:
            try:
                agent = classify_proc(proc.name(), proc.cmdline(),
                                      _safe_exe(proc))
            except Exception:
                continue
            if agent:
                return agent
        return None

    # ``cwd()`` is inherited from PtyBackend: it reports the foreground agent's
    # own working dir (falling back to the shell), so the AGENTS.md button opens
    # the dir the agent actually runs in even when the shell sits a level up
    # (issue #47).

    # -- psutil-free destroy-window fallback -----------------------------

    def kill_proc_fallback(self, pid: int) -> Tuple[bool, Optional[str]]:
        """Tear down the window without psutil by killing the shell's whole
        POSIX session. Scoped to ``pid == self.pid`` (the spawned shell); any
        other pid is refused — without psutil there is no descendant list to
        validate an arbitrary pid against, and the task-manager list is empty
        anyway, so the only pid the UI can send for "destroy" is the shell's.

        Why a session-scope kill is correct AND safe here:
        * The shell is spawned with ``start_new_session=True``, so ``self.pid``
          is its own session + process-group leader (``sid == self.pid``). The
          agent itself is in a DIFFERENT session (also ``start_new_session`` at
          launch), so a ``/proc`` scan filtered by the shell's SID can never
          match the agent, the broker, or PID 1.
        * A plain ``killpg(getpgid(self.pid))`` would miss a foreground app
          (e.g. ``claude``) that job control placed in its OWN process group;
          session scope catches the shell AND its job-control children.

        PID-reuse safety without psutil's ``create_time`` guard. This runs in
        an executor thread WHILE the event loop may concurrently reap the shell
        (killing it triggers PTY EOF -> reap), so a stale ``self.pid`` (or any
        member pid) could in principle be recycled mid-operation. Layered
        defenses:
        * ``self._proc.poll() is None`` gates entry (live, unreaped child), and
          the numeric ``sid`` (== ``pid_root``) cannot be recycled while the
          session has any member — the kernel reserves a pid used as a SID
          (verified against ``kernel/pid.c``).
        * Every signal is routed through a **pidfd** (:meth:`_signal_pid`): the
          leader via a handle pinned BEFORE its SID is read, each member via a
          handle pinned at signal time. A pidfd targets the pinned process or
          fails with ESRCH once reaped, so no member is ever signalled by a
          recycled pid — closing the enumerate-then-``os.kill`` TOCTOU.
        On kernels/Python builds without ``pidfd_send_signal`` (e.g. Ubuntu
        24.04, or Linux < 5.3) signalling degrades to numeric ``os.kill`` with a
        ``getsid`` recheck; the residual reuse window then matches the primary
        psutil path's own enumerate-then-kill window.

        Accepted limitations: a descendant that calls ``setsid()`` (daemons,
        double-forkers, a tmux/screen server) gets a NEW session and escapes
        the SID scan — a parity gap vs psutil's ppid tree walk, acceptable for
        a fallback (shell + foreground app + jobs is fully covered). Non-Linux
        POSIX without ``/proc`` (macOS) degrades to a process-group kill. Never
        raises."""
        if pid != self.pid:
            return False, "psutil_unavailable"
        proc = self._proc
        if proc is None or proc.poll() is not None:
            # No live, unreaped child -> self.pid may be recycled; refuse.
            return False, "session_gone"
        pid_root = self.pid
        # macOS et al.: no /proc to enumerate the session -> process-group kill.
        if not os.path.isdir("/proc/self"):
            return self._killpg_fallback(pid_root)
        # Pin the leader with a reuse-proof handle BEFORE reading its session
        # id, so a concurrent reap+recycle can't make getsid() resolve a
        # different process's session.
        leader_fd, leader_gone = _pin_proc(pid_root)
        if leader_gone:
            return False, "session_gone"
        try:
            try:
                sid = os.getsid(pid_root)
            except OSError:
                return False, "session_gone"
            return self._kill_session_via_proc(sid, pid_root, leader_fd)
        finally:
            if leader_fd is not None:
                try:
                    os.close(leader_fd)
                except OSError:
                    pass

    def _session_members(self, sid: int):
        """``(pids, scanned_ok)`` — pids whose session id is ``sid`` (including
        zombies), via ``/proc``. ``scanned_ok`` is False only when ``/proc``
        itself could not be listed, so the caller can distinguish "session is
        empty" from "enumeration failed". Never raises."""
        try:
            entries = os.listdir("/proc")
        except OSError:
            return [], False
        out = []
        for name in entries:
            if not name.isdigit():
                continue
            p = int(name)
            try:
                if os.getsid(p) == sid:
                    out.append(p)
            except OSError:
                continue
        return out, True

    def _signal_pid(self, pid: int, sid: int, sig: int, fd=None) -> None:
        """Reuse-safely send ``sig`` to ``pid``, which must still be a member of
        session ``sid``. When a pidfd is usable the signal goes THROUGH it (the
        provided ``fd`` for the leader, or a freshly pinned one for a member),
        so a pid recycled between the ``getsid`` check and the signal is never
        hit — the pidfd targets the pinned process or fails with ESRCH. Only
        when pidfd is unsupported does it degrade to numeric ``os.kill`` with a
        ``getsid`` recheck (a tiny TOCTOU window, on par with the primary psutil
        path's enumerate-then-kill). Never raises."""
        if fd is not None:
            try:
                if os.getsid(pid) == sid:
                    os.pidfd_send_signal(fd, sig)
            except OSError:
                pass
            return
        own_fd, gone = _pin_proc(pid)
        if own_fd is not None:
            try:
                if os.getsid(pid) == sid:
                    os.pidfd_send_signal(own_fd, sig)
            except OSError:
                pass
            finally:
                try:
                    os.close(own_fd)
                except OSError:
                    pass
            return
        if gone:
            # pidfd IS supported and reports the pid already reaped -> nothing
            # to signal (and no recycled pid to risk hitting).
            return
        # No pidfd here: numeric recheck-then-kill (documented residual).
        try:
            if os.getsid(pid) == sid:
                os.kill(pid, sig)
        except OSError:
            pass

    def _kill_session_via_proc(
        self, sid: int, pid_root: int, leader_fd,
    ) -> Tuple[bool, Optional[str]]:
        """SIGTERM, then (after a grace) SIGKILL every member of session
        ``sid``, each via :meth:`_signal_pid` (pidfd-routed when available, so
        no member is ever signalled by a recycled pid). Never signals the
        agent's own pid. Never raises."""
        my_pid = os.getpid()
        _, scanned_ok = self._session_members(sid)
        if not scanned_ok:
            # /proc unusable -> can't enumerate the session; fall back to the
            # leader's process group rather than silently signalling nothing.
            return self._killpg_fallback(pid_root)

        def _broadcast(sig: int) -> None:
            members, _ = self._session_members(sid)
            for p in members:
                if p == my_pid:
                    continue
                # The leader reuses the handle pinned before getsid(); members
                # are pinned per-signal inside _signal_pid.
                self._signal_pid(p, sid, sig, fd=leader_fd if p == pid_root else None)

        _broadcast(signal.SIGTERM)
        # Poll ~50ms up to ~1.5s for the session's LIVE members to drain
        # (matches the psutil path's wait_procs(timeout=1.5) feel) before
        # escalating. Zombies don't count — they're already dead. Break ONLY on
        # a successful scan that shows no live members, so a transient /proc
        # read failure can't be mistaken for "drained".
        for _ in range(30):
            members, ok = self._session_members(sid)
            if ok and not any(
                _proc_state(p) not in (None, "Z", "X", "x") for p in members
            ):
                break
            time.sleep(0.05)
        _broadcast(signal.SIGKILL)
        return True, None

    def _killpg_fallback(self, pid_root: int) -> Tuple[bool, Optional[str]]:
        """No ``/proc`` (non-Linux POSIX) or unusable ``/proc``: kill the
        shell's process group. Misses job-control children in their own groups,
        but is the best we can do without enumeration. SIGKILL only escalates
        while the group LEADER (our shell) is still alive — a live leader keeps
        the numeric pgid reserved, so it can't have been recycled by an
        unrelated group. (The initial SIGTERM is sent immediately after
        ``getpgid`` with the leader just confirmed live at the call site, so its
        reuse window is negligible and on par with the primary path.) Never
        raises."""
        try:
            pgid = os.getpgid(pid_root)
        except OSError:
            return False, "session_gone"
        try:
            os.killpg(pgid, signal.SIGTERM)
        except OSError:
            pass
        proc = self._proc
        leader_alive = True
        for _ in range(30):
            if proc is None or proc.poll() is not None:
                leader_alive = False
                break
            time.sleep(0.05)
        if leader_alive:
            try:
                os.killpg(pgid, signal.SIGKILL)
            except OSError:
                pass
        return True, None
