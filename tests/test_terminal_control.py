"""Issue #238: an external controller for one terminal.

A controller is a /ws subscriber that claims a terminal: while it holds it,
only its socket's input and resize reach the producer, and the desktop is told
on three surfaces (the /ws ``control`` frame, the /control ``terminal_control``
push, the /sessions ``controller`` field). The sections below follow the
build: the wire shapes and label rule (protocol.py), the transition API and
its ordered publisher (registry.py), the relay gate, and the HTTP surfaces.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from webterm import protocol
from webterm.broker.registry import BrokerRegistry, run_producer_session


# ---- wire shapes + label rule (protocol.py) -------------------------------

@pytest.mark.parametrize("label", [
    "a",
    "x" * protocol.CONTROL_LABEL_MAX,
    "Notifier (phone)",
    "\U0001F600" * protocol.CONTROL_LABEL_MAX,  # 64 astral code points
    "a‍b",                                 # ZWJ (emoji sequences)
    "a‌b",                                 # ZWNJ
    " lead and trail ",
])
def test_control_label_accepts(label):
    assert protocol.control_label_ok(label) is True


@pytest.mark.parametrize("label", [
    "",
    "x" * (protocol.CONTROL_LABEL_MAX + 1),
    "\U0001F600" * (protocol.CONTROL_LABEL_MAX + 1),
    "   ",
    "\t",
    "bell\x07",
    "nl\n",
    "del\x7f",
    "c1\x85",
    "rlo‮",
    "lri⁦",
    "alm؜",
    "rlm‏",
    "zwsp​",
    "bom﻿",
    "lone\ud800",
    "ls ",
    "ps ",
    "unassigned͸",
    None,
    5,
    b"bytes",
    ["a"],
])
def test_control_label_refuses(label):
    assert protocol.control_label_ok(label) is False


def test_control_view_shapes():
    assert protocol.control_view("controlled", "phone", 1700000000123, 7) == {
        "state": "controlled", "label": "phone", "since": 1700000000123,
        "gen": 7}
    # A free view never carries a stale label/since, whatever it is handed.
    assert protocol.control_view("free", "phone", 1700000000123, 8) == {
        "state": "free", "label": None, "since": None, "gen": 8}


def test_control_frames_exact():
    view = protocol.control_view("controlled", "phone", 1700000000123, 7)
    assert json.loads(protocol.control_frame(view)) == {
        "type": "control", "state": "controlled", "label": "phone",
        "since": 1700000000123, "gen": 7}
    assert json.loads(protocol.control_revoked_frame("claim", 9)) == {
        "type": "control", "state": "revoked", "by": "claim", "gen": 9}
    free = protocol.control_view("free", None, None, 8)
    assert json.loads(protocol.terminal_control_frame(42, free)) == {
        "type": "terminal_control", "id": 42, "state": "free",
        "label": None, "since": None, "gen": 8}


# ---- transition API + ordered publisher (registry.py) ----------------------

class FakeWS:
    """A subscriber socket: records every frame; ``gate`` (an Event) makes
    each send wait until it is set, to hold a publication mid-flight."""

    def __init__(self, gate=None):
        self.sent = []
        self.gate = gate
        self.closed = None

    async def send(self, data):
        if self.gate is not None:
            await self.gate.wait()
        self.sent.append(data)

    async def close(self, code=1000, reason=""):
        self.closed = code

    def controls(self):
        """The ``control`` frames received, decoded, in order."""
        out = []
        for raw in self.sent:
            if isinstance(raw, str):
                frame = json.loads(raw)
                if frame.get("type") == "control":
                    out.append(frame)
        return out


def _hello(wid, pid=5):
    return {"type": "hello", "window_id": wid, "pid": pid, "title": "t",
            "cols": 80, "rows": 24}


async def _registered(reg, wid=1, pid=5):
    return await reg.register(FakeWS(), _hello(wid, pid))


def _live_frame(entry):
    return dict({"type": "control"}, **entry.control_view())


def test_claim_takeover_relabel_and_gens():
    async def scenario():
        reg = BrokerRegistry()
        entry = await _registered(reg)
        a, b = FakeWS(), FakeWS()
        entry.add_subscriber(a, "ext-a")
        entry.add_subscriber(b, "ext-b")
        g0 = entry.control_gen
        assert entry.control_view() == {"state": "free", "label": None,
                                        "since": None, "gen": g0}
        assert reg.claim(entry, a, "phone", now_ms=1000) is None
        g1 = entry.control_gen
        assert g1 > g0 and entry.controller.ws is a
        assert entry.summary()["controller"] == {"label": "phone",
                                                 "since": 1000, "gen": g1}
        # Relabel from the holder: new gen, nobody revoked.
        assert reg.claim(entry, a, "phone 2", now_ms=1001) is None
        g2 = entry.control_gen
        assert g2 > g1 and a not in entry.revoked
        # Takeover by b: a is revoked and gated.
        assert reg.claim(entry, b, "tablet", now_ms=1002) is None
        g3 = entry.control_gen
        assert g3 > g2 and entry.controller.ws is b and a in entry.revoked
        # A foreign release is refused; the holder's own release frees it.
        assert reg.release_by_ws(entry, a) == "not_controller"
        assert reg.release_by_ws(entry, b) is None
        g4 = entry.control_gen
        assert g4 > g3 and entry.controller is None
        assert entry.summary()["controller"] is None
        assert reg.release_by_ws(entry, b) == "not_controller"
        assert reg.claim(entry, a, "", now_ms=1003) == "bad_label"
        await reg.drain_control()
        # a saw every change, the revoke first among the takeover's frames.
        assert [(f["state"], f["gen"]) for f in a.controls()] == [
            ("controlled", g1), ("controlled", g2), ("revoked", g3),
            ("controlled", g3), ("free", g4)]
        assert a.controls()[2]["by"] == "claim"
        assert [(f["state"], f["gen"]) for f in b.controls()] == [
            ("controlled", g1), ("controlled", g2), ("controlled", g3),
            ("free", g4)]
        assert entry.published_view == entry.control_view()
    asyncio.run(scenario())


def test_reclaim_after_revoke_clears_the_revoked_mark():
    async def scenario():
        reg = BrokerRegistry()
        entry = await _registered(reg)
        a, b = FakeWS(), FakeWS()
        for ws in (a, b):
            entry.add_subscriber(ws, "")
        reg.claim(entry, a, "a")
        reg.claim(entry, b, "b")
        assert a in entry.revoked
        reg.claim(entry, a, "a again")
        assert a not in entry.revoked and b in entry.revoked
        entry.remove_subscriber(b)
        assert b not in entry.revoked
        await reg.drain_control()
    asyncio.run(scenario())


def test_release_by_gen_matches_exactly():
    async def scenario():
        reg = BrokerRegistry()
        entry = await _registered(reg)
        a = FakeWS()
        entry.add_subscriber(a, "")
        assert reg.release_by_gen(entry, entry.control_gen) == "stale_gen"
        reg.claim(entry, a, "phone")
        g1 = entry.control_gen
        assert reg.release_by_gen(entry, g1 - 1) == "stale_gen"
        assert reg.release_by_gen(entry, g1) is None
        g2 = entry.control_gen
        assert g2 > g1 and entry.controller is None and a in entry.revoked
        # A double click: the second one finds it free.
        assert reg.release_by_gen(entry, g1) == "stale_gen"
        await reg.drain_control()
        assert [(f["state"], f["gen"], f.get("by")) for f in a.controls()] \
            == [("controlled", g1, None), ("revoked", g2, "browser"),
                ("free", g2, None)]
    asyncio.run(scenario())


def test_deregister_releases_and_refuses_later_ops():
    async def scenario():
        reg = BrokerRegistry()
        entry = await _registered(reg)
        a = FakeWS()
        entry.add_subscriber(a, "")
        reg.claim(entry, a, "phone")
        g1 = entry.control_gen
        await reg.deregister(entry.id, entry)
        assert entry.controller is None and entry.control_gen > g1
        assert entry.closing is True
        assert reg.claim(entry, a, "phone") == "unknown_session"
        assert reg.release_by_ws(entry, a) == "unknown_session"
        assert reg.release_by_gen(entry, entry.control_gen) \
            == "unknown_session"
        await reg.drain_control()
        assert a.controls()[-1] == {"type": "control", "state": "free",
                                    "label": None, "since": None,
                                    "gen": entry.control_gen}
    asyncio.run(scenario())


def test_replacement_releases_and_gen_never_goes_backwards():
    async def scenario():
        reg = BrokerRegistry()
        hooked = []

        async def hook(wid, view):
            hooked.append((wid, view["state"], view["gen"]))
        reg.on_control = hook
        old = await _registered(reg, 7)
        a = FakeWS()
        old.add_subscriber(a, "")
        reg.claim(old, a, "phone")
        g1 = old.control_gen
        new = await _registered(reg, 7)
        assert old.controller is None and old.closing is True
        assert old.control_gen > g1
        assert new.control_gen >= old.control_gen
        assert new.control_view()["state"] == "free"
        assert a.closed == 1012
        assert reg.claim(old, a, "phone") == "unknown_session"
        await reg.drain_control()
        assert hooked == [(7, "controlled", g1), (7, "free", old.control_gen)]
        # An UNcontrolled replacement shows no older gen either.
        newer = await _registered(reg, 7)
        assert newer.control_view()["gen"] >= new.control_view()["gen"]
        assert newer.published_view == newer.control_view()
    asyncio.run(scenario())


def test_crossed_publications_arrive_in_gen_order_and_rest_on_live():
    async def scenario():
        reg = BrokerRegistry()
        entry = await _registered(reg)
        gate = asyncio.Event()
        slow, fast, ctl = FakeWS(gate), FakeWS(), FakeWS()
        for ws in (slow, fast, ctl):
            entry.add_subscriber(ws, "")
        reg.claim(entry, ctl, "phone")
        await asyncio.sleep(0)          # the first publication is now held
        reg.release_by_ws(entry, ctl)
        reg.claim(entry, ctl, "phone again")
        await asyncio.sleep(0.01)
        gate.set()
        await reg.drain_control()
        for ws in (slow, fast, ctl):
            gens = [f["gen"] for f in ws.controls()]
            assert gens == sorted(gens) and len(gens) == 3
            assert ws.controls()[-1] == _live_frame(entry)
    asyncio.run(scenario())


def test_revoke_crossed_with_reclaim_keeps_revoked_first():
    async def scenario():
        reg = BrokerRegistry()
        entry = await _registered(reg)
        gate = asyncio.Event()
        a = FakeWS(gate)
        entry.add_subscriber(a, "")
        reg.claim(entry, a, "phone")
        await asyncio.sleep(0)
        g1 = entry.control_gen
        reg.release_by_gen(entry, g1)     # POST: revoke a
        g2 = entry.control_gen
        reg.claim(entry, a, "phone")      # a re-opens at once
        g3 = entry.control_gen
        gate.set()
        await reg.drain_control()
        assert [(f["state"], f["gen"]) for f in a.controls()] == [
            ("controlled", g1), ("revoked", g2), ("free", g2),
            ("controlled", g3)]
        assert a.controls()[-1] == _live_frame(entry)
    asyncio.run(scenario())


def test_publication_survives_the_committing_task_being_cancelled():
    async def scenario():
        reg = BrokerRegistry()
        hooked = []

        async def hook(wid, view):
            hooked.append(view["gen"])
        reg.on_control = hook
        entry = await _registered(reg)
        gate = asyncio.Event()
        a = FakeWS(gate)
        entry.add_subscriber(a, "")

        async def committer():
            reg.claim(entry, a, "phone")
            await asyncio.sleep(3600)
        task = asyncio.get_running_loop().create_task(committer())
        await asyncio.sleep(0.01)
        task.cancel()
        await asyncio.sleep(0.01)
        gate.set()
        await reg.drain_control()
        assert a.controls() == [_live_frame(entry)]
        assert hooked == [entry.control_gen]
    asyncio.run(scenario())


def test_old_entry_publication_in_flight_precedes_the_replacement():
    async def scenario():
        reg = BrokerRegistry()
        hooked = []

        async def hook(wid, view):
            hooked.append((view["state"], view["gen"]))
        reg.on_control = hook
        old = await _registered(reg, 9)
        gate = asyncio.Event()
        held = FakeWS(gate)
        old.add_subscriber(held, "")
        reg.claim(old, held, "phone")
        g1 = old.control_gen
        await asyncio.sleep(0)            # old's claim publication is held
        new = await _registered(reg, 9)   # old released (g2), queued behind
        g2 = old.control_gen
        ctl = FakeWS()
        new.add_subscriber(ctl, "")
        assert reg.claim(new, ctl, "tablet") is None
        g3 = new.control_gen
        await asyncio.sleep(0.01)
        gate.set()
        await reg.drain_control()
        assert hooked == [("controlled", g1), ("free", g2),
                          ("controlled", g3)]
    asyncio.run(scenario())


def test_claim_during_the_exit_broadcast_is_refused():
    async def scenario():
        reg = BrokerRegistry()
        frames = asyncio.Queue()

        class Producer:
            async def recv(self):
                return await frames.get()

            async def send(self, data):
                pass
        await frames.put(json.dumps(_hello(11)))
        prod = asyncio.get_running_loop().create_task(
            run_producer_session(Producer(), reg))
        for _ in range(100):
            if reg.get(11) is not None:
                break
            await asyncio.sleep(0.01)
        entry = reg.get(11)
        gate = asyncio.Event()
        held = FakeWS(gate)
        entry.add_subscriber(held, "")
        await frames.put(json.dumps({"type": "exit", "code": 0}))
        await asyncio.sleep(0.05)         # exit broadcast is now held
        assert reg.get(11) is entry
        assert reg.claim(entry, FakeWS(), "phone") == "unknown_session"
        gate.set()
        await asyncio.wait_for(prod, 5)
        await reg.drain_control()
    asyncio.run(scenario())


def test_producer_drop_without_exit_releases():
    async def scenario():
        reg = BrokerRegistry()
        frames = asyncio.Queue()

        class Producer:
            async def recv(self):
                return await frames.get()

            async def send(self, data):
                pass
        await frames.put(json.dumps(_hello(12)))
        prod = asyncio.get_running_loop().create_task(
            run_producer_session(Producer(), reg))
        for _ in range(100):
            if reg.get(12) is not None:
                break
            await asyncio.sleep(0.01)
        entry = reg.get(12)
        a = FakeWS()
        entry.add_subscriber(a, "")
        reg.claim(entry, a, "phone")
        g1 = entry.control_gen
        await frames.put(None)            # the socket just goes away
        await asyncio.wait_for(prod, 5)
        await reg.drain_control()
        assert entry.controller is None and entry.control_gen > g1
        assert a.controls()[-1]["state"] == "free"
    asyncio.run(scenario())


def test_a_raising_hook_neither_loses_the_view_nor_the_next_change():
    async def scenario():
        reg = BrokerRegistry()
        calls = []

        async def hook(wid, view):
            calls.append(view["gen"])
            if len(calls) == 1:
                raise RuntimeError("boom")
        reg.on_control = hook
        entry = await _registered(reg)
        a = FakeWS()
        entry.add_subscriber(a, "")
        reg.claim(entry, a, "phone")
        await reg.drain_control()
        assert entry.published_view == entry.control_view()
        reg.release_by_ws(entry, a)
        await reg.drain_control()
        assert calls == [entry.control_gen - 1, entry.control_gen]
        assert entry.published_view == entry.control_view()
    asyncio.run(scenario())


def test_control_lock_map_is_bounded_by_live_ids():
    async def scenario():
        reg = BrokerRegistry()
        for i in range(25):
            entry = await _registered(reg, 100 + i)
            a = FakeWS()
            entry.add_subscriber(a, "")
            async with reg.control_section(entry.id):
                pass
            reg.claim(entry, a, "phone")
            await reg.deregister(entry.id, entry)
            await reg.drain_control()
        assert reg._control_locks == {}
        assert reg._control_users == {}
        # A live id keeps its lock.
        live = await _registered(reg, 500)
        async with reg.control_section(live.id):
            pass
        assert list(reg._control_locks) == [500]
    asyncio.run(scenario())


def test_send_to_producer_if_rechecks_under_the_send_lock():
    async def scenario():
        reg = BrokerRegistry()
        entry = await _registered(reg)
        allowed = {"v": True}
        await entry._send_lock.acquire()
        pending = asyncio.get_running_loop().create_task(
            entry.send_to_producer_if("x", lambda: allowed["v"]))
        await asyncio.sleep(0.01)
        allowed["v"] = False
        entry._send_lock.release()
        assert await pending is False
        assert entry.ws.sent == []
        assert await entry.send_to_producer_if("y", lambda: True) is True
        assert entry.ws.sent == ["y"]
    asyncio.run(scenario())
