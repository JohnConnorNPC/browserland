"""Foreground-agent plumbing through the broker registry: a producer 'agent'
frame must update entry.agent (whitelisted), surface in summary(), and
re-broadcast to attached browsers. The hello's optional 'agent' field seeds
it; junk values collapse to "".

It also pins the registry's per-window MCP facts (#227): ``mcp_scope`` in
summary() and the ``on_register`` hook's contract (args, ordering, lock,
failure handling)."""

from __future__ import annotations

import asyncio
import gc
import json
import logging
import warnings

from webterm.broker.registry import (BrokerRegistry, _whitelist_agent,
                                      run_producer_session)


class FeedWS:
    """Producer WS whose recv() is fed frames on demand; feeding None ends the
    session loop."""

    def __init__(self):
        self._q: asyncio.Queue = asyncio.Queue()
        self.sent = []

    async def recv(self):
        return await self._q.get()

    async def send(self, payload):
        self.sent.append(payload)

    async def close(self, *a, **k):
        pass

    def feed(self, frame):
        self._q.put_nowait(frame)


class CaptureWS:
    """Attached browser that records what the broker broadcasts to it."""

    def __init__(self):
        self.sent = []

    async def send(self, payload):
        self.sent.append(payload)

    async def close(self, *a, **k):
        pass


async def _wait(pred, tries=200):
    for _ in range(tries):
        if pred():
            return True
        await asyncio.sleep(0.005)
    return False


def _hello(window_id, pid=1):
    return {"type": "hello", "window_id": window_id, "pid": pid, "title": "t",
            "cols": 80, "rows": 24, "kind": "agent"}


def _hook_records(caplog, window_id, text):
    """ERROR records from the registry logger that name ``window_id`` and carry
    ``text`` — the INFO 'registered'/'replacing' lines name the id too, so the
    level and text filters are what make this specific."""
    return [r for r in caplog.records
            if r.name == "webterm.broker.registry"
            and r.levelno == logging.ERROR
            and "on_register" in r.getMessage()
            and text in r.getMessage()
            and str(window_id) in r.getMessage()]


def test_agent_frame_updates_entry_summary_and_broadcasts():
    async def scenario():
        reg = BrokerRegistry()
        ws = FeedWS()
        ws.feed(json.dumps({"type": "hello", "window_id": 1, "pid": 5,
                            "title": "t", "cols": 80, "rows": 24,
                            "kind": "agent"}))
        task = asyncio.create_task(run_producer_session(ws, reg))
        assert await _wait(lambda: reg.get(1) is not None)
        entry = reg.get(1)
        assert entry.agent == ""               # nothing in hello
        assert entry.summary()["agent"] == ""

        sub = CaptureWS()
        entry.add_subscriber(sub)

        # A real 'agent' frame: entry updates + browser gets a live push.
        ws.feed(json.dumps({"type": "agent", "data": "codex"}))
        assert await _wait(lambda: entry.agent == "codex")
        assert entry.summary()["agent"] == "codex"
        assert any(json.loads(s) == {"type": "agent", "data": "codex"}
                   for s in sub.sent)

        # Junk collapses to "" (and is broadcast as such).
        ws.feed(json.dumps({"type": "agent", "data": "pwned; rm -rf"}))
        assert await _wait(lambda: entry.agent == "")

        ws.feed(None)
        await asyncio.wait_for(task, 5)

    asyncio.run(scenario())


def test_mode_frame_caches_app_cursor_without_broadcast():
    """A 'mode' frame caches DECCKM on the entry (for send_keys, via
    /mcp/terminals) without broadcasting — browsers track their own DECCKM (#23)."""
    async def scenario():
        reg = BrokerRegistry()
        ws = FeedWS()
        ws.feed(json.dumps({"type": "hello", "window_id": 1, "pid": 5,
                            "title": "t", "cols": 80, "rows": 24,
                            "kind": "agent"}))
        task = asyncio.create_task(run_producer_session(ws, reg))
        assert await _wait(lambda: reg.get(1) is not None)
        entry = reg.get(1)
        assert entry.app_cursor is False
        assert entry.summary()["app_cursor"] is False

        sub = CaptureWS()
        entry.add_subscriber(sub)

        ws.feed(json.dumps({"type": "mode", "app_cursor": True}))
        assert await _wait(lambda: entry.app_cursor is True)
        assert entry.summary()["app_cursor"] is True
        assert sub.sent == []                  # not pushed to browsers

        ws.feed(json.dumps({"type": "mode", "app_cursor": False}))
        assert await _wait(lambda: entry.app_cursor is False)

        ws.feed(None)
        await asyncio.wait_for(task, 5)

    asyncio.run(scenario())


def test_hello_agent_field_seeds_and_whitelists():
    async def scenario():
        reg = BrokerRegistry()
        ws = FeedWS()
        entry = await reg.register(ws, {
            "type": "hello", "window_id": 3, "pid": 1, "title": "t",
            "cols": 80, "rows": 24, "agent": "grok"})
        assert entry.agent == "grok"
        assert entry.summary()["agent"] == "grok"
        # Hostile/buggy value never sticks.
        entry2 = await reg.register(ws, {
            "type": "hello", "window_id": 4, "pid": 1, "title": "t",
            "cols": 80, "rows": 24, "agent": "../../etc/passwd"})
        assert entry2.agent == ""
        # Absent field -> "" (non-agent producers / old agents).
        entry3 = await reg.register(ws, {
            "type": "hello", "window_id": 5, "pid": 1, "title": "t",
            "cols": 80, "rows": 24})
        assert entry3.agent == ""

    asyncio.run(scenario())


def test_hello_version_field_seeds_summary():
    """The hello's optional 'version' (build id) seeds entry.version + summary,
    and is absent ('') for a pre-#22 agent — itself a staleness signal (#22)."""
    async def scenario():
        reg = BrokerRegistry()
        ws = FeedWS()
        entry = await reg.register(ws, {
            "type": "hello", "window_id": 7, "pid": 1, "title": "t",
            "cols": 80, "rows": 24, "version": "0.8.0+abc"})
        assert entry.version == "0.8.0+abc"
        assert entry.summary()["version"] == "0.8.0+abc"
        entry2 = await reg.register(ws, {
            "type": "hello", "window_id": 8, "pid": 1, "title": "t",
            "cols": 80, "rows": 24})
        assert entry2.version == ""
        assert entry2.summary()["version"] == ""

    asyncio.run(scenario())


def test_hello_pyte_field_seeds_summary():
    """#134: the hello's optional 'pyte' seeds entry.pyte + summary(); absence
    defaults True — a pre-#134 agent predates the signal, and assuming it is
    pyte-less would raise a false 'degraded' alarm. An explicit False sticks (the
    agent's read_screen uses the dependency-free textgrid fallback)."""
    async def scenario():
        reg = BrokerRegistry()
        ws = FeedWS()
        # Explicit False -> pyte-less.
        entry = await reg.register(ws, {
            "type": "hello", "window_id": 21, "pid": 1, "title": "t",
            "cols": 80, "rows": 24, "pyte": False})
        assert entry.pyte is False
        assert entry.summary()["pyte"] is False
        # Absent -> default True (older agent / non-signal producer).
        entry2 = await reg.register(ws, {
            "type": "hello", "window_id": 22, "pid": 1, "title": "t",
            "cols": 80, "rows": 24})
        assert entry2.pyte is True
        assert entry2.summary()["pyte"] is True
        # Explicit True.
        entry3 = await reg.register(ws, {
            "type": "hello", "window_id": 23, "pid": 1, "title": "t",
            "cols": 80, "rows": 24, "pyte": True})
        assert entry3.pyte is True
        assert entry3.summary()["pyte"] is True

    asyncio.run(scenario())


def test_hello_profile_field_seeds_summary():
    """The hello's optional 'profile' (launch-profile name) seeds entry.profile +
    summary(), and is absent ('') for a non-launcher / old producer (#115). This
    is what survives a broker restart: the detached agent re-announces its
    profile on reconnect, so /sessions re-reports it deterministically."""
    async def scenario():
        reg = BrokerRegistry()
        ws = FeedWS()
        entry = await reg.register(ws, {
            "type": "hello", "window_id": 9, "pid": 1, "title": "t",
            "cols": 80, "rows": 24, "profile": "prod-ssh"})
        assert entry.profile == "prod-ssh"
        assert entry.summary()["profile"] == "prod-ssh"
        entry2 = await reg.register(ws, {
            "type": "hello", "window_id": 10, "pid": 1, "title": "t",
            "cols": 80, "rows": 24})
        assert entry2.profile == ""
        assert entry2.summary()["profile"] == ""

    asyncio.run(scenario())


def test_flush_input_done_reply_resolves_pending_rpc():
    """#133: a producer 'flush_input_done' reply must be on the management-RPC
    allow-list so run_producer_session routes it to resolve_rpc — the broker half
    of the /mcp/flush round-trip. A req that matches a pending flush RPC resolves
    its Future; the same reply for an unknown req is dropped, never crashing."""
    async def scenario():
        reg = BrokerRegistry()
        ws = FeedWS()
        ws.feed(json.dumps({"type": "hello", "window_id": 1, "pid": 5,
                            "title": "t", "cols": 80, "rows": 24,
                            "kind": "agent"}))
        task = asyncio.create_task(run_producer_session(ws, reg))
        assert await _wait(lambda: reg.get(1) is not None)
        entry = reg.get(1)

        # Park a flush RPC (as /mcp/flush does) and feed the matching reply.
        allocated = entry.new_rpc("flush_input_done")
        assert allocated is not None
        req, future = allocated
        ws.feed(json.dumps({"type": "flush_input_done", "req": req, "ok": True}))
        payload = await asyncio.wait_for(future, 5)
        assert payload["ok"] is True
        assert req not in entry.pending_rpc          # resolved + cleared

        # A reply for a stale/unknown req is dropped without error.
        ws.feed(json.dumps({"type": "flush_input_done", "req": 999, "ok": True}))
        await asyncio.sleep(0.05)                    # let the loop process it

        ws.feed(None)
        await asyncio.wait_for(task, 5)

    asyncio.run(scenario())


def test_summary_includes_pace_ms_default_zero():
    """#133: WindowEntry.summary() carries pace_ms (the per-terminal default
    send_keys pacing), defaulting to 0 (single-burst). It is EPHEMERAL
    per-connection like app_cursor/mcp_mode — set directly, not via a hello."""
    async def scenario():
        reg = BrokerRegistry()
        ws = FeedWS()
        entry = await reg.register(ws, {
            "type": "hello", "window_id": 12, "pid": 1, "title": "t",
            "cols": 80, "rows": 24, "kind": "agent"})
        assert entry.pace_ms == 0
        assert entry.summary()["pace_ms"] == 0
        # A set (as POST /mcp/pace does) surfaces in the next summary.
        entry.pace_ms = 60
        assert entry.summary()["pace_ms"] == 60

    asyncio.run(scenario())


def test_summary_carries_mcp_scope_raw():
    """#227: summary() carries mcp_scope as a bare str|None — None (unscoped) by
    default, the set value when set, and NEVER defaulted from mcp_default the
    way the effective ``mcp`` mode is. ``mcp`` and ``pace_ms`` are unchanged."""
    async def scenario():
        reg = BrokerRegistry()
        ws = FeedWS()
        entry = await reg.register(ws, {
            "type": "hello", "window_id": 13, "pid": 1, "title": "t",
            "cols": 80, "rows": 24, "kind": "agent"})
        s = entry.summary()
        assert "mcp_scope" in s
        assert s["mcp_scope"] is None
        # The broker default feeds ``mcp`` only, never the scope.
        s = entry.summary("readwrite")
        assert s["mcp_scope"] is None
        assert s["mcp"] == "readwrite"
        assert s["pace_ms"] == 0

        entry.mcp_scope = "teamA"
        entry.mcp_mode = "read"
        s = entry.summary("readwrite")
        assert s["mcp_scope"] == "teamA"
        assert s["mcp"] == "read"                # the override still wins
        assert s["pace_ms"] == 0

    asyncio.run(scenario())


def test_on_register_gets_old_entry_or_none():
    """#227: the hook is called as (new_entry, old) — old is None on a fresh
    register and the replaced entry (by identity) on a same-id re-register."""
    async def scenario():
        reg = BrokerRegistry()
        calls = []
        reg.on_register = lambda new, old: calls.append((new, old))
        first = await reg.register(FeedWS(), _hello(31))
        assert len(calls) == 1
        assert calls[0][0] is first
        assert calls[0][1] is None
        second = await reg.register(FeedWS(), _hello(31))
        assert second is not first
        assert len(calls) == 2
        assert calls[1][0] is second
        assert calls[1][1] is first

    asyncio.run(scenario())


def test_on_register_runs_before_the_entry_is_visible():
    """#227: the hook runs BEFORE insertion — a registry read from inside it
    sees the old entry (or nothing), never the entry being registered."""
    async def scenario():
        reg = BrokerRegistry()
        seen = []
        reg.on_register = lambda new, old: seen.append((reg.get(32), 32 in reg))
        first = await reg.register(FeedWS(), _hello(32))
        assert seen[0] == (None, False)
        second = await reg.register(FeedWS(), _hello(32))
        assert seen[1][0] is first
        assert seen[1][0] is not second
        assert reg.get(32) is second             # visible once register returns

    asyncio.run(scenario())


def test_on_register_runs_under_the_registry_lock():
    """#227: the hook runs INSIDE the registry lock (the contract that makes
    'must not take any other lock' matter), on fresh and same-id registers."""
    async def scenario():
        reg = BrokerRegistry()
        locked = []
        reg.on_register = lambda new, old: locked.append(reg._lock.locked())
        await reg.register(FeedWS(), _hello(33))
        await reg.register(FeedWS(), _hello(33))
        assert locked == [True, True]
        assert not reg._lock.locked()

    asyncio.run(scenario())


def test_raising_on_register_hook_is_harmless(caplog):
    """#227: a hook that raises never fails registration — the entry is still
    inserted and visible, and the failure is logged (with its traceback) under
    the window id."""
    caplog.set_level(logging.DEBUG, logger="webterm.broker.registry")

    def hook(new, old):
        raise RuntimeError("hook boom")

    async def scenario():
        reg = BrokerRegistry()
        reg.on_register = hook
        entry = await reg.register(FeedWS(), _hello(34))
        assert reg.get(34) is entry
        again = await reg.register(FeedWS(), _hello(34))
        assert reg.get(34) is again

    asyncio.run(scenario())
    records = _hook_records(caplog, 34, "failed")
    assert len(records) == 2                     # one per register
    for record in records:
        assert record.exc_info is not None
        assert record.exc_info[0] is RuntimeError


def test_async_on_register_hook_is_closed_and_logged(caplog):
    """#227: an accidentally ``async def`` hook returns a coroutine without
    running its body. register() must not await it (the hook is sync by
    contract): it closes the coroutine — so no 'never awaited' RuntimeWarning
    escapes — logs an ERROR under the window id, and still registers."""
    caplog.set_level(logging.DEBUG, logger="webterm.broker.registry")
    ran = []

    async def hook(new, old):
        ran.append(new)

    async def scenario():
        reg = BrokerRegistry()
        reg.on_register = hook
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            entry = await reg.register(FeedWS(), _hello(35))
            gc.collect()
        assert reg.get(35) is entry
        return caught

    caught = asyncio.run(scenario())
    assert ran == []                             # the body never ran
    assert not [w for w in caught
                if issubclass(w.category, RuntimeWarning)
                and "never awaited" in str(w.message)]
    records = _hook_records(caplog, 35, "coroutine")
    assert len(records) == 1


def test_whitelist_agent_helper():
    assert _whitelist_agent("claude") == "claude"
    assert _whitelist_agent("GROK") == "grok"
    assert _whitelist_agent("  codex  ") == "codex"
    assert _whitelist_agent("hermes") == "hermes"     # #156
    assert _whitelist_agent("vim") == ""
    assert _whitelist_agent("") == ""
    assert _whitelist_agent(None) == ""


def test_hermes_agent_frame_survives_the_whitelist():
    """#156: a hermes session must be attributable end to end — the label
    survives _whitelist_agent, lands on the entry, shows in summary() (the
    /sessions poll shape), and is re-broadcast to attached browsers."""
    async def scenario():
        reg = BrokerRegistry()
        ws = FeedWS()
        ws.feed(json.dumps({"type": "hello", "window_id": 1, "pid": 5,
                            "title": "t", "cols": 80, "rows": 24,
                            "kind": "agent"}))
        task = asyncio.create_task(run_producer_session(ws, reg))
        assert await _wait(lambda: reg.get(1) is not None)
        entry = reg.get(1)
        sub = CaptureWS()
        entry.add_subscriber(sub)

        ws.feed(json.dumps({"type": "agent", "data": "hermes"}))
        # Wait on the BROADCAST, not on entry.agent: the entry is written first
        # and the broadcast awaited after, so waiting on the entry could observe
        # the update before the push and read sub.sent one scheduling slot early.
        assert await _wait(lambda: any(
            json.loads(s) == {"type": "agent", "data": "hermes"}
            for s in sub.sent))
        assert entry.agent == "hermes"
        assert entry.summary()["agent"] == "hermes"

        ws.feed(None)
        await asyncio.wait_for(task, 5)

    asyncio.run(scenario())


def test_agent_whitelists_do_not_drift():
    """#156: the whitelist is duplicated in two packages on purpose (the broker
    must not import the agent package for one constant), so pin them equal here
    — a name added to only one side silently collapses to "" on the wire.

    MEMBERSHIP is the invariant, so compare as sets: the order inside either
    tuple carries no meaning and a harmless reorder must not fail the suite."""
    from webterm.agent import detect
    from webterm.broker import registry

    assert set(detect._AGENTS) == set(registry._AGENTS), (
        "agent whitelists drifted: webterm/agent/detect.py has "
        f"{detect._AGENTS}, webterm/broker/registry.py has {registry._AGENTS}")


def test_exit_frame_broadcasts_and_deregisters_immediately():
    """A producer 'exit' frame (child PTY EOF) must push an exit event to every
    attached browser AND deregister the session at once — so the next /sessions
    poll already omits it, instead of the browser waiting out the poll grace
    cycle. Issue #1 (slow session-exit detection)."""
    async def scenario():
        reg = BrokerRegistry()
        ws = FeedWS()
        ws.feed(json.dumps({"type": "hello", "window_id": 1, "pid": 5,
                            "title": "t", "cols": 80, "rows": 24,
                            "kind": "agent"}))
        task = asyncio.create_task(run_producer_session(ws, reg))
        assert await _wait(lambda: reg.get(1) is not None)
        entry = reg.get(1)

        sub = CaptureWS()
        entry.add_subscriber(sub)

        # Child exits: the broker forwards the exit event and drops the session.
        ws.feed(json.dumps({"type": "exit", "code": 0}))
        assert await _wait(lambda: reg.get(1) is None)
        assert any(json.loads(s) == {"type": "exit", "code": 0}
                   for s in sub.sent)
        # The session loop ends on its own after the exit frame (no None feed).
        await asyncio.wait_for(task, 5)

    asyncio.run(scenario())


def test_exit_frame_garbled_code_defaults_to_zero():
    """A missing/garbled exit code never breaks teardown — it maps to 0 and the
    browser still gets a well-formed exit frame."""
    async def scenario():
        reg = BrokerRegistry()
        ws = FeedWS()
        ws.feed(json.dumps({"type": "hello", "window_id": 2, "pid": 5,
                            "title": "t", "cols": 80, "rows": 24}))
        task = asyncio.create_task(run_producer_session(ws, reg))
        assert await _wait(lambda: reg.get(2) is not None)
        sub = CaptureWS()
        reg.get(2).add_subscriber(sub)

        ws.feed(json.dumps({"type": "exit", "code": "boom"}))
        assert await _wait(lambda: reg.get(2) is None)
        assert any(json.loads(s) == {"type": "exit", "code": 0}
                   for s in sub.sent)
        await asyncio.wait_for(task, 5)

    asyncio.run(scenario())
