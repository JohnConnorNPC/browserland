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
        assert new.control_gen > old.control_gen
        assert new.control_view()["state"] == "free"
        assert a.closed == 1012
        assert reg.claim(old, a, "phone") == "unknown_session"
        await reg.drain_control()
        assert hooked == [(7, "controlled", g1), (7, "free", old.control_gen)]
        # An UNcontrolled replacement's view is newer too (clients order
        # views by gen).
        newer = await _registered(reg, 7)
        assert newer.control_view()["gen"] > new.control_view()["gen"]
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


# ---- relay gate, in process (relay.py) -------------------------------------

class FakeBrowser(FakeWS):
    """A /ws socket the relay reads from: ``push`` a frame, ``hang_up`` to end
    it. ``gate_first`` holds only the FIRST send (the attach's `resized`)."""

    def __init__(self, gate=None, gate_first=None):
        super().__init__(gate)
        self.gate_first = gate_first
        self.inbox = asyncio.Queue()

    async def send(self, data):
        if self.gate_first is not None:
            gate, self.gate_first = self.gate_first, None
            await gate.wait()
        await super().send(data)

    def push(self, frame):
        self.inbox.put_nowait(frame if isinstance(frame, (bytes, str))
                              else json.dumps(frame))

    def hang_up(self):
        self.inbox.put_nowait(None)

    def __aiter__(self):
        return self

    async def __anext__(self):
        message = await self.inbox.get()
        if message is None:
            raise StopAsyncIteration
        return message


class FakeRequest:
    def __init__(self, sid, client_id=""):
        self.args = {"session": str(sid), "clientId": client_id}


class FakeCtx:
    def __init__(self, active):
        self.active_client_id = active


def _producer_frames(entry, kinds=("input", "resize")):
    out = []
    for raw in entry.ws.sent:
        frame = json.loads(raw)
        if frame.get("type") in kinds:
            out.append(frame)
    return out


async def _settle():
    for _ in range(5):
        await asyncio.sleep(0.005)


def _attach(reg, ctx, entry, ws, client_id=""):
    from webterm.broker import relay
    return asyncio.get_running_loop().create_task(relay.handle_browser_ws(
        FakeRequest(entry.id, client_id), ws, reg, ctx))


def test_relay_frame_admitted_before_a_claim_does_not_land_after_it():
    async def scenario():
        reg = BrokerRegistry()
        ctx = FakeCtx("lease")
        entry = await _registered(reg)
        holder, ctl = FakeBrowser(), FakeBrowser()
        tasks = [_attach(reg, ctx, entry, holder, "lease"),
                 _attach(reg, ctx, entry, ctl, "ext-c")]
        await _settle()
        await entry._send_lock.acquire()      # a snapshot/RPC in flight
        holder.push({"type": "input", "data": "late"})
        holder.push(b"late-binary")
        holder.push({"type": "resize", "cols": 1, "rows": 1})
        await _settle()                       # admitted, waiting on the lock
        ctl.push({"type": "control", "op": "claim", "label": "phone"})
        await _settle()
        entry._send_lock.release()
        await _settle()
        assert _producer_frames(entry) == []
        for ws in (holder, ctl):
            ws.hang_up()
        await asyncio.gather(*tasks)
        await reg.drain_control()
    asyncio.run(scenario())


def test_relay_attach_reads_resized_before_any_control_change():
    async def scenario():
        reg = BrokerRegistry()
        ctx = FakeCtx("lease")
        entry = await _registered(reg)
        ctl = FakeBrowser()
        tasks = [_attach(reg, ctx, entry, ctl, "ext-c")]
        await _settle()
        gate = asyncio.Event()
        late = FakeBrowser(gate_first=gate)
        tasks.append(_attach(reg, ctx, entry, late, "lease"))
        await _settle()                       # late's `resized` is held
        ctl.push({"type": "control", "op": "claim", "label": "phone"})
        await _settle()
        gate.set()
        await _settle()
        await reg.drain_control()
        first = json.loads(late.sent[0])
        assert first["type"] == "resized"
        gens = [f["gen"] for f in late.controls()]
        assert gens == sorted(gens)
        assert late.controls()[-1] == _live_frame(entry)
        assert late.controls()[-1]["state"] == "controlled"
        for ws in (ctl, late):
            ws.hang_up()
        await asyncio.gather(*tasks)
    asyncio.run(scenario())


def test_relay_revoked_lease_holder_socket_stays_gated_until_it_reclaims():
    async def scenario():
        reg = BrokerRegistry()
        ctx = FakeCtx("lease")
        entry = await _registered(reg)
        holder = FakeBrowser()
        task = _attach(reg, ctx, entry, holder, "lease")
        await _settle()
        holder.push({"type": "control", "op": "claim", "label": "me"})
        await _settle()
        assert reg.release_by_gen(entry, entry.control_gen) is None
        holder.push({"type": "input", "data": "after-revoke"})
        holder.push(b"after-revoke-binary")
        await _settle()
        assert _producer_frames(entry) == []
        holder.push({"type": "control", "op": "claim", "label": "me again"})
        holder.push({"type": "input", "data": "reclaimed"})
        await _settle()
        assert _producer_frames(entry) == [
            {"type": "input", "data": "reclaimed"}]
        holder.hang_up()
        await task
        await reg.drain_control()
    asyncio.run(scenario())


def test_relay_control_refusals_answer_and_keep_the_socket():
    async def scenario():
        reg = BrokerRegistry()
        ctx = FakeCtx("lease")
        entry = await _registered(reg)
        a, b = FakeBrowser(), FakeBrowser()
        tasks = [_attach(reg, ctx, entry, a, "ext-a"),
                 _attach(reg, ctx, entry, b, "ext-b")]
        await _settle()
        a.push({"type": "control", "op": "claim", "label": ""})
        a.push({"type": "control", "op": "claim"})
        a.push({"type": "control", "op": "steal"})
        a.push({"type": "control", "op": "release"})
        a.push({"type": "control", "op": "claim", "label": "phone"})
        b.push({"type": "control", "op": "release"})
        await _settle()
        errors = [json.loads(m) for m in a.sent if isinstance(m, str)
                  and json.loads(m).get("type") == "error"]
        assert errors == [
            {"type": "error", "reason": r, "session_id": entry.id}
            for r in ("bad_label", "bad_label", "bad_op", "not_controller")]
        assert entry.controller.ws is a       # the socket stayed usable
        b_errors = [json.loads(m) for m in b.sent if isinstance(m, str)
                    and json.loads(m).get("type") == "error"]
        assert b_errors == [{"type": "error", "reason": "not_controller",
                             "session_id": entry.id}]
        # On a dead terminal every op answers unknown_session.
        await reg.deregister(entry.id, entry)
        for op in ({"op": "claim", "label": "x"}, {"op": "release"},
                   {"op": "steal"}):
            b.push(dict({"type": "control"}, **op))
        await _settle()
        b_errors = [json.loads(m)["reason"] for m in b.sent
                    if isinstance(m, str)
                    and json.loads(m).get("type") == "error"]
        assert b_errors == ["not_controller"] + ["unknown_session"] * 3
        for ws in (a, b):
            ws.hang_up()
        await asyncio.gather(*tasks)
        await reg.drain_control()
    asyncio.run(scenario())


def test_relay_controller_socket_closing_releases():
    async def scenario():
        reg = BrokerRegistry()
        ctx = FakeCtx("lease")
        entry = await _registered(reg)
        ctl, holder = FakeBrowser(), FakeBrowser()
        tasks = [_attach(reg, ctx, entry, ctl, "ext-c"),
                 _attach(reg, ctx, entry, holder, "lease")]
        await _settle()
        ctl.push({"type": "control", "op": "claim", "label": "phone"})
        await _settle()
        g1 = entry.control_gen
        ctl.hang_up()
        await tasks[0]
        assert entry.controller is None and entry.control_gen > g1
        holder.push({"type": "input", "data": "mine again"})
        await _settle()
        assert _producer_frames(entry) == [
            {"type": "input", "data": "mine again"}]
        holder.hang_up()
        await tasks[1]
        await reg.drain_control()
    asyncio.run(scenario())


# ---- end to end: a broker subprocess and real sockets ----------------------
#
# The fixture follows tests/test_broker_e2e.py's broker_proc (same env, same
# readiness probe) minus its /mod-store reset, which nothing here touches.
# Every test uses its own window ids and takes the lease explicitly, so the
# order the tests run in does not matter.

import os  # noqa: E402
import subprocess  # noqa: E402
import sys  # noqa: E402
import time  # noqa: E402
from pathlib import Path  # noqa: E402

import websockets  # noqa: E402

from tests.test_broker_e2e import _free_port, _http  # noqa: E402

REPO = Path(__file__).resolve().parents[1]
TOKEN = "terminal-control-token"
AUTH = {"Authorization": f"Bearer {TOKEN}",
        "Content-Type": "application/json"}


@pytest.fixture(scope="module")
def broker():
    port = _free_port()
    env = dict(os.environ)
    env["WEB_TERMINAL_TOKEN"] = TOKEN
    env["PYTHONPATH"] = str(REPO) + os.pathsep + env.get("PYTHONPATH", "")
    env.pop("BROWSERLAND_BROKER_URL", None)
    proc = subprocess.Popen(
        [sys.executable, "-m", "webterm.broker",
         "--host", "127.0.0.1", "--port", str(port)],
        cwd=str(REPO), env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    base = f"http://127.0.0.1:{port}"
    deadline = time.time() + 20
    while True:
        try:
            status, _ = _http("GET", f"{base}/sessions?token={TOKEN}")
            if status == 200:
                break
        except OSError:
            pass
        if proc.poll() is not None:
            raise RuntimeError(f"broker exited early: {proc.returncode}")
        if time.time() > deadline:
            proc.kill()
            raise RuntimeError("broker did not come up")
        time.sleep(0.2)
    yield port, base
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()


async def _until(ws, pred, timeout=5.0):
    """Read text frames until ``pred(frame)``; binary frames are skipped.
    Returns the matching frame."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while True:
        raw = await asyncio.wait_for(ws.recv(), deadline - loop.time())
        if isinstance(raw, bytes):
            continue
        frame = json.loads(raw)
        if pred(frame):
            return frame


def _is_control(state=None):
    return lambda f: f.get("type") == "control" and (
        state is None or f.get("state") == state)


async def _lease(port, client_id):
    """A /control socket for ``client_id`` that holds the lease."""
    ws = await websockets.connect(
        f"ws://127.0.0.1:{port}/control?clientId={client_id}&token={TOKEN}")
    status = json.loads(await asyncio.wait_for(ws.recv(), 5))
    if not status["active"]:
        await ws.send(json.dumps({"type": "become_active"}))
        await _until(ws, lambda f: f.get("type") == "status"
                     and f["active"] is True)
    return ws


async def _producer(port, base, wid):
    ws = await websockets.connect(
        f"ws://127.0.0.1:{port}/browserland?token={TOKEN}", max_size=None)
    await ws.send(json.dumps({"type": "hello", "window_id": wid, "pid": 5,
                              "title": "ctl", "cols": 80, "rows": 24}))
    for _ in range(50):
        _, sessions = _http("GET", f"{base}/sessions?token={TOKEN}")
        if any(s["id"] == wid for s in sessions):
            return ws
        await asyncio.sleep(0.1)
    raise AssertionError(f"window {wid} never registered")


async def _browser(port, wid, client_id):
    """Attach a /ws socket and consume its attach frames; returns
    (ws, [resized, control])."""
    ws = await websockets.connect(
        f"ws://127.0.0.1:{port}/ws?session={wid}&token={TOKEN}"
        f"&clientId={client_id}", max_size=None)
    first = json.loads(await asyncio.wait_for(ws.recv(), 5))
    second = json.loads(await asyncio.wait_for(ws.recv(), 5))
    return ws, [first, second]


async def _next_drive(producer, timeout=5.0):
    """The producer's next input/resize frame (snapshot_please skipped)."""
    while True:
        frame = json.loads(await asyncio.wait_for(producer.recv(), timeout))
        if frame.get("type") != "snapshot_please":
            return frame


async def _claim(ws, label="phone"):
    await ws.send(json.dumps({"type": "control", "op": "claim",
                              "label": label}))
    return await _until(ws, _is_control("controlled"))


def _session(base, wid):
    _, sessions = _http("GET", f"{base}/sessions?token={TOKEN}")
    return next(s for s in sessions if s["id"] == wid)


def test_e2e_attach_reads_resized_then_the_control_view(broker):
    port, base = broker

    async def scenario():
        lease = await _lease(port, "tc-attach")
        prod = await _producer(port, base, 238001)
        first, frames = await _browser(port, 238001, "tc-attach")
        assert frames[0] == {"type": "resized", "cols": 80, "rows": 24}
        assert frames[1]["type"] == "control"
        assert frames[1]["state"] == "free"
        assert (frames[1]["label"], frames[1]["since"]) == (None, None)
        ctl, _ = await _browser(port, 238001, "ext-attach")
        claimed = await _claim(ctl, "Notifier")
        # A socket attaching mid-control reads `controlled` right after
        # `resized`, before the snapshot.
        late, frames = await _browser(port, 238001, "tc-attach")
        assert frames[0]["type"] == "resized"
        assert frames[1] == claimed
        assert frames[1]["label"] == "Notifier"
        assert isinstance(frames[1]["since"], int)
        # The already-attached watcher got the change too.
        assert await _until(first, _is_control()) == claimed
        for ws in (first, ctl, late, prod, lease):
            await ws.close()
    asyncio.run(scenario())


def test_e2e_gate_while_controlled(broker):
    port, base = broker

    async def scenario():
        lease = await _lease(port, "tc-gate")
        prod_a = await _producer(port, base, 238011)
        prod_b = await _producer(port, base, 238012)
        holder_a, _ = await _browser(port, 238011, "tc-gate")
        holder_b, _ = await _browser(port, 238012, "tc-gate")
        ctl, _ = await _browser(port, 238011, "ext-gate")
        await _claim(ctl)
        # The controller drives: text, binary, resize.
        await ctl.send(json.dumps({"type": "input", "data": "c1"}))
        assert await _next_drive(prod_a) == {"type": "input", "data": "c1"}
        await ctl.send(b"c2")
        assert await _next_drive(prod_a) == {"type": "input", "data": "c2"}
        # The lease holder's text, BINARY, legacy paste and resize are all
        # dropped on the controlled terminal: the next thing the producer
        # sees is the controller's marker.
        await holder_a.send(json.dumps({"type": "input", "data": "h1"}))
        await holder_a.send(b"h2")
        await holder_a.send(json.dumps({"type": "paste", "data": "h3"}))
        await holder_a.send(json.dumps({"type": "resize", "cols": 100,
                                        "rows": 30}))
        await ctl.send(json.dumps({"type": "resize", "cols": 40,
                                   "rows": 20}))
        assert await _next_drive(prod_a) == {"type": "resize", "cols": 40,
                                             "rows": 20}
        # ...while the same holder still drives its OTHER terminal.
        await holder_b.send(json.dumps({"type": "input", "data": "b1"}))
        assert await _next_drive(prod_b) == {"type": "input", "data": "b1"}
        for ws in (holder_a, holder_b, ctl, prod_a, prod_b, lease):
            await ws.close()
    asyncio.run(scenario())


def test_e2e_release_by_frame_close_and_exit(broker):
    port, base = broker

    async def scenario():
        lease = await _lease(port, "tc-rel")
        prod = await _producer(port, base, 238021)
        holder, _ = await _browser(port, 238021, "tc-rel")
        # 1. by frame
        ctl, _ = await _browser(port, 238021, "ext-rel")
        g1 = (await _claim(ctl))["gen"]
        await ctl.send(json.dumps({"type": "control", "op": "release"}))
        freed = await _until(holder, _is_control("free"))
        assert freed["gen"] > g1
        assert _session(base, 238021)["controller"] is None
        await holder.send(json.dumps({"type": "input", "data": "h1"}))
        assert await _next_drive(prod) == {"type": "input", "data": "h1"}
        await holder.send(json.dumps({"type": "resize", "cols": 90,
                                      "rows": 25}))
        assert await _next_drive(prod) == {"type": "resize", "cols": 90,
                                           "rows": 25}
        # 2. by socket close
        g2 = (await _claim(ctl))["gen"]
        assert g2 > freed["gen"]
        assert _session(base, 238021)["controller"]["gen"] == g2
        await ctl.close()
        freed = await _until(holder, _is_control("free"))
        assert freed["gen"] > g2
        await holder.send(json.dumps({"type": "input", "data": "h2"}))
        assert await _next_drive(prod) == {"type": "input", "data": "h2"}
        # 3. by terminal exit: the controller hears `exit`, then `free`.
        ctl, _ = await _browser(port, 238021, "ext-rel")
        g3 = (await _claim(ctl))["gen"]
        await prod.send(json.dumps({"type": "exit", "code": 0}))
        assert (await _until(ctl, lambda f: f.get("type") in
                             ("exit", "control")))["type"] == "exit"
        freed = await _until(ctl, _is_control("free"))
        assert freed["gen"] > g3
        for ws in (holder, ctl, prod, lease):
            await ws.close()
    asyncio.run(scenario())


def test_e2e_second_claim_revokes_the_first(broker):
    port, base = broker

    async def scenario():
        lease = await _lease(port, "tc-take")
        prod = await _producer(port, base, 238031)
        first, _ = await _browser(port, 238031, "ext-one")
        second, _ = await _browser(port, 238031, "ext-two")
        await _claim(first, "one")
        await _until(second, _is_control("controlled"))
        taken = await _claim(second, "two")
        revoked = await _until(first, _is_control())
        assert revoked == {"type": "control", "state": "revoked",
                           "by": "claim", "gen": taken["gen"]}
        assert await _until(first, _is_control()) == taken
        # The revoked socket stays open as a watcher, its input dropped.
        await first.send(json.dumps({"type": "input", "data": "x"}))
        await second.send(json.dumps({"type": "input", "data": "y"}))
        assert await _next_drive(prod) == {"type": "input", "data": "y"}
        for ws in (first, second, prod, lease):
            await ws.close()
    asyncio.run(scenario())


def test_e2e_become_active_spares_a_distinct_controller(broker):
    port, base = broker

    async def scenario():
        loser = await _lease(port, "tc-loser")
        prod = await _producer(port, base, 238041)
        page, _ = await _browser(port, 238041, "tc-loser")
        ctl, _ = await _browser(port, 238041, "ext-survivor")
        claimed = await _claim(ctl)
        # Another browser takes the lease: the loser's /ws gets 4409...
        winner = await _lease(port, "tc-winner")
        with pytest.raises(websockets.ConnectionClosed) as closed:
            while True:
                await asyncio.wait_for(page.recv(), 10)
        assert closed.value.rcvd.code == 4409
        # ...the controller's does not, and it still drives.
        assert _session(base, 238041)["controller"]["gen"] == claimed["gen"]
        await ctl.send(json.dumps({"type": "input", "data": "still"}))
        assert await _next_drive(prod) == {"type": "input", "data": "still"}
        for ws in (ctl, prod, loser, winner):
            await ws.close()
    asyncio.run(scenario())


def test_e2e_controller_sharing_the_losers_client_id_is_closed_and_released(
        broker):
    port, base = broker

    async def scenario():
        loser = await _lease(port, "tc-shared")
        prod = await _producer(port, base, 238051)
        watcher, _ = await _browser(port, 238051, "ext-watch")
        ctl, _ = await _browser(port, 238051, "tc-shared")
        claimed = await _claim(ctl)
        winner = await _lease(port, "tc-shared-winner")
        with pytest.raises(websockets.ConnectionClosed) as closed:
            while True:
                await asyncio.wait_for(ctl.recv(), 10)
        assert closed.value.rcvd.code == 4409
        freed = await _until(watcher, _is_control("free"))
        assert freed["gen"] > claimed["gen"]
        assert _session(base, 238051)["controller"] is None
        for ws in (watcher, prod, loser, winner):
            await ws.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("label,wid", [
    ("", 238061), ("x" * 65, 238062), ("bell\x07", 238063),
    ("rlo‮", 238064)])
def test_e2e_bad_label(broker, label, wid):
    port, base = broker

    async def scenario():
        prod = await _producer(port, base, wid)
        ctl, _ = await _browser(port, wid, "ext-label")
        await ctl.send(json.dumps({"type": "control", "op": "claim",
                                   "label": label}))
        assert await _until(ctl, lambda f: f.get("type") == "error") == {
            "type": "error", "reason": "bad_label", "session_id": wid}
        assert _session(base, wid)["controller"] is None
        # The socket stays open and a good claim then works.
        assert (await _claim(ctl, "ok"))["label"] == "ok"
        for ws in (ctl, prod):
            await ws.close()
    asyncio.run(scenario())


def test_e2e_take_back_over_post_and_the_control_push(broker):
    port, base = broker

    async def scenario():
        lease = await _lease(port, "tc-post")
        other = await websockets.connect(
            f"ws://127.0.0.1:{port}/control?clientId=tc-post-other"
            f"&token={TOKEN}")
        prod = await _producer(port, base, 238071)
        ctl, _ = await _browser(port, 238071, "ext-post")
        claimed = await _claim(ctl)
        # Every /control socket hears the claim.
        for sock in (lease, other):
            pushed = await _until(sock, lambda f: f.get("type")
                                  == "terminal_control" and f["id"] == 238071)
            assert pushed == dict(claimed, type="terminal_control",
                                  id=238071)
        assert _session(base, 238071)["controller"] == {
            "label": claimed["label"], "since": claimed["since"],
            "gen": claimed["gen"]}
        body = {"id": 238071, "op": "release", "gen": claimed["gen"]}
        # A browser that does not hold the lease is refused.
        status, resp = _http("POST", f"{base}/session/control",
                             json.dumps(dict(body, clientId="tc-post-other"))
                             .encode(), AUTH)
        assert (status, resp) == (409, {"ok": False, "error": "not_active"})
        status, resp = _http("POST", f"{base}/session/control",
                             json.dumps(dict(body, clientId="tc-post"))
                             .encode(), AUTH)
        assert status == 200 and resp["state"] == "free"
        assert resp["gen"] > claimed["gen"]
        # The controller is told first, then sees the free view; its socket
        # stays open as a watcher and its input is dropped.
        assert await _until(ctl, _is_control()) == {
            "type": "control", "state": "revoked", "by": "browser",
            "gen": resp["gen"]}
        assert await _until(ctl, _is_control()) == {
            "type": "control", "state": "free", "label": None,
            "since": None, "gen": resp["gen"]}
        for sock in (lease, other):
            pushed = await _until(sock, lambda f: f.get("type")
                                  == "terminal_control" and f["id"] == 238071)
            assert pushed["state"] == "free" and pushed["gen"] == resp["gen"]
        assert _session(base, 238071)["controller"] is None
        page, _ = await _browser(port, 238071, "tc-post")
        await ctl.send(json.dumps({"type": "input", "data": "dropped"}))
        await page.send(json.dumps({"type": "input", "data": "page"}))
        assert await _next_drive(prod) == {"type": "input", "data": "page"}
        for ws in (ctl, page, prod, lease, other):
            await ws.close()
    asyncio.run(scenario())


# ---- HTTP surfaces, in process (app.py) ------------------------------------

from webterm.broker import app as app_mod  # noqa: E402

from tests.auth_helpers import TEST_TOKEN, authed  # noqa: E402

MCP_TOKEN = "terminal-control-mcp-token"
_app_seq = 0


def _control_app(tmp_path, monkeypatch):
    global _app_seq
    _app_seq += 1
    monkeypatch.delenv("WEB_TERMINAL_TOKEN", raising=False)
    monkeypatch.delenv("WEB_TERMINAL_MCP_TOKEN", raising=False)
    cfg = {
        "state_path": str(tmp_path / "webterm_state.json"),
        "mcp_state_path": str(tmp_path / "webterm_mcp.json"),
        "mcp_windows_path": str(tmp_path / "webterm_mcp_windows.json"),
        "auth_token": TEST_TOKEN,
        "mcp_enabled": True,
        "mcp_token": MCP_TOKEN,
        "mcp_default_mode": "readwrite",
    }
    return app_mod.create_app(cfg, name=f"webterm-terminal-control-{_app_seq}")


def _app_entry(app, wid):
    return asyncio.run(app.ctx.registry.register(FakeWS(), _hello(wid)))


def _app_claim(app, entry, label="phone"):
    async def go():
        ws = FakeWS()
        entry.add_subscriber(ws, "ext")
        assert app.ctx.registry.claim(entry, ws, label) is None
        await app.ctx.registry.drain_control()
        return ws
    return asyncio.run(go())


def _post_control(app, body, token=True):
    client = authed(app) if token else app.test_client
    _, resp = client.post("/session/control", json=body)
    return resp.status, resp.json


def test_post_session_control_refusal_ladder(tmp_path, monkeypatch):
    app = _control_app(tmp_path, monkeypatch)
    entry = _app_entry(app, 41)
    _app_claim(app, entry)
    gen = entry.control_gen
    app.ctx.active_client_id = "page"
    ok = {"id": 41, "op": "release", "gen": gen, "clientId": "page"}
    assert _post_control(app, ok, token=False) == (
        401, {"ok": False, "error": "auth_required"})
    _, resp = authed(app).post("/session/control", data="[1]")
    assert (resp.status, resp.json) == (400, {"ok": False,
                                              "error": "bad_json"})
    assert _post_control(app, dict(ok, id="x")) == (
        400, {"ok": False, "error": "bad_id"})
    assert _post_control(app, dict(ok, id=999)) == (
        404, {"ok": False, "error": "unknown_session"})
    for op in (None, "claim", "RELEASE"):
        assert _post_control(app, dict(ok, op=op)) == (
            400, {"ok": False, "error": "bad_op"})
    for bad in ("5", True, False, -1, 1.5, None):
        assert _post_control(app, dict(ok, gen=bad)) == (
            400, {"ok": False, "error": "bad_gen"})
    # Precedence: op before gen before the lease.
    assert _post_control(app, dict(ok, op="x", gen="x", clientId="o")) == (
        400, {"ok": False, "error": "bad_op"})
    assert _post_control(app, dict(ok, gen="x", clientId="o")) == (
        400, {"ok": False, "error": "bad_gen"})
    for client in ("other", "", None, 5):
        assert _post_control(app, dict(ok, clientId=client)) == (
            409, {"ok": False, "error": "not_active"})
    missing = dict(ok)
    del missing["clientId"]
    assert _post_control(app, missing) == (
        409, {"ok": False, "error": "not_active"})
    # The lease before the gen match: a stale gen from a non-holder is
    # not_active, never a leak of the live view.
    assert _post_control(app, dict(ok, gen=gen - 1, clientId="o")) == (
        409, {"ok": False, "error": "not_active"})
    # Nothing above released it.
    assert entry.controller is not None and entry.control_gen == gen


def test_post_session_control_refuses_a_none_lease(tmp_path, monkeypatch):
    # Owner ruling (Q6, STRICT): unlike PUT /state, a None lease refuses.
    app = _control_app(tmp_path, monkeypatch)
    entry = _app_entry(app, 42)
    _app_claim(app, entry)
    assert app.ctx.active_client_id is None
    body = {"id": 42, "op": "release", "gen": entry.control_gen,
            "clientId": "page"}
    assert _post_control(app, body) == (
        409, {"ok": False, "error": "not_active"})
    assert _post_control(app, dict(body, clientId="")) == (
        409, {"ok": False, "error": "not_active"})
    assert entry.controller is not None


def test_post_session_control_release_stale_and_double_click(tmp_path,
                                                             monkeypatch):
    app = _control_app(tmp_path, monkeypatch)
    entry = _app_entry(app, 43)
    app.ctx.active_client_id = "page"
    free_gen = entry.control_gen
    body = {"id": 43, "op": "release", "gen": free_gen, "clientId": " page "}
    # A free terminal: stale_gen with the live (free) view.
    assert _post_control(app, body) == (409, {
        "ok": False, "error": "stale_gen", "id": 43, "state": "free",
        "label": None, "since": None, "gen": free_gen})
    _app_claim(app, entry, "phone")
    claim = entry.controller
    # Another gen: stale_gen with the live CONTROLLED view.
    assert _post_control(app, dict(body, gen=claim.gen - 1)) == (409, {
        "ok": False, "error": "stale_gen", "id": 43, "state": "controlled",
        "label": "phone", "since": claim.since, "gen": claim.gen})
    status, resp = _post_control(app, dict(body, gen=claim.gen))
    assert status == 200
    assert resp == {"ok": True, "id": 43, "state": "free", "label": None,
                    "since": None, "gen": entry.control_gen}
    assert entry.control_gen > claim.gen and entry.controller is None
    # The double click finds it free.
    assert _post_control(app, dict(body, gen=claim.gen)) == (409, {
        "ok": False, "error": "stale_gen", "id": 43, "state": "free",
        "label": None, "since": None, "gen": entry.control_gen})


def test_post_session_control_crossed_claim_keeps_the_newer(tmp_path,
                                                           monkeypatch):
    app = _control_app(tmp_path, monkeypatch)
    entry = _app_entry(app, 44)
    app.ctx.active_client_id = "page"
    _app_claim(app, entry, "phone")
    seen_gen = entry.control_gen          # what the page's overlay holds
    _app_claim(app, entry, "tablet")      # a crossed takeover
    newer = entry.controller
    assert _post_control(app, {"id": 44, "op": "release", "gen": seen_gen,
                               "clientId": "page"}) == (409, {
        "ok": False, "error": "stale_gen", "id": 44, "state": "controlled",
        "label": "tablet", "since": newer.since, "gen": newer.gen})
    assert entry.controller is newer


def test_post_session_control_on_a_dying_terminal(tmp_path, monkeypatch):
    app = _control_app(tmp_path, monkeypatch)
    entry = _app_entry(app, 45)
    app.ctx.active_client_id = "page"
    _app_claim(app, entry)
    entry.closing = True                  # an `exit` mid-broadcast
    assert _post_control(app, {"id": 45, "op": "release",
                               "gen": entry.control_gen,
                               "clientId": "page"}) == (
        404, {"ok": False, "error": "unknown_session"})


def test_on_control_pushes_terminal_control_to_every_control_socket(
        tmp_path, monkeypatch):
    app = _control_app(tmp_path, monkeypatch)
    entry = _app_entry(app, 46)
    page_a, page_a2, page_b = FakeWS(), FakeWS(), FakeWS()
    app.ctx.control_clients = {"a": {page_a, page_a2}, "b": {page_b}}
    _app_claim(app, entry, "phone")
    claimed = entry.control_view()

    async def release():
        app.ctx.registry.force_release(entry)
        await app.ctx.registry.drain_control()
    asyncio.run(release())
    freed = entry.control_view()
    for sock in (page_a, page_a2, page_b):
        assert [json.loads(m) for m in sock.sent] == [
            dict({"type": "terminal_control", "id": 46}, **claimed),
            dict({"type": "terminal_control", "id": 46}, **freed)]


def test_sessions_and_mcp_rows_report_the_controller(tmp_path, monkeypatch):
    app = _control_app(tmp_path, monkeypatch)
    entry = _app_entry(app, 47)
    mcp = {"Authorization": f"Bearer {MCP_TOKEN}"}

    def rows():
        _, sessions = authed(app).get("/sessions")
        _, terms = app.test_client.get("/mcp/terminals", headers=mcp)
        return (next(s for s in sessions.json if s["id"] == 47),
                next(t for t in terms.json if t["id"] == 47))
    session, term = rows()
    assert session["controller"] is None and term["controlled"] is False
    _app_claim(app, entry, "phone")
    session, term = rows()
    assert session["controller"] == {"label": "phone",
                                     "since": entry.controller.since,
                                     "gen": entry.controller.gen}
    assert term["controlled"] is True


def test_wiki_documents_the_contract():
    wiki = REPO / "wiki"
    tr = (wiki / "Technical-Reference.md").read_text(encoding="utf-8")
    assert "### Terminal control (#238)" in tr
    assert '`"terminal_control": 1`' in tr
    assert "**`POST /session/control`**" in tr
    assert "**stricter than `PUT /state`**" in tr
    assert "clients may order\nviews by it" in tr
    assert '"controlled":false' in tr
    hosts = (wiki / "Hosts-and-Multi-Browser.md").read_text(encoding="utf-8")
    assert ("One terminal can also be driven from outside without taking the "
            "lease at all") in hosts
    assert "[[Technical-Reference#terminal-control-238]]" in hosts


def test_mcp_input_still_reaches_a_controlled_terminal(tmp_path, monkeypatch):
    # Owner decision 1: MCP is not a viewer; its input bypasses the gate.
    app = _control_app(tmp_path, monkeypatch)
    entry = _app_entry(app, 48)
    _app_claim(app, entry, "phone")
    _, resp = app.test_client.post(
        "/mcp/input", json={"id": 48, "data": "from-mcp"},
        headers={"Authorization": f"Bearer {MCP_TOKEN}"})
    assert resp.status == 200
    assert {"type": "input", "data": "from-mcp"} in [
        json.loads(m) for m in entry.ws.sent]
