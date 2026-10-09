"""In-memory registry of live producer sessions (agents + terminal windows).

Adapted from xterm-py ``browser/broker.py`` (the relay origin; a separate
codebase at https://github.com/JohnConnorNPC/xterm-py), plus a
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
import contextlib
import inspect
import json
import logging
import socket
import time
from typing import (Any, Awaitable, Callable, Dict, List, Mapping, Optional,
                    Set)

from .. import protocol

LOGGER = logging.getLogger(__name__)

# Only these foreground-agent labels are accepted from a producer; anything
# else (a hostile or buggy agent) collapses to "" = nothing running.
# DRIFT: duplicated as ``_AGENTS`` in ``webterm/agent/detect.py`` (the producer
# side that PRODUCES these labels) — keep both in step; deliberately not a shared
# import, so the broker package stays independent of the agent package.
# ``tests/test_registry_agent.py::test_agent_whitelists_do_not_drift`` enforces it.
_AGENTS = ("claude", "grok", "codex", "opencode", "hermes")


def _whitelist_agent(value: Any) -> str:
    name = str(value or "").strip().lower()
    return name if name in _AGENTS else ""


# A management RPC (procs / kill / git_status) is at most this many in flight
# per producer connection; the (N+1)th request is rejected so a flood of
# /session/* calls can't pile Futures or executor jobs on one agent.
RPC_MAX_INFLIGHT = 4

# #238: the longest a control publication waits on ONE socket's send (the
# revoked frame, a /ws subscriber's control frame, a /control socket's push).
# There is no broker-wide send timeout to reuse; this bounds how long a stalled
# peer can hold a terminal's publisher lock, which every /ws attach to that
# terminal also takes. A socket that misses it (a /ws subscriber or a /control
# socket) is dropped and closed with 1012 so its page reconnects; a controller
# dropped this way loses control at once.
CONTROL_SEND_TIMEOUT = 0.5


class _PendingRpc:
    """One in-flight management RPC: the Future the endpoint awaits plus the
    reply ``type`` it expects (so a ``git_status`` reply can't satisfy a
    pending ``procs`` request just because the ``req`` id collides)."""
    __slots__ = ("future", "expected")

    def __init__(self, future: "asyncio.Future", expected: str) -> None:
        self.future = future
        self.expected = expected


class _Controller:
    """The one external controller of a terminal (#238): the /ws socket that
    claimed it, the label the owner is shown, when (epoch ms) and at which
    gen. Replaced whole on every claim, never mutated."""
    __slots__ = ("ws", "label", "since", "gen")

    def __init__(self, ws, label: str, since: int, gen: int) -> None:
        self.ws = ws
        self.label = label
        self.since = since
        self.gen = gen


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
        profile: str = "",
        version: str = "",
        pyte: bool = True,
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
        # The launch-profile name this session was spawned from (#115), from the
        # hello. "" for a non-launcher/old producer. Immutable — no update frame.
        self.profile = profile
        # The producer's self-reported build id (#22); "" for a pre-#22 agent
        # (which is itself a staleness signal).
        self.version = version
        # Whether the producer has pyte (#134). True for a pre-#134 agent that
        # omits the hello field — absence means "unknown/old" (the signal
        # postdates it), so defaulting True avoids a false pyte-less alarm. A
        # reported False means read_screen uses the dependency-free textgrid
        # fallback: no attr_runs (#128) / keyframe repair (#130); a sparse
        # alt-screen frame after ring eviction is flagged `partial` only.
        self.pyte = bool(pyte)
        # Live DECCKM / application-cursor-key state (#23), pushed by the agent
        # via `mode` frames; lets send_keys pick CSI vs SS3 arrows cheaply (no
        # screen render). False until the agent reports otherwise.
        self.app_cursor = False
        # Per-window MCP access mode: None = inherit the broker default
        # (mcp_cfg.default_mode), else an explicit "off"/"read"/"readwrite"
        # override. WindowEntry stays ignorant of app.ctx: the effective mode is
        # resolved by the handlers that know the default.
        #
        # mcp_mode and mcp_scope (below) are owned by the broker's per-window
        # store (mcp_windows.McpWindowStore), which create_app installs as
        # BrokerRegistry.on_register: when a window registers, a stored row
        # whose host and pid match the hello (a null one is claimed by the
        # first hello) re-applies both; failing that, both carry over from a
        # replaced same-id entry reporting the same host and the same nonzero
        # pid (registry.same_producer, the gate register() itself applies
        # when no hook is installed). Everything else starts at None: a
        # broker restart or a reconnect with no matching row (nothing was ever
        # set for that producer: POST /session/mcp writes a row for every
        # change), a relaunch (launcher ids are fresh per launch; an agent
        # pinned with --window-id comes back under the same id with a new
        # shell pid).
        self.mcp_mode: Optional[str] = None
        # Per-window MCP scope (#237): the MCP-client partition this window
        # belongs to, or None = unscoped. A bare str|None fact: nothing here
        # validates or defaults it (summary() reports it raw). Same ownership
        # and lifetime as mcp_mode above.
        self.mcp_scope: Optional[str] = None
        # Per-terminal DEFAULT inter-key pacing for send_keys (#133): the ms an
        # MCP send_keys with no explicit delay_ms auto-paces at, so a frame-
        # polling raw-input TUI (e.g. Dwarf Fortress) auto-paces once an agent
        # sets it, instead of every call passing delay_ms. 0 = single-burst (the
        # default, back-compat). Set via POST /mcp/pace and surfaced by
        # /mcp/terminals; the pacing itself is done client-side in the MCP
        # server. EPHEMERAL per-connection like app_cursor above, and unlike
        # mcp_mode/mcp_scope it has no store and is never carried over or
        # re-applied: it resets whenever the window registers again
        # (acceptable for v1; no persistence).
        self.pace_ms = 0
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
        # #238 terminal control. Every field is written ONLY by the
        # BrokerRegistry transition ops (claim / release_by_ws /
        # release_by_gen / force_release) and register(); the relay gate
        # reads controller and revoked per frame. control_gen is the gen of
        # the current view (register() gives every new entry the next value of
        # the registry's seq, so within a broker run it is strictly increasing
        # across a same-id replacement too: clients may order by it). closing
        # is set synchronously once the entry is dying (an `exit` frame,
        # deregister, replacement), so no control op lands on a terminal
        # already known to be dead. revoked holds the sockets that lost the
        # terminal (by POST or another claim): they watch, and the gate drops
        # their input even if their clientId holds the lease, until they claim
        # again.
        # published_view is the last view the ordered publisher finished
        # sending, which is what a socket attaching now is told.
        self.controller: Optional[_Controller] = None
        self.control_gen = 0
        self.closing = False
        self.revoked: Set[Any] = set()
        self.published_view: Dict[str, Any] = protocol.control_view(
            "free", None, None, 0)

    def control_view(self) -> Dict[str, Any]:
        """The LIVE control view (protocol.control_view's shape)."""
        ctl = self.controller
        if ctl is None:
            return protocol.control_view("free", None, None, self.control_gen)
        return protocol.control_view("controlled", ctl.label, ctl.since,
                                     ctl.gen)

    def summary(self, mcp_default: str = "off") -> Dict[str, Any]:
        # ``mcp`` is the EFFECTIVE access mode (per-window override or the
        # broker default), so a /sessions consumer sees what MCP would honor.
        # ``mcp_scope`` is the RAW scope (None = unscoped), never defaulted. It
        # reaches GET /sessions through this dict as is; /mcp/terminals
        # rebuilds its rows field by field, filters on it and publishes it as
        # ``scope``.
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
            "profile": self.profile,
            "version": self.version,
            "pyte": self.pyte,
            "app_cursor": self.app_cursor,
            "pace_ms": self.pace_ms,
            "mcp": self.mcp_mode or mcp_default,
            "mcp_scope": self.mcp_scope,
            # #238: who is driving this terminal from outside, or None. A
            # page that boots or rebuilds mid-control re-enters the state
            # from here.
            "controller": (None if self.controller is None else {
                "label": self.controller.label,
                "since": self.controller.since,
                "gen": self.controller.gen}),
        }

    async def send_to_producer(self, text: str) -> None:
        """Forward a JSON text frame to the producer. Single in-flight per WS."""
        async with self._send_lock:
            try:
                await self.ws.send(text)
            except Exception as exc:
                LOGGER.debug("send_to_producer failed for window %s: %s",
                             self.id, exc)

    async def send_to_producer_if(self, text: str,
                                  may: Callable[[], bool]) -> bool:
        """send_to_producer, but only if ``may()`` still holds once the send
        lock is taken (#238). A frame the relay gate admitted can wait here
        behind a snapshot request or an RPC; a claim or revoke that lands in
        that wait must still stop it. Returns whether it was sent."""
        async with self._send_lock:
            if not may():
                return False
            try:
                await self.ws.send(text)
            except Exception as exc:
                LOGGER.debug("send_to_producer failed for window %s: %s",
                             self.id, exc)
            return True

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
        self.revoked.discard(ws)

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


def _run_on_register(hook: Callable[[WindowEntry, Optional[WindowEntry]], None],
                     entry: WindowEntry, old: Optional[WindowEntry]) -> None:
    """Call an on_register hook under register()'s contract: an Exception, a
    returned coroutine or any other returned awaitable is logged with the
    window id, never raised and never awaited. A BaseException (e.g.
    KeyboardInterrupt, CancelledError) deliberately propagates."""
    try:
        result = hook(entry, old)
        if inspect.iscoroutine(result):
            # An ``async def`` hook handed back a coroutine without running
            # its body. Say so, then close it (no "never awaited" warning):
            # dropped silently it would pass for a hook with nothing to apply.
            # Log FIRST: a coroutine the hook itself started can raise from
            # close(), and the "must be synchronous" diagnostic must survive
            # that; the Exception is then logged below and not re-raised.
            LOGGER.error("on_register hook for window %s returned a coroutine;"
                         " it must be synchronous, so the registry is closing"
                         " it unawaited", entry.id)
            result.close()
        elif inspect.isawaitable(result):
            # A Task/Future/other awaitable: not ours to close or cancel (a
            # Task is already scheduled), so only say it is ignored.
            LOGGER.error("on_register hook for window %s returned an awaitable;"
                         " it must be synchronous, and the registry never"
                         " awaits it", entry.id)
    except Exception:
        LOGGER.exception("on_register hook failed for window %s; "
                         "registering it anyway", entry.id)


def _producer_key(producer: Any):
    """``(host, pid)`` off a WindowEntry-like object or a mapping (a store row
    is a dict), so one gate serves both shapes."""
    if isinstance(producer, Mapping):
        return producer.get("host"), producer.get("pid")
    return producer.host, producer.pid


def same_producer(a: Any, b: Any) -> bool:
    """Whether ``a`` and ``b`` report the same producer: the same host and the
    same known pid. The ONE gate behind every MCP carry-over and re-apply:
    register()'s no-hook carry-over below, and the per-window store's row
    gate, fallback carry-over and prune liveness test (mcp_windows.py),
    which call it as ``registry.same_producer`` so none of them can drift
    from the others.

    Each side is a WindowEntry-like object (``.host``/``.pid``) or a mapping
    with "host"/"pid" keys. The host match is byte-exact (register() has
    already stripped it; no case folding). A pid of 0 or None is unknown and
    never matches, not even another unknown pid. A collision guard, not
    security: both halves are self-reported."""
    a_host, a_pid = _producer_key(a)
    b_host, b_pid = _producer_key(b)
    return (a_pid is not None and a_pid != 0 and a_pid == b_pid
            and a_host == b_host)


class BrokerRegistry:
    """Process-wide map id -> WindowEntry, plus launch waiters."""

    def __init__(self) -> None:
        self._entries: Dict[int, WindowEntry] = {}
        self._lock = asyncio.Lock()
        self._waiters: Dict[int, asyncio.Event] = {}
        # The broker-installed re-apply hook for per-window facts; per-registry
        # state beside _lock and _waiters. None = no hook. Contract: register().
        self.on_register: Optional[
            Callable[[WindowEntry, Optional[WindowEntry]], None]] = None
        # #238 terminal control. ONE broker-global gen sequence. Every control
        # change and every newly registered entry takes the next value, so
        # within one broker run gens are strictly increasing per terminal id,
        # across same-id replacement too, with no carry-over: clients may
        # order views by gen. Across a restart it is only seeded from the wall
        # clock in ms (far below 2**53 for JS): that normally keeps it
        # increasing, but a clock step back (or more than one change per ms of
        # the previous run's uptime) can reissue lower gens, so a client
        # re-baselines when the broker restarts.
        self._control_seq = int(time.time() * 1000)
        # The ordered publisher: one asyncio.Lock per terminal ID (not per
        # entry, so an old entry's in-flight publication finishes before its
        # replacement's), counted users so a lock is dropped once its id is
        # deregistered and nobody holds or waits on it, and the retained
        # publication tasks (so a cancelled request or socket task can never
        # cut a committed change's notification short).
        self._control_locks: Dict[int, asyncio.Lock] = {}
        self._control_users: Dict[int, int] = {}
        self._control_tasks: Set["asyncio.Task"] = set()
        # The broker-installed /control push: ``await hook(window_id, view)``
        # after each published change, outside the terminal's publisher
        # section (so a stalled browser never blocks an attach, and two pushes
        # for one terminal may land out of order). None = no hook (tests). An
        # Exception is logged and swallowed.
        self.on_control: Optional[
            Callable[[int, Dict[str, Any]], Awaitable[None]]] = None

    async def register(self, ws, hello: Dict[str, Any]) -> WindowEntry:
        """Build a WindowEntry from ``hello`` and make it visible, replacing any
        stale entry with the same window_id.

        ``on_register``, when installed, is called as ``hook(new_entry, old)``
        INSIDE ``self._lock``, after the new entry is built and BEFORE it is
        inserted, so the broker can re-apply persisted per-window facts before
        anything can observe the entry. ``old`` is the replaced same-id entry,
        or None on a fresh register. The contract:

        * Synchronous and memory-only. It must not await (an ``async def``
          hook's coroutine is closed unawaited and logged; any other awaitable
          it returns is logged and ignored) and must not take any other lock,
          the sidecar store's in particular: the store's durable writes are its
          own separately scheduled job.
        * It runs before ``old.fail_all_rpc`` and before old's subscribers get
          their 1012 close, so ``old`` still has live subscribers and pending
          RPCs: read it, don't drive it.
        * Every sync registry read (get, __contains__, entries,
          session_summaries, live_cwds, is_pending) is lock-free and safe
          inside the hook, and sees ``old`` or nothing, never ``new_entry``.
          register/deregister must not be called: scheduling one with
          create_task from the hook is a bug, as it runs after the lock and can
          delete the entry just inserted. The sync add_waiter/remove_waiter
          must not be touched either (a pending launcher spawn is parked on its
          waiter).
        * Any Exception raised by the hook, or by closing a coroutine it
          returned, is logged with the window id and swallowed, and the entry
          is inserted as the hook left it (a partial apply is not rolled
          back). A BaseException (KeyboardInterrupt, CancelledError, ...) is
          deliberately not caught and escapes before the insertion.

        With NO hook installed, a same-id replacement carries ``mcp_mode`` and
        ``mcp_scope`` over from ``old`` when both hellos report the same host
        and the same nonzero pid (``same_producer``), so a producer
        reconnecting over a half-open socket keeps its override (readwrite
        included) instead of falling back to the broker default. The host+pid
        check guards against an accidental id collision; it is not security
        (both are self-reported, and public on /sessions; the producer token is
        the boundary). An agent pinned with ``--window-id`` relaunches under
        the same id with a new shell pid, and two hosts pinning one id are
        different producers even with equal pids: neither may inherit the
        other's access. A hello that omits or blanks ``host`` is recorded as
        the broker's own hostname, so two such producers collide on host: this
        is a collision guard, not an identity. The host match is byte-exact
        after the parse-time strip (no case folding), and a pid of 0 (unknown)
        never matches. The carry-over only reaches that half-open window: a
        clean close or an exit frame deregisters the old entry first, so that
        reconnect is a fresh register and starts at None. With a hook installed
        there is no default carry-over: the hook (the per-window store) is the
        authority, so one that raises before applying anything leaves both at
        None. The broker's hook is ``McpWindowStore.apply`` (mcp_windows.py),
        whose fallback re-applies this same carry-over through
        ``same_producer``.
        """
        window_id = int(hello.get("window_id"))
        pid = int(hello.get("pid", 0))
        title = str(hello.get("title", ""))
        cols = int(hello.get("cols", 80))
        rows = int(hello.get("rows", 24))
        host = str(hello.get("host", "") or "").strip() or socket.gethostname()
        kind = str(hello.get("kind", "") or "").strip() or "terminal"
        agent = _whitelist_agent(hello.get("agent"))
        cwd = str(hello.get("cwd", "") or "")
        # #115: the launch-profile name (self-reported, untrusted — a hint, not
        # an attestation). Cap length like version; the UI only uses it as a key
        # into that host's /profiles color map, so a junk value just misses.
        profile = str(hello.get("profile", "") or "")[:64]
        # Self-reported + untrusted: cap length (a producer can send anything,
        # incl. a value matching the broker — it is a hint, not an attestation).
        version = str(hello.get("version", "") or "")[:64]
        # #134: whether the agent has pyte. Absent -> True (a pre-#134 agent
        # predates the signal; assuming pyte-less would raise a false 'degraded'
        # alarm). An explicit False means read_screen uses the textgrid fallback.
        pyte = bool(hello.get("pyte", True))

        entry = WindowEntry(window_id, pid, title, cols, rows, ws,
                            host=host, kind=kind, agent=agent, cwd=cwd,
                            profile=profile, version=version, pyte=pyte)
        async with self._lock:
            old = self._entries.get(window_id)
            if old is not None:
                # Stale entry from a dropped connection — replace.
                LOGGER.info("replacing stale entry for window %s", window_id)
            hook = self.on_register
            if hook is not None:
                _run_on_register(hook, entry, old)
            elif old is not None and same_producer(old, entry):
                # No hook: keep the MCP facts across the replacement (see the
                # docstring for the host+pid gate and its reach).
                entry.mcp_mode = old.mcp_mode
                entry.mcp_scope = old.mcp_scope
            if old is not None:
                # #238: a replaced producer's terminal is dead, and its
                # controller (closed below with 1012 like every subscriber)
                # loses it HERE, before the new entry is visible, so no claim
                # on the new entry can be overtaken by the old one's release.
                old.closing = True
                self.force_release(old)
            # The new entry starts free at the NEXT gen: above any gen this id
            # has shown, so a client ordering views by gen accepts it.
            entry.control_gen = self._next_gen()
            entry.published_view = entry.control_view()
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
                # #238: the terminal exited or its producer dropped; either
                # way its controller (if any) loses it now.
                current.closing = True
                self.force_release(current)
                self._prune_control_lock(window_id)

    def get(self, window_id: int) -> Optional[WindowEntry]:
        return self._entries.get(int(window_id))

    def __contains__(self, window_id: int) -> bool:
        return int(window_id) in self._entries

    def entries(self) -> List[WindowEntry]:
        """A snapshot list of the live entries (a new list; the entries are
        the live objects). Lock-free and sync like get()/session_summaries(),
        so it is safe inside an on_register hook, where it sees the replaced
        entry or nothing, never the one being registered."""
        return list(self._entries.values())

    def session_summaries(self, mcp_default: str = "off") -> List[Dict[str, Any]]:
        return [e.summary(mcp_default) for e in self._entries.values()]

    def live_cwds(self) -> List[str]:
        """Non-empty working dirs of all registered windows. Backs the editor's
        agent-doc scoping (#16): AGENTS.md/CLAUDE.md are editable at a terminal's
        own cwd even when it lies outside editor_root. Sync + no ``await``, so it
        runs non-interleaved on the event loop (registry mutations are all on the
        loop too); it is not otherwise thread-safe."""
        return [e.cwd for e in self._entries.values() if e.cwd]

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

    # -- terminal control (#238) --------------------------------------------
    #
    # Four SYNC transition ops commit a change in one step (nothing awaits
    # between the check and the set, so the relay gate sees the new state at
    # once) and enqueue its publication. Each returns None on success or the
    # refusal reason the caller reports. The publication of every change runs
    # in a registry-owned task, serialized FIFO per terminal id: the revoked
    # frame (if any) to the controller that lost it, then that change's OWN
    # view to every subscriber (each send bounded by CONTROL_SEND_TIMEOUT). So
    # every /ws socket gets every change, in gen order, and rests on the live
    # view. The on_control hook (the /control fan-out) runs after, outside
    # that serialization: its pushes are ordered by gen, not by arrival.

    def is_live(self, entry: WindowEntry) -> bool:
        """Whether ``entry`` is the registered entry for its id and not dying.
        Every control op checks it first."""
        return self._entries.get(entry.id) is entry and not entry.closing

    def _next_gen(self) -> int:
        self._control_seq += 1
        return self._control_seq

    def claim(self, entry: WindowEntry, ws, label: Any,
              now_ms: Optional[int] = None) -> Optional[str]:
        """``ws`` claims ``entry``. A claim while another socket holds it takes
        over (that socket is revoked ``by: claim``); a re-claim from the
        holder itself is a relabel at a new gen. Refusals: ``unknown_session``
        (not live), ``bad_label``."""
        if not self.is_live(entry):
            return "unknown_session"
        if not protocol.control_label_ok(label):
            return "bad_label"
        since = int(time.time() * 1000) if now_ms is None else int(now_ms)
        prev = entry.controller
        gen = self._next_gen()
        entry.controller = _Controller(ws, label, since, gen)
        entry.control_gen = gen
        entry.revoked.discard(ws)
        revoked_ws = None
        if prev is not None and prev.ws is not ws:
            revoked_ws = prev.ws
            entry.revoked.add(revoked_ws)
        self._enqueue(entry, revoked_ws, "claim")
        return None

    def release_by_ws(self, entry: WindowEntry, ws) -> Optional[str]:
        """The controller's own release (its ``release`` frame, or its socket
        closing). Refusals: ``unknown_session``, ``not_controller``."""
        if not self.is_live(entry):
            return "unknown_session"
        ctl = entry.controller
        if ctl is None or ctl.ws is not ws:
            return "not_controller"
        self._release(entry)
        return None

    def release_by_gen(self, entry: WindowEntry, gen: int) -> Optional[str]:
        """The desktop takes the terminal back (POST /session/control). Only
        the controller at exactly ``gen`` is released, so a double click or a
        crossed claim never releases a newer one; it is revoked ``by:
        browser`` and keeps watching. Refusals: ``unknown_session``,
        ``stale_gen`` (free, or another gen)."""
        if not self.is_live(entry):
            return "unknown_session"
        ctl = entry.controller
        if ctl is None or ctl.gen != gen:
            return "stale_gen"
        entry.revoked.add(ctl.ws)
        self._release(entry, ctl.ws, "browser")
        return None

    def force_release(self, entry: WindowEntry) -> None:
        """Release whatever controls a DYING entry (exit, producer drop,
        replacement); no liveness check, no revoked frame. A no-op when
        nothing controls it."""
        if entry.controller is not None:
            self._release(entry)

    def _release(self, entry: WindowEntry, revoked_ws=None,
                 by: Optional[str] = None) -> None:
        entry.controller = None
        entry.control_gen = self._next_gen()
        self._enqueue(entry, revoked_ws, by)

    def _enqueue(self, entry: WindowEntry, revoked_ws, by) -> None:
        # The view is captured NOW, at commit: each change publishes its own.
        view = entry.control_view()
        task = asyncio.get_running_loop().create_task(
            self._publish(entry, view, revoked_ws, by))
        self._control_tasks.add(task)
        task.add_done_callback(self._control_tasks.discard)

    async def _publish(self, entry: WindowEntry, view: Dict[str, Any],
                       revoked_ws, by) -> None:
        async with self.control_section(entry.id):
            if revoked_ws is not None:
                await self._bounded_send(
                    entry, revoked_ws,
                    protocol.control_revoked_frame(by, view["gen"]))
            # Every subscriber at once, each bounded: a stalled socket holds
            # the section (and so every attach to this terminal) for at most
            # CONTROL_SEND_TIMEOUT, however many there are.
            frame = protocol.control_frame(view)
            await asyncio.gather(*(self._bounded_send(entry, sub, frame)
                                   for sub in list(entry.subscribers)))
            entry.published_view = view
        # The /control fan-out runs OUTSIDE the section, so a stalled browser
        # never blocks an attach. Pushes for one terminal may then land out of
        # order; a client orders them by gen.
        hook = self.on_control
        if hook is not None:
            try:
                await hook(entry.id, view)
            except Exception:
                LOGGER.exception("on_control hook failed for window %s",
                                 entry.id)

    async def _bounded_send(self, entry: WindowEntry, sock,
                            frame: str) -> None:
        # A send that fails or misses CONTROL_SEND_TIMEOUT drops the socket.
        try:
            await asyncio.wait_for(sock.send(frame), CONTROL_SEND_TIMEOUT)
        except Exception:
            self._drop_stalled(entry, sock)

    def _drop_stalled(self, entry: WindowEntry, sock) -> None:
        """A subscriber that could not take a control frame in time: remove
        it, and CLOSE it (1012, like the replacement sweep) so the page
        reattaches instead of sitting open and starved. If it was the
        controller, release now: a stalled peer's close may never complete, so
        the relay's on-close release cannot be relied on to come."""
        entry.remove_subscriber(sock)
        ctl = entry.controller
        if ctl is not None and ctl.ws is sock:
            self._release(entry)
        self.close_soon(sock, 1012, "control send timed out")

    def close_soon(self, sock, code: int, reason: str) -> None:
        """Close ``sock`` in a retained task, bounded by CONTROL_SEND_TIMEOUT,
        so a caller holding a publisher section never waits on a stalled
        peer's close handshake."""
        task = asyncio.get_running_loop().create_task(
            self._close_bounded(sock, code, reason))
        self._control_tasks.add(task)
        task.add_done_callback(self._control_tasks.discard)

    @staticmethod
    async def _close_bounded(sock, code: int, reason: str) -> None:
        try:
            await asyncio.wait_for(sock.close(code=code, reason=reason),
                                   CONTROL_SEND_TIMEOUT)
        except Exception as exc:
            LOGGER.debug("closing a stalled control peer failed: %r", exc)

    @contextlib.asynccontextmanager
    async def control_section(self, window_id: int):
        """Hold terminal ``window_id``'s publisher lock: the publication of
        each change runs in one, and so does a /ws attach (subscribe, then
        ``resized``, then the published view), so an attach never interleaves
        with a publication."""
        wid = int(window_id)
        lock = self._control_locks.get(wid)
        if lock is None:
            lock = self._control_locks[wid] = asyncio.Lock()
        self._control_users[wid] = self._control_users.get(wid, 0) + 1
        try:
            async with lock:
                yield
        finally:
            left = self._control_users[wid] - 1
            if left:
                self._control_users[wid] = left
            else:
                del self._control_users[wid]
                self._prune_control_lock(wid)

    def _prune_control_lock(self, window_id: int) -> None:
        # Drop an id's lock once it is deregistered and nobody holds or waits
        # on it, so the map is bounded by the live ids.
        wid = int(window_id)
        if wid not in self._entries and wid not in self._control_users:
            self._control_locks.pop(wid, None)

    async def drain_control(self) -> None:
        """Wait until every enqueued publication has run (tests, shutdown)."""
        while self._control_tasks:
            await asyncio.gather(*list(self._control_tasks),
                                 return_exceptions=True)

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
            elif mtype == "mode":
                # DECCKM cache (#23): consumed only by send_keys via /mcp/
                # terminals. Browsers track their own DECCKM from the byte
                # stream, so there's nothing to broadcast.
                entry.app_cursor = bool(data.get("app_cursor", entry.app_cursor))
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
                # #238: dying from here on, BEFORE the first await, so no
                # control op lands on it during the broadcast.
                entry.closing = True
                await entry.broadcast_text(protocol.exit_frame(_code(data)))
                await registry.deregister(entry.id, entry)
                break
            elif mtype in ("procs", "killed", "git_status", "screen_text",
                           "reset_done", "flush_input_done"):
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
