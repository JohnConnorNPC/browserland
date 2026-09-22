"""Foreground-agent plumbing through the broker registry: a producer 'agent'
frame must update entry.agent (whitelisted), surface in summary(), and
re-broadcast to attached browsers. The hello's optional 'agent' field seeds
it; junk values collapse to "".

It also pins the registry's per-window MCP facts (#227): ``mcp_scope`` in
summary(), the ``on_register`` hook's contract (args, ordering, lock, failure
handling), and the host+pid-gated default carry-over of
``mcp_mode``/``mcp_scope`` across a same-id replacement when no hook is
installed."""

from __future__ import annotations

import asyncio
import gc
import json
import logging
import warnings

import pytest

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


class ClosingWS(CaptureWS):
    """Attached browser that also records the close codes it is sent."""

    def __init__(self):
        super().__init__()
        self.closed = []

    async def close(self, *a, code=None, **k):
        self.closed.append(code)


async def _wait(pred, tries=200):
    for _ in range(tries):
        if pred():
            return True
        await asyncio.sleep(0.005)
    return False


def _hello(window_id, pid=1, host=None):
    hello = {"type": "hello", "window_id": window_id, "pid": pid, "title": "t",
             "cols": 80, "rows": 24, "kind": "agent"}
    if host is not None:
        hello["host"] = host
    return hello


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
    per-connection like app_cursor — set directly, not via a hello. That a
    re-register resets it is pinned by test_replacement_does_not_carry_pace_ms."""
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
        entry = await reg.register(FeedWS(), _hello(13))
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


def test_on_register_gets_old_entry_or_none(caplog):
    """#227: the hook is called as (new_entry, old) — old is None on a fresh
    register and the replaced entry (by identity) on a same-id re-register. A
    well-behaved sync hook produces no on_register ERROR record at all."""
    caplog.set_level(logging.DEBUG, logger="webterm.broker.registry")

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
    assert _hook_records(caplog, 31, "") == []


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
    """#227: a hook that raises an Exception never fails registration — the
    entry is still inserted and visible, and the failure is logged (with its
    traceback) under the window id. A hook that raises before applying anything
    leaves both MCP facts at None: no fallback to the default carry-over, even
    on a same-host, same-pid replacement whose old entry had them set."""
    caplog.set_level(logging.DEBUG, logger="webterm.broker.registry")

    def hook(new, old):
        raise RuntimeError("hook boom")

    async def scenario():
        reg = BrokerRegistry()
        reg.on_register = hook
        entry = await reg.register(FeedWS(), _hello(34))
        assert reg.get(34) is entry
        entry.mcp_mode = "readwrite"
        entry.mcp_scope = "teamA"
        again = await reg.register(FeedWS(), _hello(34))
        assert reg.get(34) is again
        assert again.mcp_mode is None
        assert again.mcp_scope is None

    asyncio.run(scenario())
    records = _hook_records(caplog, 34, "failed")
    assert len(records) == 2                     # one per register
    for record in records:
        assert record.exc_info is not None
        assert record.exc_info[0] is RuntimeError


def test_base_exception_from_on_register_hook_propagates():
    """#227: only an Exception is swallowed. A BaseException from the hook
    (here KeyboardInterrupt) deliberately escapes register() before the
    insertion: nothing is registered and the registry lock is released."""
    def hook(new, old):
        raise KeyboardInterrupt

    async def scenario():
        reg = BrokerRegistry()
        reg.on_register = hook
        with pytest.raises(KeyboardInterrupt):
            await reg.register(FeedWS(), _hello(46))
        assert reg.get(46) is None
        assert not reg._lock.locked()

    asyncio.run(scenario())


def test_async_on_register_hook_is_closed_and_logged(caplog):
    """#227: an accidentally ``async def`` hook returns a coroutine without
    running its body. register() must not await it (the hook is sync by
    contract): it logs an ERROR under the window id, then closes the coroutine
    — so no 'never awaited' RuntimeWarning escapes — and still registers."""
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


def test_on_register_coroutine_whose_close_raises_is_harmless(caplog):
    """#227: the coroutine's close() runs inside the hook's try. A coroutine the
    hook itself started, and that ignores GeneratorExit, makes close() raise
    RuntimeError — that Exception must not fail the registration, and the
    "must be synchronous" diagnostic is logged BEFORE the close, so it
    survives the raise instead of being replaced by the generic failure."""
    caplog.set_level(logging.DEBUG, logger="webterm.broker.registry")
    state = {"stubborn": True}

    async def stubborn():
        while True:
            try:
                await asyncio.sleep(0)
            except GeneratorExit:
                if state["stubborn"]:
                    continue
                raise

    started = []

    def hook(new, old):
        coro = stubborn()
        coro.send(None)                  # started: suspended at its first await
        started.append(coro)
        return coro

    async def scenario():
        reg = BrokerRegistry()
        reg.on_register = hook
        entry = await reg.register(FeedWS(), _hello(37))
        assert reg.get(37) is entry

    asyncio.run(scenario())
    records = _hook_records(caplog, 37, "failed")
    assert len(records) == 1
    assert records[0].exc_info[0] is RuntimeError
    assert len(_hook_records(caplog, 37, "coroutine")) == 1
    # Let the coroutine finish so its finalizer has nothing left to raise.
    state["stubborn"] = False
    started[0].close()


@pytest.mark.parametrize("kind", ["future", "await-object"])
def test_on_register_other_awaitable_is_logged_not_closed(caplog, kind):
    """#227: a returned awaitable that is not a coroutine (a Future, or any
    object with __await__) is logged and ignored: the registry never awaits it,
    never calls a close() on it (a raising close is never reached), and does
    not claim a body 'never ran' — a Task's body may well run later."""
    caplog.set_level(logging.DEBUG, logger="webterm.broker.registry")
    closes = []
    returned = []

    class AwaitObject:
        def __await__(self):
            yield

        def close(self):
            closes.append(True)
            raise RuntimeError("close must not be called")

    def hook(new, old):
        if kind == "future":
            obj = asyncio.get_running_loop().create_future()
        else:
            obj = AwaitObject()
        returned.append(obj)
        return obj

    async def scenario():
        reg = BrokerRegistry()
        reg.on_register = hook
        entry = await reg.register(FeedWS(), _hello(38))
        assert reg.get(38) is entry
        if kind == "future":
            assert not returned[0].done()    # never awaited, never resolved

    asyncio.run(scenario())
    assert closes == []
    assert len(_hook_records(caplog, 38, "awaitable")) == 1
    assert _hook_records(caplog, 38, "coroutine") == []
    assert _hook_records(caplog, 38, "failed") == []


def test_on_register_hook_is_the_authority_on_a_fresh_register():
    """#227: what the hook applies is what gets registered. On a FRESH register
    (old is None) a hook that sets mcp_mode/mcp_scope makes both visible through
    reg.get and summary(); nothing after the hook resets them."""
    def hook(new, old):
        new.mcp_mode = "readwrite"
        new.mcp_scope = "teamA"

    async def scenario():
        reg = BrokerRegistry()
        reg.on_register = hook
        await reg.register(FeedWS(), _hello(39))
        entry = reg.get(39)
        assert entry.mcp_mode == "readwrite"
        assert entry.mcp_scope == "teamA"
        s = entry.summary("off")
        assert s["mcp"] == "readwrite"
        assert s["mcp_scope"] == "teamA"

    asyncio.run(scenario())


def test_on_register_sees_the_old_entry_still_live():
    """#227: the hook runs BEFORE the replaced entry is torn down — old still
    holds its pending RPC and its subscriber has not had the 1012 close yet;
    both happen after the hook, once the lock is released. (A 1012 close does
    not remove the subscriber from old.subscribers, so the close is observed on
    the socket itself.)"""
    async def scenario():
        reg = BrokerRegistry()
        first = await reg.register(FeedWS(), _hello(36))
        sub = ClosingWS()
        first.add_subscriber(sub)
        allocated = first.new_rpc("procs")
        assert allocated is not None
        _req, future = allocated
        seen = []
        reg.on_register = lambda new, old: seen.append(
            (len(old.pending_rpc), future.done(), list(sub.closed)))
        await reg.register(FeedWS(), _hello(36))
        assert seen == [(1, False, [])]
        # ...and the teardown did happen afterwards.
        assert first.pending_rpc == {}
        assert isinstance(future.exception(), ConnectionError)
        assert sub.closed == [1012]

    asyncio.run(scenario())


@pytest.mark.parametrize("field,value", [("mcp_mode", "readwrite"),
                                         ("mcp_scope", "teamA")])
def test_replacement_without_hook_carries_mcp_field(field, value):
    """#227: with no hook installed, a same-id re-register whose hello reports
    the same host and the same nonzero pid carries mcp_mode and mcp_scope from
    the replaced entry.
    Both fields are set on the old entry in every cell and each cell asserts
    only its own, so dropping either copy reds exactly that field's cell. The
    fresh-register arm is a sanity check only: the constructor defaults None."""
    async def scenario():
        reg = BrokerRegistry()
        first = await reg.register(FeedWS(), _hello(41, pid=77))
        assert getattr(first, field) is None
        first.mcp_mode = "readwrite"
        first.mcp_scope = "teamA"
        second = await reg.register(FeedWS(), _hello(41, pid=77))
        assert second is not first
        assert reg.get(41) is second
        assert getattr(second, field) == value

    asyncio.run(scenario())


def test_installed_hook_replaces_the_default_carry_over():
    """#227: with a hook installed there is NO default carry-over — the hook
    (the per-window store) is the authority, so a hook that applies nothing
    leaves a same-id, same-host, same-pid replacement at None for both
    fields."""
    async def scenario():
        reg = BrokerRegistry()
        reg.on_register = lambda new, old: None
        first = await reg.register(FeedWS(), _hello(42, pid=77))
        first.mcp_mode = "readwrite"
        first.mcp_scope = "teamA"
        second = await reg.register(FeedWS(), _hello(42, pid=77))
        assert second is not first
        assert second.mcp_mode is None
        assert second.mcp_scope is None

    asyncio.run(scenario())


def test_replacement_does_not_carry_pace_ms():
    """#227: pace_ms stays EPHEMERAL — a same-id, same-host, same-pid
    replacement (the path that carries mcp_mode/mcp_scope) still starts it
    at 0."""
    async def scenario():
        reg = BrokerRegistry()
        first = await reg.register(FeedWS(), _hello(43, pid=77))
        first.pace_ms = 60
        second = await reg.register(FeedWS(), _hello(43, pid=77))
        assert second is not first
        assert second.pace_ms == 0

    asyncio.run(scenario())


def test_replacement_with_the_same_explicit_host_carries():
    """#227: the host term is pinned in the positive too. Every other carry
    test lets both hellos fall back to the broker's own hostname; here both
    name the same NON-local host explicitly, and both fields still carry."""
    async def scenario():
        reg = BrokerRegistry()
        old = await reg.register(FeedWS(), _hello(45, pid=77, host="hostA"))
        old.mcp_mode = "readwrite"
        old.mcp_scope = "teamA"
        new = await reg.register(FeedWS(), _hello(45, pid=77, host="hostA"))
        assert new is not old
        assert new.host == "hostA"
        assert new.mcp_mode == "readwrite"
        assert new.mcp_scope == "teamA"

    asyncio.run(scenario())


@pytest.mark.parametrize("first,second", [
    ((77, "hostA"), (78, "hostA")),
    ((0, "hostA"), (0, "hostA")),
    ((77, "hostA"), (77, "hostB")),
    ((77, "HOSTA"), (77, "hostA")),
], ids=["pid-changed", "pid-unknown", "host-changed", "host-case"])
def test_replacement_without_a_matching_host_and_pid_does_not_carry(first,
                                                                    second):
    """#227: the default carry-over needs the SAME host and the SAME NONZERO pid.
    An agent pinned with --window-id relaunches under the same id with a new
    shell pid; two hosts pinning one id are different producers even with equal
    pids; two pid-less hellos (0 = unknown) are no evidence of one producer;
    the host match is byte-exact, so a case-only difference is a different
    host. None of them may inherit the old entry's mode/scope. A collision
    guard, not security: host and pid are self-reported."""
    async def scenario():
        reg = BrokerRegistry()
        old = await reg.register(FeedWS(),
                                 _hello(44, pid=first[0], host=first[1]))
        old.mcp_mode = "readwrite"
        old.mcp_scope = "teamA"
        new = await reg.register(FeedWS(),
                                 _hello(44, pid=second[0], host=second[1]))
        assert new is not old
        assert new.mcp_mode is None
        assert new.mcp_scope is None

    asyncio.run(scenario())


def test_half_open_reconnect_carry_survives_the_old_socket_closing():
    """#227 end to end through run_producer_session: a second hello (same id,
    host and pid) on a new socket while the first is still open replaces the entry
    and carries mcp_mode. When the first socket finally closes, its `finally`
    deregister must leave the NEW entry in place (deregister only removes the
    entry it registered)."""
    async def scenario():
        reg = BrokerRegistry()
        ws1 = FeedWS()
        ws1.feed(json.dumps(_hello(51, pid=77)))
        task1 = asyncio.create_task(run_producer_session(ws1, reg))
        assert await _wait(lambda: reg.get(51) is not None)
        first = reg.get(51)
        first.mcp_mode = "readwrite"

        ws2 = FeedWS()
        ws2.feed(json.dumps(_hello(51, pid=77)))
        task2 = asyncio.create_task(run_producer_session(ws2, reg))
        assert await _wait(lambda: reg.get(51) is not first)
        second = reg.get(51)
        assert second is not None                # the wait also passes on None
        assert second.mcp_mode == "readwrite"

        ws1.feed(None)                           # the stale socket closes
        await asyncio.wait_for(task1, 5)
        assert reg.get(51) is second
        assert reg.get(51).mcp_mode == "readwrite"

        ws2.feed(None)
        await asyncio.wait_for(task2, 5)
        assert reg.get(51) is None

    asyncio.run(scenario())


def test_reconnect_after_a_clean_close_starts_at_none():
    """#227 control: the carry-over is confined to the half-open window. A
    clean close deregisters the entry first, so the next hello on the same id
    and pid is a fresh register and comes back with mcp_mode/mcp_scope None."""
    async def scenario():
        reg = BrokerRegistry()
        ws1 = FeedWS()
        ws1.feed(json.dumps(_hello(52, pid=77)))
        task1 = asyncio.create_task(run_producer_session(ws1, reg))
        assert await _wait(lambda: reg.get(52) is not None)
        first = reg.get(52)
        first.mcp_mode = "readwrite"
        first.mcp_scope = "teamA"
        ws1.feed(None)
        await asyncio.wait_for(task1, 5)
        assert reg.get(52) is None

        ws2 = FeedWS()
        ws2.feed(json.dumps(_hello(52, pid=77)))
        task2 = asyncio.create_task(run_producer_session(ws2, reg))
        assert await _wait(lambda: reg.get(52) is not None)
        second = reg.get(52)
        assert second is not first
        assert second.mcp_mode is None
        assert second.mcp_scope is None

        ws2.feed(None)
        await asyncio.wait_for(task2, 5)

    asyncio.run(scenario())


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
