"""Agent orchestrator: PTY backend + ring + title sniffer + broker client.

Everything runs on one event loop. All outbound traffic goes through one
unbounded queue drained by the client's single sender task — that single
ordered channel is what makes snapshot/live-output interleaving correct:
a snapshot enqueued between two PTY chunks is delivered between them.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import re
import socket
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import List, Optional

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

# read_screen wait-for-change cap (#26): the AGENT holds the reply for at most
# this long, which also bounds how long one wait occupies a broker RPC slot
# (RPC_MAX_INFLIGHT). The broker clamps the same ceiling.
MAX_WAIT_MS = 15000
# Delta read_screen (#52): how many recent frames to retain for row-diffing, and
# the change ratio above which a delta is NOT worth it (mostly-changed grid ->
# the row indices would cost more than they save, so return the full grid).
_FRAME_CACHE_MAX = 16
_DELTA_MAX_CHANGED_RATIO = 0.6


def _content_hash(text: str) -> str:
    """A stable, process-independent digest of the rendered screen text, so a
    caller can detect change across reads without diffing full text (#26). 128
    bits — small on the wire, collision-safe for change detection."""
    return hashlib.blake2b(text.encode("utf-8", "replace"),
                           digest_size=16).hexdigest()


def _render_screen_text(data: bytes, cols: int, rows: int,
                        view: str = "screen", lines: int = 0,
                        alt_screen: bool = False, evicted: bool = False,
                        attrs: bool = False,
                        keyframe: bytes = b"", keyframe_k: int = 0,
                        keyframe_dims: tuple = (0, 0),
                        evicted_total: int = 0, total_appended: int = 0):
    """Render the ring for an MCP read; return a dict
    ``{text, degraded, alt_screen, cursor, view, history_lines}`` (plus
    ``attr_runs`` when ``attrs`` and the pyte path ran, ``partial`` on an
    unreconstructable evicted alt-screen read, and ``keyframe``/``keyframe_k``
    for the agent to re-stash). Runs in a worker thread; never raises (#15, #21).

    ALT-SCREEN KEYFRAME (#130): a long-running full-screen TUI paints its whole
    frame once, then streams only diffs. Once >256 KiB of diffs evict the
    ``\\x1b[?1049h`` marker AND that one-time paint, a fresh replay of the
    surviving ring lands diffs on a blank grid and the static panels vanish. To
    survive that, a trustworthy full frame is re-emitted as an IMMUTABLE byte
    ``keyframe`` tagged with ``keyframe_k`` = the absolute ``total_appended`` it
    represents. On a later read that hits the bug (``evicted`` and ``alt_screen``
    and no restart marker survives the trim), the keyframe is prepended to the
    surviving ring tail sliced at ``off = keyframe_k - evicted_total`` — a chunk
    boundary, since appends and whole-chunk eviction both move in chunk units —
    reproducing keyframe-state + all post-keyframe diffs = the correct full
    screen. Only immutable bytes cross to this worker; there is no shared mutable
    pyte screen. When reconstruction is impossible (the keyframe was itself
    evicted: ``off < 0``; dims changed; or none exists), the read is flagged
    ``partial`` (distinct from ``degraded`` — the grid + cursor are still valid,
    just possibly incomplete) so the agent stops fully trusting it and can
    trigger a repaint. Self-heals on the next in-window read or any app repaint.

    pyte is the high-fidelity renderer for the CURRENT screen (trimmed to the
    last restart so it's bounded — #21 B). The dependency-free :mod:`textgrid`
    owns scrollback (it captures primary-screen history) and is the no-pyte
    fallback. ``alt_screen`` (the agent's live-tracked state) overrides
    ``view="scrollback"`` — the grid is the whole story for a full-screen TUI,
    so scrollback is meaningless; the returned ``view`` reflects what was
    actually produced. ``evicted`` (the ring dropped its head) lets the
    head-trim resync a cut leading escape sequence instead of mis-decoding it
    into ghost glyphs (#28). ``degraded=True`` (``view="raw"``, ``cursor=None``)
    is the last-ditch raw decode when no grid could be produced (#15 symptom).

    ``attrs`` (#128) adds ``attr_runs`` — the styled fg/bg/reverse cell runs, so
    a color-only menu selection the plain text drops is visible. It rides the
    pyte path only (the dependency-free fallback carries no SGR) and is
    best-effort: a failure to compute it just omits the key, never the text."""
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
            from .snapshot.textgrid import _trim_for_screen, _RESTART_MARKERS
            # The bug condition (#130): the ring evicted its head, we're in an
            # alt-screen TUI, and no restart marker survives — so the trim can't
            # anchor on a repaint and a fresh replay would drop the static paint.
            # ``best < 0`` (marker absent), NOT ``best <= 0``: a marker sitting
            # exactly at index 0 is a valid trim anchor — _trim_for_screen keeps
            # the full ring (best==0 branch), so the normal replay already yields
            # a COMPLETE frame with everything after the clear present. Flagging
            # that partial (and skipping its keyframe) would be a false alarm and
            # a chain stall; best==0 must take the normal, keyframe-emitting path.
            best = max(data.rfind(m) for m in _RESTART_MARKERS)
            bug_condition = bool(evicted and alt_screen and best < 0)
            screen = pyte.Screen(cols, rows)
            stream = pyte.ByteStream(screen)
            reconstructed = False
            if bug_condition and keyframe and keyframe_dims == (cols, rows):
                # off is a chunk boundary: keyframe_k and evicted_total are both
                # sums of whole chunk lengths. off in [0, len(data)] means the
                # keyframe point still lies within (or at the head of) the ring.
                off = keyframe_k - evicted_total
                if 0 <= off <= len(data):
                    try:
                        stream.feed(keyframe
                                    + _trim_for_screen(data[off:], evicted=True))
                        reconstructed = True
                    except Exception as exc:
                        LOGGER.debug("keyframe reconstruction failed (%s); "
                                     "plain replay", exc)
                        screen = pyte.Screen(cols, rows)
                        stream = pyte.ByteStream(screen)
            if not reconstructed:
                stream.feed(_trim_for_screen(data, evicted))
            cur = {"row": min(max(screen.cursor.y, 0), rows - 1),
                   "col": min(max(screen.cursor.x, 0), cols - 1)}
            result = {"text": "\n".join(screen.display), "degraded": False,
                      "alt_screen": alt_screen, "cursor": cur,
                      "view": "screen", "history_lines": 0}
            # Honest signal: a bug read we could NOT reconstruct is possibly
            # incomplete. A trustworthy full frame (normal replay OR a successful
            # reconstruction) is re-emitted as a fresh keyframe while alt-screen
            # is active, so the chain stays ahead of eviction; never emit one on
            # the partial path (it would encode a known-incomplete grid).
            trustworthy = not (bug_condition and not reconstructed)
            if not trustworthy:
                result["partial"] = True
            elif alt_screen:
                try:
                    from .snapshot import pyte_snap
                    result["keyframe"] = pyte_snap.emit_screen(screen, cols, rows)
                    result["keyframe_k"] = int(total_appended)
                except Exception as exc:
                    LOGGER.debug("keyframe emit failed (%s); omitting", exc)
            if attrs:
                # Best-effort: a failed attr extraction drops the key only, never
                # the (already-computed) text — so an odd grid can't blank a read.
                try:
                    from .snapshot import pyte_snap
                    result["attr_runs"] = pyte_snap.attr_runs(screen, cols, rows)
                except Exception as exc:
                    LOGGER.debug("attr_runs failed (%s); omitting", exc)
            return result
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
    # The launch-profile name (#115), echoed in every hello so the broker can
    # surface it in /sessions and the UI can seed a per-profile color. Immutable
    # for the life of the agent (unlike cwd, which the shell can change).
    profile: str = ""
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
            profile=config.profile or "",
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
            on_reset_request=self._on_reset_request,
            on_flush_request=self._on_flush_request,
        )
        self._exit_fut: Optional[asyncio.Future] = None
        # read_screen wait-for-change (#26): a monotonic counter bumped on every
        # PTY append, plus the futures of reads currently parked waiting for the
        # next output. The counter is the source of truth (a parked read that
        # snapshotted before an append sees gen advance and re-renders), so no
        # shared Event/clear can swallow a wakeup across concurrent waiters.
        self._output_gen = 0
        self._output_waiters: List[asyncio.Future] = []
        # Delta read_screen (#52): a small LRU of recently-returned frames,
        # content_hash -> list[str] rows, so a follow-up read can ask for only
        # the rows that changed since a prior hash (`since`) instead of the
        # whole grid. Bounded against memory; misses fall back to a full grid.
        self._frame_cache: "OrderedDict[str, List[str]]" = OrderedDict()
        # Alt-screen keyframe (#130): an IMMUTABLE byte re-emit of the last
        # trustworthy full frame, plus the absolute ring offset (total_appended)
        # it represents and the dims it was captured at. Stashed on the loop from
        # a read's result and passed by value into the render worker, so a read
        # after >256 KiB eviction can reconstruct the static paint the ring lost.
        # Nulled on reset/resize so a cleared ring or new dims can't reconstruct
        # a stale grid. Never a live pyte.Screen crosses threads — only bytes.
        self._screen_keyframe: Optional[bytes] = None
        self._keyframe_k = 0
        self._keyframe_dims = (0, 0)
        # Set when a /session/kill RPC targets the session-root shell itself: a
        # deliberate UI Terminate, not a crash. run() then returns 0 so a
        # supervisor with Restart=on-failure does NOT respawn the shell, while a
        # genuine non-zero crash still does. Written on the loop in
        # _on_kill_request before kill_proc, read in _on_pty_exit/run().
        self._terminated_by_request = False

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
        if self._terminated_by_request:
            # Deliberate UI Terminate of the session-root shell: report a clean
            # exit so an on-failure supervisor leaves it dead instead of
            # respawning. A real crash never sets the flag, so it still respawns.
            LOGGER.info("exit was a requested terminate; reporting code 0")
            return 0
        return code

    # -- PTY -> broker (called on the loop by the backend) -------------------

    def _wake_output_waiters(self) -> None:
        # A screen-affecting event (new PTY output, or a reset_terminal): bump
        # the change generation FIRST — so a read that already snapshotted but
        # hasn't parked re-renders rather than missing it — then resolve every
        # parked wait_for_change future (#26). Always called on the loop, so it
        # is atomic vs a waiter's gen-check-then-park.
        self._output_gen += 1
        if self._output_waiters:
            waiters, self._output_waiters = self._output_waiters, []
            for fut in waiters:
                if not fut.done():
                    fut.set_result(None)

    def _on_pty_data(self, chunk: bytes) -> None:
        self.ring.append(chunk)
        self._wake_output_waiters()              # wake read_screen waiters (#26)
        # Track DEC modes live (#21/#23): survives ring eviction, unlike a re-scan.
        prev_alt = self.state.alt_screen
        self._mode_sniffer.feed(chunk)
        self.state.alt_screen = self._mode_sniffer.alt_screen
        if self.state.alt_screen != prev_alt:
            # Alt-screen transition (#130): drop the keyframe on EITHER edge.
            # Entering alt starts a NEW full-frame session whose one-time paint we
            # have not captured yet; leaving alt ends the session. A keyframe from
            # a different (or ended) alt session must never reconstruct onto the
            # current one (that silently bleeds the previous app's static panels
            # across, worse than a missed panel), so it can't wait for a read to
            # clear it -- the failing case has no read between the old app's exit
            # and the new app's post-eviction read. Cheap and on the loop; the
            # bool compare + three assignments can't raise into byte delivery, so
            # no guard is needed. The next trustworthy read re-seeds; until then a
            # bug read honestly returns `partial` (see _render_screen_text #130).
            self._screen_keyframe = None
            self._keyframe_k = 0
            self._keyframe_dims = (0, 0)
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
            # Live cwd (best-effort): the foreground agent's working dir (the
            # shell's as fallback), so the AGENTS.md button tracks where the
            # agent actually runs even when the shell sits a level up (#47), and
            # follows a `cd`. State updates before the frame (reconnect hello
            # accuracy), same as agent above. Errors are swallowed by the
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
            # Drop the alt-screen keyframe (#130): it was captured at the old
            # dims, so its per-row CUP moves would misposition on the new grid.
            # The TUI repaints on SIGWINCH and the chain re-seeds from the next
            # trustworthy read; until then a bug read returns partial.
            self._screen_keyframe = None
            self._keyframe_k = 0
            self._keyframe_dims = (0, 0)
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

        # If the kill targets the session-root shell (pinned at spawn), this is a
        # deliberate Terminate. Mark it now, on the loop and before kill_proc
        # runs, so the flag is reliably set before a successful kill makes
        # _on_pty_exit resolve _exit_fut (run() reads the flag after the await).
        # kill_proc may still reject the kill (recycled PID / identity mismatch /
        # psutil unavailable); in that case the shell keeps running, so we clear
        # the flag below once the executor confirms ok=False — otherwise a later
        # genuine crash would be masked as a clean exit and not respawn.
        root_kill = bool(self.state.pid) and pid == self.state.pid
        if root_kill:
            self._terminated_by_request = True

        async def _run() -> None:
            try:
                ok, err = await loop.run_in_executor(
                    None, self.backend.kill_proc, pid)
            except Exception as exc:
                ok, err = False, str(exc)[:200]
            if root_kill and not ok and not (
                    self._exit_fut is not None and self._exit_fut.done()):
                # Kill was rejected and the shell hasn't exited yet: this was not
                # an effective Terminate, so don't let it mask a future crash.
                self._terminated_by_request = False
            self._enqueue("txt", protocol.killed_frame(req, ok, error=err,
                                                        pid=pid))

        loop.create_task(_run())

    def _on_reset_request(self, req: int) -> None:
        # MCP reset_terminal (#27): wipe Browserland's PTY-output ring so the
        # next read_screen (and any reconnecting browser's snapshot) renders
        # from a clean slate — independent of what the app does, since the
        # renderer reads the ring, not the app's stdin. Clearing also resets the
        # ring's evicted flag (an empty head can't be a cut escape sequence).
        # The app's live alt-screen / DECCKM state is sniffed off the byte
        # stream, not the ring, so it is intentionally left intact. Handled
        # inline on the loop — ByteRing.clear() is O(1), never blocking.
        try:
            self.ring.clear()
            # Drop the alt-screen keyframe (#130): the ring (and its
            # total_appended) is zeroed, so a retained keyframe would reconstruct
            # a stale grid onto the clean slate reset promises.
            self._screen_keyframe = None
            self._keyframe_k = 0
            self._keyframe_dims = (0, 0)
            self._wake_output_waiters()      # a parked wait_for_change re-renders
            ok, err = True, None
        except Exception as exc:
            ok, err = False, str(exc)[:200]
        self._enqueue("txt", protocol.reset_done_frame(req, ok, error=err))

    def _on_flush_request(self, req: int) -> None:
        # MCP flush_input (#133): discard keystrokes queued toward the app but
        # not yet consumed — the INPUT-side mirror of reset_terminal. Where reset
        # wipes our OUTPUT ring, this drops the pending INPUT backlog (e.g. a
        # runaway send_keys burst a frame-polling TUI hasn't drained). Flushing
        # input changes no OUTPUT, so — unlike _on_reset_request — we deliberately
        # do NOT clear the ring, do NOT wake wait_for_change waiters, and do NOT
        # touch the keyframe: the rendered screen is unaffected. The backend does
        # the platform-specific flush (a no-op where none exists) and is built
        # never to raise; we guard anyway so the ack always goes out.
        try:
            self.backend.flush_input()
            ok, err = True, None
        except Exception as exc:
            ok, err = False, str(exc)[:200]
        self._enqueue("txt", protocol.flush_input_done_frame(req, ok, error=err))

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
                           lines: int = 0,
                           wait_for_change: Optional[str] = None,
                           timeout_ms: int = 0,
                           wait_for_text: Optional[str] = None,
                           wait_for_regex: Optional[str] = None,
                           wait_absent: bool = False,
                           since: Optional[str] = None,
                           attrs: bool = False) -> None:
        # MCP /mcp/read: render the live screen as plain text. Each render
        # snapshots the ring + dims + live alt-screen state synchronously on the
        # loop (an immutable, consistent view), then renders off-loop (pyte can
        # be CPU-heavy on a large ring) and enqueues the reply on the SAME
        # ordered out-queue. With wait_for_change (#26) it re-renders on each
        # PTY-output nudge until the screen hash differs from the baseline; with
        # wait_for_text / wait_for_regex (#51) it instead waits until the screen
        # CONTAINS (or, with wait_absent, no longer contains) the match — waking
        # once on the awaited event instead of on every noisy TUI frame. Either
        # way the hold is bounded by timeout_ms — one round-trip, no busy-poll.
        loop = asyncio.get_running_loop()
        # Precompile the content predicate once. The broker validates the regex
        # up front (bad_regex 400), so a compile error here is belt-and-braces:
        # treat it as "no predicate" rather than waiting out the whole timeout.
        regex = None
        if wait_for_regex:
            try:
                regex = re.compile(wait_for_regex)
            except re.error:
                wait_for_regex = None
        has_predicate = bool(wait_for_text) or regex is not None

        def _predicate_met(text: str) -> bool:
            if wait_for_text:
                present = wait_for_text in text
            elif regex is not None:
                present = regex.search(text) is not None
            else:
                return False
            return (not present) if wait_absent else present

        async def _render() -> dict:
            # Snapshot on the loop (ring.get() returns immutable bytes) so the
            # executor never walks the live deque; re-read dims/alt each pass.
            data = self.ring.get()
            evicted = self.ring.evicted          # head may start mid-seq (#28)
            cols, rows = self.state.cols, self.state.rows
            # Snapshot the keyframe machinery on the loop (immutable bytes/ints
            # only) alongside the ring, so the worker never touches live state or
            # a mutable pyte screen (#130). evicted_total is derivable from the
            # monotonic counter; both it and keyframe_k sit on chunk boundaries.
            evicted_total = self.ring.total_appended - len(self.ring)
            total_appended = self.ring.total_appended
            keyframe = self._screen_keyframe or b""
            keyframe_k = self._keyframe_k
            keyframe_dims = self._keyframe_dims
            r = await loop.run_in_executor(
                None, _render_screen_text, data, cols, rows, view, lines,
                self.state.alt_screen, evicted, attrs,
                keyframe, keyframe_k, keyframe_dims, evicted_total,
                total_appended)
            r["cols"], r["rows"] = cols, rows
            # Re-stash a freshly-emitted keyframe on the loop (serialized with
            # every other loop mutation of it — race-free, no locks).
            if "keyframe" in r:
                self._screen_keyframe = r["keyframe"]
                self._keyframe_k = int(r.get("keyframe_k", 0))
                self._keyframe_dims = (cols, rows)
            return r

        def _reply(r: dict, content_hash: str,
                   matched: Optional[bool] = None) -> None:
            rows_list = r["text"].split("\n")
            changed_rows = None
            delta = False
            # Delta mode (#52): if the caller passed a `since` hash we still
            # hold a frame for, and this is a clean same-size CURRENT-screen
            # render, return only the rows that changed. The change-ratio guard
            # falls back to the full grid when most rows changed (a scrolled
            # shell), so a delta is never bigger than the full read.
            if (since and not r["degraded"] and r["view"] == "screen"):
                base = self._frame_cache.get(since)
                if base is not None and len(base) == len(rows_list):
                    diff = [{"row": i, "text": rows_list[i]}
                            for i in range(len(rows_list))
                            if rows_list[i] != base[i]]
                    # Worth it only when a minority of rows changed. The empty
                    # diff (unchanged screen) is always worth it; otherwise the
                    # ratio guard (no max(1,...), so a 1-row 100%-changed screen
                    # is excluded) keeps a delta from ever exceeding the full
                    # grid it replaces.
                    if (not diff or len(diff)
                            <= int(len(rows_list) * _DELTA_MAX_CHANGED_RATIO)):
                        changed_rows = diff
                        delta = True
            # Remember this frame so a later `since=content_hash` can diff it.
            self._remember_frame(content_hash, rows_list)
            self._enqueue("txt", protocol.screen_text_frame(
                req, ("" if delta else r["text"]), r["cols"], r["rows"],
                degraded=r["degraded"], alt_screen=r["alt_screen"],
                cursor=r["cursor"], view=r["view"],
                history_lines=r["history_lines"],
                app_cursor=self.state.app_cursor, content_hash=content_hash,
                matched=matched, delta=delta, changed_rows=changed_rows,
                attr_runs=r.get("attr_runs"), partial=r.get("partial", False)))

        async def _run() -> None:
            try:
                wait_ms = max(0, min(int(timeout_ms or 0), MAX_WAIT_MS))
                deadline = loop.time() + wait_ms / 1000.0
                while True:
                    observed = self._output_gen
                    r = await _render()
                    h = _content_hash(r["text"])
                    # Evaluate the content predicate OFF the event loop: a
                    # catastrophic-backtracking regex must not block the loop
                    # (input/output/other RPCs). The gen-recheck below still
                    # guards the park against a nudge during this await.
                    pred = bool(await loop.run_in_executor(
                        None, _predicate_met, r["text"])) if has_predicate else None
                    timed_out = loop.time() >= deadline
                    # Decide whether this render satisfies the request. An
                    # immediate read (no wait mode) always replies on pass one.
                    matched: Optional[bool] = None
                    done = not wait_for_change and not has_predicate
                    if pred:
                        done, matched = True, True
                    if wait_for_change and h != wait_for_change:
                        done = True
                    if timed_out:
                        done = True
                        if has_predicate and matched is None:
                            matched = False        # waited out, never matched
                    if done:
                        _reply(r, h, matched)
                        return
                    # New output during the render/predicate await? Re-render
                    # before parking so we never miss a frame.
                    if self._output_gen != observed:
                        continue
                    remaining = deadline - loop.time()
                    if remaining <= 0:
                        continue                 # deadline -> reply next pass
                    # No await between the gen-check above and this append, so
                    # _on_pty_data can't slip a nudge past us (loop atomicity).
                    fut = loop.create_future()
                    self._output_waiters.append(fut)
                    try:
                        await asyncio.wait_for(fut, timeout=remaining)
                    except asyncio.TimeoutError:
                        pass                     # deadline -> reply next pass
                    finally:
                        try:
                            self._output_waiters.remove(fut)
                        except ValueError:
                            pass                 # drained by _on_pty_data
            except Exception as exc:
                # Always answer the RPC — an unhandled failure here would leave
                # the broker waiting until it times out (no_producer_rpc).
                LOGGER.warning("screen_text request failed (%s); degraded reply",
                               exc)
                self._enqueue("txt", protocol.screen_text_frame(
                    req, "", self.state.cols, self.state.rows, degraded=True,
                    alt_screen=self.state.alt_screen, cursor=None, view="raw",
                    history_lines=0, app_cursor=self.state.app_cursor,
                    content_hash=""))

        loop.create_task(_run())

    # -- internal -------------------------------------------------------------

    def _remember_frame(self, content_hash: str, rows: List[str]) -> None:
        """Cache a rendered frame's rows under its content_hash for a later
        delta read (#52), as a bounded LRU. Empty hashes (degraded renders that
        didn't compute one) are not cached — there's nothing to diff against."""
        if not content_hash:
            return
        fc = self._frame_cache
        if content_hash in fc:
            fc.move_to_end(content_hash)
        else:
            fc[content_hash] = rows
        while len(fc) > _FRAME_CACHE_MAX:
            fc.popitem(last=False)

    def _enqueue(self, kind: str, payload) -> None:
        # While disconnected nothing is queued: the ring keeps history and
        # the post-reconnect snapshot heals attached browsers.
        if not self.client.connected:
            return
        self.out_q.put_nowait((kind, payload))
