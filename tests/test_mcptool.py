"""Tests for the Browserland MCP server (webterm/mcptool/).

Skipped wholesale when the optional `mcp` SDK is missing — matches the repo's
platform-skip style (test_pyte_snap, test_conpty). The client tests need only
httpx (a transitive `mcp` dep, also installable on its own), but gating on `mcp`
keeps the module's skip condition single and obvious.
"""

from __future__ import annotations

import json

import pytest

pytest.importorskip("mcp")

import httpx

from webterm.mcptool import BrowserlandClient, BrowserlandError
from webterm.protocol import MCP_ADMIN_HEADER, SCOPE_HEADER


# ---- client unit tests (httpx.MockTransport, no real broker) -------------

def _client(handler, token="secret"):
    """A BrowserlandClient whose HTTP layer is a MockTransport running `handler`."""
    return BrowserlandClient(
        base="http://broker:4445", token=token,
        transport=httpx.MockTransport(handler),
    )


# ---- server-side install/reset of the per-host client map (#24) -----------
# The tools route on a namespaced "<host>:<int>" id, so tests inject a host map
# (name -> client) plus the matching _host_configs the router validates against.

def _mock_client(handler, base="http://broker:4445", token="t"):
    return BrowserlandClient(base=base, token=token,
                             transport=httpx.MockTransport(handler))


def _install(handlers):
    """Install one host per (name -> handler) entry, returning the server module.
    `handlers` order is preserved (it drives list_terminals merge order)."""
    from webterm.mcptool import server
    server._host_configs = {n: (f"http://{n}:4445", "t", None)
                            for n in handlers}
    server._clients = {n: _mock_client(h, base=f"http://{n}:4445")
                       for n, h in handlers.items()}
    return server


def _reset_server():
    from webterm.mcptool import server
    for c in server._clients.values():
        c.close()
    server._host_configs = {}
    server._clients = {}
    # Cross-config state the _install() injections bypass configure() for:
    # cleared here the way a real configure() would clear them.
    server._handles.clear()
    server._admin_hosts.clear()


@pytest.fixture(autouse=True)
def _no_scope_env(monkeypatch):
    """#232/#245: --scope defaults from $BROWSERLAND_MCP_SCOPE and the admin
    token from $BROWSERLAND_MCP_ADMIN_TOKEN / $WEB_TERMINAL_MCP_ADMIN_TOKEN,
    so a value in the developer's shell must not leak into any
    argument-parsing cell."""
    monkeypatch.delenv("BROWSERLAND_MCP_SCOPE", raising=False)
    monkeypatch.delenv("BROWSERLAND_MCP_ADMIN_TOKEN", raising=False)
    monkeypatch.delenv("WEB_TERMINAL_MCP_ADMIN_TOKEN", raising=False)


def test_info_get():
    seen = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["method"] = req.method
        seen["url"] = str(req.url)
        seen["auth"] = req.headers.get("authorization")
        return httpx.Response(200, json={"ok": True, "allow_launch": False,
                                         "default_mode": "off"})

    with _client(handler) as c:
        out = c.info()
    assert out == {"ok": True, "allow_launch": False, "default_mode": "off"}
    assert seen["method"] == "GET"
    assert seen["url"] == "http://broker:4445/mcp/info"
    assert seen["auth"] == "Bearer secret"


def test_info_surfaces_broker_version():
    """#22: mcp_info carries the broker build id; the client returns it verbatim."""
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True, "allow_launch": False,
                                         "default_mode": "off",
                                         "version": "0.8.0+abc"})
    with _client(handler) as c:
        out = c.info()
    assert out["version"] == "0.8.0+abc"


def test_list_terminals_surfaces_version_and_stale():
    """#22: per-window build id + stale flag flow through list_terminals."""
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[
            {"id": 1, "mode": "read", "version": "0.8.0+new", "stale": False},
            {"id": 2, "mode": "read", "version": "", "stale": True}])
    with _client(handler) as c:
        out = c.list_terminals()
    assert out[0]["version"] == "0.8.0+new" and out[0]["stale"] is False
    assert out[1]["version"] == "" and out[1]["stale"] is True


def test_list_terminals_passes_through_pyte_flag():
    """#134: the per-terminal `pyte` flag flows through the client verbatim (the
    server tool just copies each dict), so a caller sees which agents lack pyte."""
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[
            {"id": 1, "mode": "read", "pyte": True},
            {"id": 2, "mode": "read", "pyte": False}])
    with _client(handler) as c:
        out = c.list_terminals()
    assert out[0]["pyte"] is True
    assert out[1]["pyte"] is False


def test_list_terminals_server_merge_preserves_pyte_flag():
    """The server-side merge (`dict(t)` + host/id rewrite) must keep the `pyte`
    flag alongside the namespacing (#134)."""
    def h(req):
        return httpx.Response(200, json=[{"id": 1, "mode": "read", "pyte": False}])
    server = _install({"local": h})
    try:
        out = server.list_terminals()
    finally:
        _reset_server()
    t = out["terminals"][0]
    assert t["id"] == "local:1" and t["pyte"] is False


def test_list_terminals_and_profiles_paths():
    paths = []

    def handler(req: httpx.Request) -> httpx.Response:
        paths.append(req.url.path)
        if req.url.path == "/mcp/terminals":
            return httpx.Response(200, json=[{"id": 1, "mode": "read"}])
        return httpx.Response(200, json={"default": "bash", "profiles": ["bash", "sh"]})

    with _client(handler) as c:
        assert c.list_terminals() == [{"id": 1, "mode": "read"}]
        assert c.list_profiles() == {"default": "bash", "profiles": ["bash", "sh"]}
    assert paths == ["/mcp/terminals", "/mcp/profiles"]


def test_read_screen_posts_id():
    seen = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["path"] = req.url.path
        seen["body"] = json.loads(req.content)
        return httpx.Response(200, json={"ok": True, "id": 42, "cols": 80,
                                         "rows": 24, "text": "hi"})

    with _client(handler) as c:
        out = c.read_screen(42)
    assert seen["path"] == "/mcp/read"
    assert seen["body"] == {"id": 42}        # back-compat: no view/lines
    assert out["text"] == "hi"


def test_read_screen_scrollback_params_and_fields():
    """#21: view/lines go in the body; alt_screen/cursor/history_lines surface."""
    seen = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(req.content)
        return httpx.Response(200, json={
            "ok": True, "id": 4, "cols": 80, "rows": 24, "text": "g",
            "alt_screen": False, "app_cursor": True, "view": "scrollback",
            "history_lines": 7, "cursor": {"row": 2, "col": 5}})

    with _client(handler) as c:
        out = c.read_screen(4, view="scrollback", lines=200)
    assert seen["body"] == {"id": 4, "view": "scrollback", "lines": 200}
    assert out["alt_screen"] is False and out["view"] == "scrollback"
    assert out["app_cursor"] is True            # #23 DECCKM surfaces
    assert out["history_lines"] == 7 and out["cursor"] == {"row": 2, "col": 5}


def test_read_screen_attrs_param_and_field():
    """#128: attrs=true puts `attrs` in the body and surfaces `attr_runs`; the
    default read omits the key entirely (back-compat)."""
    seen = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(req.content)
        return httpx.Response(200, json={
            "ok": True, "id": 3, "cols": 80, "rows": 24, "text": "menu",
            "attr_runs": [{"row": 1, "col": 0, "len": 7, "fg": "default",
                           "bg": "default", "reverse": True}]})

    with _client(handler) as c:
        out = c.read_screen(3, attrs=True)
    assert seen["body"] == {"id": 3, "attrs": True}
    assert out["attr_runs"] == [{"row": 1, "col": 0, "len": 7, "fg": "default",
                                 "bg": "default", "reverse": True}]


def test_read_screen_default_omits_attrs():
    """No attrs -> no `attrs` key on the wire (unchanged from before #128)."""
    seen = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(req.content)
        return httpx.Response(200, json={"ok": True, "id": 3, "cols": 80,
                                         "rows": 24, "text": "hi"})

    with _client(handler) as c:
        c.read_screen(3)
    assert "attrs" not in seen["body"]


def test_read_screen_surfaces_partial_flag():
    """#130: a `partial` flag on the broker reply (an alt-screen grid that lost
    its full-frame paint to ring eviction and couldn't be reconstructed) is
    passed through to the tool result verbatim."""
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "ok": True, "id": 5, "cols": 80, "rows": 24, "text": "grid",
            "alt_screen": True, "partial": True})

    with _client(handler) as c:
        out = c.read_screen(5)
    assert out["partial"] is True
    assert out["alt_screen"] is True


def test_read_screen_surfaces_idle_ms():
    """#133: idle_ms on the broker reply (best-effort ms since the last PTY
    output) is passed through to the tool result verbatim — read_screen returns
    the broker dict as-is, so no client-side handling is needed."""
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "ok": True, "id": 5, "cols": 80, "rows": 24, "text": "grid",
            "idle_ms": 250})

    with _client(handler) as c:
        out = c.read_screen(5)
    assert out["idle_ms"] == 250


def test_send_input_posts_id_and_data():
    seen = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["path"] = req.url.path
        seen["body"] = json.loads(req.content)
        return httpx.Response(200, json={"ok": True})

    # The client is the thin 1:1 endpoint wrapper: it forwards `data` VERBATIM.
    # Mapping a logical Enter to CR is the tool's job, not the client's (#13).
    with _client(handler) as c:
        out = c.send_input(42, "ls\n")
    assert seen["path"] == "/mcp/input"
    assert seen["body"] == {"id": 42, "data": "ls\n"}
    assert out == {"ok": True}


def test_reset_terminal_posts_id():
    # #27: reset_terminal is a thin POST /mcp/reset {id} that clears the
    # agent's render buffer; the broker enforces readwrite, not the client.
    seen = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["path"] = req.url.path
        seen["body"] = json.loads(req.content)
        return httpx.Response(200, json={"ok": True})

    with _client(handler) as c:
        out = c.reset_terminal(42)
    assert seen["path"] == "/mcp/reset"
    assert seen["body"] == {"id": 42}
    assert out == {"ok": True}


def test_flush_input_posts_id():
    # #133: flush_input is a thin POST /mcp/flush {id} that discards the app's
    # queued-but-unread input; the broker enforces readwrite, not the client.
    seen = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["path"] = req.url.path
        seen["body"] = json.loads(req.content)
        return httpx.Response(200, json={"ok": True, "id": 42})

    with _client(handler) as c:
        out = c.flush_input(42)
    assert seen["path"] == "/mcp/flush"
    assert seen["body"] == {"id": 42}
    assert out == {"ok": True, "id": 42}


def test_flush_failed_error_message_is_readable():
    # #133: the broker's flush_failed (502) surfaces as a readable tool error,
    # not the raw code — it's in the client's _ERROR_MESSAGES table.
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(502, json={"error": "flush_failed"})

    with _client(handler) as c:
        with pytest.raises(BrowserlandError) as excinfo:
            c.flush_input(42)
    assert excinfo.value.code == "flush_failed"
    assert "pending input" in str(excinfo.value)


def test_set_pace_posts_id_and_pace():
    # #133: set_pace is a thin POST /mcp/pace {id, pace_ms}; the broker enforces
    # readwrite + clamps, not the client. The reply echoes the clamped value.
    seen = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["path"] = req.url.path
        seen["body"] = json.loads(req.content)
        return httpx.Response(200, json={"ok": True, "id": 42, "pace_ms": 40})

    with _client(handler) as c:
        out = c.set_pace(42, 40)
    assert seen["path"] == "/mcp/pace"
    assert seen["body"] == {"id": 42, "pace_ms": 40}
    assert out == {"ok": True, "id": 42, "pace_ms": 40}


def test_bad_pace_error_message_is_readable():
    # #133: the broker's bad_pace (400) surfaces as a readable tool error, not the
    # raw code — it's in the client's _ERROR_MESSAGES table.
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": "bad_pace"})

    with _client(handler) as c:
        with pytest.raises(BrowserlandError) as excinfo:
            c.set_pace(42, "fast")
    assert excinfo.value.code == "bad_pace"
    assert "integer" in str(excinfo.value)


# ---- #13: MCP tool maps newlines -> Enter (CR); client stays verbatim -----

@pytest.mark.parametrize("data,expected", [
    ("ls\n", "ls\r"),                 # lone LF -> CR (the bug: LF doesn't submit)
    ("ls\r\n", "ls\r"),               # CRLF collapses to a single Enter
    ("ls\r", "ls\r"),                 # explicit CR untouched
    ("a\nb\r\nc\n", "a\rb\rc\r"),     # mixed newlines, each an Enter
    ("ls", "ls"),                     # no newline -> no submit (partial line)
    ("", ""),                         # empty stays empty
    ("\x03", "\x03"),                 # Ctrl-C control byte passes through
    ("\x1b[C", "\x1b[C"),             # ESC arrow sequence passes through
    ("git commit -m \"x\ny\"\n", "git commit -m \"x\ry\"\r"),  # newlines in args too
])
def test_newlines_to_enter(data, expected):
    from webterm.mcptool import server
    assert server._newlines_to_enter(data) == expected


def test_tool_normalizes_but_client_is_verbatim():
    """The same "\\n" submits via the tool (-> "\\r") but is forwarded raw by the
    low-level client — the layer split that keeps POST /mcp/input verbatim."""
    from webterm.mcptool import server

    bodies = []

    def handler(req: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(req.content))
        return httpx.Response(200, json={"ok": True})

    # Low-level client: verbatim.
    with _client(handler) as c:
        c.send_input(1, "echo hi\n")
    # High-level tool: maps the trailing LF to CR so PowerShell actually runs it.
    _install({"default": handler})
    try:
        server.send_input("default:1", "echo hi\n")
    finally:
        _reset_server()

    assert bodies == [
        {"id": 1, "data": "echo hi\n"},   # client path: unchanged
        {"id": 1, "data": "echo hi\r"},   # tool path: Enter -> CR
    ]


# ---- #14: send_keys translates key tokens to terminal byte sequences -------

@pytest.mark.parametrize("keys,expected", [
    (["C-c"], "\x03"),                       # Ctrl-C — the issue's target
    (["C-C"], "\x03"),                       # case-insensitive chord
    (["C-a"], "\x01"), (["C-z"], "\x1a"),    # control letter bounds
    (["C-@"], "\x00"), (["C-Space"], "\x00"),  # NUL
    (["C-["], "\x1b"),                       # ESC via chord
    (["C-\\"], "\x1c"), (["C-]"], "\x1d"),
    (["C-^"], "\x1e"), (["C-_"], "\x1f"),
    (["C-?"], "\x7f"),                       # DEL
    (["C-h"], "\x08"),                       # BS (distinct from Backspace->DEL)
    (["Enter"], "\r"), (["return"], "\r"),
    (["LF"], "\n"), (["lf"], "\n"), (["linefeed"], "\n"),  # #127: LF (0x0A)
    (["Tab"], "\t"), (["Esc"], "\x1b"), (["escape"], "\x1b"),
    (["Space"], " "),
    (["Backspace"], "\x7f"), (["BS"], "\x7f"),
    (["Delete"], "\x1b[3~"),
    (["Up"], "\x1b[A"), (["Down"], "\x1b[B"),
    (["Right"], "\x1b[C"), (["Left"], "\x1b[D"),
    (["Home"], "\x1b[H"), (["End"], "\x1b[F"),
    (["PageUp"], "\x1b[5~"), (["PageDown"], "\x1b[6~"), (["Insert"], "\x1b[2~"),
    (["F1"], "\x1bOP"), (["F5"], "\x1b[15~"), (["F12"], "\x1b[24~"),
    (["M-x"], "\x1bx"),                       # Alt-x = ESC prefix
    (["S-Tab"], "\x1b[Z"), (["s-tab"], "\x1b[Z"),  # #132: Shift+Tab = back-tab
    (["S-Up"], "\x1b[1;2A"), (["S-Down"], "\x1b[1;2B"),  # shifted cursor keys
    (["S-Right"], "\x1b[1;2C"), (["S-Left"], "\x1b[1;2D"),
    (["S-Home"], "\x1b[1;2H"), (["S-End"], "\x1b[1;2F"),
    (["a"], "a"), (["^"], "^"), (["é"], "é"),  # single literal chars (UTF-8)
    (["C-c", "Enter"], "\x03\r"),            # sequence
    (["Up", "Up", "Enter"], "\x1b[A\x1b[A\r"),
    (["S-Tab", "S-Tab"], "\x1b[Z\x1b[Z"),    # #132: Shift chord in a sequence
])
def test_keys_to_text(keys, expected):
    from webterm.mcptool import server
    assert server._keys_to_text(keys) == expected


@pytest.mark.parametrize("bad", [
    [], "C-c", ["Retun"], ["Ctrl-C"], ["C-"], ["M-Enter"], ["Up arrow"],
    ["S-Enter"], ["S-Return"], ["S-Esc"], ["S-x"], ["S-"],  # #132: no shifted form
])
def test_keys_to_text_rejects(bad):
    from webterm.mcptool import server
    with pytest.raises(ValueError):
        server._keys_to_text(bad)


def test_lf_token_distinct_from_enter():
    """#127: LF/linefeed emit a bare line-feed (0x0A) for raw-mode
    ncurses/PDCurses TUIs, while Enter/Return keep sending CR (0x0D). LF is
    byte-for-byte the C-j chord, just discoverable by name."""
    from webterm.mcptool import server
    assert server._keys_to_text(["LF"]) == "\n"
    assert server._keys_to_text(["linefeed"]) == "\n"
    assert server._keys_to_text(["LF"]) == server._keys_to_text(["C-j"])  # same byte
    # The CR-only Enter policy is unchanged.
    assert server._keys_to_text(["Enter"]) == "\r"
    assert server._keys_to_text(["return"]) == "\r"


# ---- #132: Shift (S-) chord for keys with a distinct shifted encoding ------

def test_shift_chord_tab_and_cursor_encodings():
    """#132: S-Tab is back-tab (CSI Z) and the shifted cursor keys take the CSI
    modifier-parameter form (CSI 1;2 <final>). Case-insensitive like the other
    chords."""
    from webterm.mcptool import server
    assert server._keys_to_text(["S-Tab"]) == "\x1b[Z"
    assert server._keys_to_text(["s-tab"]) == "\x1b[Z"      # case-insensitive
    assert server._keys_to_text(["S-Up"]) == "\x1b[1;2A"
    assert server._keys_to_text(["S-Down"]) == "\x1b[1;2B"
    assert server._keys_to_text(["S-Right"]) == "\x1b[1;2C"
    assert server._keys_to_text(["S-Left"]) == "\x1b[1;2D"
    assert server._keys_to_text(["S-Home"]) == "\x1b[1;2H"
    assert server._keys_to_text(["S-End"]) == "\x1b[1;2F"


def test_shift_cursor_ignores_decckm():
    """A modified cursor key always uses the CSI form — never SS3 — so the shifted
    encoding is identical whether or not application-cursor mode (DECCKM) is on,
    unlike the plain cursor keys (#23)."""
    from webterm.mcptool import server
    assert server._token_to_text("S-Up") == "\x1b[1;2A"
    assert server._token_to_text("S-Up", True) == "\x1b[1;2A"   # DECCKM ignored
    assert server._token_to_text("S-End", True) == "\x1b[1;2F"
    # The plain cursor key still flips to SS3 under DECCKM (unchanged from #23).
    assert server._token_to_text("Up", True) == "\x1bOA"


@pytest.mark.parametrize("tok", ["S-Enter", "S-Return", "S-Esc", "S-Space",
                                 "S-F1", "S-a", "S-"])
def test_shift_chord_no_shifted_form_raises(tok):
    """A key with no distinct shifted terminal encoding raises a clear error
    rather than silently sending the unshifted bytes (option 3)."""
    from webterm.mcptool import server
    with pytest.raises(ValueError, match="no shifted form"):
        server._keys_to_text([tok])


def test_shift_chord_leaves_other_chords_unchanged():
    """Adding S- must not perturb the existing C-/M- chords or single literals."""
    from webterm.mcptool import server
    assert server._keys_to_text(["C-c"]) == "\x03"
    assert server._keys_to_text(["M-x"]) == "\x1bx"
    assert server._keys_to_text(["Tab"]) == "\t"          # plain Tab still HT
    assert server._keys_to_text(["s"]) == "s"             # bare 's' is a literal


def test_send_keys_posts_shift_tab_verbatim():
    """#132 end-to-end: S-Tab reaches the wire as back-tab (ESC [ Z) and S-Up as
    the CSI modifier-parameter form (ESC [ 1;2A) — a shift chord's encoding is
    independent of DECCKM, even though the app_cursor the cache reports here is
    True. (Under #133 the multi-key default send does consult the cache — for a
    pace default — but with none configured it stays a single burst.)"""
    from webterm.mcptool import server

    seen = {"body": None, "paths": []}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["paths"].append(req.url.path)
        if req.url.path == "/mcp/terminals":
            return httpx.Response(200, json=[{"id": 9, "app_cursor": True}])
        seen["body"] = json.loads(req.content)
        return httpx.Response(200, json={"ok": True})

    _install({"default": handler})
    try:
        assert server.send_keys("default:9", ["S-Tab", "S-Up"]) == {"ok": True}
    finally:
        _reset_server()
    # One burst (no pace_ms configured), CSI form for the shifted cursor despite
    # the reported DECCKM.
    assert seen["body"] == {"id": 9, "data": "\x1b[Z\x1b[1;2A"}


def test_send_keys_posts_translated_bytes_verbatim():
    from webterm.mcptool import server

    seen = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["path"] = req.url.path
        seen["body"] = json.loads(req.content)
        return httpx.Response(200, json={"ok": True})

    _install({"default": handler})
    try:
        # Ctrl-C then Enter: a real 0x03 (not the literal "^C" that left a stray
        # 'c' in #14), sent verbatim — NOT through the newline->CR text policy.
        assert server.send_keys("default:9", ["C-c", "Enter"]) == {"ok": True}
    finally:
        _reset_server()
    assert seen["path"] == "/mcp/input"
    assert seen["body"] == {"id": 9, "data": "\x03\r"}


def test_send_keys_invalid_token_sends_nothing():
    """A bad token must raise before any HTTP call — no half-typed line."""
    from webterm.mcptool import server

    calls = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(req.url.path)
        return httpx.Response(200, json={"ok": True})

    _install({"default": handler})
    try:
        with pytest.raises(ValueError):
            server.send_keys("default:9", ["C-c", "Bogus", "Enter"])
    finally:
        _reset_server()
    assert calls == []   # atomic: nothing was sent


# ---- #131: recovery guidance must not over-recommend C-l as a repaint key --

def test_recovery_docstrings_drop_generic_ctrl_l_repaint():
    """#131: no recovery docstring may steer agents to the bare `["C-l"]` keypress
    as a generic repaint. Ctrl-L is a live keybinding in full-screen TUIs (Dwarf
    Fortress fires a game action, not a redraw), so the guidance must not present
    it as an app-agnostic fix."""
    from webterm.mcptool import server
    for tool in (server.send_keys, server.send_input, server.reset_terminal,
                 server.read_screen):
        assert '["C-l"]' not in (tool.__doc__ or ""), (
            f"{tool.__name__} still recommends the bare [\"C-l\"] repaint keypress")
    # The specific pre-#131 send_keys steer is gone.
    assert "to make the app repaint" not in server.send_keys.__doc__


def test_recovery_docstrings_caveat_any_ctrl_l_mention():
    """Where a docstring still *names* Ctrl-L it must be flagged as a shell-only
    convention that collides with full-screen TUIs — never a bare recommendation —
    and `reset_terminal` must stay the app-agnostic Browserland-side fallback."""
    from webterm.mcptool import server
    for tool in (server.send_keys, server.send_input, server.reset_terminal):
        doc = tool.__doc__ or ""
        if "c-l" in doc.lower():
            assert "full-screen" in doc.lower(), (
                f"{tool.__name__}: a Ctrl-L mention must carry the full-screen-TUI "
                "caveat, not recommend it as a generic repaint")
            assert "readline/shell" in doc.lower(), (
                f"{tool.__name__}: Ctrl-L must be scoped to the readline/shell case")
    assert "reset_terminal" in server.send_keys.__doc__   # fallback still offered


def test_ctrl_l_token_translation_unchanged():
    """#131 is docs-only: `C-l` stays a valid Ctrl chord (form-feed, 0x0C), so an
    agent that genuinely wants it (for a shell) can still send it — only the
    guidance changed, not the key translation."""
    from webterm.mcptool import server
    assert server._keys_to_text(["C-l"]) == "\x0c"
    assert server._keys_to_text(["C-L"]) == "\x0c"   # case-insensitive, unchanged


# ---- #243 review round 1: launch docstrings must state the unified wire ----

def test_launch_docstrings_match_the_unified_mode_wire():
    """Owner decision 2 on #243 (broker atom B2): EVERY /mcp/launch, scoped or
    not, honors a valid `mode` and defaults to readwrite; the answer carries
    `handle`. The shipped docstrings used to describe the pre-B2 wire ("a
    valid mode is ignored" unscoped) while every test stayed green — exactly
    the class of quiet wrongness the #131 docstring pins above exist for."""
    from webterm.mcptool import server
    doc = server.launch_terminal.__doc__ or ""
    assert "ignored" not in doc, (
        "launch_terminal still claims a valid mode can be ignored")
    assert "default mode" not in doc, (
        "launch_terminal still describes the unscoped default-mode inheritance")
    assert "readwrite" in doc          # the new default is stated
    assert "handle" in doc             # the launch handle is stated
    cdoc = BrowserlandClient.launch_terminal.__doc__ or ""
    assert "ignores a valid one" not in cdoc
    assert "readwrite" in cdoc
    assert "handle" in cdoc


def test_262_desktop_docstrings_state_open_on_demand():
    """#262: an agent reads the tool text, not the wiki. arrange_window must
    say a chip-only window is opened first (and that this reopens a window
    the user closed), get_desktop must name the `open` row key, and the
    send-window op line must say it opens a chip too."""
    from webterm.mcptool import server
    adoc = " ".join((server.arrange_window.__doc__ or "").split())
    assert "taskbar chip" in adoc and "is opened first" in adoc
    assert "reopens a window the user closed" in adoc
    gdoc = " ".join((server.get_desktop.__doc__ or "").split())
    assert "`open`" in gdoc and "false for a terminal that is only a" in gdoc
    wdoc = " ".join((server.workspaces.__doc__ or "").split())
    assert "taskbar chip is opened first" in wdoc


# ---- #129: send_keys inter-key pacing (frame-polling TUIs drop bursts) -----

def _paced_capture(monkeypatch):
    """Install a single 'default' host, stub out server.time.sleep (recording
    the pauses), and capture each /mcp/input body. Returns (inputs, sleeps),
    both live lists filled as send_keys runs."""
    from webterm.mcptool import server
    inputs: list = []
    sleeps: list = []
    monkeypatch.setattr(server.time, "sleep", lambda s: sleeps.append(s))

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/mcp/terminals":       # DECCKM cache (cursor keys)
            return httpx.Response(200, json=[{"id": 9, "app_cursor": True}])
        inputs.append(json.loads(req.content))
        return httpx.Response(200, json={"ok": True})

    _install({"default": handler})
    return inputs, sleeps


def test_send_keys_paces_tokens_with_delay(monkeypatch):
    """A positive delay_ms writes each token in its OWN POST with a time.sleep
    pause between them (never before the first / after the last), so a
    frame-polling TUI sees one keypress per frame."""
    from webterm.mcptool import server
    inputs, sleeps = _paced_capture(monkeypatch)
    try:
        assert server.send_keys("default:9", ["Space", "Space", "Space"],
                                delay_ms=5) == {"ok": True}
    finally:
        _reset_server()
    assert inputs == [{"id": 9, "data": " "},
                      {"id": 9, "data": " "},
                      {"id": 9, "data": " "}]     # one token per write
    assert sleeps == [5 / 1000.0, 5 / 1000.0]     # a pause only BETWEEN tokens


def test_send_keys_default_delay_is_single_burst(monkeypatch):
    """Back-compat: omitting delay_ms keeps the whole list in one POST and
    never sleeps (no per-terminal default set) — the pre-#129 behavior."""
    from webterm.mcptool import server
    inputs, sleeps = _paced_capture(monkeypatch)
    try:
        server.send_keys("default:9", ["Space", "Space", "Space"])
    finally:
        _reset_server()
    assert inputs == [{"id": 9, "data": "   "}]   # single burst
    assert sleeps == []                           # no pacing


def test_send_keys_delay_single_token_not_paced(monkeypatch):
    """A single token has nothing to pace: one POST, no sleep, even with a
    delay_ms (pacing needs >1 token)."""
    from webterm.mcptool import server
    inputs, sleeps = _paced_capture(monkeypatch)
    try:
        server.send_keys("default:9", ["Enter"], delay_ms=50)
    finally:
        _reset_server()
    assert inputs == [{"id": 9, "data": "\r"}]
    assert sleeps == []


def test_send_keys_delay_capped_and_negative_ignored(monkeypatch):
    """delay_ms is clamped to _MAX_KEY_DELAY_MS; a negative value disables pacing
    (single burst, back-compat)."""
    from webterm.mcptool import server
    inputs, sleeps = _paced_capture(monkeypatch)
    try:
        server.send_keys("default:9", ["a", "b"], delay_ms=999999)   # over the cap
        server.send_keys("default:9", ["a", "b"], delay_ms=-5)       # negative
    finally:
        _reset_server()
    assert sleeps == [server._MAX_KEY_DELAY_MS / 1000.0]   # clamped, one pause
    assert inputs == [{"id": 9, "data": "a"}, {"id": 9, "data": "b"},  # paced pair
                      {"id": 9, "data": "ab"}]                          # burst (neg)


def test_send_keys_paced_arrows_keep_ss3_under_decckm(monkeypatch):
    """#129 + #23: paced writes still honor DECCKM — each arrow token goes out as
    SS3 in its own POST when the terminal has application-cursor mode on."""
    from webterm.mcptool import server
    inputs, sleeps = _paced_capture(monkeypatch)   # handler reports app_cursor=True
    try:
        server.send_keys("default:9", ["Down", "Down"], delay_ms=5)
    finally:
        _reset_server()
    assert inputs == [{"id": 9, "data": "\x1bOB"}, {"id": 9, "data": "\x1bOB"}]
    assert sleeps == [5 / 1000.0]


def test_send_keys_paced_bad_token_sends_nothing(monkeypatch):
    """Atomicity holds under pacing too: a bad token raises before any POST or
    sleep, leaving no half-typed sequence."""
    from webterm.mcptool import server
    inputs, sleeps = _paced_capture(monkeypatch)
    try:
        with pytest.raises(ValueError):
            server.send_keys("default:9", ["Space", "Bogus", "Space"], delay_ms=5)
    finally:
        _reset_server()
    assert inputs == [] and sleeps == []


# ---- #133: per-terminal DEFAULT send_keys pace (set once via set_pace) ------

def _pace_default_capture(monkeypatch, terminal_pace, app_cursor=False):
    """Like _paced_capture but the mock /mcp/terminals reports a per-terminal
    ``pace_ms`` (#133) so send_keys can auto-pace off the terminal default even
    with no explicit delay_ms. Returns (inputs, sleeps, paths) — all live lists
    filled as send_keys runs."""
    from webterm.mcptool import server
    inputs: list = []
    sleeps: list = []
    paths: list = []
    monkeypatch.setattr(server.time, "sleep", lambda s: sleeps.append(s))

    def handler(req: httpx.Request) -> httpx.Response:
        paths.append(req.url.path)
        if req.url.path == "/mcp/terminals":
            return httpx.Response(200, json=[{"id": 9, "app_cursor": app_cursor,
                                              "pace_ms": terminal_pace}])
        inputs.append(json.loads(req.content))
        return httpx.Response(200, json={"ok": True})

    _install({"default": handler})
    return inputs, sleeps, paths


def test_send_keys_auto_paces_from_terminal_default(monkeypatch):
    """#133: omitting delay_ms, a multi-key send to a terminal whose
    list_terminals reports pace_ms>0 auto-paces — one token per POST with a pause
    between — WITHOUT the caller ever passing delay_ms."""
    from webterm.mcptool import server
    inputs, sleeps, paths = _pace_default_capture(monkeypatch, terminal_pace=7)
    try:
        assert server.send_keys(
            "default:9", ["Space", "Space", "Space"]) == {"ok": True}
    finally:
        _reset_server()
    assert inputs == [{"id": 9, "data": " "},
                      {"id": 9, "data": " "},
                      {"id": 9, "data": " "}]     # one token per write
    assert sleeps == [7 / 1000.0, 7 / 1000.0]     # paced at the terminal default
    assert "/mcp/terminals" in paths              # the default was fetched


def test_send_keys_single_key_ignores_terminal_default(monkeypatch):
    """A single-token send never paces, even with a per-terminal pace_ms set: one
    POST, no sleep, and no fetch (pacing needs >1 token, no cursor to re-encode)."""
    from webterm.mcptool import server
    inputs, sleeps, paths = _pace_default_capture(monkeypatch, terminal_pace=7)
    try:
        server.send_keys("default:9", ["Enter"])
    finally:
        _reset_server()
    assert inputs == [{"id": 9, "data": "\r"}]
    assert sleeps == []
    assert "/mcp/terminals" not in paths          # nothing to pace -> no fetch


def test_send_keys_zero_terminal_default_is_single_burst(monkeypatch):
    """A terminal default of 0 (the default) keeps the single-burst write even for
    a multi-key no-delay send — the fetch happens but resolves to no pacing."""
    from webterm.mcptool import server
    inputs, sleeps, paths = _pace_default_capture(monkeypatch, terminal_pace=0)
    try:
        server.send_keys("default:9", ["Space", "Space", "Space"])
    finally:
        _reset_server()
    assert inputs == [{"id": 9, "data": "   "}]   # single burst
    assert sleeps == []
    assert "/mcp/terminals" in paths              # consulted, but pace_ms=0


def test_send_keys_explicit_delay_overrides_terminal_default(monkeypatch):
    """An explicit delay_ms>0 wins over the per-terminal default and, being
    non-zero, skips the default fetch entirely (delay_ms already decides pace)."""
    from webterm.mcptool import server
    inputs, sleeps, paths = _pace_default_capture(monkeypatch, terminal_pace=7)
    try:
        server.send_keys("default:9", ["a", "b"], delay_ms=3)
    finally:
        _reset_server()
    assert inputs == [{"id": 9, "data": "a"}, {"id": 9, "data": "b"}]
    assert sleeps == [3 / 1000.0]                 # the explicit delay, not 7
    assert "/mcp/terminals" not in paths          # non-zero delay -> no default fetch


def test_send_keys_explicit_zero_forces_burst_on_paced_terminal(monkeypatch):
    """#133: an explicit delay_ms=0 forces a single burst even when the terminal
    has a pace_ms default set, and skips the default fetch (0 is an explicit
    override — 'burst this one' — not the 'unspecified' None that uses the
    default)."""
    from webterm.mcptool import server
    inputs, sleeps, paths = _pace_default_capture(monkeypatch, terminal_pace=7)
    try:
        server.send_keys("default:9", ["Space", "Space", "Space"], delay_ms=0)
    finally:
        _reset_server()
    assert inputs == [{"id": 9, "data": "   "}]   # single burst despite pace_ms=7
    assert sleeps == []
    assert "/mcp/terminals" not in paths          # explicit 0 -> no default fetch


def test_send_keys_terminal_default_paces_arrows_ss3(monkeypatch):
    """#133 + #23: a per-terminal default paces cursor keys too, and each arrow
    still goes out as SS3 — decoded from the SAME single fetch that carried
    pace_ms — when the terminal has DECCKM on."""
    from webterm.mcptool import server
    inputs, sleeps, paths = _pace_default_capture(monkeypatch, terminal_pace=4,
                                                  app_cursor=True)
    try:
        server.send_keys("default:9", ["Down", "Down"])
    finally:
        _reset_server()
    assert inputs == [{"id": 9, "data": "\x1bOB"}, {"id": 9, "data": "\x1bOB"}]
    assert sleeps == [4 / 1000.0]
    assert paths.count("/mcp/terminals") == 1     # ONE fetch backs both lookups


# ---- #23: DECCKM-aware cursor keys ----------------------------------------

def test_token_to_text_cursor_mode():
    from webterm.mcptool import server
    assert server._token_to_text("up") == "\x1b[A"          # default CSI
    assert server._token_to_text("up", True) == "\x1bOA"    # SS3 under DECCKM
    assert server._token_to_text("home", True) == "\x1bOH"
    assert server._token_to_text("end", False) == "\x1b[F"


def test_keys_have_cursor():
    from webterm.mcptool import server
    assert server._keys_have_cursor(["Down"]) is True
    assert server._keys_have_cursor(["x", "Up"]) is True
    assert server._keys_have_cursor(["C-c", "Enter"]) is False


def _send_keys_capturing(app_cursor, keys, list_status=200):
    """Drive send_keys with a mock that answers /mcp/terminals (the cheap DECCKM
    cache) and captures the /mcp/input body. Returns (input_body_or_None, paths)."""
    from webterm.mcptool import server
    seen = {"body": None, "paths": []}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["paths"].append(req.url.path)
        if req.url.path == "/mcp/terminals":
            if list_status != 200:
                return httpx.Response(list_status, json={"error": "boom"})
            return httpx.Response(200, json=[{"id": 9, "app_cursor": app_cursor}])
        seen["body"] = json.loads(req.content)
        return httpx.Response(200, json={"ok": True})

    _install({"default": handler})
    try:
        server.send_keys("default:9", keys)
    finally:
        _reset_server()
    return seen["body"], seen["paths"]


def test_send_keys_arrows_csi_in_normal_mode():
    body, paths = _send_keys_capturing(False, ["Down", "Down"])
    assert body == {"id": 9, "data": "\x1b[B\x1b[B"}
    assert "/mcp/terminals" in paths      # consulted the DECCKM cache


def test_send_keys_arrows_ss3_under_decckm():
    body, _ = _send_keys_capturing(True, ["Up", "Home"])
    assert body == {"id": 9, "data": "\x1bOA\x1bOH"}


def test_send_keys_no_cursor_single_key_skips_lookup():
    # A single non-cursor key has nothing to pace (pacing needs >1 token) and no
    # cursor to re-encode, so send_keys skips the list_terminals fetch entirely.
    # (A MULTI-key default send now consults it for a pace default — see #133.)
    body, paths = _send_keys_capturing(True, ["C-c"])
    assert body == {"id": 9, "data": "\x03"}
    assert "/mcp/terminals" not in paths  # nothing to pace or re-encode -> no lookup


def test_send_keys_arrows_fall_back_to_csi_on_lookup_error():
    # A non-agent producer (or any lookup failure) -> CSI form, still sent.
    body, _ = _send_keys_capturing(True, ["Left"], list_status=502)
    assert body == {"id": 9, "data": "\x1b[D"}


def test_send_keys_invalid_token_raises_before_lookup():
    # A bad token raises before any /mcp/terminals lookup or /mcp/input send.
    from webterm.mcptool import server
    paths = []

    def handler(req: httpx.Request) -> httpx.Response:
        paths.append(req.url.path)
        return httpx.Response(200, json=[])

    _install({"default": handler})
    try:
        with pytest.raises(ValueError):
            server.send_keys("default:9", ["Down", "Bogus"])
    finally:
        _reset_server()
    assert paths == []                    # validated before any external call


def test_launch_omits_none_fields():
    seen = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(req.content)
        return httpx.Response(200, json={"ok": True, "id": 7, "registered": True})

    # Only profile + dims supplied; title/cwd omitted -> must not appear.
    with _client(handler) as c:
        c.launch_terminal(profile="bash", cols=100, rows=30)
    assert seen["body"] == {"profile": "bash", "cols": 100, "rows": 30}
    assert "title" not in seen["body"] and "cwd" not in seen["body"]


def test_launch_includes_all_fields_when_given():
    seen = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(req.content)
        return httpx.Response(200, json={"ok": True})

    with _client(handler) as c:
        c.launch_terminal(profile="bash", cols=80, rows=24,
                          title="t", cwd="/tmp")
    assert seen["body"] == {"profile": "bash", "cols": 80, "rows": 24,
                            "title": "t", "cwd": "/tmp"}


def test_error_translation_read_only():
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"error": "read_only"})

    with _client(handler) as c:
        with pytest.raises(BrowserlandError) as ei:
            c.send_input(1, "x")
    err = ei.value
    assert err.status == 403
    assert err.code == "read_only"
    assert "readwrite" in str(err)


def test_error_translation_unknown_code_falls_back():
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(418, json={"error": "teapot"})

    with _client(handler) as c:
        with pytest.raises(BrowserlandError) as ei:
            c.info()
    assert ei.value.status == 418
    assert ei.value.code == "teapot"
    assert "teapot" in str(ei.value)


def test_error_no_json_body():
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    with _client(handler) as c:
        with pytest.raises(BrowserlandError) as ei:
            c.info()
    assert ei.value.status == 500
    assert ei.value.code is None


def test_connection_error_becomes_browserland_error():
    def handler(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=req)

    with _client(handler) as c:
        with pytest.raises(BrowserlandError) as ei:
            c.info()
    assert ei.value.status == 0
    assert ei.value.code == "connection_error"
    assert "broker" in str(ei.value)


# ---- #232: the scope header and the fail-closed /mcp/info probe ------------

_ECHO = object()      # /mcp/info echoes the request's header, like a broker
_OMIT = object()      # /mcp/info has no "scope" key: a broker before scopes


class _ScopeBroker:
    """MockTransport handler for one broker. Records every request.
    /mcp/info answers with ``echo`` as its ``scope`` (_ECHO: whatever header
    arrived, null when none, as the real broker does; _OMIT: no key; else
    that value); ``faults`` maps a path to a queue consumed one entry per
    request to it (None: answer normally; an httpx exception class: raise
    it; an httpx.Response: return it); every other request answers
    ``{"ok": true}`` (a list for /mcp/terminals)."""

    def __init__(self, echo=_ECHO, terminals=None, faults=None):
        self.echo = echo
        self.terminals = terminals if terminals is not None else []
        self.faults = faults or {}
        self.requests = []

    def __call__(self, request):
        self.requests.append(request)
        path = request.url.path
        queue = self.faults.get(path)
        fault = queue.pop(0) if queue else None
        if isinstance(fault, httpx.Response):
            return fault
        if fault is not None:
            raise fault("injected", request=request)
        if path == "/mcp/info":
            body = {"ok": True, "allow_launch": True, "default_mode": "off"}
            if self.echo is _ECHO:
                body["scope"] = request.headers.get(SCOPE_HEADER) or None
            elif self.echo is not _OMIT:
                body["scope"] = self.echo
            return httpx.Response(200, json=body)
        if path == "/mcp/terminals":
            return httpx.Response(200, json=self.terminals)
        return httpx.Response(200, json={"ok": True})

    def count(self, path):
        return sum(1 for r in self.requests if r.url.path == path)


def _scoped(broker, scope="teamA", name="local"):
    return BrowserlandClient(base="http://broker:4445", token="t",
                             scope=scope, name=name,
                             transport=httpx.MockTransport(broker))


#: One call per client method, each a (label, callable) on a client.
_EVERY_CALL = [
    ("info", lambda c: c.info()),
    ("list_terminals", lambda c: c.list_terminals()),
    ("list_profiles", lambda c: c.list_profiles()),
    ("read_screen", lambda c: c.read_screen(5)),
    ("send_input", lambda c: c.send_input(5, "x")),
    ("reset_terminal", lambda c: c.reset_terminal(5)),
    ("flush_input", lambda c: c.flush_input(5)),
    ("set_pace", lambda c: c.set_pace(5, 10)),
    ("launch_terminal", lambda c: c.launch_terminal()),
]


def test_a_scoped_client_sends_its_scope_on_every_request():
    """#232: the header rides EVERY request, the probe included."""
    broker = _ScopeBroker()
    with _scoped(broker) as c:
        for _label, call in _EVERY_CALL:
            call(c)
    assert len(broker.requests) == len(_EVERY_CALL)
    assert [r.headers.get(SCOPE_HEADER) for r in broker.requests] == [
        "teamA"] * len(_EVERY_CALL)


@pytest.mark.parametrize("scope", [None, ""], ids=["none", "empty"])
def test_an_unscoped_client_never_sends_the_header_or_probes(scope):
    """#232: no scope (or an empty one, which the broker reads as none): no
    header on any request, and no /mcp/info beyond the explicit info()."""
    broker = _ScopeBroker()
    with _scoped(broker, scope=scope) as c:
        for _label, call in _EVERY_CALL:
            call(c)
    assert all(SCOPE_HEADER not in r.headers for r in broker.requests)
    assert broker.count("/mcp/info") == 1
    assert len(broker.requests) == len(_EVERY_CALL)


@pytest.mark.parametrize("echo,reason", [
    (_OMIT, "predates scopes"), (None, "dropped"), ("teamB", "'teamB'")],
    ids=["no-key", "null", "other-scope"])
@pytest.mark.parametrize("label,call", _EVERY_CALL,
                         ids=[label for label, _ in _EVERY_CALL])
def test_an_unconfirmed_scope_fails_closed(echo, reason, label, call):
    """#232: a broker that does not echo the declared scope gets NOTHING but
    the probe: every call raises scope_unsupported, naming the host, the
    scope and why."""
    broker = _ScopeBroker(echo=echo)
    with _scoped(broker) as c:
        with pytest.raises(BrowserlandError) as ei:
            call(c)
    assert ei.value.code == "scope_unsupported"
    message = str(ei.value)
    assert "'local'" in message and "'teamA'" in message and reason in message
    assert [r.url.path for r in broker.requests] == ["/mcp/info"]


def test_a_confirmed_scope_is_probed_once():
    broker = _ScopeBroker()
    with _scoped(broker) as c:
        c.list_terminals()
        c.list_terminals()
    assert (broker.count("/mcp/info"), broker.count("/mcp/terminals")) == (1, 2)


def test_scoped_info_is_its_own_probe():
    """#232: info() on an unconfirmed scoped client is ONE GET, the probe
    itself; later calls are plain GETs."""
    broker = _ScopeBroker()
    with _scoped(broker) as c:
        assert c.info()["scope"] == "teamA"
        c.info()
    assert broker.count("/mcp/info") == 2


def test_an_unreachable_broker_is_probed_again():
    """#232: connection_error forgets the confirmation (the broker may come
    back as another build), so the next call probes first."""
    broker = _ScopeBroker(faults={"/mcp/terminals": [None, httpx.ConnectError]})
    with _scoped(broker) as c:
        c.list_terminals()
        with pytest.raises(BrowserlandError) as ei:
            c.list_terminals()
        assert ei.value.code == "connection_error"
        c.list_terminals()
    assert broker.count("/mcp/info") == 2


def test_a_codeless_error_is_probed_again_and_a_coded_one_is_not():
    """#232: a non-2xx with no broker error code (a proxy's 502 while the
    broker restarts) forgets the confirmation like connection_error does; a
    broker's own coded error (unknown_or_off) does not."""
    broker = _ScopeBroker(faults={"/mcp/terminals": [
        httpx.Response(404, json={"error": "unknown_or_off"}),
        httpx.Response(502, text="Bad Gateway")]})
    with _scoped(broker) as c:
        c.info()                                             # probe 1
        with pytest.raises(BrowserlandError) as coded:
            c.list_terminals()
        assert coded.value.code == "unknown_or_off"
        with pytest.raises(BrowserlandError) as codeless:
            c.list_terminals()                               # still confirmed
        assert codeless.value.code is None
        assert broker.count("/mcp/info") == 1
        c.list_terminals()                                   # probe 2
    assert broker.count("/mcp/info") == 2


def test_a_refused_scope_is_probed_again_on_the_next_call():
    """#232: only a match is remembered, so a broker upgraded (or a proxy
    fixed) between two calls is picked up without a restart."""
    broker = _ScopeBroker(echo=_OMIT)
    with _scoped(broker) as c:
        with pytest.raises(BrowserlandError):
            c.list_terminals()
        broker.echo = _ECHO
        assert c.list_terminals() == []
    assert broker.count("/mcp/info") == 2


def test_a_probe_failing_for_another_reason_raises_that_error():
    """#232: a probe that cannot read /mcp/info (here a bad token) raises
    the broker's own error, not scope_unsupported, and confirms nothing."""
    broker = _ScopeBroker(faults={"/mcp/info": [
        httpx.Response(401, json={"error": "auth_required"})]})
    with _scoped(broker) as c:
        with pytest.raises(BrowserlandError) as ei:
            c.list_terminals()
        assert ei.value.code == "auth_required"
        c.list_terminals()
    assert broker.count("/mcp/info") == 2
    assert broker.count("/mcp/terminals") == 1


def test_bad_scope_surfaces_with_its_message():
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": "bad_scope"})

    with _client(handler) as c:
        with pytest.raises(BrowserlandError) as ei:
            c.list_terminals()
    assert ei.value.code == "bad_scope"
    assert "scope name" in str(ei.value) and "(HTTP 400)" in str(ei.value)


@pytest.mark.parametrize("code,fragment", [
    ("bad_mode", "'mode'"), ("prespawn_failed", "spawned nothing")])
def test_the_launch_errors_surface_with_their_messages(code, fragment):
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": code})

    with _client(handler) as c:
        with pytest.raises(BrowserlandError) as ei:
            c.launch_terminal()
    assert fragment in str(ei.value)


def test_launch_sends_mode_only_when_given():
    broker = _ScopeBroker()
    with _scoped(broker, scope=None) as c:
        c.launch_terminal(mode="read")
        c.launch_terminal()
    bodies = [json.loads(r.content) for r in broker.requests]
    assert bodies[0]["mode"] == "read"
    assert "mode" not in bodies[1]


# ---- token / config resolution (__main__) --------------------------------

def test_token_precedence_flag_over_env(monkeypatch):
    from webterm.mcptool import __main__ as m

    monkeypatch.setenv(m.TOKEN_ENV, "from-env")
    args = m._parse_args(["--token", "from-flag"])
    assert m._resolve_token(args) == "from-flag"


def test_token_env_primary_over_alt(monkeypatch):
    from webterm.mcptool import __main__ as m

    monkeypatch.setenv(m.TOKEN_ENV, "primary")
    monkeypatch.setenv(m.TOKEN_ENV_ALT, "alt")
    args = m._parse_args([])
    assert m._resolve_token(args) == "primary"


def test_token_env_alt_fallback(monkeypatch):
    from webterm.mcptool import __main__ as m

    monkeypatch.delenv(m.TOKEN_ENV, raising=False)
    monkeypatch.setenv(m.TOKEN_ENV_ALT, "alt")
    args = m._parse_args([])
    assert m._resolve_token(args) == "alt"


def test_token_from_file_when_no_flag_or_env(tmp_path, monkeypatch):
    from webterm.mcptool import __main__ as m

    monkeypatch.delenv(m.TOKEN_ENV, raising=False)
    monkeypatch.delenv(m.TOKEN_ENV_ALT, raising=False)
    sidecar = tmp_path / "webterm_mcp.json"
    sidecar.write_text(json.dumps({"token": "from-file", "enabled": True}))
    args = m._parse_args(["--token-file", str(sidecar)])
    assert m._resolve_token(args) == "from-file"


def test_token_file_null_returns_none(tmp_path, monkeypatch):
    from webterm.mcptool import __main__ as m

    monkeypatch.delenv(m.TOKEN_ENV, raising=False)
    monkeypatch.delenv(m.TOKEN_ENV_ALT, raising=False)
    sidecar = tmp_path / "webterm_mcp.json"
    sidecar.write_text(json.dumps({"token": None}))  # broker pins via env
    args = m._parse_args(["--token-file", str(sidecar)])
    assert m._resolve_token(args) is None


def test_broker_url_default_and_env(monkeypatch):
    from webterm.mcptool import __main__ as m

    monkeypatch.delenv(m.URL_ENV, raising=False)
    assert m._parse_args([]).broker_url == m.DEFAULT_URL
    monkeypatch.setenv(m.URL_ENV, "http://elsewhere:9999")
    assert m._parse_args([]).broker_url == "http://elsewhere:9999"


def test_main_missing_token_exits_2(monkeypatch, capsys):
    from webterm.mcptool import __main__ as m

    monkeypatch.delenv(m.TOKEN_ENV, raising=False)
    monkeypatch.delenv(m.TOKEN_ENV_ALT, raising=False)
    monkeypatch.delenv(m.HOSTS_ENV, raising=False)
    assert m.main([]) == 2
    assert "no MCP token" in capsys.readouterr().err


# ---- #24: multi-host parsing (_parse_hosts / _resolve_hosts) -------------

def test_parse_hosts_valid():
    from webterm.mcptool import __main__ as m
    raw = json.dumps([{"name": "a", "url": "http://x:4445", "token": "t1"},
                      {"name": "b", "url": "https://y:4445", "token": "t2"}])
    assert m._parse_hosts(raw) == [("a", "http://x:4445", "t1", None, None, None),
                                    ("b", "https://y:4445", "t2", None, None, None)]


@pytest.mark.parametrize("raw", [
    "not json",                                              # not JSON
    "{}",                                                    # object, not array
    "[]",                                                    # empty array
    '["nope"]',                                              # entry not an object
    json.dumps([{"name": "a", "url": "http://x"}]),          # missing token
    json.dumps([{"name": "a", "url": "http://x", "token": ""}]),   # empty token
    json.dumps([{"name": "", "url": "http://x", "token": "t"}]),   # empty name
    json.dumps([{"name": "a:b", "url": "http://x", "token": "t"}]),  # colon in name
    json.dumps([{"name": "a", "url": 123, "token": "t"}]),    # url present but not a string
    json.dumps([{"name": "a", "url": "u", "token": 5}]),      # token present but not a string
    json.dumps([{"name": "a", "url": "u", "token": "t"},
                {"name": "a", "url": "u2", "token": "t2"}]),  # duplicate name
])
def test_parse_hosts_rejects(raw):
    from webterm.mcptool import __main__ as m
    with pytest.raises(ValueError):
        m._parse_hosts(raw)


def test_resolve_hosts_single_default(monkeypatch):
    from webterm.mcptool import __main__ as m
    monkeypatch.delenv(m.HOSTS_ENV, raising=False)
    args = m._parse_args(["--token", "t", "--broker-url", "http://h:4445"])
    assert m._resolve_hosts(args) == [("default", "http://h:4445", "t", None,
                                       None, None)]


def test_resolve_hosts_multi_from_env(monkeypatch):
    from webterm.mcptool import __main__ as m
    raw = json.dumps([{"name": "a", "url": "http://x:4445", "token": "t"}])
    monkeypatch.setenv(m.HOSTS_ENV, raw)
    args = m._parse_args([])                       # --hosts defaults from env
    assert m._resolve_hosts(args) == [("a", "http://x:4445", "t", None, None, None)]


def test_resolve_hosts_flag_beats_single_host(monkeypatch):
    from webterm.mcptool import __main__ as m
    monkeypatch.delenv(m.HOSTS_ENV, raising=False)
    raw = json.dumps([{"name": "a", "url": "http://x:4445", "token": "ta"}])
    args = m._parse_args(["--hosts", raw, "--token", "ignored",
                          "--broker-url", "http://ignored:4445"])
    assert m._resolve_hosts(args) == [("a", "http://x:4445", "ta", None, None, None)]


def _hosts_json(*entries):
    """A --hosts value: each entry gets url http://<name>:4445 and token t
    unless it names its own."""
    return json.dumps([dict({"url": f"http://{e['name']}:4445", "token": "t"},
                            **e) for e in entries])


def test_parse_hosts_reads_a_per_host_scope():
    from webterm.mcptool import __main__ as m
    raw = _hosts_json({"name": "a", "scope": "teamA"}, {"name": "b"})
    assert m._parse_hosts(raw) == [("a", "http://a:4445", "t", "teamA", None, None),
                                    ("b", "http://b:4445", "t", None, None, None)]


@pytest.mark.parametrize("entry", [{"name": "b"}, {"name": "b", "scope": ""}],
                         ids=["absent", "empty"])
def test_an_absent_or_empty_host_scope_inherits(entry):
    from webterm.mcptool import __main__ as m
    raw = _hosts_json({"name": "a", "scope": "teamA"}, entry)
    assert m._parse_hosts(raw, "proc") == [
        ("a", "http://a:4445", "t", "teamA", None, None),
        ("b", "http://b:4445", "t", "proc", None, None)]


@pytest.mark.parametrize("bad", ["a b", "a:b", "x" * 65, "-lead", 5, None,
                                 ["a"]],
                         ids=["space", "colon", "too-long", "lead-dash",
                              "int", "null", "list"])
def test_a_bad_host_scope_exits_2_naming_the_host(capsys, bad):
    """#232: a per-host scope must be a string in the broker's grammar
    ("" or absent inherits); anything else stops startup and says which
    host."""
    from webterm.mcptool import __main__ as m
    raw = _hosts_json({"name": "a"}, {"name": "second", "scope": bad})
    assert m.main(["--hosts", raw]) == 2
    err = capsys.readouterr().err
    assert "--hosts[1] ('second')" in err and "scope" in err


def test_the_scope_env_applies_to_every_host(monkeypatch):
    from webterm.mcptool import __main__ as m
    monkeypatch.setenv(m.SCOPE_ENV, "envscope")
    multi = m._parse_args(["--hosts", _hosts_json({"name": "a"},
                                                  {"name": "b"})])
    assert [h[3] for h in m._resolve_hosts(multi)] == ["envscope",
                                                       "envscope"]
    single = m._parse_args(["--token", "t", "--broker-url", "http://h:4445"])
    assert m._resolve_hosts(single) == [("default", "http://h:4445", "t",
                                         "envscope", None, None)]


def test_a_host_scope_beats_the_env(monkeypatch):
    from webterm.mcptool import __main__ as m
    monkeypatch.setenv(m.SCOPE_ENV, "envscope")
    args = m._parse_args(["--hosts", _hosts_json({"name": "a", "scope": "mine"},
                                                 {"name": "b"})])
    assert [h[3] for h in m._resolve_hosts(args)] == ["mine", "envscope"]


def test_the_scope_flag_beats_the_env(monkeypatch):
    from webterm.mcptool import __main__ as m
    monkeypatch.setenv(m.SCOPE_ENV, "envscope")
    args = m._parse_args(["--scope", "flagscope", "--token", "t",
                          "--broker-url", "http://h:4445"])
    assert m._resolve_hosts(args) == [("default", "http://h:4445", "t",
                                       "flagscope", None, None)]


@pytest.mark.parametrize("source", ["flag", "env"])
def test_an_invalid_process_scope_exits_2_naming_both_sources(
        monkeypatch, capsys, source):
    """#232: --scope takes its default from the env, so one message names
    both places the value may have come from."""
    from webterm.mcptool import __main__ as m
    argv = ["--token", "t"]
    if source == "flag":
        argv += ["--scope", "a b"]
    else:
        monkeypatch.setenv(m.SCOPE_ENV, "a b")
    assert m.main(argv) == 2
    assert ("--scope/$BROWSERLAND_MCP_SCOPE 'a b' is not a valid scope name"
            in capsys.readouterr().err)


def test_main_wires_scopes_into_configure(monkeypatch):
    from webterm.mcptool import __main__ as m
    from webterm.mcptool import server

    captured = {}
    monkeypatch.setattr(server, "configure",
                        lambda hosts: captured.__setitem__("hosts", list(hosts)))
    monkeypatch.setattr(server.mcp, "run", lambda: None)
    raw = _hosts_json({"name": "a"}, {"name": "b", "scope": "teamB"})
    assert m.main(["--scope", "teamA", "--hosts", raw]) == 0
    assert captured["hosts"] == [("a", "http://a:4445", "t", "teamA", None, None),
                                 ("b", "http://b:4445", "t", "teamB", None, None)]


def test_resolve_hosts_single_no_token_returns_none(monkeypatch):
    from webterm.mcptool import __main__ as m
    monkeypatch.delenv(m.HOSTS_ENV, raising=False)
    monkeypatch.delenv(m.TOKEN_ENV, raising=False)
    monkeypatch.delenv(m.TOKEN_ENV_ALT, raising=False)
    args = m._parse_args([])
    assert m._resolve_hosts(args) is None


# ---- #24: namespaced-id routing across hosts ------------------------------

def _ok(payload):
    """A handler that records (path, body) into `seen[name]` and returns payload."""
    def make(name, seen):
        def handler(req: httpx.Request) -> httpx.Response:
            body = json.loads(req.content) if req.content else None
            seen.setdefault(name, []).append((req.url.path, body))
            return httpx.Response(200, json=payload)
        return handler
    return make


def test_route_sends_each_id_to_its_host():
    seen = {}
    mk = _ok({"ok": True})
    server = _install({"local": mk("local", seen), "remote": mk("remote", seen)})
    try:
        server.send_input("remote:42", "hi\n")
        server.send_input("local:7", "yo\n")
        server.read_screen("remote:9")
    finally:
        _reset_server()
    assert seen["remote"] == [("/mcp/input", {"id": 42, "data": "hi\r"}),
                              ("/mcp/read", {"id": 9})]
    assert seen["local"] == [("/mcp/input", {"id": 7, "data": "yo\r"})]


def test_route_unknown_host_raises():
    server = _install({"local": lambda r: httpx.Response(200, json={"ok": True})})
    try:
        with pytest.raises(BrowserlandError) as ei:
            server.send_input("ghost:1", "x")
    finally:
        _reset_server()
    assert ei.value.code == "unknown_host"


@pytest.mark.parametrize("bad", [
    "1", "local:", "local:abc", ":5", "a:b:c", 5, None,
    "local:-1",       # sign rejected (int() would accept it)
    "local:1_000",    # underscore rejected (int() would accept it)
    "local: 5",       # surrounding whitespace rejected
    "local:٥",   # Unicode digit rejected (isdigit() true, isascii() false)
    "ghost:abc",      # bad int part is reported before the unknown host
])
def test_route_malformed_id_raises(bad):
    server = _install({"local": lambda r: httpx.Response(200, json={"ok": True})})
    try:
        with pytest.raises(BrowserlandError) as ei:
            server.read_screen(bad)
    finally:
        _reset_server()
    # The host part is only checked once the "<host>:<int>" shape parses, so a
    # malformed int (even with an unknown host like "ghost:abc") is malformed_id,
    # never unknown_host. This pins that ordering.
    assert ei.value.code == "malformed_id"


def test_send_keys_routes_decckm_lookup_to_same_host():
    # The DECCKM cache read must hit the routed host's client, not some default.
    def remote_h(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/mcp/terminals":
            return httpx.Response(200, json=[{"id": 9, "app_cursor": True}])
        return httpx.Response(200, json={"ok": True})

    def local_h(req: httpx.Request) -> httpx.Response:
        # If routing leaked to 'local', it would report no app_cursor -> CSI.
        if req.url.path == "/mcp/terminals":
            return httpx.Response(200, json=[{"id": 9, "app_cursor": False}])
        return httpx.Response(200, json={"ok": True})

    seen = {}

    def wrap(name, inner):
        def handler(req: httpx.Request) -> httpx.Response:
            r = inner(req)
            if req.url.path == "/mcp/input":
                seen[name] = json.loads(req.content)
            return r
        return handler

    server = _install({"local": wrap("local", local_h),
                       "remote": wrap("remote", remote_h)})
    try:
        server.send_keys("remote:9", ["Up"])
    finally:
        _reset_server()
    assert seen == {"remote": {"id": 9, "data": "\x1bOA"}}   # SS3 from remote's DECCKM


# ---- #24: list_terminals merge + per-host error isolation -----------------

def test_list_terminals_merges_and_namespaces():
    def local_h(req):
        return httpx.Response(200, json=[{"id": 1, "mode": "read"},
                                         {"id": 2, "mode": "readwrite"}])
    def remote_h(req):
        return httpx.Response(200, json=[{"id": 5, "mode": "read"}])
    server = _install({"local": local_h, "remote": remote_h})
    try:
        out = server.list_terminals()
    finally:
        _reset_server()
    assert out["errors"] == {}
    got = {(t["host"], t["id"]) for t in out["terminals"]}
    assert got == {("local", "local:1"), ("local", "local:2"), ("remote", "remote:5")}


def test_list_terminals_one_host_down_does_not_suppress_others():
    def local_h(req):
        return httpx.Response(200, json=[{"id": 1, "mode": "read"}])
    def remote_h(req):
        return httpx.Response(503, json={"error": "mcp_disabled"})
    server = _install({"local": local_h, "remote": remote_h})
    try:
        out = server.list_terminals()
    finally:
        _reset_server()
    assert [t["id"] for t in out["terminals"]] == ["local:1"]   # good host kept
    assert "remote" in out["errors"] and "disabled" in out["errors"]["remote"]


# ---- #24: mcp_info / list_profiles aggregate vs per-host ------------------

def test_mcp_info_aggregated_and_per_host():
    def local_h(req):
        return httpx.Response(200, json={"ok": True, "allow_launch": True,
                                         "default_mode": "read"})
    def remote_h(req):
        return httpx.Response(200, json={"ok": True, "allow_launch": False,
                                         "default_mode": "off"})
    server = _install({"local": local_h, "remote": remote_h})
    try:
        agg = server.mcp_info()
        one = server.mcp_info(host="remote")
    finally:
        _reset_server()
    assert set(agg) == {"local", "remote"}
    assert agg["local"]["allow_launch"] is True
    assert agg["remote"]["default_mode"] == "off"
    assert one == {"ok": True, "allow_launch": False, "default_mode": "off"}


def test_mcp_info_aggregated_isolates_host_error():
    def local_h(req):
        return httpx.Response(200, json={"ok": True, "default_mode": "off"})
    def remote_h(req):
        return httpx.Response(401, json={"error": "auth_required"})
    server = _install({"local": local_h, "remote": remote_h})
    try:
        agg = server.mcp_info()
    finally:
        _reset_server()
    assert agg["local"]["ok"] is True
    # The error sentinel carries ok: False so it's distinguishable from real data.
    assert agg["remote"]["ok"] is False and "error" in agg["remote"]


def test_list_profiles_aggregated_and_per_host():
    def local_h(req):
        return httpx.Response(200, json={"default": "bash", "profiles": ["bash"]})
    def remote_h(req):
        return httpx.Response(200, json={"default": "pwsh", "profiles": ["pwsh"]})
    server = _install({"local": local_h, "remote": remote_h})
    try:
        agg = server.list_profiles()
        one = server.list_profiles(host="local")
    finally:
        _reset_server()
    assert agg["local"]["default"] == "bash" and agg["remote"]["default"] == "pwsh"
    assert one == {"default": "bash", "profiles": ["bash"]}


def test_per_host_tool_unknown_host_raises():
    server = _install({"local": lambda r: httpx.Response(200, json={"ok": True})})
    try:
        with pytest.raises(BrowserlandError) as ei:
            server.mcp_info(host="ghost")
    finally:
        _reset_server()
    assert ei.value.code == "unknown_host"


# ---- #24: launch_terminal host selection + namespaced result id -----------

def test_launch_single_host_optional_and_namespaces_id():
    def h(req):
        return httpx.Response(200, json={"ok": True, "id": 7, "registered": True})
    server = _install({"default": h})
    try:
        out = server.launch_terminal(profile="bash")     # host omitted is fine
    finally:
        _reset_server()
    assert out["id"] == "default:7" and out["registered"] is True


def test_launch_routes_to_named_host_and_namespaces_id():
    def mk(name):
        def h(req):
            return httpx.Response(200, json={"ok": True, "id": 3, "seen": name})
        return h
    server = _install({"local": mk("local"), "remote": mk("remote")})
    try:
        out = server.launch_terminal(profile="bash", host="remote")
    finally:
        _reset_server()
    assert out["id"] == "remote:3" and out["seen"] == "remote"


def test_launch_requires_host_when_multiple():
    server = _install({"local": lambda r: httpx.Response(200, json={"ok": True}),
                       "remote": lambda r: httpx.Response(200, json={"ok": True})})
    try:
        with pytest.raises(BrowserlandError) as ei:
            server.launch_terminal(profile="bash")
    finally:
        _reset_server()
    assert ei.value.code == "host_required"


def test_launch_unknown_named_host_raises():
    server = _install({"local": lambda r: httpx.Response(200, json={"ok": True})})
    try:
        with pytest.raises(BrowserlandError) as ei:
            server.launch_terminal(profile="bash", host="ghost")
    finally:
        _reset_server()
    assert ei.value.code == "unknown_host"


def test_launch_result_without_id_returned_verbatim():
    # A launch error payload (HTTP 200, no `id`) must pass through untouched —
    # the namespacing guard must not KeyError on the missing id.
    def h(req):
        return httpx.Response(200, json={"ok": False, "error": "unknown_profile"})
    server = _install({"default": h})
    try:
        out = server.launch_terminal(profile="bash")
    finally:
        _reset_server()
    assert out == {"ok": False, "error": "unknown_profile"}   # no "id" injected


# ---- #24: list_terminals host-field handling + payload robustness ---------

def test_list_terminals_sets_config_host_and_preserves_machine_host():
    # The broker already sends `host` = machine hostname; the tool sets `host` to
    # the config name and moves the machine hostname to `machine_host`.
    def h(req):
        return httpx.Response(200, json=[{"id": 1, "mode": "read", "host": "JC-SERVER"}])
    server = _install({"local": h})
    try:
        out = server.list_terminals()
    finally:
        _reset_server()
    t = out["terminals"][0]
    assert t["host"] == "local"          # config name (per spec)
    assert t["machine_host"] == "JC-SERVER"   # machine hostname preserved
    assert t["id"] == "local:1"


def test_list_terminals_terminal_without_id_is_merged():
    def h(req):
        return httpx.Response(200, json=[{"mode": "read"}])   # no id field
    server = _install({"local": h})
    try:
        out = server.list_terminals()
    finally:
        _reset_server()
    t = out["terminals"][0]
    assert t == {"mode": "read", "host": "local"}   # merged, host added, no id rewrite


def test_list_terminals_isolates_non_json_host():
    # A host answering HTTP 200 with a non-JSON body raises JSONDecodeError (a
    # ValueError, NOT a BrowserlandError) — it must still not sink the good host.
    def good(req):
        return httpx.Response(200, json=[{"id": 1, "mode": "read"}])
    def bad(req):
        return httpx.Response(200, content=b"<html>not json</html>")
    server = _install({"good": good, "bad": bad})
    try:
        out = server.list_terminals()
    finally:
        _reset_server()
    assert [t["id"] for t in out["terminals"]] == ["good:1"]   # good host survives
    assert "bad" in out["errors"]


def test_aggregate_isolates_non_json_host():
    def good(req):
        return httpx.Response(200, json={"ok": True, "default_mode": "off"})
    def bad(req):
        return httpx.Response(200, content=b"<html>not json</html>")
    server = _install({"good": good, "bad": bad})
    try:
        agg = server.mcp_info()
    finally:
        _reset_server()
    assert agg["good"]["ok"] is True
    assert agg["bad"]["ok"] is False and "error" in agg["bad"]


def test_send_keys_decckm_lookup_tolerates_malformed_terminals():
    # The DECCKM cache read uses a broad except: a /mcp/terminals payload that
    # breaks iteration (list of non-dicts) must fall back to CSI, not raise.
    seen = {}
    def h(req):
        if req.url.path == "/mcp/terminals":
            return httpx.Response(200, json=["notadict"])
        seen["body"] = json.loads(req.content)
        return httpx.Response(200, json={"ok": True})
    server = _install({"local": h})
    try:
        server.send_keys("local:9", ["Up"])
    finally:
        _reset_server()
    assert seen["body"] == {"id": 9, "data": "\x1b[A"}   # CSI fallback, no crash


# ---- #24: empty-host string falls through to the single-host default ------

def test_empty_host_string_uses_single_default():
    # MCP clients often pass "" for an optional string param; "" must behave like
    # omitted, not route to _named_client("") -> unknown_host.
    def h(req):
        if req.url.path == "/mcp/launch":
            return httpx.Response(200, json={"ok": True, "id": 4})
        return httpx.Response(200, json={"ok": True, "default_mode": "off"})
    server = _install({"default": h})
    try:
        assert server.launch_terminal(profile="bash", host="")["id"] == "default:4"
        agg = server.mcp_info(host="")
    finally:
        _reset_server()
    assert set(agg) == {"default"} and agg["default"]["ok"] is True   # aggregate form


# ---- #24: configure() lazy build + reconfigure, and main() wiring ---------

def test_configure_builds_clients_lazily_and_routes(monkeypatch):
    from webterm.mcptool import server

    built = []

    def factory(base, token, **kwargs):
        built.append(base)

        def handler(req: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"ok": True})

        return BrowserlandClient(base=base, token=token,
                                 transport=httpx.MockTransport(handler))

    monkeypatch.setattr(server, "BrowserlandClient", factory)
    server.configure([("local", "http://local:4445", "t", None),
                      ("remote", "http://remote:4445", "t", None)])
    try:
        assert built == []                              # lazy: nothing built yet
        server.send_input("remote:5", "hi\n")           # builds only the routed host
        assert built == ["http://remote:4445"]
        server.send_input("remote:6", "yo\n")           # reuses the cached client
        assert built == ["http://remote:4445"]
    finally:
        server.configure([])


def test_configure_closes_old_clients_on_reconfigure():
    from webterm.mcptool import server

    class SpyClient:
        def __init__(self):
            self.closed = False

        def close(self):
            self.closed = True

    spy = SpyClient()
    server._host_configs = {"old": ("http://old:4445", "t", None)}
    server._clients = {"old": spy}
    try:
        server.configure([("new", "http://new:4445", "t", None)])
        assert spy.closed is True                       # old client was closed
        assert set(server._host_configs) == {"new"}     # map rebuilt
        assert server._clients == {}                    # new clients are lazy
    finally:
        server._host_configs = {}
        server._clients = {}


def test_main_wires_multi_host_into_configure(monkeypatch):
    from webterm.mcptool import __main__ as m
    from webterm.mcptool import server

    captured = {}
    monkeypatch.setattr(server, "configure",
                        lambda hosts: captured.__setitem__("hosts", list(hosts)))
    monkeypatch.setattr(server.mcp, "run", lambda: None)

    raw = json.dumps([{"name": "a", "url": "http://x:4445", "token": "ta"},
                      {"name": "b", "url": "http://y:4445", "token": "tb"}])
    assert m.main(["--hosts", raw]) == 0
    assert captured["hosts"] == [("a", "http://x:4445", "ta", None, None, None),
                                 ("b", "http://y:4445", "tb", None, None, None)]


# ---- tool smoke test (FastMCP server wired to a MockTransport) -----------

@pytest.mark.asyncio
async def test_tools_registered():
    from webterm.mcptool import server

    tools = await server.mcp.list_tools()
    names = sorted(t.name for t in tools)
    assert names == ["arrange_window", "flush_input", "get_desktop",
                     "launch_terminal", "list_profiles", "list_terminals",
                     "mcp_info", "read_screen", "reset_terminal",
                     "send_input", "send_keys", "set_pace", "set_window"]


def test_tools_round_trip_through_http():
    from webterm.mcptool import server

    calls = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append((req.url.path, json.loads(req.content) if req.content else None))
        if req.url.path == "/mcp/read":
            return httpx.Response(200, json={"ok": True, "id": 5, "cols": 80,
                                             "rows": 24, "text": "screen"})
        return httpx.Response(200, json={"ok": True})

    # Inject a stubbed host into the per-host map the tools route through; the
    # namespaced "default:5" id routes to it and unwraps to the int id 5 on the
    # wire.
    _install({"default": handler})
    try:
        assert server.read_screen("default:5")["text"] == "screen"
        # The tool maps the logical Enter ("\n") to a carriage return so it
        # submits on PowerShell (#13); the wire payload is therefore "ls\r".
        assert server.send_input("default:5", "ls\n") == {"ok": True}
    finally:
        _reset_server()

    assert calls == [
        ("/mcp/read", {"id": 5}),
        ("/mcp/input", {"id": 5, "data": "ls\r"}),
    ]


def test_read_screen_tool_passes_attrs_and_returns_runs():
    """#128: the read_screen tool forwards attrs=true to the wire and returns the
    agent's attr_runs unchanged, so a color-only selection is visible."""
    from webterm.mcptool import server

    seen = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(req.content)
        return httpx.Response(200, json={
            "ok": True, "id": 5, "cols": 80, "rows": 24, "text": "menu",
            "attr_runs": [{"row": 2, "col": 0, "len": 20, "fg": "default",
                           "bg": "default", "reverse": True}]})

    _install({"default": handler})
    try:
        out = server.read_screen("default:5", attrs=True)
    finally:
        _reset_server()
    assert seen["body"] == {"id": 5, "attrs": True}   # int id + attrs on the wire
    assert out["attr_runs"][0]["reverse"] is True


def test_flush_input_tool_routes_to_wire():
    """#133: the flush_input tool unwraps the namespaced id and POSTs the bare
    int to /mcp/flush, returning the broker's reply."""
    from webterm.mcptool import server

    seen = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["path"] = req.url.path
        seen["body"] = json.loads(req.content)
        return httpx.Response(200, json={"ok": True, "id": 5})

    _install({"default": handler})
    try:
        out = server.flush_input("default:5")
    finally:
        _reset_server()
    assert seen["path"] == "/mcp/flush"
    assert seen["body"] == {"id": 5}                  # namespaced id -> bare int
    assert out == {"ok": True, "id": 5}


def test_set_pace_tool_routes_to_wire():
    """#133: the set_pace tool unwraps the namespaced id and POSTs
    {id, pace_ms} to /mcp/pace, returning the broker's (clamped) reply."""
    from webterm.mcptool import server

    seen = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["path"] = req.url.path
        seen["body"] = json.loads(req.content)
        return httpx.Response(200, json={"ok": True, "id": 5, "pace_ms": 40})

    _install({"default": handler})
    try:
        out = server.set_pace("default:5", 40)
    finally:
        _reset_server()
    assert seen["path"] == "/mcp/pace"
    assert seen["body"] == {"id": 5, "pace_ms": 40}   # namespaced id -> bare int
    assert out == {"ok": True, "id": 5, "pace_ms": 40}


# ---- #232: scoped hosts through the tools -----------------------------------

def _install_hosts(hosts):
    """Install prebuilt clients: ``hosts`` maps a name to ``(broker, scope)``
    (a _ScopeBroker handler and that host's scope, None = unscoped), built
    the way configure()'s _get_client builds them."""
    from webterm.mcptool import server
    server._host_configs = {n: (f"http://{n}:4445", "t", scope)
                            for n, (_broker, scope) in hosts.items()}
    server._clients = {n: BrowserlandClient(
        base=f"http://{n}:4445", token="t", scope=scope, name=n,
        transport=httpx.MockTransport(broker))
        for n, (broker, scope) in hosts.items()}
    return server


def test_a_host_that_fails_its_probe_is_isolated():
    """#232: a scoped host that does not echo its scope contributes no
    terminals and a scope_unsupported error naming it; the other host's
    terminals still arrive."""
    good = _ScopeBroker(terminals=[{"id": 1, "scope": None}])
    bad = _ScopeBroker(echo=_OMIT, terminals=[{"id": 2, "scope": None}])
    server = _install_hosts({"good": (good, None), "bad": (bad, "teamA")})
    try:
        out = server.list_terminals()
    finally:
        _reset_server()
    assert [t["id"] for t in out["terminals"]] == ["good:1"]
    assert list(out["errors"]) == ["bad"]
    assert "did not confirm" in out["errors"]["bad"]
    assert "'bad'" in out["errors"]["bad"]
    assert bad.count("/mcp/terminals") == 0


def test_a_scoped_host_is_probed_once_across_tool_calls():
    broker = _ScopeBroker(terminals=[{"id": 1, "scope": "teamA"}])
    server = _install_hosts({"h": (broker, "teamA")})
    try:
        server.list_terminals()
        server.send_input("h:1", "x")
    finally:
        _reset_server()
    assert broker.count("/mcp/info") == 1
    assert (broker.count("/mcp/terminals"), broker.count("/mcp/input")) == (1,
                                                                          1)


def test_an_unscoped_host_is_never_probed():
    broker = _ScopeBroker(terminals=[{"id": 1, "scope": "teamA"}])
    server = _install_hosts({"h": (broker, None)})
    try:
        server.list_terminals()
        server.send_input("h:1", "x")
        server.read_screen("h:1")
    finally:
        _reset_server()
    assert broker.count("/mcp/info") == 0
    assert all(SCOPE_HEADER not in r.headers for r in broker.requests)


def test_a_scoped_host_is_probed_again_after_it_was_unreachable():
    broker = _ScopeBroker(faults={"/mcp/input": [None, httpx.ConnectError]})
    server = _install_hosts({"h": (broker, "teamA")})
    try:
        server.send_input("h:1", "x")
        with pytest.raises(BrowserlandError) as ei:
            server.send_input("h:1", "x")
        assert ei.value.code == "connection_error"
        server.send_input("h:1", "x")
    finally:
        _reset_server()
    assert broker.count("/mcp/info") == 2


def test_configure_hands_each_host_its_scope_and_name():
    """#232: _get_client builds each host's client with that host's scope
    (header + probe) and its configured name (for messages)."""
    from webterm.mcptool import server
    server.configure([("a", "http://a:4445", "t", "teamA"),
                      ("b", "http://b:4445", "t", None)])
    try:
        a, b = server._get_client("a"), server._get_client("b")
        assert (a.scope, a.name, b.scope, b.name) == ("teamA", "a", None, "b")
    finally:
        server.configure([])


def test_list_terminals_passes_each_rows_scope_through():
    scoped = _ScopeBroker(terminals=[{"id": 1, "scope": "teamA"}])
    open_ = _ScopeBroker(terminals=[{"id": 2, "scope": None},
                                    {"id": 3, "scope": "teamB"}])
    server = _install_hosts({"s": (scoped, "teamA"), "o": (open_, None)})
    try:
        out = server.list_terminals()
    finally:
        _reset_server()
    assert [(t["id"], t["scope"]) for t in out["terminals"]] == [
        ("s:1", "teamA"), ("o:2", None), ("o:3", "teamB")]
    assert out["errors"] == {}


@pytest.mark.parametrize("row", [{"id": 9, "scope": "teamB"}, {"id": 9},
                                 {"id": 9, "scope": None}],
                         ids=["other-scope", "no-key", "untagged"])
def test_a_scoped_listing_with_a_foreign_row_fails_closed(row):
    """#232: the broker confirmed the scope but listed a window without that
    tag: its filter disagrees with its echo, so the whole listing is refused
    rather than leaking that row."""
    broker = _ScopeBroker(terminals=[{"id": 1, "scope": "teamA"}, row])
    server = _install_hosts({"h": (broker, "teamA")})
    try:
        out = server.list_terminals()
    finally:
        _reset_server()
    assert out["terminals"] == []
    assert "listed window 9" in out["errors"]["h"]


def test_the_launch_tool_passes_mode_through():
    broker = _ScopeBroker()
    server = _install_hosts({"h": (broker, None)})
    try:
        server.launch_terminal(mode="read")
        server.launch_terminal(mode="")
    finally:
        _reset_server()
    bodies = [json.loads(r.content) for r in broker.requests]
    assert bodies[0]["mode"] == "read"
    assert "mode" not in bodies[1]


async def test_the_tool_descriptions_explain_scopes():
    """#232: the descriptions are what the model reads: a scoped server sees
    only its own windows (hand-started ones stay invisible), a scoped launch
    comes up readwrite unless `mode` is given, and mcp_info shows the
    scope."""
    from webterm.mcptool import server

    tools = {t.name: t for t in await server.mcp.list_tools()}

    def text(name):                    # docstring line wraps flattened
        return " ".join(tools[name].description.split())

    assert "sees ONLY the windows tagged with that scope" in text(
        "list_terminals")
    assert "started by hand stay invisible" in text("list_terminals")
    assert "or 'readwrite' when `mode` is omitted" in text("launch_terminal")
    assert "mode" in tools["launch_terminal"].inputSchema["properties"]
    assert "the scope this server declared" in text("mcp_info")


def test_the_mcp_server_never_imports_the_broker(tmp_path):
    """#232: the MCP server is the optional `mcp` extra and must not pull in
    webterm.broker (and sanic with it). A FRESH interpreter, because this
    test process imported the broker long ago; it also proves the scope
    names really came from webterm.protocol."""
    import os
    import subprocess
    import sys
    from pathlib import Path

    # PYTHONPATH makes the fresh interpreter import THIS checkout; cwd keeps
    # anything it might write out of the repo.
    repo = Path(__file__).resolve().parents[1]
    code = ("import sys\n"
            "import webterm.mcptool.__main__, webterm.mcptool.server\n"
            "import webterm.mcptool.client\n"
            "print(sorted(m for m in sys.modules\n"
            "             if m == 'webterm.broker'\n"
            "             or m.startswith('webterm.broker.')))\n"
            "print('webterm.protocol' in sys.modules)\n")
    env = dict(os.environ)
    env["PYTHONPATH"] = str(repo) + os.pathsep + env.get("PYTHONPATH", "")
    out = subprocess.run([sys.executable, "-c", code], cwd=str(tmp_path),
                         env=env, capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stderr
    assert out.stdout.split() == ["[]", "True"]


def test_the_mcp_server_uses_the_protocol_scope_names():
    """#232: the grammar the MCP server validates with and the header it
    sends are webterm.protocol's own objects, the ones the broker re-exports,
    so the two ends cannot drift apart."""
    import webterm.protocol as protocol
    from webterm.mcptool import __main__ as m
    from webterm.mcptool import client

    assert m.SCOPE_RE is protocol.SCOPE_RE
    assert client.SCOPE_HEADER is protocol.SCOPE_HEADER


def test_the_readme_documents_scopes():
    """#232: the README is the user-facing contract for the flag, the env,
    the Scopes section and launch_terminal's mode."""
    from pathlib import Path

    readme = (Path(__file__).resolve().parents[1] / "webterm" / "mcptool"
              / "README.md").read_text(encoding="utf-8").splitlines()
    section = readme[readme.index("## Configure"):
                     readme.index("### Multi-host (#24)")]
    config_rows = [line for line in section if line.startswith("| ")]
    assert len(config_rows) >= 6                  # header, rule, 4+ settings
    assert any("`--scope NAME`" in row and "`BROWSERLAND_MCP_SCOPE`" in row
               for row in config_rows)
    assert "### Scopes (#232)" in readme
    launch = next(line for line in readme
                  if line.startswith("| `launch_terminal("))
    assert "mode?)" in launch and "`readwrite`" in launch


# ---- #245 D1: the admin credential plumbs through every layer -------------

def test_admin_header_sent_when_configured():
    """#245: a client built with an admin token sends X-Browserland-Mcp-Admin
    on EVERY request — beside the Bearer MCP token, never replacing it (the
    admin credential unlocks admin-only ops; the MCP token still
    authenticates the caller)."""
    seen = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append((req.headers.get(MCP_ADMIN_HEADER),
                     req.headers.get("authorization")))
        return httpx.Response(200, json={"ok": True})

    with BrowserlandClient(base="http://broker:4445", token="t",
                           admin_token="adm-sekrit",
                           transport=httpx.MockTransport(handler)) as c:
        for _label, call in _EVERY_CALL:
            call(c)
    assert seen == [("adm-sekrit", "Bearer t")] * len(_EVERY_CALL)


def test_no_admin_header_without_token():
    """#245: without an admin token the header is never sent — the wire is
    exactly the pre-#245 surface for a non-admin host."""
    broker = _ScopeBroker()
    with _scoped(broker) as c:          # scope=None: plain unscoped client
        c.list_terminals()
        c.info()
    assert all(MCP_ADMIN_HEADER not in r.headers for r in broker.requests)


def test_admin_token_env_and_hosts_entry_plumb_through(monkeypatch):
    """#245: --admin-token > $BROWSERLAND_MCP_ADMIN_TOKEN >
    $WEB_TERMINAL_MCP_ADMIN_TOKEN (empty = none); a --hosts entry's own
    "admin_token" is that host's credential (never inherited from the
    process-wide value); the single-host fallback carries the resolved
    token as the 5th descriptor field."""
    from webterm.mcptool import __main__ as m

    # flag beats both env vars
    monkeypatch.setenv(m.ADMIN_TOKEN_ENV, "from-env")
    monkeypatch.setenv(m.ADMIN_TOKEN_ENV_ALT, "from-alt")
    args = m._parse_args(["--token", "t", "--admin-token", "from-flag"])
    assert m._resolve_admin_token(args) == "from-flag"
    # primary env beats the alt env
    args = m._parse_args(["--token", "t"])
    assert m._resolve_admin_token(args) == "from-env"
    # alt env used when the primary is unset; nothing set -> None
    monkeypatch.delenv(m.ADMIN_TOKEN_ENV, raising=False)
    assert m._resolve_admin_token(args) == "from-alt"
    monkeypatch.delenv(m.ADMIN_TOKEN_ENV_ALT, raising=False)
    assert m._resolve_admin_token(args) is None

    # --hosts entries carry their own admin_token; absent = None
    raw = _hosts_json({"name": "a", "admin_token": "per-host"},
                      {"name": "b"})
    assert m._parse_hosts(raw) == [("a", "http://a:4445", "t", None,
                                    "per-host", None),
                                   ("b", "http://b:4445", "t", None, None,
                                    None)]
    with pytest.raises(ValueError) as ei:
        m._parse_hosts(_hosts_json({"name": "a", "admin_token": 5}))
    assert "admin_token" in str(ei.value) and "'a'" in str(ei.value)

    # the single-host shorthand carries the resolved admin token
    monkeypatch.setenv(m.ADMIN_TOKEN_ENV, "env-admin")
    single = m._parse_args(["--token", "t", "--broker-url", "http://h:4445"])
    assert m._resolve_hosts(single) == [("default", "http://h:4445", "t",
                                         None, "env-admin", None)]


def test_get_client_carries_the_hosts_admin_token():
    """#245: _get_client builds each host's client with that host's admin
    token (the header rides every request of that host), via the HostConfig
    map configure() installs."""
    from webterm.mcptool import server
    server.configure([("a", "http://a:4445", "t", None, "adm-a"),
                      server.HostConfig("b", "http://b:4445", "t")])
    try:
        a, b = server._get_client("a"), server._get_client("b")
        assert (a.admin_token, b.admin_token) == ("adm-a", None)
        # the internal map is HostConfig-shaped; a legacy positional tuple
        # still reads back normalized (test injections keep working)
        server._host_configs["legacy"] = ("http://c:4445", "t", "teamA")
        assert server._get_client("legacy").admin_token is None
        assert server._get_client("legacy").scope == "teamA"
    finally:
        server._host_configs.pop("legacy", None)
        server.configure([])


# ---- #243/#244/#245: a mock broker for the capability-gated tool cells ----

class _ToolBroker:
    """MockTransport broker for the #243/#245/#244 tool cells. /mcp/info
    answers the ``caps`` dict (default: every capability on); /mcp/launch
    answers ``launch`` (default: an id + handle); /mcp/terminals answers
    ``terminals``; everything else answers ``{"ok": True}``. ``responses``
    maps a path to a ``request -> httpx.Response`` callable for bespoke
    cells; ``faults`` works like _ScopeBroker's (a queue consumed one entry
    per request to that path: None = answer normally, an httpx exception
    class = raise, an httpx.Response = return). Every request is recorded."""

    ALL_CAPS = {"admin": True, "window_edit": True, "allow_desktop": True}

    def __init__(self, caps=None, launch=None, terminals=(), responses=None,
                 faults=None):
        self.caps = dict(self.ALL_CAPS) if caps is None else dict(caps)
        self.launch = launch if launch is not None else {
            "ok": True, "id": 7, "handle": "hh"}
        self.terminals = list(terminals)
        self.responses = responses or {}
        self.faults = faults or {}
        self.requests = []

    def __call__(self, request):
        self.requests.append(request)
        path = request.url.path
        queue = self.faults.get(path)
        fault = queue.pop(0) if queue else None
        if isinstance(fault, httpx.Response):
            return fault
        if fault is not None:
            raise fault("injected", request=request)
        custom = self.responses.get(path)
        if custom is not None:
            return custom(request)
        if path == "/mcp/info":
            return httpx.Response(200, json=dict({"ok": True}, **self.caps))
        if path == "/mcp/launch":
            return httpx.Response(200, json=self.launch)
        if path == "/mcp/terminals":
            return httpx.Response(200, json=self.terminals)
        return httpx.Response(200, json={"ok": True})

    def count(self, path):
        return sum(1 for r in self.requests if r.url.path == path)

    def bodies(self, path):
        return [json.loads(r.content) for r in self.requests
                if r.url.path == path]


# ---- #243 D2: launch handles, owned: true, set_window ----------------------

def test_launch_stores_handle_and_list_marks_owned():
    """#243: launch_terminal keeps the broker's handle keyed by the
    namespaced id, and list_terminals marks exactly those rows ``owned:
    true`` (a key only the handle-holders get, so a caller can tell which
    windows set_window can still manage)."""
    def handler(req):
        if req.url.path == "/mcp/terminals":
            return httpx.Response(200, json=[{"id": 7, "mode": "readwrite"},
                                             {"id": 8, "mode": "read"}])
        if req.url.path == "/mcp/launch":
            return httpx.Response(200, json={"ok": True, "id": 7,
                                             "handle": "h-7"})
        return httpx.Response(200, json={"ok": True})

    server = _install({"h": handler})
    try:
        out = server.launch_terminal()
        listing = server.list_terminals()
    finally:
        _reset_server()
    assert out["id"] == "h:7" and out["handle"] == "h-7"
    owned = {t["id"]: t.get("owned") for t in listing["terminals"]}
    assert owned == {"h:7": True, "h:8": None}


def test_set_window_sends_handle_with_set_and_clear_semantics():
    """#243: the tool composes the wire body from keep|set|clear semantics —
    a non-empty string SETS the field, a clear_* flag sends an explicit
    null, a key left empty+unflagged is absent from the body (the broker
    keeps the field) — and the stored handle rides every call. A call with
    nothing to change is a LOCAL refusal (no request)."""
    broker = _ToolBroker()
    server = _install({"h": broker})
    try:
        server.launch_terminal()                     # mints + stores "hh"
        server.set_window("h:7", scope="teamA")
        server.set_window("h:7", clear_mode=True)
        server.set_window("h:7", mode="read", clear_scope=True)
        with pytest.raises(BrowserlandError) as ei:
            server.set_window("h:7")
    finally:
        _reset_server()
    assert broker.bodies("/mcp/window") == [
        {"id": 7, "handle": "hh", "scope": "teamA"},
        {"id": 7, "handle": "hh", "mode": None},
        {"id": 7, "handle": "hh", "scope": None, "mode": "read"},
    ]
    assert ei.value.code == "bad_args"


def test_set_window_without_handle_refuses_locally_and_sends_nothing():
    """#243: no stored handle and no confirmed admin credential for the host
    — the tool refuses locally (no_handle) and the broker sees NOTHING, not
    even /mcp/info."""
    broker = _ToolBroker()
    server = _install({"h": broker})
    try:
        with pytest.raises(BrowserlandError) as ei:
            server.set_window("h:999", scope="x")
    finally:
        _reset_server()
    assert ei.value.code == "no_handle"
    assert broker.requests == []


def test_set_window_fails_closed_without_window_edit_flag():
    """#243: a broker whose /mcp/info does not advertise window_edit (an
    older build) never sees a /mcp/window request: the owner path fails
    closed after exactly one capability fetch."""
    broker = _ToolBroker(caps={"admin": False, "window_edit": False,
                               "allow_desktop": False})
    server = _install({"h": broker})
    try:
        server.launch_terminal()                     # handle stored
        with pytest.raises(BrowserlandError) as ei:
            server.set_window("h:7", mode="read")
    finally:
        _reset_server()
    assert ei.value.code == "window_edit_unsupported"
    assert broker.count("/mcp/info") == 1
    assert broker.count("/mcp/window") == 0


def test_set_window_admin_path_without_admin_flag_refuses():
    """#245: on a host marked admin-capable at startup, the no-handle path
    still fails closed when the broker's /mcp/info no longer answers
    admin: true (a rotated credential): local refusal, nothing sent."""
    broker = _ToolBroker(caps={"admin": False, "window_edit": True,
                               "allow_desktop": False})
    server = _install({"h": broker})
    server._admin_hosts.add("h")       # what D3's startup probe would set
    try:
        with pytest.raises(BrowserlandError) as ei:
            server.set_window("h:7", mode="read")
    finally:
        _reset_server()
    assert ei.value.code == "admin_unsupported"
    assert broker.count("/mcp/window") == 0


def test_handles_lru_bounded_not_pruned_by_listing():
    """#243: the handle store is an LRU bounded at 256 — a listing that HIDES
    a window (off windows never arrive in /mcp/terminals) must not prune its
    handle (it is the recovery path), while 256 newer handles evict the
    OLDEST, whose set_window then refuses locally."""
    state = {"next": 0}
    broker = _ToolBroker(terminals=[{"id": 999, "mode": "read"}])

    def launch(req):
        state["next"] += 1
        return httpx.Response(200, json={"ok": True, "id": state["next"],
                                         "handle": f"h{state['next']}"})

    broker.responses["/mcp/launch"] = launch
    server = _install({"h": broker})
    try:
        first = server.launch_terminal()             # h:1, the eviction victim
        for _ in range(server._HANDLES_MAX):         # 256 newer handles
            server.launch_terminal()
        assert first["id"] == "h:1"
        server.list_terminals()                      # shows only 999
        # h:2's handle survived a listing that hides it
        server.set_window("h:2", mode="read")
        assert broker.bodies("/mcp/window")[-1] == {
            "id": 2, "handle": "h2", "mode": "read"}
        # the OLDEST handle was evicted by the 256 newer ones
        with pytest.raises(BrowserlandError) as ei:
            server.set_window("h:1", mode="read")
    finally:
        _reset_server()
    assert ei.value.code == "no_handle"


def test_caps_are_forgotten_when_the_broker_goes_away():
    """#243: the capability cache follows the scope cache's rule — a failure
    that can mean 'the broker behind this URL went away' (connection_error)
    forgets it, so the next call re-probes /mcp/info instead of trusting
    flags from the previous build."""
    phase = {"info": 0}

    def info(req):
        phase["info"] += 1
        if phase["info"] == 1:
            return httpx.Response(200, json={"ok": True,
                                             "window_edit": True})
        return httpx.Response(200, json={"ok": True})   # no window_edit

    broker = _ToolBroker(responses={"/mcp/info": info},
                         faults={"/mcp/input": [httpx.ConnectError]})
    server = _install({"h": broker})
    try:
        server.launch_terminal()
        server.set_window("h:7", mode="read")         # caches caps (v1)
        assert broker.count("/mcp/info") == 1
        with pytest.raises(BrowserlandError) as gone:
            server.send_input("h:7", "x")
        assert gone.value.code == "connection_error"
        assert broker.count("/mcp/info") == 1         # not yet re-probed
        with pytest.raises(BrowserlandError) as ei:
            server.set_window("h:7", mode="read")     # re-probes: caps v2
    finally:
        _reset_server()
    assert ei.value.code == "window_edit_unsupported"
    assert broker.count("/mcp/info") == 2
    assert broker.count("/mcp/window") == 1           # only the v1 send


def test_configure_clears_handles_admin_and_caps():
    """#243/#245: configure() clears the handle store and the admin-host set
    atomically with the rebuild (host names are reusable; a stale
    'host:id' handle must not survive a re-pointed host), and the
    capability caches die with the closed clients."""
    from webterm.mcptool import server
    server.configure([("h", "http://h:4445", "t", None)])
    try:
        old_client = server._get_client("h")
        server._handle_remember("h:7", "stale")
        server._admin_hosts.add("h")
        server.configure([("h", "http://elsewhere:4445", "t", None)])
        assert list(server._handles) == [] and server._admin_hosts == set()
        assert server._get_client("h") is not old_client
        with pytest.raises(BrowserlandError) as ei:
            server.set_window("h:7", mode="read")
    finally:
        server.configure([])
    assert ei.value.code == "no_handle"


# ---- #245 D3: the startup admin probe + admin-only tools -------------------

def _configure_with(monkeypatch, hosts, handler_for):
    """configure() with every host's client built over a MockTransport
    running ``handler_for(name)`` (the way _get_client builds them), so the
    startup admin probe is exercised for real without any network."""
    from webterm.mcptool import server

    def factory(base, token, scope=None, admin_token=None, name=None,
                **kw):
        return BrowserlandClient(base=base, token=token, scope=scope,
                                 admin_token=admin_token, name=name,
                                 transport=httpx.MockTransport(
                                     handler_for(name)))
    monkeypatch.setattr(server, "BrowserlandClient", factory)
    server.configure(hosts)


@pytest.mark.asyncio
async def test_admin_tools_absent_when_no_host_admin(monkeypatch):
    """#245: with no admin-token host configured, the probe never runs and
    the admin tools stay unregistered (hidden unless admin). MUST run
    before any test that registers them: FastMCP cannot unregister, so a
    registration is process-lifetime [c24]."""
    from webterm.mcptool import server
    broker = _ToolBroker()
    _configure_with(monkeypatch, [("h", "http://h:4445", "t", None, None)],
                    lambda name: broker)
    try:
        assert server._admin_hosts == set()
        assert broker.requests == []               # nothing probed at all
        names = [t.name for t in await server.mcp.list_tools()]
        assert "set_mcp_knobs" not in names
    finally:
        _reset_server()


@pytest.mark.asyncio
async def test_admin_probe_registers_admin_tools_when_any_host_admin(
        monkeypatch):
    """#245: the startup probe GETs /mcp/info on every admin-token host (the
    header riding), records exactly the hosts whose body says admin: true —
    with the header present; a probe without it reads admin: false and
    registers nothing — and registers the admin tools once ANY host
    qualifies. A host without an admin token is never probed."""
    from webterm.mcptool import server

    def a_info(req):
        # admin is confirmed ONLY when the admin header rode the probe
        return httpx.Response(200, json={
            "ok": True, "admin": req.headers.get(MCP_ADMIN_HEADER) == "adm-a"})

    brokers = {"a": _ToolBroker(responses={"/mcp/info": a_info}),
               "b": _ToolBroker(caps={"window_edit": True})}   # no admin key
    seen = []

    def handler_for(name):
        def handler(req):
            seen.append((name, req.url.path))
            return brokers[name](req)
        return handler

    _configure_with(monkeypatch, [
        ("a", "http://a:4445", "t", None, "adm-a"),
        ("b", "http://b:4445", "t", None, "adm-b"),
        ("c", "http://c:4445", "t", None)],            # no admin token
        handler_for)
    try:
        assert server._admin_hosts == {"a"}
        assert sorted(seen) == [("a", "/mcp/info"), ("b", "/mcp/info")]
        assert server._admin_tools_registered is True
        names = [t.name for t in await server.mcp.list_tools()]
        assert "set_mcp_knobs" in names
    finally:
        _reset_server()


def test_admin_probe_failure_fails_closed(monkeypatch):
    """#245: an admin host that cannot be reached (or answers anything but
    admin: true) is NOT admin-capable: the probe fails closed, the host is
    absent from _admin_hosts, and its admin tool calls refuse locally."""
    from webterm.mcptool import server
    broker = _ToolBroker(faults={"/mcp/info": [httpx.ConnectError]})
    _configure_with(monkeypatch, [("h", "http://h:4445", "t", None, "adm")],
                    lambda name: broker)
    try:
        assert server._admin_hosts == set()
        with pytest.raises(BrowserlandError) as ei:
            server.set_mcp_knobs("h", default_mode="read")
    finally:
        _reset_server()
    assert ei.value.code == "admin_required"
    assert broker.count("/mcp/knobs") == 0


def test_admin_probe_concurrent_deadline(monkeypatch):
    """#245: the probes run concurrently under ONE deadline — three slow
    admin hosts cost about one wait, not three (the owner's worst case for
    stdio startup is one client timeout, not timeout × hosts)."""
    import time
    from webterm.mcptool import server

    def handler(req):
        time.sleep(0.6)
        return httpx.Response(200, json={"ok": True, "admin": True})

    started = time.monotonic()
    try:
        _configure_with(monkeypatch, [
            (n, f"http://{n}:4445", "t", None, "adm") for n in "abc"],
            lambda name: handler)
        elapsed = time.monotonic() - started
        assert server._admin_hosts == {"a", "b", "c"}
    finally:
        _reset_server()
    assert elapsed < 1.5, f"probe looked serial: {elapsed:.2f}s for 3 hosts"


def test_admin_tool_refuses_locally_for_non_admin_host_and_sends_nothing():
    """#245: set_mcp_knobs on a host without a confirmed admin credential
    refuses LOCALLY (admin_required naming the host) and the broker sees
    nothing — not even /mcp/info."""
    broker = _ToolBroker()
    server = _install({"h": broker})
    try:
        with pytest.raises(BrowserlandError) as ei:
            server.set_mcp_knobs("h", default_mode="read")
    finally:
        _reset_server()
    assert ei.value.code == "admin_required"
    assert "'h'" in str(ei.value)
    assert broker.requests == []


def test_knobs_success_refreshes_capability_cache():
    """#245 [c23]: a successful knobs call re-reads the host's /mcp/info, so
    a knob that flips a capability (allow_desktop) is visible to the next
    fail-closed tool call without restarting this server."""
    broker = _ToolBroker()

    def info(req):
        if broker.count("/mcp/knobs"):
            return httpx.Response(200, json={"ok": True,
                                             "allow_desktop": True})
        return httpx.Response(200, json={"ok": True})

    broker.responses["/mcp/info"] = info
    server = _install({"h": broker})
    server._admin_hosts.add("h")     # what the startup probe would set
    try:
        client = server._get_client("h")
        assert client.capabilities()["allow_desktop"] is False
        out = server.set_mcp_knobs("h", allow_desktop=True)
    finally:
        _reset_server()
    assert out == {"ok": True}
    assert broker.bodies("/mcp/knobs") == [{"allow_desktop": True}]
    assert client.capabilities()["allow_desktop"] is True
    assert broker.count("/mcp/info") == 2


def test_rotated_admin_token_error_names_rotation():
    """#245 [c10]: a broker-side 403 admin_required on a host the probe once
    cleared surfaces as a LOUD error naming the host and the rotation
    remedy — never a silent retry without the header."""
    broker = _ToolBroker(responses={
        "/mcp/knobs": lambda req: httpx.Response(
            403, json={"error": "admin_required"})})
    server = _install({"h": broker})
    server._admin_hosts.add("h")
    try:
        with pytest.raises(BrowserlandError) as ei:
            server.set_mcp_knobs("h", default_mode="read")
    finally:
        _reset_server()
    assert ei.value.code == "admin_required"
    assert "'h'" in str(ei.value) and "rotated" in str(ei.value)


def test_admin_required_message_says_when_every_call_fails():
    """#260 review round 1: the client's own admin_required sentence, on a
    plain (non-admin) call. While the broker HAS an admin token, a wrong header
    (rotated or mistyped) is refused on every /mcp call (app.py
    _mcp_admin_error); once the broker's token is cleared it ignores the header
    and only the admin-only routes refuse (_mcp_knobs, the workspace ops). The
    sentence must say both, and none of the earlier wordings may come back."""
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"error": "admin_required"})

    with _client(handler) as c:
        with pytest.raises(BrowserlandError) as ei:
            c.flush_input(42)
    msg = str(ei.value)
    assert ei.value.code == "admin_required"
    assert "While the broker has an admin token" in msg
    assert "fails every call to this host" in msg
    assert "if the broker's admin token was cleared, only admin-only " \
           "operations fail" in msg
    for gone in ("an unrotated credential never yields this",
                 "for an admin-only operation",
                 "not only admin-only ones, so if the token was rotated or "
                 "cleared"):
        assert gone not in msg, gone


# ---- #244 D4: get_desktop / arrange_window / workspaces --------------------

def test_get_desktop_fails_closed_without_allow_desktop():
    """#244: a broker whose /mcp/info does not advertise allow_desktop never
    sees a /mcp/desktop request — the named form raises desktop_unsupported
    after exactly one capability fetch, and the host-less aggregate reports
    the refusal as that host's error entry instead of sinking the call."""
    broker = _ToolBroker(caps={"admin": True, "window_edit": True,
                               "allow_desktop": False})
    server = _install({"h": broker})
    try:
        with pytest.raises(BrowserlandError) as ei:
            server.get_desktop("h")
        agg = server.get_desktop()
    finally:
        _reset_server()
    assert ei.value.code == "desktop_unsupported"
    assert broker.count("/mcp/info") == 1     # cached; the aggregate re-reads it
    assert broker.count("/mcp/desktop") == 0
    assert agg["h"]["ok"] is False
    assert "allow_desktop" in agg["h"]["error"]


def test_get_desktop_returns_the_snapshot():
    """#244: with allow_desktop on, GET /mcp/desktop answers the snapshot the
    browser produced (the broker re-identifies rows against its registry
    before answering)."""
    snapshot = {"ok": True, "viewport": {"w": 1920, "h": 1040},
                "home": True, "windows": [{"id": 7, "title": "t",
                                           "tiled": True}]}
    broker = _ToolBroker(responses={"/mcp/desktop": lambda req: httpx.Response(
        200, json=snapshot)})
    server = _install({"h": broker})
    try:
        out = server.get_desktop("h")
    finally:
        _reset_server()
    assert out == {**snapshot, "windows": [
        {"id": "h:7", "title": "t", "tiled": True}]}


def test_get_desktop_namespaces_ids_in_both_forms():
    """#244: window ids come back as "<host>:<int>" (the list_terminals
    form) for the named-host and the aggregate form; a null id stays null."""
    snapshot = {"ok": True, "home": True, "windows": [
        {"id": 7, "title": "a"}, {"id": None, "title": "b"}]}
    broker = _ToolBroker(responses={"/mcp/desktop": lambda req: httpx.Response(
        200, json=snapshot)})
    server = _install({"h": broker})
    try:
        named = server.get_desktop("h")
        agg = server.get_desktop()
    finally:
        _reset_server()
    for out in (named, agg["h"]):
        assert [r["id"] for r in out["windows"]] == ["h:7", None]


def test_needs_confirm_error_carries_the_count():
    """#245: the 409 needs_confirm count reaches the agent in the message
    and on the exception body."""
    broker = _ToolBroker(responses={
        "/mcp/desktop": lambda req: httpx.Response(
            409, json={"error": "needs_confirm", "count": 3})})
    server = _install({"h": broker})
    try:
        with pytest.raises(BrowserlandError) as ei:
            server.arrange_window("h:7", "focus")
    finally:
        _reset_server()
    assert "3 windows" in str(ei.value)
    assert ei.value.body == {"error": "needs_confirm", "count": 3}


def test_arrange_window_builds_op_args_and_routes_id():
    """#244: every op composes its args from the non-default params only,
    the namespaced id is SPLIT to the bare int on the wire, a numeric
    move_column `to` rides as an int, and a same-host `target` rides as an
    int too."""
    broker = _ToolBroker()
    server = _install({"h": broker})
    try:
        server.arrange_window("h:7", "focus")
        server.arrange_window("h:7", "move_column", to="left")
        server.arrange_window("h:7", "move_column", to="2")
        server.arrange_window("h:7", "set_width", preset="1/3")
        server.arrange_window("h:7", "tile")
        server.arrange_window("h:7", "set_geometry", x=10, y=20, w=640, h=480)
        server.arrange_window("h:7", "tab_into", target="h:9")
        server.arrange_window("h:7", "split", target="h:9", axis="row")
    finally:
        _reset_server()
    assert broker.bodies("/mcp/desktop") == [
        {"op": "focus", "args": {"id": 7}},
        {"op": "move_column", "args": {"id": 7, "to": "left"}},
        {"op": "move_column", "args": {"id": 7, "to": 2}},
        {"op": "set_width", "args": {"id": 7, "preset": "1/3"}},
        {"op": "tile", "args": {"id": 7}},
        {"op": "set_geometry", "args": {"id": 7, "x": 10, "y": 20,
                                        "w": 640, "h": 480}},
        {"op": "tab_into", "args": {"id": 7, "target": 9}},
        {"op": "split", "args": {"id": 7, "target": 9, "axis": "row"}},
    ]


def test_arrange_window_rejects_unknown_op_locally():
    """#244: an unknown op, a missing required param and a cross-host target
    are all LOCAL refusals — the broker sees nothing, not even /mcp/info."""
    broker = _ToolBroker()
    server = _install({"h": broker, "o": _ToolBroker()})
    try:
        for kwargs in (
                {"op": "explode"},                              # unknown op
                {"op": "set_width"},                            # no preset
                {"op": "move_column"},                          # no to
                {"op": "set_geometry"},                         # no x/y/w/h
                {"op": "tab_into"},                             # no target
                {"op": "split", "target": "h:9"},               # no axis
                {"op": "tab_into", "target": "o:9"}):           # other host
            with pytest.raises(BrowserlandError) as ei:
                server.arrange_window("h:7", kwargs.pop("op"), **kwargs)
            assert ei.value.code in ("bad_op", "bad_args"), ei.value
    finally:
        _reset_server()
    assert broker.requests == []


def test_workspaces_admin_gate_and_arg_building():
    """#244: workspace ops are admin-only — a host without a confirmed
    admin credential refuses locally and sees nothing — and the ops compose
    their args (bare ints for win, force only when true, the wire op
    prefixed workspaces:)."""
    broker = _ToolBroker()
    server = _install({"h": broker})
    try:
        with pytest.raises(BrowserlandError) as ei:
            server.workspaces("h", "list")
    finally:
        _reset_server()
    assert ei.value.code == "admin_required"
    assert broker.requests == []

    server = _install({"h": broker})
    server._admin_hosts.add("h")     # what the startup probe would set
    try:
        server.workspaces("h", "list")
        server.workspaces("h", "add", name="build")
        server.workspaces("h", "rename", index=1, name="w2")
        server.workspaces("h", "remove", index=1, force=True)
        server.workspaces("h", "switch", index=0)
        server.workspaces("h", "send-window", win="h:5", target=2)
        with pytest.raises(BrowserlandError) as bad:
            server.workspaces("h", "send-window", target=2)   # no win
    finally:
        _reset_server()
    assert bad.value.code == "bad_args"
    assert broker.bodies("/mcp/desktop") == [
        {"op": "workspaces:list", "args": {}},
        {"op": "workspaces:add", "args": {"name": "build"}},
        {"op": "workspaces:rename", "args": {"index": 1, "name": "w2"}},
        {"op": "workspaces:remove", "args": {"index": 1, "force": True}},
        {"op": "workspaces:switch", "args": {"index": 0}},
        {"op": "workspaces:send-window", "args": {"win": 5, "target": 2}},
    ]


def test_workspaces_force_is_remove_only_and_refuses_locally():
    """Review round 1 on #244: `force` means "bypass remove's needs_confirm".
    On any other op it is a misunderstood call — silently ignoring it hides
    the mistake — so the tool refuses locally (bad_args) and the broker sees
    NOTHING, not even /mcp/info (the guard sits before the admin gate)."""
    broker = _ToolBroker()
    server = _install({"h": broker})
    try:
        for op, kwargs in (("list", {}), ("add", {"name": "w"}),
                           ("switch", {"index": 0}),
                           ("send-window", {"win": "h:5", "target": 1})):
            with pytest.raises(BrowserlandError) as ei:
                server.workspaces("h", op, force=True, **kwargs)
            assert ei.value.code == "bad_args", (
                f"force on {op!r} must refuse bad_args, got {ei.value.code!r}")
    finally:
        _reset_server()
    assert broker.requests == []


def test_desktop_error_codes_map_to_readable_messages():
    """#244: the relay's error codes surface as readable tool errors via the
    client's table (never a raw code passthrough): no holder, not_home, and
    the needs_confirm + force guidance."""
    cases = [
        ("no_active_browser", 409, "control lease"),
        ("not_home", 409, "home"),
        ("needs_confirm", 409, "force"),
        ("desktop_timeout", 504, "answer"),
    ]
    for code, status, fragment in cases:
        broker = _ToolBroker(responses={
            "/mcp/desktop": lambda req, code=code, status=status: (
                httpx.Response(status, json={"error": code}))})
        server = _install({"h": broker})
        try:
            with pytest.raises(BrowserlandError) as ei:
                server.arrange_window("h:7", "focus")
        finally:
            _reset_server()
        assert ei.value.code == code
        assert fragment in str(ei.value), (code, str(ei.value))


# ---- #252 phase 3 (A10): a host reached through a hub's /peer/<id> prefix --

def _seen_headers(**kw):
    seen = []

    def handler(request):
        seen.append(dict(request.headers))
        return httpx.Response(200, json={"ok": True, "terminals": []})
    c = BrowserlandClient(base="http://hub:4445/peer/m", token="t",
                          transport=httpx.MockTransport(handler), **kw)
    c._send("GET", "/mcp/terminals")
    return seen[0]


def test_a_configured_peer_origin_rides_every_request():
    from webterm.protocol import PEER_ORIGIN_HEADER
    h = _seen_headers(peer_origin="http://b:4448")
    assert h[PEER_ORIGIN_HEADER.lower()] == "http://b:4448"


@pytest.mark.parametrize("kw", [{}, {"peer_origin": None},
                                {"peer_origin": ""}])
def test_without_a_peer_origin_the_requests_are_as_before(kw):
    """An mcptool host config without peer_origin sends exactly what it sent
    before #252 phase 3: the same header set, no pairing header."""
    from webterm.protocol import PEER_ORIGIN_HEADER
    h = _seen_headers(**kw)
    assert PEER_ORIGIN_HEADER.lower() not in h
    assert sorted(h) == sorted(_seen_headers())
    assert h["authorization"] == "Bearer t"


@pytest.mark.parametrize("raw, canon", [
    ("http://B:4445/", "http://b:4445"),
    ("http://b:80", "http://b"),
    ("http://b./", "http://b"),
    ("http://[::1]:4445", "http://[::1]:4445"),
    ("HTTPS://Hub.Example:443", "https://hub.example"),
    # round 1b N8: one spelling per IPv6 address on both sides
    ("http://[0:0::1]:4445", "http://[::1]:4445"),
    ("http://[0000:0000:0000:0000:0000:0000:0000:0001]", "http://[::1]"),
])
def test_the_peer_origin_is_sent_in_the_hubs_own_form(raw, canon):
    """M2: the client sends the very form the hub compares against (the
    hub's config goes through the same normalize_origin), so a correct
    config never reads as a re-pointed peer."""
    from webterm.broker import peers
    from webterm.protocol import PEER_ORIGIN_HEADER
    assert peers.normalize_origin(raw)[0] == canon
    assert _seen_headers(peer_origin=raw)[PEER_ORIGIN_HEADER.lower()] == canon


def test_a_bad_peer_origin_is_refused_at_construction():
    with pytest.raises(ValueError):
        BrowserlandClient(base="http://hub:4445", token="t",
                          peer_origin="http://b:4448/path")


def test_a_pairing_refusal_reads_as_such():
    def handler(request):
        return httpx.Response(409, json={"error": "peer_origin_changed"},
                              headers={"X-Browserland-Peer-Refusal":
                                       "peer_origin_changed"})
    c = BrowserlandClient(base="http://hub:4445/peer/m", token="t",
                          transport=httpx.MockTransport(handler),
                          peer_origin="http://b:4448")
    with pytest.raises(BrowserlandError) as ei:
        c._send("GET", "/mcp/terminals")
    assert ei.value.code == "peer_origin_changed"
    assert "publishes this peer at another address" in str(ei.value)
    assert "HTTP 409" in str(ei.value)


def test_hosts_entries_carry_a_canonical_peer_origin():
    from webterm.mcptool import __main__ as m
    raw = _hosts_json({"name": "a", "peer_origin": "http://B:4448/"},
                      {"name": "b", "peer_origin": ""})
    assert [h[5] for h in m._parse_hosts(raw)] == ["http://b:4448", None]


@pytest.mark.parametrize("bad", ["http://b:4448/p", "http://b:4448/?q=1",
                                 "http://u:p@b:4448", "ftp://b:4448", 5,
                                 # round 1 NIT 3: refused, never trimmed
                                 " http://b:4448 "])
def test_a_bad_hosts_peer_origin_names_the_host(bad):
    from webterm.mcptool import __main__ as m
    with pytest.raises(ValueError) as ei:
        m._parse_hosts(_hosts_json({"name": "a", "peer_origin": bad}))
    assert "peer_origin" in str(ei.value) and "'a'" in str(ei.value)


def test_get_client_carries_the_hosts_peer_origin():
    from webterm.mcptool import server
    server.configure([("a", "http://hub:4445/peer/m", "t", None, None,
                       "http://b:4448"),
                      ("b", "http://b:4445", "t")])
    try:
        assert server._get_client("a").peer_origin == "http://b:4448"
        assert server._get_client("b").peer_origin is None
    finally:
        server.configure([])



@pytest.mark.parametrize("padded", [" http://b:4448 ", "http://b:4448\t",
                                    "\nhttp://b:4448"])
def test_the_client_refuses_a_padded_peer_origin(padded):
    """Round 2 SHOULD 2: the client itself refuses a padded peer_origin --
    never trims it into a pairing the operator did not write."""
    with pytest.raises(ValueError):
        BrowserlandClient(base="http://hub:4445/peer/m", token="t",
                          peer_origin=padded)


def test_workspaces_258_ops_compose_their_args():
    """#258 MT1: move/color/stack/unstack and rename-a-stack compose their
    wire args (only what was supplied; "none" clears a colour)."""
    broker = _ToolBroker()
    server = _install({"h": broker})
    server._admin_hosts.add("h")
    try:
        server.workspaces("h", "move", index=2, direction="left")
        server.workspaces("h", "move", stack="L9", direction="right")
        server.workspaces("h", "color", index=1, color="#E96D6D")
        server.workspaces("h", "color", stack="L9", color="none")
        server.workspaces("h", "stack", index=0, target=3)
        server.workspaces("h", "unstack", index=1)
        server.workspaces("h", "rename", stack="L9", name="dev")
    finally:
        _reset_server()
    assert broker.bodies("/mcp/desktop") == [
        {"op": "workspaces:move", "args": {"index": 2, "direction": "left"}},
        {"op": "workspaces:move", "args": {"stack": "L9", "direction": "right"}},
        {"op": "workspaces:color", "args": {"index": 1, "color": "#E96D6D"}},
        {"op": "workspaces:color", "args": {"stack": "L9", "color": None}},
        {"op": "workspaces:stack", "args": {"index": 0, "target": 3}},
        {"op": "workspaces:unstack", "args": {"index": 1}},
        {"op": "workspaces:rename", "args": {"stack": "L9", "name": "dev"}},
    ]


def test_workspaces_258_refuses_locally_and_sends_nothing():
    """#258 MT2: a misplaced or malformed newer argument is refused before
    anything is sent -- the broker sees no request at all."""
    broker = _ToolBroker()
    server = _install({"h": broker})
    server._admin_hosts.add("h")
    calls = [
        ("list", {"direction": "left"}),                     # not move
        ("switch", {"index": 0, "color": "#e96d6d"}),         # not color
        ("stack", {"index": 0, "target": 1, "stack": "L9"}),  # stack id on stack
        ("move", {"index": 0}),                               # no direction
        ("move", {"index": 0, "direction": "up"}),
        ("move", {"index": 0, "stack": "L9", "direction": "left"}),  # both
        ("move", {"direction": "left"}),                      # neither
        ("color", {"index": 0, "color": "red"}),
        ("color", {"index": 0}),
        ("stack", {"index": 0}),
        ("unstack", {}),
        ("rename", {"index": 0, "stack": "L9", "name": "x"}),
    ]
    try:
        for op, kwargs in calls:
            with pytest.raises(BrowserlandError) as ei:
                server.workspaces("h", op, **kwargs)
            assert ei.value.code == "bad_args", (op, kwargs, ei.value.code)
    finally:
        _reset_server()
    assert broker.requests == []


def test_workspaces_258_ops_are_known():
    """#258 MT5: the four new ops are in the tool's op list."""
    from webterm.mcptool import server as srv
    for op in ("move", "color", "stack", "unstack"):
        assert op in srv._WORKSPACE_OPS, op
