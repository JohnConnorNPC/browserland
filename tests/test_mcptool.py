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
    server._host_configs = {n: (f"http://{n}:4445", "t") for n in handlers}
    server._clients = {n: _mock_client(h, base=f"http://{n}:4445")
                       for n, h in handlers.items()}
    return server


def _reset_server():
    from webterm.mcptool import server
    for c in server._clients.values():
        c.close()
    server._host_configs = {}
    server._clients = {}


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
                                         "version": "0.1.0+abc"})
    with _client(handler) as c:
        out = c.info()
    assert out["version"] == "0.1.0+abc"


def test_list_terminals_surfaces_version_and_stale():
    """#22: per-window build id + stale flag flow through list_terminals."""
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[
            {"id": 1, "mode": "read", "version": "0.1.0+new", "stale": False},
            {"id": 2, "mode": "read", "version": "", "stale": True}])
    with _client(handler) as c:
        out = c.list_terminals()
    assert out[0]["version"] == "0.1.0+new" and out[0]["stale"] is False
    assert out[1]["version"] == "" and out[1]["stale"] is True


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
    assert m._parse_hosts(raw) == [("a", "http://x:4445", "t1"),
                                   ("b", "https://y:4445", "t2")]


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
    assert m._resolve_hosts(args) == [("default", "http://h:4445", "t")]


def test_resolve_hosts_multi_from_env(monkeypatch):
    from webterm.mcptool import __main__ as m
    raw = json.dumps([{"name": "a", "url": "http://x:4445", "token": "t"}])
    monkeypatch.setenv(m.HOSTS_ENV, raw)
    args = m._parse_args([])                       # --hosts defaults from env
    assert m._resolve_hosts(args) == [("a", "http://x:4445", "t")]


def test_resolve_hosts_flag_beats_single_host(monkeypatch):
    from webterm.mcptool import __main__ as m
    monkeypatch.delenv(m.HOSTS_ENV, raising=False)
    raw = json.dumps([{"name": "a", "url": "http://x:4445", "token": "ta"}])
    args = m._parse_args(["--hosts", raw, "--token", "ignored",
                          "--broker-url", "http://ignored:4445"])
    assert m._resolve_hosts(args) == [("a", "http://x:4445", "ta")]


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

    def factory(base, token):
        built.append(base)

        def handler(req: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"ok": True})

        return BrowserlandClient(base=base, token=token,
                                 transport=httpx.MockTransport(handler))

    monkeypatch.setattr(server, "BrowserlandClient", factory)
    server.configure([("local", "http://local:4445", "t"),
                      ("remote", "http://remote:4445", "t")])
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
    server._host_configs = {"old": ("http://old:4445", "t")}
    server._clients = {"old": spy}
    try:
        server.configure([("new", "http://new:4445", "t")])
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
    assert captured["hosts"] == [("a", "http://x:4445", "ta"),
                                 ("b", "http://y:4445", "tb")]


# ---- tool smoke test (FastMCP server wired to a MockTransport) -----------

@pytest.mark.asyncio
async def test_tools_registered():
    from webterm.mcptool import server

    tools = await server.mcp.list_tools()
    names = sorted(t.name for t in tools)
    assert names == ["flush_input", "launch_terminal", "list_profiles",
                     "list_terminals", "mcp_info", "read_screen",
                     "reset_terminal", "send_input", "send_keys", "set_pace"]


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
