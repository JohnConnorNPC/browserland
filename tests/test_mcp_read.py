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
