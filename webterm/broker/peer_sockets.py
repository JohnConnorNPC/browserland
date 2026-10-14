"""The peer proxy's WebSocket half (#252 phase 2): tickets and socket pools.

A page that reaches a peer (B) only through this broker (A) cannot open a
WebSocket with headers, so it cannot carry B's token the way a proxied HTTP
request does. Instead it asks A for a one-time **ticket** over the browser
gate (A's bearer, B's token in :data:`peers.PEER_TOKEN_HEADER`, the pairing
header), then opens ``/peer/<id>/ws`` or ``/peer/<id>/control`` with only
``?ticket=``. A redeems the ticket once, dials B with B's token, and relays
messages unchanged.

A ticket is bound to the peer, the route, the clientId, the peer's origin
and this broker's token AT MINT TIME, lives :data:`PEER_TICKET_TTL_S`, and is
burned by its first presentation whatever the outcome. The store holds B's
token only inside an unredeemed ticket, and A's token never (only a digest,
:func:`token_id`).
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import secrets
import time
import urllib.parse
from typing import Dict, Optional

from . import peers

#: How long a minted ticket may wait for its socket. The page dials right
#: after the mint, so this only has to cover one round trip on a slow phone.
PEER_TICKET_TTL_S = 30.0

#: Unredeemed tickets, per peer and in total. Over either: 503 ``peer_busy``
#: at the mint.
PEER_MAX_TICKETS_PER_PEER = 16
PEER_MAX_TICKETS = 64

#: Relayed sockets, per peer and in total -- a pool of their own, separate
#: from the HTTP dial pools (a socket lives for hours, a dial for seconds).
#: A ticket holds its slot from the mint, so an upgrade never fails for the
#: pool: the refusal is the mint's 503 ``peer_busy``.
PEER_MAX_SOCKETS_PER_PEER = 32
PEER_MAX_SOCKETS = 128

#: A relayed socket with no frame in either direction for this long is
#: closed (1000 ``idle``). Protocol pings are not frames.
PEER_WS_IDLE_S = 3600.0

#: The largest frame any socket of this broker takes, its own ``/ws`` and
#: ``/control`` included (``create_app`` sets ``WEBSOCKET_MAX_SIZE`` from it).
#: Browser pastes arrive as one input/paste frame and producer snapshots
#: scale with --ring-bytes; Sanic's default WEBSOCKET_MAX_SIZE (1 MiB) kills
#: either socket with a 1009 close and the bytes silently vanish (Linux
#: verification finding F2). 16 MiB bounds memory while clearing any
#: realistic paste or snapshot; the UI additionally chunks its sends.
PEER_WS_FRAME_MAX = 16 * 2**20

ROUTES = ("ws", "control")

#: The longest clientId a ticket binds (the page's is a 36-character UUID).
PEER_CLIENT_ID_MAX = 128


def cid8(cid: Optional[str]) -> str:
    """#248 A6: log lines name a client by an 8-char prefix of its clientId,
    never the whole id, and never a raw control character (a clientId is
    client-chosen: no forged log lines)."""
    return "".join(ch if ch.isprintable() else "?"
                   for ch in (cid or "-")[:8])


def token_id(token: Optional[str]) -> str:
    """What a ticket remembers of this broker's token: enough to see that
    the token changed, never the token itself (its SHA-256, in hex)."""
    return hashlib.sha256((token or "").encode("utf-8")).hexdigest()


class Ticket:
    """One unredeemed ticket: its binding, B's token for the dial, and its
    deadline. The id is the store's key, not a field."""

    __slots__ = ("peer_id", "route", "client_id", "origin", "token_id",
                 "peer_token", "expires_at")

    def __init__(self, peer_id: str, route: str, client_id: str, origin: str,
                 token_id: str, peer_token: str, expires_at: float) -> None:
        self.peer_id = peer_id
        self.route = route
        self.client_id = client_id
        self.origin = origin
        self.token_id = token_id
        self.peer_token = peer_token
        self.expires_at = expires_at

    def __repr__(self) -> str:
        return "<Ticket peer=%s route=%s cid=%s>" % (
            self.peer_id, self.route, cid8(self.client_id))


class TicketStore:
    """Unredeemed tickets. One event loop, so plain dicts are enough.

    ``sock_cap`` is the socket pool (a :class:`peers.DialCap` sized
    :data:`PEER_MAX_SOCKETS` / :data:`PEER_MAX_SOCKETS_PER_PEER`): a mint
    takes a slot, a refused or expired or voided ticket gives it back, and a
    redeemed one hands it to its socket, whose handler releases it."""

    def __init__(self, sock_cap: peers.DialCap, clock=time.monotonic) -> None:
        self.sock_cap = sock_cap
        self._clock = clock
        self._tickets: Dict[str, Ticket] = {}

    def __len__(self) -> int:
        return len(self._tickets)

    def _drop(self, ticket_id: str) -> None:
        t = self._tickets.pop(ticket_id, None)
        if t is not None:
            self.sock_cap.release(t.peer_id)

    def sweep_expired(self) -> None:
        now = self._clock()
        for tid in [k for k, t in self._tickets.items()
                    if t.expires_at <= now]:
            self._drop(tid)

    def mint(self, peer_id: str, route: str, client_id: str, origin: str,
             peer_token: str, token_id: str) -> str:
        """A new ticket id, or :class:`peers.PolicyError` 503 ``peer_busy``
        when a ticket cap or the socket pool is full."""
        self.sweep_expired()
        if len(self._tickets) >= PEER_MAX_TICKETS:
            raise peers.PolicyError(503, "peer_busy")
        mine = sum(1 for t in self._tickets.values() if t.peer_id == peer_id)
        if mine >= PEER_MAX_TICKETS_PER_PEER:
            raise peers.PolicyError(503, "peer_busy")
        if not self.sock_cap.acquire(peer_id):
            raise peers.PolicyError(503, "peer_busy")
        tid = secrets.token_urlsafe(32)
        while tid in self._tickets:          # never overwrite a held slot
            tid = secrets.token_urlsafe(32)
        self._tickets[tid] = Ticket(
            peer_id, route, client_id, origin, token_id, peer_token,
            self._clock() + PEER_TICKET_TTL_S)
        # Expiry is not left to the next mint: an unredeemed ticket (and
        # the peer token in it) is dropped, and its slot released, just
        # after its deadline even if nothing else happens.
        try:
            asyncio.get_running_loop().call_later(
                PEER_TICKET_TTL_S + 0.05, self.sweep_expired)
        except RuntimeError:                 # no loop: a synchronous caller
            pass
        return tid

    def redeem(self, ticket_id: Optional[str], *, peer_id: str, route: str,
               client_id: Optional[str], origin: Optional[str],
               token_id: str) -> Optional[Ticket]:
        """The ticket, once. Popped FIRST, so a presentation with the wrong
        peer, route, clientId, a changed origin or a changed token burns it
        as surely as a good one. ``None`` on any refusal (the slot goes
        back); on success the caller owns the slot."""
        t = self._tickets.pop(ticket_id or "", None)
        if t is None:
            return None
        if (t.expires_at <= self._clock()
                or t.peer_id != peer_id or t.route != route
                or t.client_id != client_id or t.origin != origin
                or t.token_id != token_id):
            self.sock_cap.release(t.peer_id)
            return None
        return t

    def void(self, peer_id: Optional[str] = None) -> None:
        """Drop the unredeemed tickets of one peer, or of every peer."""
        for tid in [k for k, t in self._tickets.items()
                    if peer_id is None or t.peer_id == peer_id]:
            self._drop(tid)

    def sweep_bindings(self, peer_map, live_token_id: str) -> None:
        """Drop every ticket whose peer is gone, whose origin is no longer
        the configured one, or that was minted under another token of this
        broker's."""
        for tid in [k for k, t in self._tickets.items()
                    if (peer_map.get(t.peer_id) or {}).get("origin")
                    != t.origin or t.token_id != live_token_id]:
            self._drop(tid)


# ---- the upstream leg ---------------------------------------------------------

def upstream_ws_url(origin: str, route: str, raw_qs: str) -> str:
    """The peer's own ``/ws`` or ``/control`` for ``origin``, with the
    client's query minus ``ticket``, ``token`` and ``auth``. No credential
    ever rides the URI: the peer's token goes in the handshake's
    ``Authorization`` header (:func:`dial_upstream`)."""
    scheme, rest = origin.split(":", 1)
    query = urllib.parse.urlencode(
        [(k, v) for k, v in urllib.parse.parse_qsl(
            peers.upstream_query(raw_qs), keep_blank_values=True)
         if k != "ticket"])
    return (("wss" if scheme == "https" else "ws") + ":" + rest + "/"
            + route + ("?" + query if query else ""))


_PEER_WS_CONNECT = []


def peer_ws_connect():
    """``PeerWsConnect``: websockets' ``connect`` that never follows a
    redirect. The stock one follows up to ten, cross-origin included, and
    sends ``additional_headers`` -- the peer's ``Authorization`` -- on every
    hop; returning the exception makes a 3xx a failed dial.

    Built on first use: the asyncio client and ``connect(proxy=)`` need
    websockets 15 (the ``proxy`` extra), while core needs only 12, so
    nothing here imports websockets at module level
    (:func:`peers.proxy_available` checks the version before any route
    lets a socket through)."""
    if not _PEER_WS_CONNECT:
        from websockets.asyncio.client import connect

        class PeerWsConnect(connect):
            def process_redirect(self, exc):
                return exc

        _PEER_WS_CONNECT.append(PeerWsConnect)
    return _PEER_WS_CONNECT[0]


#: Every keyword of the upstream handshake, in one place. No environment
#: proxy (the stock default reads ``HTTPS_PROXY``/``WSS_PROXY``), no
#: User-Agent, no compression (messages cross unchanged, and a deflate context
#: per socket is memory the relay has no use for), the broker's own frame
#: cap, and connect and close deadlines below the page's 3 s.
#: ``max_queue=2``: at most two frames buffered from the peer (the default
#: is 16); its low-water mark is ``2 // 4 = 0``, so reading resumes only once
#: the buffer is empty -- a slow page slows the peer instead of filling this
#: broker's memory. The peer is pinged every 20 s and dropped after 20 s
#: without a pong; a ping is not a frame for the idle ceiling.
UPSTREAM_WS_KWARGS = {
    "proxy": None,
    "user_agent_header": None,
    "compression": None,
    "max_size": PEER_WS_FRAME_MAX,
    "max_queue": 2,
    "open_timeout": peers.PEER_CONNECT_TIMEOUT_S,
    "close_timeout": peers.PEER_CONNECT_TIMEOUT_S,
    "ping_interval": 20,
    "ping_timeout": 20,
}


async def dial_upstream(url: str, peer_token: str):
    """One upstream socket; raises when the dial fails. Awaited once:
    ``async for`` over a ``connect`` would reconnect on its own."""
    # websockets logs the request line and headers at DEBUG -- the URI and
    # the peer's Authorization. The broker's own lines name a peer id.
    logging.getLogger("websockets").setLevel(logging.WARNING)
    PeerWsConnect = peer_ws_connect()
    return await PeerWsConnect(
        url, additional_headers=[("Authorization", "Bearer " + peer_token)],
        **UPSTREAM_WS_KWARGS)


# ---- the relay ----------------------------------------------------------------

def _sendable(code: Optional[int], reason: str):
    """``(code, reason)`` when a close frame may carry them, else 1012
    (the peer vanished, or sent a code nobody may send on)."""
    from websockets.exceptions import ProtocolError
    from websockets.frames import Close
    if code is None:
        return 1012, ""
    try:
        Close(code, reason).check()
    except ProtocolError:
        return 1012, ""
    return code, reason


async def quiet_close(ws, code: int, reason: str = "") -> None:
    """Close ``ws``, bounded; a failure is not news (the socket is going
    either way). A cancellation of the caller is NOT swallowed."""
    try:
        await asyncio.wait_for(ws.close(code, reason),
                               peers.PEER_CONNECT_TIMEOUT_S)
    except Exception:  # noqa: BLE001 -- includes the timeout
        pass


_DETACHED: set = set()


def _detached(coro):
    """A task the loop keeps a strong reference to until it is done."""
    task = asyncio.ensure_future(coro)
    _DETACHED.add(task)
    task.add_done_callback(_DETACHED.discard)
    return task


async def drain_closes(timeout: float = 2 * peers.PEER_CONNECT_TIMEOUT_S):
    """Wait (bounded) for the closes a stopping server's cancelled
    handlers left running, so the peer hears 1001 before the loop goes."""
    loop = asyncio.get_running_loop()
    mine = [t for t in _DETACHED if t.get_loop() is loop]
    if mine:
        await asyncio.wait(mine, timeout=timeout)


async def _cancel(tasks) -> None:
    """Cancel ``tasks`` and wait for them, bounded (a pump stuck in a send
    must not hold the closes behind it)."""
    for t in tasks:
        t.cancel()
    await asyncio.wait(tasks, timeout=peers.PEER_CONNECT_TIMEOUT_S)


async def relay_sockets(down, up, *, peer_id: str, route: str,
                        idle_s: Optional[float] = None, stop=None,
                        stopping=lambda: False, log) -> str:
    """Relay messages between the page's socket ``down`` (Sanic's) and the
    peer's ``up`` until either ends, unchanged both ways: a text message
    stays text, a binary one binary, nothing is parsed. (Each leg keeps its
    own framing and its own pings.) Returns why it ended: ``page``,
    ``shutdown``, ``peer``, ``idle``, ``revoked`` or ``error``.

    * The page went (its close, a dropped connection, or a close Sanic
      started, e.g. 1009 for an oversized message): the peer gets 1000.
    * ``stopping()`` is true (this broker is stopping -- the caller's
      explicit signal, never guessed from the close order): both get 1001.
    * The peer closed: the page gets the peer's code and reason when a
      close frame may carry them, else 1012 (no close frame at all, or a
      code like 1005 or 2000).
    * No message either way for ``idle_s`` (:data:`PEER_WS_IDLE_S`): both
      get 1000 ``idle``.
    * ``stop`` (a future, :class:`Pair`) resolved with ``(why, page code,
      page reason, peer code)``: the peer's leg is closed first, then the
      page's, each with its own code (revoked: 1000, then 4401).
    * Anything else: both 1011, one log line naming the exception class.

    Each direction awaits its send before reading again, so a slow reader
    slows the writer (Sanic's ``send`` waits for the transport to drain;
    the upstream buffer is ``max_queue`` messages). Nothing reconnects.

    Sanic cancels this coroutine when the page's connection is lost and
    when the server stops, possibly more than once. Every close therefore
    runs in ONE shielded, strongly referenced task (drained at server stop
    by :func:`drain_closes`): a cancellation still propagates to the
    caller, but can never abandon a close half-way."""
    from websockets.protocol import State
    loop = asyncio.get_running_loop()
    idle_s = PEER_WS_IDLE_S if idle_s is None else idle_s
    last = [loop.time()]

    async def down_to_up():
        while True:
            # Sanic's recv returns None only on a timeout (none is set); a
            # close RAISES (CancelledError mid-wait, WebsocketClosed after).
            frame = await down.recv()
            if frame is None:
                continue
            last[0] = loop.time()
            await up.send(frame)

    async def up_to_down():
        async for frame in up:
            last[0] = loop.time()
            await down.send(frame)

    async def watchdog():
        while True:
            left = last[0] + idle_s - loop.time()
            if left <= 0:
                return
            await asyncio.sleep(left)

    tasks = [loop.create_task(down_to_up()), loop.create_task(up_to_down()),
             loop.create_task(watchdog())]
    waits = tasks + ([stop] if stop is not None else [])
    cancelled = False
    try:
        done, _ = await asyncio.wait(waits,
                                     return_when=asyncio.FIRST_COMPLETED)
    except asyncio.CancelledError:
        cancelled, done = True, set()

    async def teardown() -> str:
        await _cancel(tasks)
        if cancelled:
            # The page's connection is gone, or the server is stopping
            # (then Sanic would close the page 1000 itself: say 1001).
            if stopping():
                await quiet_close(up, 1001)
                await quiet_close(down, 1001)
                return "shutdown"
            await quiet_close(up, 1000)
            return "page"
        if tasks[2] in done:
            await quiet_close(down, 1000, "idle")
            await quiet_close(up, 1000)
            return "idle"
        if stop is not None and stop in done and not stop.cancelled():
            # Explicit codes, B's leg first.
            why, down_code, reason, up_code = stop.result()
            await quiet_close(up, up_code)
            await quiet_close(down, down_code, reason)
            return why
        if up.state in (State.CLOSING, State.CLOSED):
            # The peer's own close frame, read as soon as it arrived
            # (``close_code`` stays None until the TCP connection is gone).
            rcvd = up.protocol.close_rcvd
            code, reason = _sendable(rcvd.code if rcvd else None,
                                     rcvd.reason if rcvd else "")
            await quiet_close(down, code, reason)
            return "peer"
        if down.ws_proto.state in (State.CLOSING, State.CLOSED):
            if stopping():
                await quiet_close(up, 1001)
                return "shutdown"
            await quiet_close(up, 1000)
            return "page"
        exc = next((t.exception() for t in tasks
                    if t.done() and not t.cancelled() and t.exception()),
                   None)
        log("peer %s: %s socket ended (%s)", peer_id, route,
            type(exc).__name__ if exc is not None else "no error")
        await quiet_close(down, 1011)
        await quiet_close(up, 1011)
        return "error"

    why = await asyncio.shield(_detached(teardown()))
    if cancelled:
        raise asyncio.CancelledError()
    return why


# ---- the registry -------------------------------------------------------------

class Pair:
    """One relayed socket pair as the registry sees it: its binding, and the
    future that stops its relay (:func:`relay_sockets` ``stop``)."""

    __slots__ = ("peer_id", "client_id", "route", "origin", "token_id",
                 "stop")

    def __init__(self, peer_id: str, client_id: Optional[str], route: str,
                 origin: str, token_id: str) -> None:
        self.peer_id = peer_id
        self.client_id = client_id
        self.route = route
        self.origin = origin
        self.token_id = token_id
        #: Resolved with (why, page code, page reason, peer code).
        self.stop = asyncio.get_running_loop().create_future()

    def revoke(self, reason: str = "revoked") -> None:
        """Close the peer's leg 1000, then the page's 4401 ``reason``."""
        if not self.stop.done():
            self.stop.set_result(("revoked", 4401, reason, 1000))

    def __repr__(self) -> str:
        return "<Pair peer=%s route=%s cid=%s>" % (
            self.peer_id, self.route, cid8(self.client_id))


class SocketRegistry:
    """The live relayed pairs, by peer and by (peer, clientId) -- the
    ``control_clients`` shape -- and the tickets that would become pairs.
    Revocation covers both: a revoked peer's pairs close and its unredeemed
    tickets are voided."""

    def __init__(self, store: TicketStore) -> None:
        self.store = store
        self.by_peer: Dict[str, set] = {}
        self.by_client: Dict[tuple, set] = {}

    def __len__(self) -> int:
        return sum(len(v) for v in self.by_peer.values())

    def add(self, pair: Pair) -> None:
        self.by_peer.setdefault(pair.peer_id, set()).add(pair)
        self.by_client.setdefault((pair.peer_id, pair.client_id),
                                  set()).add(pair)

    def remove(self, pair: Pair) -> None:
        for table, key in ((self.by_peer, pair.peer_id),
                           (self.by_client, (pair.peer_id, pair.client_id))):
            group = table.get(key)
            if group is not None:
                group.discard(pair)
                if not group:
                    del table[key]

    def pairs(self, peer_id: Optional[str] = None) -> list:
        if peer_id is not None:
            return list(self.by_peer.get(peer_id, ()))
        return [p for group in self.by_peer.values() for p in group]

    def revoke(self, peer_id: Optional[str] = None,
               reason: str = "revoked") -> None:
        """Close one peer's pairs (or every pair) and void its tickets."""
        for pair in self.pairs(peer_id):
            pair.revoke(reason)
        self.store.void(peer_id)

    def sweep(self, peer_map, live_token_id: str) -> None:
        """Close every pair, and void every ticket, whose peer is gone, whose
        origin is no longer the configured one, or that was opened under
        another token of this broker's. Run on every mint and redemption,
        and by :func:`peers.apply_change`."""
        for pair in self.pairs():
            if ((peer_map.get(pair.peer_id) or {}).get("origin")
                    != pair.origin or pair.token_id != live_token_id):
                pair.revoke()
        self.store.sweep_bindings(peer_map, live_token_id)
