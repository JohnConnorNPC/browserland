"""Foreground-agent plumbing through the broker registry: a producer 'agent'
frame must update entry.agent (whitelisted), surface in summary(), and
re-broadcast to attached browsers. The hello's optional 'agent' field seeds
it; junk values collapse to ""."""

from __future__ import annotations

import asyncio
import json

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
            "cols": 80, "rows": 24, "version": "0.1.0+abc"})
        assert entry.version == "0.1.0+abc"
        assert entry.summary()["version"] == "0.1.0+abc"
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


def test_whitelist_agent_helper():
    assert _whitelist_agent("claude") == "claude"
    assert _whitelist_agent("GROK") == "grok"
    assert _whitelist_agent("  codex  ") == "codex"
    assert _whitelist_agent("vim") == ""
    assert _whitelist_agent("") == ""
    assert _whitelist_agent(None) == ""


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
