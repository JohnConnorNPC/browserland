"""Windows ConPTY backend via pywinpty's low-level ``winpty.PTY``.

Why not ``winpty.PtyProcess``: it adds its own reader thread, a localhost
TCP socketpair, and a ``b'0011Ignore'`` write sentinel, and returns ``str``
anyway — needless layers when we bridge to asyncio ourselves.

Windows asyncio is the Proactor loop, which has no ``add_reader`` for pipe
handles, so a dedicated blocking reader thread is *required*, not a choice.
A single thread funneling both data and exit through
``loop.call_soon_threadsafe`` preserves the backend ordering contract
(on_exit after the last on_data).

pywinpty 3.x is str-based: ``read()`` returns str (decoded by winpty from
the ConPTY UTF-8 stream), ``write()`` takes str. We re-encode reads to
UTF-8 bytes and decode input bytes back to str at the boundary.

Backend choice (verified empirically on Server 2022, pywinpty 3.0.3): a
broker-launched agent is spawned DETACHED_PROCESS with no console, so
``_auto_backend()`` used to pick WinPTY — which interrupts running processes
but mistranslates a bare ^C into a literal ``c`` at a PSReadLine edit prompt.
ConPTY cancels the edit buffer cleanly and supports live resize, so on the
"auto" path we now ``_ensure_hidden_console()`` first: give the detached agent
a hidden console (flips auto to ConPTY) AND re-enable Ctrl-C for its process
group (see that function for the two levers and the #25 story). If acquisition
fails we stay on WinPTY and keep its working interrupt — safe degradation,
never a regression. ``backend="conpty"|"winpty"`` overrides; ``"winpty"`` skips
acquisition (no pointless flash; its interrupt works headless anyway).
"""

from __future__ import annotations

import asyncio
import ctypes
import logging
import shutil
import subprocess
import threading
from ctypes import wintypes
from typing import Callable, Mapping, Optional, Sequence

from winpty import PTY  # type: ignore
from winpty.enums import Backend  # type: ignore

from ..detect import _safe_exe, classify_proc
from .base import PtyBackend

LOGGER = logging.getLogger(__name__)


def _auto_backend() -> int:
    if ctypes.windll.kernel32.GetConsoleWindow():
        return Backend.ConPTY
    return Backend.WinPTY


# Kept alive for the process lifetime once installed: a ctypes callback that is
# GC'd would leave a dangling pointer in the console handler list.
_CTRL_GUARD = None


def _install_agent_ctrl_guard() -> bool:
    """Install a console-control handler that swallows CTRL_C/CTRL_BREAK for the
    AGENT process, so re-enabling group Ctrl-C (below, for the pty child) can't
    terminate the agent itself. Handler *routines* are per-process and are NOT
    inherited by children (only the NULL-handler "ignore" attribute is), so the
    pty child still receives normal Ctrl-C. Idempotent; never raises."""
    global _CTRL_GUARD
    if _CTRL_GUARD is not None:
        return True
    try:
        proto = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.DWORD)

        def _guard(ctrl_type: int) -> bool:
            # CTRL_C_EVENT=0, CTRL_BREAK_EVENT=1 -> handled (swallow); other
            # events (close/logoff/shutdown) fall through to default handling.
            return ctrl_type in (0, 1)

        cb = proto(_guard)
        if ctypes.windll.kernel32.SetConsoleCtrlHandler(cb, True):
            _CTRL_GUARD = cb  # keep the callback alive
            return True
    except Exception:
        pass
    return False


def _ensure_hidden_console() -> bool:
    """Make a detached agent able to run ConPTY with a working ^C (#25); returns
    True iff a hidden console is now present AND the pty child will be
    interruptible (so it is safe to select ConPTY).

    A broker-launched agent is spawned DETACHED_PROCESS + CREATE_NEW_PROCESS_GROUP
    (launcher.py ``_spawn_detached``). Two consequences, two levers:

    1. No console -> ``_auto_backend()`` picks WinPTY. ``AllocConsole()`` +
       ``ShowWindow(SW_HIDE)`` gives it a hidden console (nothing flashes on the
       streamed desktop), flipping auto to ConPTY (clean edit-buffer cancel +
       live resize). AllocConsole's canonical success case is exactly a process
       with no console; it fails ERROR_ACCESS_DENIED only when one already
       exists (interactive / ConPTY-hosted), which the first check short-circuits
       so we never disturb an existing console.
    2. ``CREATE_NEW_PROCESS_GROUP`` DISABLES Ctrl-C for the group, inherited by
       the pty child. Without undoing it, ConPTY delivers 0x03 to apps that READ
       input (PSReadLine, cmdlets, raw TUIs) but the CTRL_C_EVENT never reaches a
       cooked child that doesn't read stdin (ping, npm, builds) — the interrupt
       WinPTY does today. ``SetConsoleCtrlHandler(None, FALSE)`` clears that
       disable BEFORE the pty child is spawned, so the child inherits normal
       Ctrl-C and a running process is interrupted. We first install an agent-only
       guard (above) so this re-enable can't terminate the agent.

    If the re-enable fails we ``FreeConsole()`` and return False, so ConPTY is
    never chosen with a dead interrupt: ``_auto_backend()`` reverts to WinPTY and
    keeps its working interrupt — safe degradation, never a regression. Never
    raises."""
    kernel32 = ctypes.windll.kernel32
    if kernel32.GetConsoleWindow():
        return True
    try:
        if not kernel32.AllocConsole():
            return False
        hwnd = kernel32.GetConsoleWindow()
        if hwnd:
            ctypes.windll.user32.ShowWindow(hwnd, 0)  # SW_HIDE
        _install_agent_ctrl_guard()
        if not kernel32.SetConsoleCtrlHandler(None, 0):  # (NULL, FALSE)
            kernel32.FreeConsole()  # couldn't re-enable child ^C -> revert
            return False
        return bool(kernel32.GetConsoleWindow())
    except Exception:
        try:
            kernel32.FreeConsole()
        except Exception:
            pass
        return False


class WinConPtyBackend(PtyBackend):
    def __init__(self, backend: str = "auto") -> None:
        if backend == "conpty":
            # Forced ConPTY: still acquire the hidden console + re-enable Ctrl-C
            # so a forced-ConPTY detached agent gets a working interrupt too.
            _ensure_hidden_console()
            self._backend = Backend.ConPTY
        elif backend == "winpty":
            # Explicit WinPTY: skip acquisition (no pointless flash; WinPTY's
            # interrupt works headless).
            self._backend = Backend.WinPTY
        else:  # "auto": acquire a hidden console + re-enable Ctrl-C, then let
            # _auto_backend() re-check — ConPTY only if a console exists now.
            _ensure_hidden_console()
            self._backend = _auto_backend()
        self._backend_name = (
            "ConPTY" if self._backend == Backend.ConPTY else "WinPTY")
        self.pid: Optional[int] = None
        self._pty: Optional[PTY] = None
        self._thread: Optional[threading.Thread] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._on_data: Optional[Callable[[bytes], None]] = None
        self._on_exit: Optional[Callable[[int], None]] = None
        self._exitcode: Optional[int] = None
        self._closing = False

    def spawn(
        self,
        argv: Sequence[str],
        cols: int,
        rows: int,
        cwd: Optional[str] = None,
        env: Optional[Mapping[str, str]] = None,
    ) -> None:
        argv = list(argv)
        # Resolve argv[0] against the CHILD's PATH (the fresh registry PATH
        # when one was passed), not just the agent's inherited PATH — that is
        # what lets a just-installed program resolve without a re-login.
        search_path = None
        if env is not None:
            search_path = env.get("PATH") or env.get("Path")
        exe = shutil.which(argv[0], path=search_path) or argv[0]
        # pywinpty's low-level PTY PREPENDS the resolved exe to `cmdline`, so
        # cmdline must be the ARGUMENTS ONLY (argv[1:]) with a leading space —
        # exactly as pywinpty's own PtyProcess.spawn builds it. Passing the
        # full argv (incl. argv[0]) duplicates argv[0]: WinPTY happens to
        # tolerate it, but under ConPTY the child then runs argv[0] as a script
        # / loses path backslashes (a fatal, backend-specific divergence that
        # only surfaces for multi-arg commands — single-arg uses cmdline=None).
        cmdline = (" " + subprocess.list2cmdline(argv[1:])
                   if len(argv) > 1 else None)
        env_block: Optional[str] = None
        if env is not None:
            env_block = "".join(f"{k}={v}\0" for k, v in env.items())

        self._pty = PTY(cols, rows, backend=self._backend)
        self._pty.spawn(exe, cmdline=cmdline, cwd=cwd, env=env_block)
        self.pid = self._pty.pid
        # Confirms in production which branch we took. With #25's hidden-console
        # acquisition, a detached broker-launched agent on "auto" should now log
        # backend=ConPTY console_window=True; a fallback to WinPTY (AllocConsole
        # failed) logs console_window=False and loses live resize.
        LOGGER.info(
            "win pty backend=%s console_window=%s pid=%s",
            self._backend_name,
            bool(ctypes.windll.kernel32.GetConsoleWindow()),
            self.pid)

    def start(
        self,
        loop: asyncio.AbstractEventLoop,
        on_data: Callable[[bytes], None],
        on_exit: Callable[[int], None],
    ) -> None:
        assert self._pty is not None
        self._loop = loop
        self._on_data = on_data
        self._on_exit = on_exit
        self._thread = threading.Thread(
            target=self._read_loop, name="conpty-reader", daemon=True)
        self._thread.start()

    def _read_loop(self) -> None:
        pty = self._pty
        loop = self._loop
        assert pty is not None and loop is not None
        try:
            while True:
                data = pty.read(blocking=True)
                if not data:
                    # blocking read returns '' at EOF or after cancel_io().
                    if pty.iseof() or self._closing:
                        break
                    continue
                chunk = data.encode("utf-8", errors="replace")
                loop.call_soon_threadsafe(self._dispatch_data, chunk)
        except Exception:
            # WinptyError on a dying/cancelled PTY — fall through to exit.
            pass
        code = self._collect_exitcode()
        try:
            loop.call_soon_threadsafe(self._dispatch_exit, code)
        except RuntimeError:
            pass  # loop already closed during shutdown

    def _collect_exitcode(self) -> int:
        code: Optional[int] = None
        try:
            if self._pty is not None:
                code = self._pty.get_exitstatus()
        except Exception:
            code = None
        if code is None:
            code = 1 if self._closing else 0
        self._exitcode = code
        return code

    def _dispatch_data(self, chunk: bytes) -> None:
        if self._on_data is not None and not self._closing:
            self._on_data(chunk)

    def _dispatch_exit(self, code: int) -> None:
        if self._on_exit is not None:
            self._on_exit(code)

    # -- input ------------------------------------------------------------

    def write(self, data: bytes) -> None:
        if self._pty is None or self._closing:
            return
        try:
            self._pty.write(data.decode("utf-8", errors="replace"))
        except Exception:
            pass  # racing child exit; reader thread reports it

    def flush_input(self) -> None:
        """No-op: ConPTY/WinPTY expose no input-queue flush primitive (#133).

        Made an explicit override rather than inherited so the best-effort
        behavior is discoverable here — flush_input on a Windows agent silently
        does nothing (there is no pty API to drop the app's unread input)."""
        return None

    # -- control ----------------------------------------------------------

    def resize(self, cols: int, rows: int) -> None:
        if self._pty is None:
            raise OSError("pty closed")
        self._pty.set_size(cols, rows)

    def kill(self) -> None:
        """cancel_io() unblocks the reader thread; dropping the PTY object
        closes the ConPTY which terminates the attached child."""
        self._closing = True
        pty = self._pty
        if pty is None:
            return
        try:
            pty.cancel_io()
        except Exception:
            pass
        self._pty = None
        del pty
        if self._thread is not None and self._thread.is_alive():
            if threading.current_thread() is not self._thread:
                self._thread.join(timeout=2)  # daemon thread is the backstop

    def exitcode(self) -> Optional[int]:
        if self._exitcode is not None:
            return self._exitcode
        try:
            if self._pty is not None and not self._pty.isalive():
                self._exitcode = self._pty.get_exitstatus()
        except Exception:
            pass
        return self._exitcode

    # -- foreground-agent detection --------------------------------------

    def foreground_command(self) -> Optional[str]:
        """Windows has no process-group / controlling-terminal concept, so we
        walk the shell's descendant tree. ``PTY.pid`` is the spawned shell
        itself for both ConPTY and WinPTY (verified empirically), so the walk
        starts there. One denied or vanished child must not blank the result;
        never raises."""
        if self.pid is None:
            return None
        try:
            import psutil
        except Exception:
            return None
        try:
            shell = psutil.Process(self.pid)
        except Exception:
            return None
        # The shell itself first (in case the agent replaced it directly),
        # then its descendants.
        candidates = [shell]
        try:
            candidates.extend(shell.children(recursive=True))
        except Exception:
            pass
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
    # the dir Claude Code actually runs in even when the shell sits a level up
    # (issue #47).

    # NOTE: no ``kill_proc_fallback`` override here -> inherits the base hook,
    # so destroying a window without psutil still returns "psutil_unavailable"
    # on Windows (the bug being fixed is Linux-only). A future Windows-parity
    # fallback would shell out to ``taskkill /PID <self.pid> /T /F`` (kill the
    # shell's whole child tree), guarded on ``PtyBackend.pid`` being live.
