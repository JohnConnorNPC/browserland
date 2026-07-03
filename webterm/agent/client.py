"""BrokerClient: the agent's outbound WS to the broker's /browserland endpoint.

Owns connect/reconnect with exponential backoff (500 ms -> 10 s cap, x2 per
failure, reset on success), sends the hello, and
runs exactly one sender task draining the agent's single outbound queue.

Queue items are ``(kind, payload)`` tuples:
  ("bin",  bytes) — live PTY output; consecutive items may be coalesced
                    into one binary frame up to 64 KiB
  ("snap", bytes) — a snapshot; sent as its own binary frame, never merged
                    with neighbors
  ("txt",  str)   — a JSON control frame (title / resized)

While disconnected the PTY keeps running and the queue is drained — there
is no replay of missed bytes; browsers re-attach and their attach triggers
``snapshot_please``, which heals from the ring.
"""

from __future__ import annotations

import asyncio
import logging
import urllib.parse
from typing import Callable, Optional, Tuple

import websockets

from .. import protocol

LOGGER = logging.getLogger(__name__)

_BACKOFF_INITIAL = 0.5
_BACKOFF_CAP = 10.0
_COALESCE_MAX = 64 * 1024

OutItem = Tuple[str, object]


def _int(value, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


class BrokerClient:
    def __init__(
        self,
        url: str,
        token: Optional[str],
        state,  # SessionState (agent.py) — read live at hello time
        out_q: "asyncio.Queue[OutItem]",
        *,
        on_input: Callable[[bytes], None],
        on_resize: Callable[[int, int], None],
        on_snapshot_request: Callable[[], None],
        on_procs_request: Optional[Callable[[int], None]] = None,
        on_kill_request: Optional[Callable[[int, int], None]] = None,
        on_git_request: Optional[Callable[[int], None]] = None,
        on_screen_request: Optional[Callable[..., None]] = None,
        on_reset_request: Optional[Callable[[int], None]] = None,
        on_flush_request: Optional[Callable[[int], None]] = None,
    ) -> None:
        self._url = url
        self._token = token
        self._state = state
        self._out_q = out_q
        self._on_input = on_input
        self._on_resize = on_resize
        self._on_snapshot_request = on_snapshot_request
        self._on_procs_request = on_procs_request
        self._on_kill_request = on_kill_request
        self._on_git_request = on_git_request
        self._on_screen_request = on_screen_request
        self._on_reset_request = on_reset_request
        self._on_flush_request = on_flush_request
        self.connected = False
        self._stopping = False
        self._stop_event: Optional[asyncio.Event] = None
        self._ws = None

    # -- public -----------------------------------------------------------

    async def run(self) -> None:
        """Connect-and-serve until stop(). Never raises on broker trouble."""
        self._stop_event = asyncio.Event()
        backoff = _BACKOFF_INITIAL
        while not self._stopping:
            try:
                ws = await websockets.connect(
                    self._connect_url(),
                    ping_interval=20,
                    ping_timeout=20,
                    max_size=None,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                LOGGER.info("broker connect to %s failed: %s",
                            self._redacted_url(), exc)
                self._drain()
                if await self._sleep(backoff):
                    break
                backoff = min(backoff * 2, _BACKOFF_CAP)
                continue

            backoff = _BACKOFF_INITIAL  # reset on success
            try:
                await self._serve(ws)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                LOGGER.info("broker session ended: %s", exc)
            finally:
                self.connected = False
                self._ws = None
                try:
                    await ws.close()
                except Exception:
                    pass

            if self._stopping:
                break
            self._drain()
            if await self._sleep(backoff):
                break
            backoff = min(backoff * 2, _BACKOFF_CAP)

    async def stop(self) -> None:
        self._stopping = True
        if self._stop_event is not None:
            self._stop_event.set()
        ws = self._ws
        if ws is not None:
            try:
                await ws.close()
            except Exception:
                pass

    # -- connection lifecycle ----------------------------------------------

    async def _serve(self, ws) -> None:
        # Anything queued while we were down is stale — browsers re-attach
        # and snapshot. Drop it before the hello so the first frames after
        # hello are coherent.
        self._drain()
        s = self._state
        await ws.send(protocol.hello_frame(
            s.window_id, s.pid, s.title, s.cols, s.rows,
            host=s.host, kind=s.kind, agent=s.agent, cwd=s.cwd,
            profile=s.profile, version=s.version,
        ))
        self._ws = ws
        self.connected = True
        # Heal the connect-window race: the detection loop updates state.agent
        # then _enqueue()-drops the frame while connected is still False. The
        # hello above carries state.agent as read *before* the send, so a change
        # landing during the send would leave the broker on the stale value with
        # no follow-up frame (detection only re-sends on change). Re-assert the
        # current agent now that we're live so the broker always converges. This
        # rides the queue, so it is ordered after the hello.
        self._out_q.put_nowait(("txt", protocol.agent_frame(s.agent)))
        LOGGER.info("registered with broker as window %d (%dx%d)",
                    s.window_id, s.cols, s.rows)

        recv_task = asyncio.create_task(self._recv_loop(ws))
        send_task = asyncio.create_task(self._sender(ws))
        try:
            done, pending = await asyncio.wait(
                {recv_task, send_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
        finally:
            self.connected = False
            for task in (recv_task, send_task):
                task.cancel()
            await asyncio.gather(recv_task, send_task, return_exceptions=True)
        for task in done:
            exc = task.exception()
            if exc is not None and not isinstance(
                    exc, websockets.ConnectionClosed):
                raise exc

    async def _recv_loop(self, ws) -> None:
        async for message in ws:
            if isinstance(message, (bytes, bytearray)):
                # Binary fallback = raw input bytes.
                self._on_input(bytes(message))
                continue
            data = protocol.parse(message)
            if data is None:
                continue
            mtype = data.get("type")
            if mtype == "input":
                self._on_input(str(data.get("data", "")).encode("utf-8"))
            elif mtype == "resize":
                try:
                    cols = int(data.get("cols", 80))
                    rows = int(data.get("rows", 24))
                except (TypeError, ValueError):
                    continue
                self._on_resize(cols, rows)
            elif mtype == "snapshot_please":
                self._on_snapshot_request()
            elif mtype == "procs_please":
                if self._on_procs_request is not None:
                    self._on_procs_request(_int(data.get("req"), -1))
            elif mtype == "kill":
                if self._on_kill_request is not None:
                    self._on_kill_request(_int(data.get("req"), -1),
                                          _int(data.get("pid"), 0))
            elif mtype == "reset_please":
                if self._on_reset_request is not None:
                    self._on_reset_request(_int(data.get("req"), -1))
            elif mtype == "flush_input_please":
                if self._on_flush_request is not None:
                    self._on_flush_request(_int(data.get("req"), -1))
            elif mtype == "git_status_please":
                if self._on_git_request is not None:
                    self._on_git_request(_int(data.get("req"), -1))
            elif mtype == "screen_text_please":
                if self._on_screen_request is not None:
                    # view/lines drive scrollback (#21); wait_for_change/
                    # timeout_ms drive wait-for-change (#26); wait_for_text/
                    # wait_for_regex/wait_absent drive wait-for-content (#51);
                    # attrs adds the styled-run map (#128). All absent for older
                    # brokers -> an immediate single plain-text read.
                    wfc = data.get("wait_for_change")
                    wft = data.get("wait_for_text")
                    wfr = data.get("wait_for_regex")
                    since = data.get("since")
                    self._on_screen_request(
                        _int(data.get("req"), -1),
                        str(data.get("view", "screen") or "screen"),
                        _int(data.get("lines"), 0),
                        wfc if isinstance(wfc, str) and wfc else None,
                        _int(data.get("timeout_ms"), 0),
                        wait_for_text=wft if isinstance(wft, str) and wft else None,
                        wait_for_regex=wfr if isinstance(wfr, str) and wfr else None,
                        wait_absent=bool(data.get("wait_absent", False)),
                        since=since if isinstance(since, str) and since else None,
                        attrs=bool(data.get("attrs", False)))
            else:
                LOGGER.debug("unknown broker frame type %r", mtype)

    async def _sender(self, ws) -> None:
        out_q = self._out_q
        while True:
            item = await out_q.get()
            items = [item]
            # Opportunistically batch whatever is immediately available so
            # consecutive small PTY chunks ride one frame. Never reach past
            # a non-"bin" item (text frames and snapshots are barriers).
            if item[0] == "bin":
                total = len(item[1])
                while total < _COALESCE_MAX:
                    try:
                        nxt = out_q.get_nowait()
                    except asyncio.QueueEmpty:
                        break
                    items.append(nxt)
                    if nxt[0] != "bin":
                        break
                    total += len(nxt[1])
            try:
                i = 0
                while i < len(items):
                    kind, payload = items[i]
                    if kind == "bin":
                        j = i
                        buf = bytearray()
                        while j < len(items) and items[j][0] == "bin":
                            buf += items[j][1]
                            j += 1
                        await ws.send(bytes(buf))
                        i = j
                    else:
                        # "txt" payloads are str -> text frame;
                        # "snap" payloads are bytes -> binary frame.
                        await ws.send(payload)
                        i += 1
            finally:
                # On send failure the items are lost by design (snapshot
                # heals after reconnect) — but join() must not deadlock.
                for _ in items:
                    out_q.task_done()

    # -- helpers ------------------------------------------------------------

    def _drain(self) -> None:
        while True:
            try:
                self._out_q.get_nowait()
            except asyncio.QueueEmpty:
                return
            self._out_q.task_done()

    async def _sleep(self, seconds: float) -> bool:
        """Backoff sleep, interruptible by stop(). True if stopping."""
        try:
            await asyncio.wait_for(self._stop_event.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            pass
        return self._stopping

    def _connect_url(self) -> str:
        if not self._token:
            return self._url
        parts = urllib.parse.urlsplit(self._url)
        query = urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
        if any(key in ("token", "auth") for key, _ in query):
            return self._url
        query.append(("token", self._token))
        return urllib.parse.urlunsplit(parts._replace(
            query=urllib.parse.urlencode(query)))

    def _redacted_url(self) -> str:
        """The broker URL without its query string — never log the token."""
        parts = urllib.parse.urlsplit(self._url)
        return urllib.parse.urlunsplit(parts._replace(query=""))
