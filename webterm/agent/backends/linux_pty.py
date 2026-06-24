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
import struct
import subprocess
import termios
from typing import Callable, Mapping, Optional, Sequence

from ..detect import classify_proc
from .base import PtyBackend

_READ_SIZE = 64 * 1024


def _safe_exe(proc) -> Optional[str]:
    """``proc.exe()`` or None — it raises AccessDenied/ZombieProcess often."""
    try:
        return proc.exe()
    except Exception:
        return None


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

    def cwd(self) -> Optional[str]:
        """The shell's current working directory via psutil, tracking the
        user's ``cd``. Never raises."""
        if self.pid is None:
            return None
        try:
            import psutil
            return psutil.Process(self.pid).cwd()
        except Exception:
            return None
