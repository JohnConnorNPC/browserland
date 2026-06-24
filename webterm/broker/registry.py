"""In-memory registry of live producer sessions (agents + terminal windows).

Adapted from xterm-py ``browser/broker.py`` (the relay origin), plus a
``host`` hello field, with two additions:

* ``kind`` — additive hello field; agents send ``"kind": "agent"``, a
  non-agent producer sends nothing and defaults to ``"terminal"`` (the
  picker shows an [agent] badge, nothing else changes).
* launch waiters — ``POST /launch`` parks an asyncio.Event on the allocated
  window_id; ``register`` fires it so the endpoint can answer 200 once the
  spawned agent's hello lands.

Invariants preserved from the reference: hello must be the first frame
(anything else drops the connection), a hello with an existing window_id
replaces the stale entry, binary frames broadcast verbatim to subscribers,
``resized`` re-broadcasts to every attached browser.
"""

from __future__ import annotations

import asyncio
import json
import logging
import socket
from typing import Any, Dict, List, Optional, Set

from .. import protocol

LOGGER = logging.getLogger(__name__)

# Only these foreground-agent labels are accepted from a producer; anything
# else (a hostile or buggy agent) collapses to "" = nothing running.
_AGENTS = ("claude", "grok", "codex", "opencode")


def _whitelist_agent(value: Any) -> str:
    name = str(value or "").strip().lower()
    return name if name in _AGENTS else ""


# A management RPC (procs / kill / git_status) is at most this many in flight
# per producer connection; the (N+1)th request is rejected so a flood of
# /session/* calls can't pile Futures or executor jobs on one agent.
RPC_MAX_INFLIGHT = 4


class _PendingRpc:
    """One in-flight management RPC: the Future the endpoint awaits plus the
    reply ``type`` it expects (so a ``git_status`` reply can't satisfy a
    pending ``procs`` request just because the ``req`` id collides)."""
    __slots__ = ("future", "expected")

    def __init__(self, future: "asyncio.Future", expected: str) -> None:
        self.future = future
        self.expected = expected


class WindowEntry:
    """One producer session: the live WS plus attached browser WSes."""

    def __init__(
        self,
        window_id: int,
        pid: int,
        title: str,
        cols: int,
        rows: int,
        ws,
        host: str = "",
        kind: str = "terminal",
        agent: str = "",
        cwd: str = "",
    ):
        self.id = int(window_id)
        self.pid = int(pid)
        self.title = title
        self.cols = int(cols)
        self.rows = int(rows)
        self.ws = ws
        self.host = host
        self.kind = kind
        self.agent = agent
        self.cwd = cwd
        # Per-window MCP access mode: None = inherit the broker default
        # (mcp_cfg.default_mode), else an explicit "off"/"read"/"readwrite"
        # override. In-memory only — resets to the default on broker restart
        # or agent relaunch (the durable policy is the global default; this is
        # a live per-window override). WindowEntry stays ignorant of app.ctx:
        # the effective mode is resolved by the handlers that know the default.
        self.mcp_mode: Optional[str] = None
        self.subscribers: Set[Any] = set()
        # Parallel map subscriber-ws -> the browser clientId that opened it (""
        # for a legacy/id-less /ws). Keeps `subscribers` a plain Set so
        # broadcast_* and the 1012 stale-close path are untouched; this is read
        # only by close_clients_terminals to cut a deactivated browser loose.
        self.subscriber_clients: Dict[Any, str] = {}
        self._send_lock = asyncio.Lock()
        # Per-connection management-RPC state (see _PendingRpc). req ids are a
        # monotonic per-entry counter that is NEVER reused on a live connection
        # (a late reply after a timeout must not satisfy a later request), and
        # the whole map dies with the connection.
        self.pending_rpc: Dict[int, _PendingRpc] = {}
        self._next_req = 1

    def summary(self, mcp_default: str = "off") -> Dict[str, Any]:
        # ``mcp`` is the EFFECTIVE access mode (per-window override or the
        # broker default), so a /sessions consumer sees what MCP would honor.
        return {
            "id": self.id,
            "pid": self.pid,
            "title": self.title,
            "cols": self.cols,
            "rows": self.rows,
            "host": self.host,
            "kind": self.kind,
            "agent": self.agent,
            "cwd": self.cwd,
            "mcp": self.mcp_mode or mcp_default,
        }

    async def send_to_producer(self, text: str) -> None:
        """Forward a JSON text frame to the producer. Single in-flight per WS."""
        async with self._send_lock:
            try:
                await self.ws.send(text)
            except Exception as exc:
                LOGGER.debug("send_to_producer failed for window %s: %s",
                             self.id, exc)

    async def request_snapshot(self) -> None:
        await self.send_to_producer(protocol.snapshot_please_frame())

    # -- management RPCs (procs / kill / git_status) ------------------------

    def new_rpc(self, expected: str):
        """Allocate a fresh req id + Future for a management RPC, or None when
        too many are already in flight. Returns ``(req, future)``."""
        if len(self.pending_rpc) >= RPC_MAX_INFLIGHT:
            return None
        req = self._next_req
        self._next_req += 1
        future = asyncio.get_running_loop().create_future()
        self.pending_rpc[req] = _PendingRpc(future, expected)
        return req, future

    def resolve_rpc(self, req: int, reply_type: str, payload: Any) -> None:
        """A reply arrived from the producer. Resolve ONLY the matching pending
        request, and only when its expected type matches — an unknown, late,
        duplicate, or type-mismatched reply is dropped, never creating state."""
        pending = self.pending_rpc.get(req)
        if pending is None:
            LOGGER.debug("rpc reply for unknown/expired req %s on window %s",
                         req, self.id)
            return
        if pending.expected != reply_type:
            LOGGER.warning("rpc reply type %r != expected %r for req %s "
                           "on window %s", reply_type, pending.expected, req,
                           self.id)
            return
        self.pending_rpc.pop(req, None)
        if not pending.future.done():
            pending.future.set_result(payload)

    def cancel_rpc(self, req: int, future: "asyncio.Future") -> None:
        """Drop a pending request (the endpoint timed out / errored). Guarded by
        identity so a recycled req id can't evict a newer request's Future."""
        pending = self.pending_rpc.get(req)
        if pending is not None and pending.future is future:
            self.pending_rpc.pop(req, None)

    def fail_all_rpc(self, exc: BaseException) -> None:
        """Connection gone (disconnect or producer replacement): fail every
        in-flight request so its endpoint returns promptly instead of waiting
        out the timeout, and clear the map so nothing leaks."""
        for pending in list(self.pending_rpc.values()):
            if not pending.future.done():
                pending.future.set_exception(exc)
        self.pending_rpc.clear()

    def add_subscriber(self, ws, client_id: str = "") -> None:
        self.subscribers.add(ws)
        self.subscriber_clients[ws] = client_id

    def remove_subscriber(self, ws) -> None:
        self.subscribers.discard(ws)
        self.subscriber_clients.pop(ws, None)

    async def broadcast_binary(self, payload: bytes) -> None:
        if not self.subscribers:
            return
        # Iterate over a snapshot; subscribers may be removed mid-send. A failed
        # send drops the sub from BOTH maps (remove_subscriber) so the
        # subscriber_clients side map never outlives its subscriber.
        for sub in list(self.subscribers):
            try:
                await sub.send(payload)
            except Exception:
                self.remove_subscriber(sub)

    async def broadcast_text(self, payload: str) -> None:
        if not self.subscribers:
            return
        for sub in list(self.subscribers):
            try:
                await sub.send(payload)
            except Exception:
                self.remove_subscriber(sub)


class BrokerRegistry:
    """Process-wide map id -> WindowEntry, plus launch waiters."""

    def __init__(self) -> None:
        self._entries: Dict[int, WindowEntry] = {}
        self._lock = asyncio.Lock()
        self._waiters: Dict[int, asyncio.Event] = {}

    async def register(self, ws, hello: Dict[str, Any]) -> WindowEntry:
        window_id = int(hello.get("window_id"))
        pid = int(hello.get("pid", 0))
        title = str(hello.get("title", ""))
        cols = int(hello.get("cols", 80))
        rows = int(hello.get("rows", 24))
        host = str(hello.get("host", "") or "").strip() or socket.gethostname()
        kind = str(hello.get("kind", "") or "").strip() or "terminal"
        agent = _whitelist_agent(hello.get("agent"))
        cwd = str(hello.get("cwd", "") or "")

        entry = WindowEntry(window_id, pid, title, cols, rows, ws,
                            host=host, kind=kind, agent=agent, cwd=cwd)
        async with self._lock:
            old = self._entries.get(window_id)
            if old is not None:
                # Stale entry from a dropped connection — replace.
                LOGGER.info("replacing stale entry for window %s", window_id)
            self._entries[window_id] = entry
            waiter = self._waiters.get(window_id)
        if old is not None:
            # The replaced producer can never answer its in-flight management
            # RPCs (and a late reply from it must not satisfy a NEW request on
            # the fresh entry — they live on separate WindowEntry objects, so
            # this just frees the old endpoints).
            old.fail_all_rpc(ConnectionError("producer replaced"))
            # Browsers attached to the dead entry would otherwise look
            # healthy (open WS) but be frozen — close them (1012 Service
            # Restart) so the page's auto-reattach lands on this entry.
            for sub in list(old.subscribers):
                try:
                    await sub.close(code=1012, reason="producer reconnected")
                except Exception:
                    pass
        if waiter is not None:
            waiter.set()
        LOGGER.info("registered window %s host=%s kind=%s pid=%s title=%r",
                    window_id, host, kind, pid, title)
        return entry

    async def deregister(self, window_id: int,
                         entry: Optional[WindowEntry] = None) -> None:
        async with self._lock:
            current = self._entries.get(window_id)
            # Only remove the same entry (a fresh registration may have
            # raced with this disconnect's cleanup).
            if current is not None and (entry is None or current is entry):
                del self._entries[window_id]
                LOGGER.info("deregistered window %s", window_id)

    def get(self, window_id: int) -> Optional[WindowEntry]:
        return self._entries.get(int(window_id))

    def __contains__(self, window_id: int) -> bool:
        return int(window_id) in self._entries

    def session_summaries(self, mcp_default: str = "off") -> List[Dict[str, Any]]:
        return [e.summary(mcp_default) for e in self._entries.values()]

    async def close_clients_terminals(self, client_id: str, code: int) -> None:
        """Close every terminal subscriber WS that belongs to ``client_id``
        (the browser just lost the single-active lease) with ``code`` (4409 =
        deactivated), so its page tears the terminals down.

        Lock-free snapshot iteration, like get()/session_summaries: a
        brand-new subscriber that races in after the snapshot is harmless —
        the relay's per-message input backstop already gates every frame on
        the live lease, so a still-open socket can watch but never type."""
        if not client_id:
            return
        for entry in list(self._entries.values()):
            for ws in list(entry.subscribers):
                if entry.subscriber_clients.get(ws) == client_id:
                    try:
                        await ws.close(code=code, reason="deactivated")
                    except Exception:
                        pass

    # -- launch waiters -----------------------------------------------------

    def add_waiter(self, window_id: int) -> asyncio.Event:
        event = asyncio.Event()
        self._waiters[int(window_id)] = event
        return event

    def remove_waiter(self, window_id: int) -> None:
        self._waiters.pop(int(window_id), None)

    def is_pending(self, window_id: int) -> bool:
        return int(window_id) in self._waiters


async def run_producer_session(ws, registry: BrokerRegistry) -> None:
    """Drive one inbound producer WS (/browserland) until it closes.

    Reads hello, registers, then:
      * binary frames -> broadcast to all subscribers (PTY bytes + snapshots)
      * text 'title'   -> update entry.title
      * text 'agent'   -> update entry.agent + re-broadcast to browsers
      * text 'resized' -> update dims + re-broadcast to attached browsers
    """
    entry: Optional[WindowEntry] = None
    try:
        first = await ws.recv()
        if first is None:
            return
        if isinstance(first, (bytes, bytearray)):
            LOGGER.warning("producer WS sent binary before hello")
            return
        try:
            hello = json.loads(first)
        except json.JSONDecodeError:
            LOGGER.warning("producer WS bad hello json: %r", first[:200])
            return
        if not isinstance(hello, dict) or hello.get("type") != "hello":
            LOGGER.warning("producer WS first frame type=%r, expected 'hello'",
                           hello.get("type") if isinstance(hello, dict)
                           else type(hello).__name__)
            return

        entry = await registry.register(ws, hello)

        while True:
            msg = await ws.recv()
            if msg is None:
                break
            if isinstance(msg, (bytes, bytearray)):
                await entry.broadcast_binary(bytes(msg))
                continue
            data = protocol.parse(msg)
            if data is None:
                LOGGER.debug("producer bad text frame: %r", msg[:200])
                continue
            mtype = data.get("type")
            if mtype == "title":
                entry.title = str(data.get("data", entry.title))
                # Live push so attached browsers update title bars without
                # waiting for the next /sessions poll.
                await entry.broadcast_text(protocol.title_frame(entry.title))
            elif mtype == "agent":
                # Foreground-agent change: whitelist, then live-push so the
                # titlebar chips highlight without waiting for /sessions.
                entry.agent = _whitelist_agent(data.get("data"))
                await entry.broadcast_text(protocol.agent_frame(entry.agent))
            elif mtype == "cwd":
                # Live working-dir change: update the entry (so the next
                # /sessions poll is accurate) and push to attached browsers so
                # the AGENTS.md button tracks a `cd` immediately.
                entry.cwd = str(data.get("data", entry.cwd) or "")
                await entry.broadcast_text(protocol.cwd_frame(entry.cwd))
            elif mtype == "resized":
                entry.cols = int(data.get("cols", entry.cols))
                entry.rows = int(data.get("rows", entry.rows))
                # Tell every attached browser to reflow xterm.js. The
                # producer is authoritative; last-writer wins across
                # multi-browser since the next producer-driven `resized`
                # reconverges them.
                await entry.broadcast_text(protocol.resized_frame(
                    entry.cols, entry.rows))
            elif mtype == "exit":
                # The child process exited (PTY EOF). Push the event to every
                # attached browser so it tears the window down at once, and
                # deregister NOW (don't wait for this producer WS to close) so
                # the next /sessions poll already omits the session — no brief
                # reappear of a dead chip. Then stop reading: the producer is
                # shutting down and sends nothing more. A transient WS drop
                # carries NO exit frame, so reconnect grace is untouched.
                await entry.broadcast_text(protocol.exit_frame(_code(data)))
                await registry.deregister(entry.id, entry)
                break
            elif mtype in ("procs", "killed", "git_status", "screen_text"):
                # Management-RPC replies: resolve the matching pending request
                # on THIS entry only. _req() tolerates a missing/garbled id by
                # mapping to -1, which simply never matches a live request.
                entry.resolve_rpc(_req(data), mtype, data)
            else:
                LOGGER.debug("producer unknown text type %r", mtype)
    except Exception as exc:
        LOGGER.info("producer session ended: %s", exc)
    finally:
        if entry is not None:
            # The connection is gone — fail any in-flight management RPCs so
            # their endpoints return now instead of waiting out the timeout.
            entry.fail_all_rpc(ConnectionError("producer disconnected"))
            await registry.deregister(entry.id, entry)


def _req(data: Dict[str, Any]) -> int:
    try:
        return int(data.get("req"))
    except (TypeError, ValueError):
        return -1


def _code(data: Dict[str, Any]) -> int:
    """Exit code off an ``exit`` frame; a missing/garbled value maps to 0 (the
    browser only uses the frame as a teardown signal, not the code)."""
    try:
        return int(data.get("code"))
    except (TypeError, ValueError):
        return 0
