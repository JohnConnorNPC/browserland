"""#252 phase 2, atom 1: the ticket store and the socket pool, before any route.

``peer_sockets.TicketStore`` with a hand-driven clock: the id, single use
(popped before any check), the TTL, the binding (peer, route, clientId, the
live origin, the live token id), the two ticket caps and the socket pool a
ticket reserves, voiding, and that nothing of B's token survives a dropped
ticket. Plus ``cid8`` (hoisted from ``_control_ws``) and the ruled numbers.
"""

from __future__ import annotations

import pytest

from webterm.broker import peer_sockets as ps
from webterm.broker import peers
from webterm.broker.app import create_app

A_TOKEN = "a-token-AAAA-0123456"
B_TOKEN = "b-token-BBBB-secret"
TID = ps.token_id(A_TOKEN)
ORIGIN_B = "http://127.0.0.1:4448"
ORIGIN_C = "http://127.0.0.1:4449"


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def _store(total=ps.PEER_MAX_SOCKETS, per_peer=ps.PEER_MAX_SOCKETS_PER_PEER):
    clock = Clock()
    cap = peers.DialCap(total, per_peer)
    return ps.TicketStore(cap, clock=clock), cap, clock


def _mint(store, peer="b", route="ws", cid="c1", origin=ORIGIN_B,
          token=B_TOKEN, tid=TID):
    return store.mint(peer, route, cid, origin, token, tid)


def _redeem(store, t, peer="b", route="ws", cid="c1", origin=ORIGIN_B,
            tid=TID):
    return store.redeem(t, peer_id=peer, route=route, client_id=cid,
                        origin=origin, token_id=tid)


def _strings(obj, seen=None):
    """Every string reachable from ``obj`` (dicts, lists, tuples, sets,
    instance dicts and slots)."""
    seen = set() if seen is None else seen
    if id(obj) in seen:
        return
    seen.add(id(obj))
    if isinstance(obj, str):
        yield obj
        return
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield from _strings(k, seen)
            yield from _strings(v, seen)
        return
    if isinstance(obj, (list, tuple, set, frozenset)):
        for v in obj:
            yield from _strings(v, seen)
        return
    if hasattr(obj, "__dict__"):
        yield from _strings(vars(obj), seen)
    for name in getattr(type(obj), "__slots__", ()):
        if hasattr(obj, name):
            yield from _strings(getattr(obj, name), seen)


def test_a_ticket_is_43_random_chars_and_holds_no_token():
    store, _, _ = _store()
    t1, t2 = _mint(store), _mint(store)
    assert len(t1) == 43 and len(t2) == 43
    assert t1 != t2
    for t in (t1, t2):
        assert B_TOKEN not in t and A_TOKEN not in t
    # The store never holds A's token, only its digest.
    assert A_TOKEN not in set(_strings(store))
    assert TID != A_TOKEN and len(TID) == 64       # the full SHA-256


def test_a_ticket_redeems_once():
    store, _, _ = _store()
    t = _mint(store)
    got = _redeem(store, t)
    assert got is not None and got.peer_token == B_TOKEN
    assert got.route == "ws" and got.client_id == "c1"
    assert _redeem(store, t) is None
    assert len(store) == 0


def test_the_ttl_with_a_patched_clock():
    store, _, clock = _store()
    t = _mint(store)
    clock.now += ps.PEER_TICKET_TTL_S - 1
    assert _redeem(store, t) is not None
    t = _mint(store)
    clock.now += ps.PEER_TICKET_TTL_S + 1
    assert _redeem(store, t) is None


@pytest.mark.parametrize("wrong", [
    {"route": "control"}, {"peer": "c"}, {"cid": "c2"}, {"cid": None},
    {"origin": ORIGIN_C}, {"tid": ps.token_id("another-token")},
])
def test_a_wrong_binding_refuses_and_burns_the_ticket(wrong):
    store, cap, _ = _store()
    t = _mint(store)
    assert _redeem(store, t, **wrong) is None
    # Burned: the right presentation after a wrong one fails too.
    assert _redeem(store, t) is None
    assert len(store) == 0
    # ...and the slot went back.
    assert cap._busy == {}


def test_the_ticket_caps_refuse_with_peer_busy():
    store, _, clock = _store()
    for _ in range(ps.PEER_MAX_TICKETS_PER_PEER):
        _mint(store, peer="b")
    with pytest.raises(peers.PolicyError) as exc:
        _mint(store, peer="b")
    assert (exc.value.status, exc.value.error) == (503, "peer_busy")
    # Another peer still mints, up to the global cap.
    for i in range(ps.PEER_MAX_TICKETS - ps.PEER_MAX_TICKETS_PER_PEER):
        _mint(store, peer="p%d" % (i % 8))
    assert len(store) == ps.PEER_MAX_TICKETS
    with pytest.raises(peers.PolicyError) as exc:
        _mint(store, peer="fresh")
    assert (exc.value.status, exc.value.error) == (503, "peer_busy")
    # Expiry gives every slot back.
    clock.now += ps.PEER_TICKET_TTL_S + 1
    assert _mint(store, peer="b")
    assert len(store) == 1


def test_a_ticket_reserves_a_socket_slot():
    # A pool of 2 so the ticket caps stay out of the way.
    store, cap, clock = _store(total=8, per_peer=2)
    t1 = _mint(store)
    _mint(store)
    with pytest.raises(peers.PolicyError) as exc:
        _mint(store)
    assert exc.value.error == "peer_busy"
    # A redeemed ticket keeps its slot (now the socket's).
    assert _redeem(store, t1) is not None
    assert cap._busy == {"b": 2}
    with pytest.raises(peers.PolicyError):
        _mint(store)
    # The socket ends: its handler releases.
    cap.release("b")
    # Expiry of the other ticket returns its slot too.
    clock.now += ps.PEER_TICKET_TTL_S + 1
    store.sweep_expired()
    assert cap._busy == {}
    _mint(store)
    _mint(store)


def test_the_full_socket_pool_refuses_the_mint():
    store, cap, _ = _store()
    for i in range(ps.PEER_MAX_SOCKETS_PER_PEER):
        assert cap.acquire("b")    # live sockets
    with pytest.raises(peers.PolicyError) as exc:
        _mint(store)
    assert (exc.value.status, exc.value.error) == (503, "peer_busy")
    assert len(store) == 0


def test_void_and_sweep_bindings():
    store, cap, _ = _store()
    tb = _mint(store, peer="b")
    tc = _mint(store, peer="c", origin=ORIGIN_C)
    store.void("b")
    assert _redeem(store, tb) is None
    assert _redeem(store, tc, peer="c", origin=ORIGIN_C) is not None
    cap.release("c")
    _mint(store, peer="b")
    _mint(store, peer="c", origin=ORIGIN_C)
    store.void()
    assert len(store) == 0 and cap._busy == {}

    # sweep_bindings: b re-pointed -> b's go, c's stay.
    tb = _mint(store, peer="b")
    tc = _mint(store, peer="c", origin=ORIGIN_C)
    live = {"b": {"origin": "http://10.0.0.9:4445"},
            "c": {"origin": ORIGIN_C}}
    store.sweep_bindings(live, TID)
    assert len(store) == 1
    assert _redeem(store, tc, peer="c", origin=ORIGIN_C) is not None
    cap.release("c")
    # A peer gone from the config: its tickets go.
    _mint(store, peer="b")
    store.sweep_bindings({"c": {"origin": ORIGIN_C}}, TID)
    assert len(store) == 0
    # A new token of this broker's voids every ticket.
    _mint(store, peer="b")
    _mint(store, peer="c", origin=ORIGIN_C)
    store.sweep_bindings({"b": {"origin": ORIGIN_B},
                          "c": {"origin": ORIGIN_C}}, ps.token_id("new"))
    assert len(store) == 0 and cap._busy == {}


def test_nothing_of_b_token_survives_a_dropped_ticket():
    store, _, clock = _store()
    _mint(store)
    assert B_TOKEN in set(_strings(store))   # the walker does see it
    store.void()
    assert B_TOKEN not in set(_strings(store))
    _mint(store)
    clock.now += ps.PEER_TICKET_TTL_S + 1
    store.sweep_expired()
    assert B_TOKEN not in set(_strings(store))
    t = _mint(store)
    assert _redeem(store, t, cid="other") is None
    assert B_TOKEN not in set(_strings(store))


def test_an_id_collision_never_overwrites_a_held_slot(monkeypatch):
    store, cap, _ = _store()
    ids = iter(["same", "same", "other"])
    monkeypatch.setattr(ps.secrets, "token_urlsafe", lambda n: next(ids))
    assert _mint(store) == "same"
    assert _mint(store) == "other"
    assert len(store) == 2 and cap._busy == {"b": 2}


def test_a_ticket_repr_names_no_secret():
    store, _, _ = _store()
    t = _mint(store, cid="client-id-0123456789")
    got = _redeem(store, t, cid="client-id-0123456789")
    text = repr(got)
    assert text == "<Ticket peer=b route=ws cid=client-i>"
    assert B_TOKEN not in text and ORIGIN_B not in text


@pytest.mark.parametrize("cid, want", [
    ("abcdefghij", "abcdefgh"),
    ("ab\x1b\ncd", "ab??cd"),
    (None, "-"),
    ("", "-"),
])
def test_cid8(cid, want):
    assert ps.cid8(cid) == want


def test_the_ruled_numbers(tmp_path):
    assert ps.PEER_TICKET_TTL_S == 30.0
    assert (ps.PEER_MAX_TICKETS_PER_PEER, ps.PEER_MAX_TICKETS) == (16, 64)
    assert (ps.PEER_MAX_SOCKETS_PER_PEER, ps.PEER_MAX_SOCKETS) == (32, 128)
    assert ps.PEER_WS_PING_S == 20
    assert not hasattr(ps, "PEER_WS_IDLE_S")     # no idle ceiling (1b)
    assert ps.PEER_CLIENT_ID_MAX == 128
    assert ps.PEER_WS_FRAME_MAX == 16 * 2**20
    app = create_app({"state_path": str(tmp_path / "s.json"),
                      "auth_token": A_TOKEN, "serve_ui": False},
                     port=4999, name="webterm-peer-tickets")
    assert app.config.WEBSOCKET_MAX_SIZE == ps.PEER_WS_FRAME_MAX
    assert app.config.WEBSOCKET_PING_INTERVAL == ps.PEER_WS_PING_S
    assert app.config.WEBSOCKET_PING_TIMEOUT == ps.PEER_WS_PING_S
    kw = ps.UPSTREAM_WS_KWARGS
    assert (kw["ping_interval"], kw["ping_timeout"]) == (20, 20)
