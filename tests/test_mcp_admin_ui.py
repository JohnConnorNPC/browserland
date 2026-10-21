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
    # #260 H3: the admin token field is read-only (only generate and clear
    # change it); the plain MCP token field stays editable.
    def _input_tag(el_id):
        at = INDEX_HTML.index(f'id="{el_id}"')
        tag = INDEX_HTML[INDEX_HTML.rindex("<input", 0, at):]
        return tag[:tag.index(">") + 1]
    assert "readonly" in _input_tag("set-mcp-admin-token")
    assert "readonly" not in _input_tag("set-mcp-token")
    # Review round 1 (N3): the checkbox carries its own hint line naming the
    # master-switch + admin-token rule.
    chunk = INDEX_HTML.split('id="set-mcp-allow-desktop"')[1][:600]
    assert "One master switch for the desktop relay;" in chunk
    assert "managing workspaces also needs the admin token." in chunk


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


# ---- #245: the taskbar admin badge ---------------------------------------- #


def _taskbar_src():
    return (BROKER_DIR / "75_js_taskbar_hosts.js").read_text(encoding="utf-8")


def test_admin_badge_merges_from_the_sessions_row():
    # The merge copies /sessions fields BY NAME; the flag is a strict === true
    # read so an absent (old broker) or garbage value never badges a chip.
    src = _taskbar_src()
    merge = src.split("merged.set(key, {")[1].split("});", 1)[0]
    assert "mcp_admin: s.mcp_admin === true," in merge


def test_admin_badge_lifecycle_matches_the_scope_badge():
    src = _taskbar_src()
    chip = src.split("function renderChipLabel")[1].split(
        "function updateTaskbarLabel")[0]
    # Created only while the flag is set, in place, ordered scope -> admin ->
    # ws, and removed on clear.
    assert "sess.mcp_admin === true" in chip
    assert "admin.className = 'ti-admin';" in chip
    assert "admin.textContent = 'admin';" in chip
    assert "const before = admin || ws;" in chip
    assert "if (ws) el.insertBefore(admin, ws);" in chip
    assert "const tail = scope || admin || ws;" in chip


def test_admin_badge_css_and_the_id_ellipsis_exception():
    css = (BROKER_DIR / "10_css_root.css").read_text(encoding="utf-8")
    assert ".taskbar-item .ti-admin {" in css
    assert "rgba(255,176,64,0.25)" in css
    # A chip can carry admin WITHOUT scope; the id must ellipsize then too.
    assert ".taskbar-item:has(> .ti-admin) .ti-id {" in css


def test_tooltip_names_the_admin_provenance():
    # The wording says what the datum IS (provenance), never "driving".
    src = (BROKER_DIR / "64_js_sessions_poll_control.js").read_text(
        encoding="utf-8")
    tip = src.split("function formatTooltip")[1].split(
        "const windows = new Map()")[0]
    assert "sess.mcp_admin === true" in tip
    assert "'Set by an MCP admin agent'" in tip
