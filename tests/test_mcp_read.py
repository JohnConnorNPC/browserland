"""Broker POST /mcp/read producer-payload mapping (#133) — the idle_ms relay.

In-process create_app + the Sanic test client (same pattern as
test_mcp_flush), with a fake producer WindowEntry injected into the registry
that answers ``screen_text_please`` with a canned payload. Covers the additive
idle_ms field: a CURRENT agent's payload carries it (relayed verbatim), an OLDER
agent's payload omits it (the broker must omit the key too — unknown, not a
misleading 0).
"""

from __future__ import annotations

import json

from webterm.broker.app import create_app
from webterm.broker.registry import WindowEntry

MCP_TOKEN = "read-http-token"
_app_seq = 0


def _make_app(tmp_path, monkeypatch, default_mode="readwrite"):
    """A broker app with MCP enabled + a known token, its state/sidecar under
    tmp_path (so no repo files are touched), and a UNIQUE Sanic name."""
    global _app_seq
    _app_seq += 1
    # Env would override config; clear both so the cfg token/enable are honored.
    monkeypatch.delenv("WEB_TERMINAL_TOKEN", raising=False)
    monkeypatch.delenv("WEB_TERMINAL_MCP_TOKEN", raising=False)
    cfg = {
        "state_path": str(tmp_path / "webterm_state.json"),
        "mcp_state_path": str(tmp_path / "webterm_mcp.json"),
        "mcp_enabled": True,
        "mcp_token": MCP_TOKEN,
        "mcp_default_mode": default_mode,
    }
    return create_app(cfg, name=f"webterm-read-http-{_app_seq}")


class _ReadWS:
    """Producer WS double: answers screen_text_please with a canned screen_text
    payload (``extra`` overlays fields like idle_ms, so we can model a current vs
    an older agent)."""

    def __init__(self, extra=None):
        self.extra = extra or {}
        self.entry = None
        self.sent = []

    async def send(self, text):
        self.sent.append(text)
        data = json.loads(text)
        if data.get("type") != "screen_text_please":
            return
        req = data["req"]
        reply = {"type": "screen_text", "req": req, "cols": 80, "rows": 24,
                 "text": "hi", "content_hash": "abc"}
        reply.update(self.extra)
        self.entry.resolve_rpc(req, "screen_text", reply)

    async def close(self, *a, **k):
        pass


def _register(app, wid, ws, mcp_mode=None):
    """Inject a fake producer entry into the live registry (constructed with no
    running loop, so its send-lock binds lazily to the request's loop)."""
    entry = WindowEntry(wid, 111, "t", 80, 24, ws, kind="agent")
    entry.mcp_mode = mcp_mode
    ws.entry = entry
    app.ctx.registry._entries[wid] = entry
    return entry


def _post_read(app, wid, token=MCP_TOKEN):
    return app.test_client.post(
        "/mcp/read", json={"id": wid},
        headers={"Authorization": f"Bearer {token}"})


def test_read_http_relays_idle_ms_when_present(tmp_path, monkeypatch):
    # A current agent stamps idle_ms; the broker relays it verbatim (coerced int).
    app = _make_app(tmp_path, monkeypatch)
    ws = _ReadWS(extra={"idle_ms": 42})
    _register(app, 5, ws)
    _, resp = _post_read(app, 5)
    assert resp.status == 200
    assert resp.json["idle_ms"] == 42


def test_read_http_omits_idle_ms_for_older_agent(tmp_path, monkeypatch):
    # An older agent's screen_text carries no idle_ms; the broker must OMIT the
    # key (unknown, not a misleading 0) rather than defaulting it.
    app = _make_app(tmp_path, monkeypatch)
    ws = _ReadWS(extra={})
    _register(app, 6, ws)
    _, resp = _post_read(app, 6)
    assert resp.status == 200
    assert "idle_ms" not in resp.json


def _sent_pleases(ws):
    """The screen_text_please frames the broker relayed to the producer."""
    out = []
    for s in ws.sent:
        d = json.loads(s)
        if d.get("type") == "screen_text_please":
            out.append(d)
    return out


def test_read_http_surfaces_stable_hash(tmp_path, monkeypatch):
    # #135: stable_hash (the cursor-blind digest) rides the result verbatim, and
    # is ALWAYS present — empty string for an older agent that omits it (mirrors
    # content_hash), never absent.
    app = _make_app(tmp_path, monkeypatch)
    ws = _ReadWS(extra={"stable_hash": "cafef00d"})
    _register(app, 9, ws)
    _, resp = _post_read(app, 9)
    assert resp.status == 200
    assert resp.json["stable_hash"] == "cafef00d"
    ws2 = _ReadWS(extra={})                       # older agent: no stable_hash
    _register(app, 10, ws2)
    _, resp2 = _post_read(app, 10)
    assert resp2.json["stable_hash"] == ""        # present, defaulted, not absent


def test_read_http_wait_for_idle_parses_and_relays(tmp_path, monkeypatch):
    # #135: wait_for_idle alone parses and rides the producer request frame; the
    # agent's matched + stable_hash come back on the result.
    app = _make_app(tmp_path, monkeypatch)
    ws = _ReadWS(extra={"stable_hash": "beef", "matched": True})
    _register(app, 7, ws)
    _, resp = app.test_client.post(
        "/mcp/read",
        json={"id": 7, "wait_for_idle": 500, "timeout_ms": 2000},
        headers={"Authorization": f"Bearer {MCP_TOKEN}"})
    assert resp.status == 200
    pleases = _sent_pleases(ws)
    assert pleases and pleases[0]["wait_for_idle"] == 500
    assert resp.json["matched"] is True
    assert resp.json["stable_hash"] == "beef"


def test_read_http_wait_for_idle_clamped(tmp_path, monkeypatch):
    # #135: an over-cap wait_for_idle is clamped to MAX_MCP_WAIT_MS before it
    # rides the frame (so one settle wait can't hold a producer RPC slot forever).
    from webterm.broker.app import MAX_MCP_WAIT_MS
    app = _make_app(tmp_path, monkeypatch)
    ws = _ReadWS()
    _register(app, 11, ws)
    _, resp = app.test_client.post(
        "/mcp/read", json={"id": 11, "wait_for_idle": 10 ** 9},
        headers={"Authorization": f"Bearer {MCP_TOKEN}"})
    assert resp.status == 200
    assert _sent_pleases(ws)[0]["wait_for_idle"] == MAX_MCP_WAIT_MS


def test_read_http_wait_for_idle_conflicts_with_other_wait(tmp_path, monkeypatch):
    # #135: wait_for_idle is a DISTINCT wait signal, so combining it with another
    # wait mode is rejected up front (conflicting_wait 400) and never dispatched.
    app = _make_app(tmp_path, monkeypatch)
    ws = _ReadWS()
    _register(app, 8, ws)
    _, resp = app.test_client.post(
        "/mcp/read",
        json={"id": 8, "wait_for_idle": 500, "wait_for_change": "abc"},
        headers={"Authorization": f"Bearer {MCP_TOKEN}"})
    assert resp.status == 400
    assert resp.json["error"] == "conflicting_wait"
    assert _sent_pleases(ws) == []                # rejected before any round-trip
