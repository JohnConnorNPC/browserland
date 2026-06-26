"""Agent orchestrator: PTY backend + ring + title sniffer + broker client.

Everything runs on one event loop. All outbound traffic goes through one
unbounded queue drained by the client's single sender task — that single
ordered channel is what makes snapshot/live-output interleaving correct:
a snapshot enqueued between two PTY chunks is delivered between them.
"""

from __future__ import annotations

import asyncio
import logging
import os
import socket
from dataclasses import dataclass, field
from typing import Optional

from .. import build_version, protocol
from .altscreen import DecModeSniffer
from .backends import create_backend
from .backends.base import PtyBackend
from .client import BrokerClient
from .config import AgentConfig
from .env_util import spawn_env
from .ringbuf import ByteRing
from .snapshot import raw as raw_snapshot
from .titles import OscTitleSniffer

LOGGER = logging.getLogger(__name__)

# Foreground-agent detection cadence. A process-tree walk runs in a thread, so
# this can be brisk without stalling the loop.
_DETECT_INTERVAL = 1.5


def _render_screen_text(data: bytes, cols: int, rows: int,
                        view: str = "screen", lines: int = 0,
                        alt_screen: bool = False, evicted: bool = False):
    """Render the ring for an MCP read; return a dict
    ``{text, degraded, alt_screen, cursor, view, history_lines}``. Runs in a
    worker thread; never raises (#15, #21).

    pyte is the high-fidelity renderer for the CURRENT screen (trimmed to the
    last restart so it's bounded — #21 B). The dependency-free :mod:`textgrid`
    owns scrollback (it captures primary-screen history) and is the no-pyte
    fallback. ``alt_screen`` (the agent's live-tracked state) overrides
    ``view="scrollback"`` — the grid is the whole story for a full-screen TUI,
    so scrollback is meaningless; the returned ``view`` reflects what was
    actually produced. ``evicted`` (the ring dropped its head) lets the
    head-trim resync a cut leading escape sequence instead of mis-decoding it
    into ghost glyphs (#28). ``degraded=True`` (``view="raw"``, ``cursor=None``)
    is the last-ditch raw decode when no grid could be produced (#15 symptom)."""
    try:
        cols = max(1, int(cols))
        rows = max(1, int(rows))
    except (TypeError, ValueError):    # never-raises: bad dims -> a sane default
        cols, rows = 80, 24
    want_scrollback = view == "scrollback" and not alt_screen
    eff_view = "scrollback" if want_scrollback else "screen"
    if not want_scrollback:
        # Current screen only: pyte (high fidelity), trimmed like textgrid —
        # with the same evicted-head resync so a cut leading sequence can't
        # mis-decode into top-left ghost glyphs (#28).
        try:
            import pyte  # type: ignore
            from .snapshot.textgrid import _trim_for_screen
            screen = pyte.Screen(cols, rows)
            stream = pyte.ByteStream(screen)
            stream.feed(_trim_for_screen(data, evicted))
            cur = {"row": min(max(screen.cursor.y, 0), rows - 1),
                   "col": min(max(screen.cursor.x, 0), cols - 1)}
            return {"text": "\n".join(screen.display), "degraded": False,
                    "alt_screen": alt_screen, "cursor": cur,
                    "view": "screen", "history_lines": 0}
        except Exception as exc:  # ImportError or any pyte parse error
            LOGGER.debug("screen_text pyte render failed (%s); built-in grid",
                         exc)
    # textgrid: scrollback (it owns history) OR the no-pyte screen fallback.
    try:
        from .snapshot import textgrid
        r = textgrid.render_screen(data, cols, rows, eff_view, lines,
                                   evicted=evicted)
        return {"text": r["text"], "degraded": False, "alt_screen": alt_screen,
                "cursor": r["cursor"], "view": eff_view,
                "history_lines": r["history_lines"]}
    except Exception as exc:  # defensive: textgrid is built never to raise
        LOGGER.warning("screen_text grid render failed (%s); raw decode", exc)
        # Bounded raw decode: cap to the tail so a degraded read can never blow
        # the MCP token budget (the original #15 symptom).
        cap = max(cols * rows * 4, 4096)
        return {"text": data.decode("utf-8", "replace")[-cap:],
                "degraded": True, "alt_screen": alt_screen, "cursor": None,
                "view": "raw", "history_lines": 0}


def _safe_cwd(configured: Optional[str]) -> str:
    """The session's launch-time working dir: the configured cwd if given,
    else the agent process's own cwd. Never raises."""
    if configured:
        return str(configured)
    try:
        return os.getcwd()
    except OSError:
        return ""


@dataclass
class SessionState:
    """Live session identity. The hello is always built from this, so a
    re-hello after reconnect carries the current title/dims/agent, not the
    ones from process start."""
    window_id: int
    pid: int = 0
    title: str = ""
    cols: int = 80
    rows: int = 24
    host: str = field(default_factory=socket.gethostname)
    kind: str = "agent"
    agent: str = ""
    cwd: str = ""
    # This agent's build id (webterm.build_version()), reported in the hello so
    # the broker can surface it and flag a stale deployment (#22).
    version: str = field(default_factory=build_version)
    # Live DEC private-mode state, tracked off the PTY stream so it survives ring
    # eviction. alt_screen (#21): screen-vs-scrollback for read_screen.
    # app_cursor / DECCKM (#23): whether send_keys must send SS3 arrows.
    alt_screen: bool = False
    app_cursor: bool = False


class Agent:
    def __init__(self, config: AgentConfig,
                 backend: Optional[PtyBackend] = None) -> None:
        self.config = config
        self.backend = (backend if backend is not None
                        else create_backend(config.pty_backend))
        self.state = SessionState(
            window_id=config.window_id,
            title=config.title or "",
            cols=config.cols,
            rows=config.rows,
            cwd=_safe_cwd(config.cwd),
        )
        self.ring = ByteRing(config.ring_bytes)
        self.sniffer = OscTitleSniffer()
        self._mode_sniffer = DecModeSniffer()    # live alt-screen + DECCKM (#21/#23)
        self.out_q: "asyncio.Queue" = asyncio.Queue()
        self.client = BrokerClient(
            config.broker_url,
            config.auth_token,
            self.state,
            self.out_q,
            on_input=self._on_input,
            on_resize=self._on_resize,
            on_snapshot_request=self._on_snapshot_request,
            on_procs_request=self._on_procs_request,
            on_kill_request=self._on_kill_request,
            on_git_request=self._on_git_request,
            on_screen_request=self._on_screen_request,
        )
        self._exit_fut: Optional[asyncio.Future] = None

    async def run(self) -> int:
        """Run until the child exits; returns the child's exit code."""
        loop = asyncio.get_running_loop()
        self._exit_fut = loop.create_future()

        # Fresh PATH from the registry so a just-installed program is found
        # without a re-login (todo task 17). spawn_env() is a no-op copy of the
        # environment off Windows.
        self.backend.spawn(
            self.config.command, self.config.cols, self.config.rows,
            cwd=self.config.cwd, env=spawn_env(),
        )
        self.state.pid = self.backend.pid or 0
        # Pin the shell's identity now (fresh spawn) so the task-manager's
        # enumerate/kill can reject a recycled session-root PID later.
        self.backend.note_session_started()
        LOGGER.info("spawned %s (pid %s) as window %d",
                    " ".join(self.config.command), self.state.pid,
                    self.state.window_id)
        self.backend.start(loop, self._on_pty_data, self._on_pty_exit)

        client_task = asyncio.create_task(self.client.run())
        detect_task = asyncio.create_task(self._detect_loop(loop))
        try:
            code = await self._exit_fut
        except asyncio.CancelledError:
            self.backend.kill()
            raise
        finally:
            detect_task.cancel()
            await asyncio.gather(detect_task, return_exceptions=True)
            # Give the sender a moment to flush the child's last output
            # (e.g. the shell's parting newline), then stop the client.
            try:
                await asyncio.wait_for(self.out_q.join(), timeout=2.0)
            except asyncio.TimeoutError:
                pass
            await self.client.stop()
            try:
                await asyncio.wait_for(client_task, timeout=2.0)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                client_task.cancel()
                await asyncio.gather(client_task, return_exceptions=True)
        LOGGER.info("child exited with code %s", code)
        return code

    # -- PTY -> broker (called on the loop by the backend) -------------------

    def _on_pty_data(self, chunk: bytes) -> None:
        self.ring.append(chunk)
        # Track DEC modes live (#21/#23): survives ring eviction, unlike a re-scan.
        self._mode_sniffer.feed(chunk)
        self.state.alt_screen = self._mode_sniffer.alt_screen
        app_cursor = self._mode_sniffer.app_cursor
        if app_cursor != self.state.app_cursor:
            # Push DECCKM changes so the broker caches them (#23): send_keys
            # reads the cache to pick CSI vs SS3 arrows without a screen render.
            self.state.app_cursor = app_cursor
            self._enqueue("txt", protocol.mode_frame(app_cursor))
        new_title = self.sniffer.feed(chunk)
        if new_title is not None and new_title != self.state.title:
            # State updates before the frame is enqueued, so a hello built
            # concurrently never lags the frames behind it.
            self.state.title = new_title
            self._enqueue("txt", protocol.title_frame(new_title))
        self._enqueue("bin", chunk)

    def _on_pty_exit(self, code: int) -> None:
        # Push an explicit exit event so the broker can tear attached browsers
        # down at once instead of waiting on its /sessions poll grace cycle
        # (~12 s). This rides the SAME ordered out-queue as live output, so it
        # lands AFTER the child's final bytes; run()'s finally then flushes the
        # queue (out_q.join, 2 s) before stopping the client, so the frame is
        # delivered before the WS closes. Fires on the loop, after the last
        # _on_pty_data (backend contract) — enqueue order is exit-last.
        self._enqueue("txt", protocol.exit_frame(code))
        if self._exit_fut is not None and not self._exit_fut.done():
            self._exit_fut.set_result(code)

    # -- foreground-agent detection ------------------------------------------

    async def _detect_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        """Poll the backend for the foreground agent (claude/grok/codex).
        The scan walks a process tree, so it runs in a thread to keep the
        event loop responsive. Cancelled in run()'s finally.

        Survives reconnects two ways: state.agent feeds the hello (so a
        re-hello is accurate) and we push a frame on the *first* detection as
        well as on every subsequent change."""
        first = True
        while True:
            await asyncio.sleep(_DETECT_INTERVAL)
            try:
                new = await loop.run_in_executor(
                    None, self.backend.foreground_command)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                LOGGER.debug("agent detection failed: %s", exc)
                continue
            new = new or ""
            if new != self.state.agent or first:
                # State first so a hello built concurrently (reconnect)
                # carries the current agent; then push the live frame.
                self.state.agent = new
                self._enqueue("txt", protocol.agent_frame(new))
            # Live cwd (best-effort): the shell's working dir, so the AGENTS.md
            # button tracks a `cd`. State updates before the frame (reconnect
            # hello accuracy), same as agent above. Errors are swallowed by the
            # backend (returns None), so a denied/dead process never blanks it.
            try:
                cwd = await loop.run_in_executor(None, self.backend.cwd)
            except asyncio.CancelledError:
                raise
            except Exception:
                cwd = None
            if cwd and cwd != self.state.cwd:
                self.state.cwd = cwd
                self._enqueue("txt", protocol.cwd_frame(cwd))
            first = False

    # -- broker -> PTY (called on the loop by the client) --------------------

    def _on_input(self, data: bytes) -> None:
        try:
            self.backend.write(data)
        except Exception as exc:
            LOGGER.debug("pty write failed: %s", exc)

    def _on_resize(self, cols: int, rows: int) -> None:
        cols = max(1, cols)
        rows = max(1, rows)
        try:
            self.backend.resize(cols, rows)
        except Exception as exc:
            # WARNING (not DEBUG): a raised resize is a real failure worth
            # surfacing. Note Windows usually fails *silently* (the child
            # never learns the new size), so a quiet log here is corroborating,
            # not proof the resize landed.
            LOGGER.warning("pty resize to %dx%d failed: %s", cols, rows, exc)
        else:
            self.state.cols = cols
            self.state.rows = rows
        # Always reply `resized` — on failure it echoes the current dims.
        self._enqueue("txt", protocol.resized_frame(
            self.state.cols, self.state.rows))

    def _on_snapshot_request(self) -> None:
        # Capture synchronously on the loop and enqueue on the same queue as
        # live output: ordering relative to surrounding chunks comes free.
        data = self.ring.get()
        payload: Optional[bytes] = None
        if self.config.snapshot_mode == "pyte":
            try:
                from .snapshot import pyte_snap
                payload = pyte_snap.render(
                    data, self.state.cols, self.state.rows)
            except Exception as exc:
                LOGGER.warning("pyte snapshot failed (%s); falling back to "
                               "raw", exc)
        if payload is None:
            payload = raw_snapshot.render(data, self.ring.evicted)
        self._enqueue("snap", payload)

    # -- management RPCs (task manager / git button) -------------------------
    # The broker sends these on demand with a correlation id; we do the heavy
    # work (psutil walk / a kill / a git subprocess) in a thread so the WS
    # receive loop never stalls, then enqueue the reply on the SAME ordered
    # out-queue as live output (one WS writer, no interleaving). Concurrency is
    # bounded upstream: the broker caps in-flight RPCs per connection.

    def _on_procs_request(self, req: int) -> None:
        loop = asyncio.get_running_loop()

        async def _run() -> None:
            try:
                procs = await loop.run_in_executor(
                    None, self.backend.enumerate_procs)
            except Exception as exc:
                LOGGER.debug("enumerate_procs failed: %s", exc)
                procs = []
            self._enqueue("txt", protocol.procs_frame(req, procs))

        loop.create_task(_run())

    def _on_kill_request(self, req: int, pid: int) -> None:
        loop = asyncio.get_running_loop()

        async def _run() -> None:
            try:
                ok, err = await loop.run_in_executor(
                    None, self.backend.kill_proc, pid)
            except Exception as exc:
                ok, err = False, str(exc)[:200]
            self._enqueue("txt", protocol.killed_frame(req, ok, error=err,
                                                        pid=pid))

        loop.create_task(_run())

    def _on_git_request(self, req: int) -> None:
        loop = asyncio.get_running_loop()
        cwd = self.state.cwd

        async def _run() -> None:
            try:
                from . import git_status
                status = await loop.run_in_executor(
                    None, git_status.collect, cwd)
            except Exception as exc:
                status = {"ok": False, "error": str(exc)[:200]}
            self._enqueue("txt", protocol.git_status_frame(req, status))

        loop.create_task(_run())

    def _on_screen_request(self, req: int, view: str = "screen",
                           lines: int = 0) -> None:
        # MCP /mcp/read: render the live screen as plain text. Snapshot the
        # ring + dims + live alt-screen state synchronously on the loop
        # (consistent view), then render off-loop (pyte can be CPU-heavy on a
        # large ring) and enqueue the reply on the SAME ordered out-queue.
        loop = asyncio.get_running_loop()
        data = self.ring.get()
        evicted = self.ring.evicted              # head may start mid-seq (#28)
        cols, rows = self.state.cols, self.state.rows
        alt = self.state.alt_screen
        app_cursor = self.state.app_cursor       # DECCKM (#23)

        async def _run() -> None:
            try:
                r = await loop.run_in_executor(
                    None, _render_screen_text, data, cols, rows, view, lines,
                    alt, evicted)
                self._enqueue("txt", protocol.screen_text_frame(
                    req, r["text"], cols, rows, degraded=r["degraded"],
                    alt_screen=r["alt_screen"], cursor=r["cursor"],
                    view=r["view"], history_lines=r["history_lines"],
                    app_cursor=app_cursor))
            except Exception as exc:
                # Always answer the RPC — an unhandled failure here would leave
                # the broker waiting until it times out (no_producer_rpc).
                LOGGER.warning("screen_text request failed (%s); degraded reply",
                               exc)
                self._enqueue("txt", protocol.screen_text_frame(
                    req, "", cols, rows, degraded=True, alt_screen=alt,
                    cursor=None, view="raw", history_lines=0,
                    app_cursor=app_cursor))

        loop.create_task(_run())

    # -- internal -------------------------------------------------------------

    def _enqueue(self, kind: str, payload) -> None:
        # While disconnected nothing is queued: the ring keeps history and
        # the post-reconnect snapshot heals attached browsers.
        if not self.client.connected:
            return
        self.out_q.put_nowait((kind, payload))
