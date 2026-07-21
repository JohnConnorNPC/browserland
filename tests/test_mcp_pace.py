"""Broker POST /mcp/pace HTTP contract (#133) — set a terminal's DEFAULT
send_keys pacing (broker-local, no producer round-trip).

In-process create_app + the Sanic test client (same pattern as test_mcp_flush),
with a fake producer WindowEntry injected into the registry. Unlike /mcp/reset
and /mcp/flush there is NO correlated round-trip: /mcp/pace just stamps
entry.pace_ms and echoes it, and /mcp/terminals then surfaces it. Covers the
happy path + echo, the clamp (over-cap pins to the cap, negative -> 0),
read_only (403), a non-integer / missing value (400 bad_pace), and auth (401).
"""

from __future__ import annotations

from .auth_helpers import TEST_TOKEN
from webterm.broker.app import MAX_MCP_PACE_MS, create_app
from webterm.broker.registry import WindowEntry

MCP_TOKEN = "pace-http-token"
_app_seq = 0


# /mcp/* is its own realm (Authorization: Bearer <mcp_token>), NOT the
# browser auth_token — so these use the RAW client. authed() would append
# ?token=<browser token>, and provided_token() prefers the query string
# over the header, which would fail the MCP bearer check.
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
        "auth_token": TEST_TOKEN,
        "mcp_enabled": True,
        "mcp_token": MCP_TOKEN,
        "mcp_default_mode": default_mode,
    }
    return create_app(cfg, name=f"webterm-pace-http-{_app_seq}")


class _WS:
    """Producer WS double: /mcp/pace is broker-local and never sends to the
    producer, so this only needs to satisfy WindowEntry's send/close surface (and
    let us assert nothing WAS sent)."""

    def __init__(self):
        self.sent = []

    async def send(self, text):
        self.sent.append(text)

    async def close(self, *a, **k):
        pass


def _register(app, wid, ws, mcp_mode=None):
    """Inject a fake producer entry into the live registry (constructed with no
    running loop, so its send-lock binds lazily to the request's loop)."""
    entry = WindowEntry(wid, 111, "t", 80, 24, ws, kind="agent")
    entry.mcp_mode = mcp_mode
    app.ctx.registry._entries[wid] = entry
    return entry


def _post_pace(app, wid, pace_ms, token=MCP_TOKEN):
    return app.test_client.post(
        "/mcp/pace", json={"id": wid, "pace_ms": pace_ms},
        headers={"Authorization": f"Bearer {token}"})


def _get_terminals(app, token=MCP_TOKEN):
    return app.test_client.get(
        "/mcp/terminals", headers={"Authorization": f"Bearer {token}"})


def test_pace_http_sets_and_echoes(tmp_path, monkeypatch):
    app = _make_app(tmp_path, monkeypatch, default_mode="readwrite")
    ws = _WS()
    entry = _register(app, 5, ws)
    _, resp = _post_pace(app, 5, 40)
    assert resp.status == 200
    assert resp.json == {"ok": True, "id": 5, "pace_ms": 40}
    assert entry.pace_ms == 40           # stamped on the live entry
    assert ws.sent == []                 # broker-local: no producer round-trip


def test_pace_http_surfaces_in_terminals(tmp_path, monkeypatch):
    # The default is 0 in /mcp/terminals; after a set it reflects the new value.
    app = _make_app(tmp_path, monkeypatch, default_mode="readwrite")
    _register(app, 6, _WS())
    _, before = _get_terminals(app)
    assert next(t for t in before.json if t["id"] == 6)["pace_ms"] == 0
    _post_pace(app, 6, 75)
    _, after = _get_terminals(app)
    assert next(t for t in after.json if t["id"] == 6)["pace_ms"] == 75


def test_terminals_surfaces_pyte_flag(tmp_path, monkeypatch):
    # #134: /mcp/terminals surfaces the per-terminal `pyte` flag. Default True (a
    # WindowEntry constructed without a pyte-less hello); a pyte-less agent
    # surfaces as False so a client can warn that read_screen degraded.
    app = _make_app(tmp_path, monkeypatch, default_mode="readwrite")
    up = _register(app, 30, _WS())            # WindowEntry defaults pyte=True
    down = _register(app, 31, _WS())
    down.pyte = False                          # as a pyte-less hello would set it
    _, resp = _get_terminals(app)
    got = {t["id"]: t["pyte"] for t in resp.json}
    assert got[30] is True
    assert got[31] is False


def test_pace_http_clamps_over_cap_and_negative(tmp_path, monkeypatch):
    # An out-of-range integer is CLAMPED (not rejected): over-cap pins to the cap,
    # a negative disables (0). Both 200 — pins the exact implemented behavior.
    app = _make_app(tmp_path, monkeypatch, default_mode="readwrite")
    entry = _register(app, 7, _WS())
    _, over = _post_pace(app, 7, MAX_MCP_PACE_MS + 5000)
    assert over.status == 200
    assert over.json["pace_ms"] == MAX_MCP_PACE_MS
    assert entry.pace_ms == MAX_MCP_PACE_MS
    _, neg = _post_pace(app, 7, -5)
    assert neg.status == 200
    assert neg.json["pace_ms"] == 0
    assert entry.pace_ms == 0


def test_pace_http_read_only_returns_403(tmp_path, monkeypatch):
    # Effective mode 'read' can't set pace — it changes how writes are delivered.
    app = _make_app(tmp_path, monkeypatch, default_mode="read")
    entry = _register(app, 8, _WS())
    _, resp = _post_pace(app, 8, 40)
    assert resp.status == 403
    assert resp.json["error"] == "read_only"
    assert entry.pace_ms == 0            # unchanged by a rejected call


def test_pace_http_non_integer_returns_400_bad_pace(tmp_path, monkeypatch):
    # A non-numeric value is rejected (reject-if-not-int), UNLIKE an out-of-range
    # integer which clamps. Mirrors /mcp/*'s bad_id handling.
    app = _make_app(tmp_path, monkeypatch, default_mode="readwrite")
    entry = _register(app, 9, _WS())
    _, resp = _post_pace(app, 9, "fast")
    assert resp.status == 400
    assert resp.json["error"] == "bad_pace"
    assert entry.pace_ms == 0


def test_pace_http_missing_value_returns_400_bad_pace(tmp_path, monkeypatch):
    # A missing pace_ms -> int(None) -> 400 bad_pace (never a silent 0).
    app = _make_app(tmp_path, monkeypatch, default_mode="readwrite")
    entry = _register(app, 10, _WS())
    _, resp = app.test_client.post(
        "/mcp/pace", json={"id": 10},
        headers={"Authorization": f"Bearer {MCP_TOKEN}"})
    assert resp.status == 400
    assert resp.json["error"] == "bad_pace"
    assert entry.pace_ms == 0


def test_pace_http_requires_mcp_token(tmp_path, monkeypatch):
    # Wrong/missing MCP token is rejected before any state change (no loopback
    # exemption on the /mcp data plane).
    app = _make_app(tmp_path, monkeypatch, default_mode="readwrite")
    entry = _register(app, 11, _WS())
    _, resp = _post_pace(app, 11, 40, token="wrong-token")
    assert resp.status == 401
    assert resp.json["error"] == "auth_required"
    assert entry.pace_ms == 0
