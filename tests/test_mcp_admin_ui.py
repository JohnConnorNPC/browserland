"""Static pins for the MCP desktop/admin Control Panel wiring (#244/#245).

The MCP applet's markup and the Control Panel's save/render wiring are DOM
behaviour node cannot observe (the applet is bespoke markup + per-host fetch
caching), so -- per the repo's established pattern -- the wiring is pinned by
source the way ``tests/test_ui_assets.py`` pins served-page symbols: the five
new element ids, the POST patches each control sends, the env-pin disabling,
and the Copy .mcp.json Admin gate. (The desktop relay's own behaviour is
covered by ``tests/test_desktop_rpc.py``; the workspaces commands by
``tests/test_workspaces_commands.py``.)
"""

import re
from pathlib import Path

from webterm.broker import ui
from webterm.broker.ui import INDEX_HTML

BROKER_DIR = Path(ui.__file__).resolve().parent


def _cp_src():
    return (BROKER_DIR / "81_js_control_panel.js").read_text(encoding="utf-8")


def test_mcp_applet_markup_wired_into_the_page():
    for el_id in (
        'id="set-mcp-allow-desktop"',    # #244 master switch
        'id="set-mcp-admin-token"',      # #245 admin credential field
        'id="set-mcp-admin-generate"',
        'id="set-mcp-admin-clear"',
        'id="set-mcp-copy-admin"',       # #245 Copy .mcp.json Admin checkbox
    ):
        assert el_id in INDEX_HTML, f"missing MCP applet element {el_id!r}"


def test_allow_desktop_roundtrips_through_mcp_config():
    src = _cp_src()
    # Render: the checkbox reflects the broker's flag (absent on an old
    # broker reads unchecked)...
    assert "setMcpAllowDesktop.checked = !!cfg.allow_desktop;" in src
    # ...and the change handler POSTs exactly that key.
    assert "saveMcpConfig({ allow_desktop: setMcpAllowDesktop.checked })" in src


def test_admin_token_generate_rotate_and_clear():
    src = _cp_src()
    # Generate = the broker mints fresh (over an existing token that IS the
    # rotate); clear is the explicit null, matching the /mcp/config contract.
    assert "saveMcpConfig({ admin_generate: true })" in src
    assert "saveMcpConfig({ admin_token: null })" in src


def test_env_pinned_admin_token_disables_the_controls():
    src = _cp_src()
    assert "cfg.admin_token_env_pinned" in src
    assert "setMcpAdminToken.disabled = adminPinned;" in src
    assert "setMcpAdminGenerate.disabled = adminPinned;" in src
    assert "setMcpAdminClear.disabled = adminPinned;" in src
    assert "'set by WEB_TERMINAL_MCP_ADMIN_TOKEN (env)'" in src


def test_copy_mcp_json_admin_gate_and_payload():
    src = _cp_src()
    # The checkbox is inert without an admin token to include...
    assert "setMcpCopyAdmin.disabled = !cfg.admin_token;" in src
    # ...the click refuses to build a file that would silently lack it...
    click = src.split("setMcpCopyJson.addEventListener('click'")[1].split(
        "});\n        }")[0]
    assert "setMcpCopyAdmin.checked" in click
    assert "no admin token on this broker" in click
    # ...and the host entry carries the admin credential only when ticked.
    assert "adminToken: wantAdmin ? cfg.admin_token : null," in click
    builder = src.split("function buildMcpJson")[1].split(
        "function mcpJsonHostName")[0]
    assert "if (o.adminToken) host.admin_token = o.adminToken;" in builder
