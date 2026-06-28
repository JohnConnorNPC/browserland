"""PTY backend interface.

Contract every backend must honor:

* ``on_data(chunk: bytes)`` always fires **on the event loop**, in stream
  order. Each callback argument is one read's worth of bytes.
* ``on_exit(code: int)`` fires exactly once, on the event loop, **after the
  last on_data** for the stream.
* ``write``/``resize``/``kill`` are called from the event loop thread and
  must not block meaningfully.
"""

from __future__ import annotations

import asyncio
import logging
from abc import ABC, abstractmethod
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from ..detect import _safe_exe, classify_proc

LOGGER = logging.getLogger(__name__)

# Caps for the task-manager process list (DoS / privacy bounds): never report
# more than this many processes, and truncate each cmdline string.
_MAX_PROCS = 500
_MAX_CMDLINE = 512
# create_time() is float seconds; allow a hair of slop when matching identity.
_CTIME_EPS = 0.5


class PtyBackend(ABC):
    pid: Optional[int] = None
    # The shell's create_time, pinned right after spawn (note_session_started).
    # enumerate_procs/kill_proc verify self.pid still has THIS create_time before
    # treating it as the session root, so a reused PID (shell exited, its pid
    # recycled before the agent does) can never become an in-scope kill target.
    _root_ctime: Optional[float] = None

    @abstractmethod
    def spawn(
        self,
        argv: Sequence[str],
        cols: int,
        rows: int,
        cwd: Optional[str] = None,
        env: Optional[Mapping[str, str]] = None,
    ) -> None:
        """Create the PTY and start the child process. Sets ``self.pid``."""

    @abstractmethod
    def start(
        self,
        loop: asyncio.AbstractEventLoop,
        on_data: Callable[[bytes], None],
        on_exit: Callable[[int], None],
    ) -> None:
        """Begin delivering output. Must be called after spawn()."""

    @abstractmethod
    def write(self, data: bytes) -> None:
        """Write raw bytes to the PTY input (keystrokes/paste)."""

    @abstractmethod
    def resize(self, cols: int, rows: int) -> None:
        """Resize the PTY. Raises on failure (caller echoes current dims)."""

    @abstractmethod
    def kill(self) -> None:
        """Forcefully terminate the child and release the PTY."""

    @abstractmethod
    def exitcode(self) -> Optional[int]:
        """Child exit code, or None while still running."""

    def foreground_command(self) -> Optional[str]:
        """Best-effort: name of the agent ('claude'/'grok'/'codex') currently
        running in this PTY, or None. Must NEVER raise — it runs on a periodic
        timer and a failure here must not disturb the session. The default
        implementation knows nothing; platform backends override it.

        This is an *indicator*, not proof the shell is idle: None only means
        none of the known agents is the foreground/descendant process."""
        return None

    def cwd(self) -> Optional[str]:
        """Best-effort working directory for this session's file tools, or None.

        Prefer the AGENT'S own cwd (Claude Code / codex / grok / opencode) over
        the shell's: the agent can run in a *subdir* of where the session shell
        sits, in which case the shell's cwd points a level too high and the
        "edit AGENTS.md for this folder" button opens the wrong place (issue
        #47). We BFS the session tree and pick the agent CLOSEST to the shell
        (the session's primary, so a deeper/nested agent can't win), and report
        its cwd.

        Fallback rules matter: if an agent IS detected but its cwd can't be read
        (denied / mid-exit race), we return None — NOT the shell's cwd — so the
        caller keeps the last-known agent dir instead of flapping to the
        known-wrong parent. Only a session with NO agent at all falls back to
        the shell's own cwd (a plain shell, where that IS the right answer).

        Unlike ``foreground_command`` this is deliberately NOT foreground-group
        filtered — the agent's working dir should stay stable even while it runs
        a foreground child (a build, a Bash tool call). Runs on a periodic
        timer; like ``foreground_command`` it must NEVER raise."""
        shell = self._session_root()
        if shell is None:
            return None
        agent = self._find_session_agent(shell)
        if agent is not None:
            # Agent known: its cwd, or None to preserve the last-known dir. We
            # never fall through to the shell here -- in the #47 case the shell
            # sits a level up, so reporting it would re-introduce the bug.
            # (A descendant PID could in theory be recycled between classify and
            # this read; the window is sub-tick and self-corrects next tick.)
            try:
                cwd = agent.cwd()
            except Exception:
                cwd = None
            return cwd or None
        # No agent in the tree -> a plain shell session; the shell's own cwd is
        # the right answer (and tracks the user's `cd`).
        try:
            return shell.cwd() or None
        except Exception:
            return None

    def _find_session_agent(self, shell):
        """The ``psutil.Process`` of the agent CLOSEST to the session ``shell``
        (breadth-first, so the session's primary agent wins over a deeper nested
        one), or None. Bounded by ``_MAX_PROCS`` against a fork-bomb; a denied or
        vanished process is skipped, never fatal. Never raises."""
        seen: set = set()
        level = [shell]
        while level and len(seen) < _MAX_PROCS:
            nxt = []
            for proc in level:
                if proc.pid in seen:
                    continue
                seen.add(proc.pid)
                try:
                    if classify_proc(proc.name(), proc.cmdline(), _safe_exe(proc)):
                        return proc
                except Exception:
                    pass
                try:
                    nxt.extend(proc.children())
                except Exception:
                    pass
            level = nxt
        return None

    # -- task manager: process tree + scoped kill ---------------------------
    # Both platform backends spawn the shell as ``self.pid`` and already walk
    # its descendant tree via psutil for agent detection, so these concrete
    # implementations are shared. They run in a thread (off the event loop) and
    # must NEVER raise — a denied/vanished child is skipped, not fatal.

    def note_session_started(self) -> None:
        """Pin the shell's create_time NOW (called right after spawn, when
        self.pid is unambiguously the freshly-spawned shell) so later
        enumerate/kill can reject a recycled PID. Best-effort, never raises."""
        self._root_ctime = None
        if self.pid is None:
            return
        try:
            import psutil
            self._root_ctime = psutil.Process(self.pid).create_time()
        except Exception:
            self._root_ctime = None

    def _session_root(self):
        """The psutil.Process for the session shell, but ONLY if its identity
        still matches the create_time pinned at spawn (guards PID reuse).
        Returns None if there is no session, psutil is missing, the pid is
        gone, or the identity no longer matches. Never raises."""
        if self.pid is None:
            return None
        try:
            import psutil
            shell = psutil.Process(self.pid)
        except Exception:
            return None
        # If we pinned a create_time at spawn, the live pid must still carry it;
        # a mismatch means the shell exited and the pid was recycled.
        if self._root_ctime is not None:
            try:
                if abs(shell.create_time() - self._root_ctime) > _CTIME_EPS:
                    return None
            except Exception:
                return None
        return shell

    def enumerate_procs(self) -> List[Dict[str, Any]]:
        """The shell process plus its descendants, as a flat list of dicts:
        ``{pid, ppid, name, cmdline, cpu, mem_mb, status, create_time}``.
        Bounded by ``_MAX_PROCS``; cmdlines truncated to ``_MAX_CMDLINE``."""
        shell = self._session_root()
        if shell is None:
            return []
        procs = [shell]
        try:
            procs.extend(shell.children(recursive=True))
        except Exception:
            pass
        out: List[Dict[str, Any]] = []
        seen: set = set()
        for proc in procs:
            if proc.pid in seen:
                continue
            seen.add(proc.pid)
            out.append(self._proc_info(proc))
            if len(out) >= _MAX_PROCS:
                break
        return out

    @staticmethod
    def _proc_info(proc) -> Dict[str, Any]:
        info: Dict[str, Any] = {"pid": proc.pid}
        try:
            info["ppid"] = proc.ppid()
        except Exception:
            info["ppid"] = None
        try:
            info["name"] = proc.name()
        except Exception:
            info["name"] = "?"
        try:
            cmd = proc.cmdline()
            info["cmdline"] = (" ".join(cmd) if cmd else "")[:_MAX_CMDLINE]
        except Exception:
            info["cmdline"] = ""
        try:
            # No interval -> non-blocking; first sample reads ~0.0, which is
            # fine for a periodically-refreshed UI.
            info["cpu"] = round(proc.cpu_percent(None), 1)
        except Exception:
            info["cpu"] = None
        try:
            info["mem_mb"] = round(proc.memory_info().rss / (1024 * 1024), 1)
        except Exception:
            info["mem_mb"] = None
        try:
            info["status"] = proc.status()
        except Exception:
            info["status"] = None
        try:
            info["create_time"] = proc.create_time()
        except Exception:
            info["create_time"] = None
        return info

    def kill_proc_fallback(self, pid: int) -> Tuple[bool, Optional[str]]:
        """Best-effort scoped kill when psutil is unavailable. Default:
        unsupported (preserves today's behavior on backends without a
        psutil-free path). Platform backends override. Never raises."""
        return False, "psutil_unavailable"

    def kill_proc(self, pid: int) -> Tuple[bool, Optional[str]]:
        """Terminate ``pid`` (and its descendants) — but ONLY if it is the
        session's own shell or one of its descendants. Returns ``(ok, error)``.

        Guards against PID reuse by checking ``create_time`` against the value
        captured when the tree was walked, and re-confirms identity on the
        Process object actually killed. Never raises."""
        if self.pid is None:
            return False, "no_session"
        try:
            pid = int(pid)
        except (TypeError, ValueError):
            return False, "bad_pid"
        if pid <= 0:
            return False, "bad_pid"
        try:
            import psutil
        except Exception:
            # No psutil: defer to the backend's best-effort fallback (the
            # default still returns "psutil_unavailable"). The pid validation
            # above stays primary so the fallback only sees a sane pid.
            return self.kill_proc_fallback(pid)
        # Identity-checked session root (rejects a recycled shell pid).
        shell = self._session_root()
        if shell is None:
            return False, "session_gone"
        # Allowed identities: {pid: create_time} for the shell + descendants.
        members: Dict[int, float] = {}
        try:
            members[shell.pid] = shell.create_time()
        except Exception:
            pass
        try:
            for child in shell.children(recursive=True):
                try:
                    members[child.pid] = child.create_time()
                except Exception:
                    continue
        except Exception:
            pass
        if pid not in members:
            return False, "not_in_session"
        # Re-instantiate and verify identity to close the PID-reuse window.
        try:
            target = psutil.Process(pid)
            if abs(target.create_time() - members[pid]) > _CTIME_EPS:
                return False, "identity_mismatch"
        except psutil.NoSuchProcess:
            return False, "already_gone"
        except Exception:
            return False, "target_gone"
        # Kill the target's own subtree (children first) so "end process" and
        # "destroy window" both cascade reliably across platforms.
        victims = []
        try:
            victims.extend(target.children(recursive=True))
        except Exception:
            pass
        victims.append(target)
        for victim in victims:
            try:
                victim.terminate()
            except Exception:
                pass
        try:
            _gone, alive = psutil.wait_procs(victims, timeout=1.5)
        except Exception:
            alive = victims
        for victim in alive:
            try:
                victim.kill()
            except Exception:
                pass
        return True, None
