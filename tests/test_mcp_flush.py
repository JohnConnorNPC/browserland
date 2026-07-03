"""Broker POST /mcp/flush HTTP contract (#133) — the input-side mirror of
/mcp/reset.

In-process create_app + the Sanic test client (same pattern as
test_broker_info), with a fake producer WindowEntry injected into the registry
that answers ``flush_input_please`` as an agent would. Covers the happy path
(200 + the correlated round-trip resolved through the registry), read_only
(403), no-producer (502), and agent-failure (502 flush_failed).
"""

from __future__ import annotations

import json

from webterm.broker.app import create_app
from webterm.broker.registry import WindowEntry

MCP_TOKEN = "flush-http-token"
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
    return create_app(cfg, name=f"webterm-flush-http-{_app_seq}")


class _FlushWS:
    """Producer WS double: answers flush_input_please as an agent would (or, with
    ``drop``, vanishes mid-RPC so the round-trip resolves as 'gone')."""

    def __init__(self, ok=True, drop=False):
        self.ok = ok
        self.drop = drop
        self.entry = None
        self.sent = []

    async def send(self, text):
        self.sent.append(text)
        data = json.loads(text)
        if data.get("type") != "flush_input_please":
            return
        req = data["req"]
        if self.drop:
            # Producer connection lost while the RPC was in flight.
            self.entry.fail_all_rpc(ConnectionError("producer gone"))
        else:
            self.entry.resolve_rpc(req, "flush_input_done",
                                   {"type": "flush_input_done", "req": req,
                                    "ok": self.ok})

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


def _post_flush(app, wid, token=MCP_TOKEN):
    return app.test_client.post(
        "/mcp/flush", json={"id": wid},
        headers={"Authorization": f"Bearer {token}"})


def test_flush_http_happy_path(tmp_path, monkeypatch):
    app = _make_app(tmp_path, monkeypatch, default_mode="readwrite")
    ws = _FlushWS(ok=True)
    _register(app, 5, ws)
    _, resp = _post_flush(app, 5)
    assert resp.status == 200
    assert resp.json == {"ok": True, "id": 5}
    # The correlated request actually reached the producer.
    assert any(json.loads(s).get("type") == "flush_input_please" for s in ws.sent)


def test_flush_http_read_only_returns_403(tmp_path, monkeypatch):
    # Effective mode 'read' (the broker default here) can't flush — that mutates.
    app = _make_app(tmp_path, monkeypatch, default_mode="read")
    ws = _FlushWS()
    _register(app, 6, ws)
    _, resp = _post_flush(app, 6)
    assert resp.status == 403
    assert resp.json["error"] == "read_only"
    assert ws.sent == []            # the mode gate precedes the producer RPC


def test_flush_http_no_producer_returns_502(tmp_path, monkeypatch):
    # A producer that drops mid-RPC -> _session_rpc 'gone' -> 502 no_producer_rpc
    # (the same status a non-agent producer's timeout yields).
    app = _make_app(tmp_path, monkeypatch, default_mode="readwrite")
    ws = _FlushWS(drop=True)
    _register(app, 7, ws)
    _, resp = _post_flush(app, 7)
    assert resp.status == 502
    assert resp.json["error"] == "no_producer_rpc"


def test_flush_http_agent_failure_returns_502_flush_failed(tmp_path, monkeypatch):
    # The agent answered but reported ok=False -> distinct 502 flush_failed.
    app = _make_app(tmp_path, monkeypatch, default_mode="readwrite")
    ws = _FlushWS(ok=False)
    _register(app, 8, ws)
    _, resp = _post_flush(app, 8)
    assert resp.status == 502
    assert resp.json["error"] == "flush_failed"


def test_flush_http_requires_mcp_token(tmp_path, monkeypatch):
    # Wrong/missing MCP token is rejected before any producer work (no loopback
    # exemption on the /mcp data plane).
    app = _make_app(tmp_path, monkeypatch, default_mode="readwrite")
    ws = _FlushWS()
    _register(app, 9, ws)
    _, resp = _post_flush(app, 9, token="wrong-token")
    assert resp.status == 401
    assert resp.json["error"] == "auth_required"
    assert ws.sent == []
