"""The #244 broker desktop relay: the ``allow_desktop`` flag (this section,
C1) and the ``GET/POST /mcp/desktop`` relay over the lease holder's
``/control`` socket (C2). Relay cells that need a real ``/control`` socket
run a real broker subprocess on a spare port and dial it with the
``websockets`` library, reusing ``_until``/``_lease`` from
tests/test_terminal_control.py verbatim (the same provenance that file's own
subprocess fixture carries from tests/test_broker_e2e.py's ``broker_proc``);
HTTP-only cells stay in-process.
"""

from __future__ import annotations

import json
from pathlib import Path

import webterm.broker.app as app_mod
from webterm.protocol import MCP_ADMIN_HEADER

from .auth_helpers import TEST_TOKEN, authed

MCP_TOKEN = "scope-store-token"
ADMIN_TOKEN = "admin-token-0123456789abcdef"
_app_seq = 0


def _make_app(tmp_path, monkeypatch, *, admin=None, **extra):
    global _app_seq
    _app_seq += 1
    monkeypatch.delenv("WEB_TERMINAL_TOKEN", raising=False)
    monkeypatch.delenv("WEB_TERMINAL_MCP_TOKEN", raising=False)
    monkeypatch.delenv("WEB_TERMINAL_MCP_ADMIN_TOKEN", raising=False)
    cfg = {
        "state_path": str(tmp_path / "webterm_state.json"),
        "mcp_state_path": str(tmp_path / "webterm_mcp.json"),
        "auth_token": TEST_TOKEN,
        "mcp_enabled": True,
        "mcp_token": MCP_TOKEN,
        "mcp_default_mode": "off",
    }
    if admin is not None:
        cfg["mcp_admin_token"] = admin
    cfg.update(extra)
    _app_seq += 0
    app = app_mod.create_app(cfg, name=f"webterm-mcp-desktop-{_app_seq}")
    return app


def _mcp(app, path, body=None, *, admin=None, method="POST"):
    headers = [("Authorization", f"Bearer {MCP_TOKEN}")]
    if admin is not None:
        headers.append((MCP_ADMIN_HEADER, admin))
    if method == "GET":
        return app.test_client.get(path, headers=headers)
    return app.test_client.post(path, json=body if body is not None else {},
                                headers=headers)


def _sidecar(tmp_path) -> Path:
    return tmp_path / "webterm_mcp.json"


# ---- C1: the allow_desktop flag ----------------------------------------------


def test_allow_desktop_defaults_off_and_info_advertises_it(tmp_path,
                                                            monkeypatch):
    """#244: the relay is off out of the box; /mcp/info answers the flag so a
    client can fail closed (a broker predating the flag answers without the
    key, which a client must read as off)."""
    app = _make_app(tmp_path, monkeypatch)
    assert app.ctx.mcp_cfg["allow_desktop"] is False
    _, resp = _mcp(app, "/mcp/info", method="GET")
    assert resp.status == 200
    assert resp.json["allow_desktop"] is False


def test_config_post_sets_allow_desktop(tmp_path, monkeypatch):
    """#244: the Control Panel checkbox rides POST /mcp/config (browser
    realm) and the value is durable — the sidecar carries it."""
    app = _make_app(tmp_path, monkeypatch)
    _, resp = authed(app).post("/mcp/config", json={"allow_desktop": True})
    assert resp.status == 200
    assert resp.json["allow_desktop"] is True
    assert app.ctx.mcp_cfg["allow_desktop"] is True
    assert json.loads(_sidecar(tmp_path).read_text(
        encoding="utf-8"))["allow_desktop"] is True

    # durability: a fresh broker on the same sidecar keeps it
    again = _make_app(tmp_path, monkeypatch)
    assert again.ctx.mcp_cfg["allow_desktop"] is True


def test_knobs_admin_sets_allow_desktop(tmp_path, monkeypatch):
    """#244: allow_desktop joined the admin knob route (strict booleans, the
    same bad_value as allow_launch) and the flag reaches /mcp/info."""
    app = _make_app(tmp_path, monkeypatch, admin=ADMIN_TOKEN,
                    mcp_allow_desktop=True)
    _, resp = _mcp(app, "/mcp/knobs", {"allow_desktop": False},
                   admin=ADMIN_TOKEN)
    assert (resp.status, resp.json["allow_desktop"]) == (200, False)
    assert app.ctx.mcp_cfg["allow_desktop"] is False
    _, info = _mcp(app, "/mcp/info", method="GET")
    assert info.json["allow_desktop"] is False

    _, resp = _mcp(app, "/mcp/knobs", {"allow_desktop": "on"},
                   admin=ADMIN_TOKEN)
    assert (resp.status, resp.json["error"]) == (400, "bad_value")
