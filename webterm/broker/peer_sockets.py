"""The peer proxy's WebSocket half (#252 phase 2): tickets and socket pools.

A page that reaches a peer (B) only through this broker (A) cannot open a
WebSocket with headers, so it cannot carry B's token the way a proxied HTTP
request does. Instead it asks A for a one-time **ticket** over the browser
gate (A's bearer, B's token in :data:`peers.PEER_TOKEN_HEADER`, the pairing
header), then opens ``/peer/<id>/ws`` or ``/peer/<id>/control`` with only
``?ticket=``. A redeems the ticket once, dials B with B's token, and relays
frames verbatim.

A ticket is bound to the peer, the route, the clientId, the peer's origin
and this broker's token AT MINT TIME, lives :data:`PEER_TICKET_TTL_S`, and is
burned by its first presentation whatever the outcome. The store holds B's
token only inside an unredeemed ticket, and A's token never (only a digest,
:func:`token_id`).
"""

from __future__ import annotations

import hashlib
import secrets
import time
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


def cid8(cid: Optional[str]) -> str:
    """#248 A6: log lines name a client by an 8-char prefix of its clientId,
    never the whole id, and never a raw control character (a clientId is
    client-chosen: no forged log lines)."""
    return "".join(ch if ch.isprintable() else "?"
                   for ch in (cid or "-")[:8])


def token_id(token: Optional[str]) -> str:
    """What a ticket remembers of this broker's token: enough to see that
    the token changed, never the token itself."""
    return hashlib.sha256((token or "").encode("utf-8")).hexdigest()[:16]


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
        self._tickets[tid] = Ticket(
            peer_id, route, client_id, origin, token_id, peer_token,
            self._clock() + PEER_TICKET_TTL_S)
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
