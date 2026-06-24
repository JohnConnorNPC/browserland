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
    server._client = BrowserlandClient(token="t", transport=httpx.MockTransport(handler))
    try:
        server.send_input(1, "echo hi\n")
    finally:
        server._client = None

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
    (["a"], "a"), (["^"], "^"), (["é"], "é"),  # single literal chars (UTF-8)
    (["C-c", "Enter"], "\x03\r"),            # sequence
    (["Up", "Up", "Enter"], "\x1b[A\x1b[A\r"),
])
def test_keys_to_text(keys, expected):
    from webterm.mcptool import server
    assert server._keys_to_text(keys) == expected


@pytest.mark.parametrize("bad", [
    [], "C-c", ["Retun"], ["Ctrl-C"], ["C-"], ["M-Enter"], ["Up arrow"],
])
def test_keys_to_text_rejects(bad):
    from webterm.mcptool import server
    with pytest.raises(ValueError):
        server._keys_to_text(bad)


def test_send_keys_posts_translated_bytes_verbatim():
    from webterm.mcptool import server

    seen = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["path"] = req.url.path
        seen["body"] = json.loads(req.content)
        return httpx.Response(200, json={"ok": True})

    server._client = BrowserlandClient(token="t", transport=httpx.MockTransport(handler))
    try:
        # Ctrl-C then Enter: a real 0x03 (not the literal "^C" that left a stray
        # 'c' in #14), sent verbatim — NOT through the newline->CR text policy.
        assert server.send_keys(9, ["C-c", "Enter"]) == {"ok": True}
    finally:
        server._client = None
    assert seen["path"] == "/mcp/input"
    assert seen["body"] == {"id": 9, "data": "\x03\r"}


def test_send_keys_invalid_token_sends_nothing():
    """A bad token must raise before any HTTP call — no half-typed line."""
    from webterm.mcptool import server

    calls = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(req.url.path)
        return httpx.Response(200, json={"ok": True})

    server._client = BrowserlandClient(token="t", transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(ValueError):
            server.send_keys(9, ["C-c", "Bogus", "Enter"])
    finally:
        server._client = None
    assert calls == []   # atomic: nothing was sent


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

    server._client = BrowserlandClient(token="t", transport=httpx.MockTransport(handler))
    try:
        server.send_keys(9, keys)
    finally:
        server._client = None
    return seen["body"], seen["paths"]


def test_send_keys_arrows_csi_in_normal_mode():
    body, paths = _send_keys_capturing(False, ["Down", "Down"])
    assert body == {"id": 9, "data": "\x1b[B\x1b[B"}
    assert "/mcp/terminals" in paths      # consulted the DECCKM cache


def test_send_keys_arrows_ss3_under_decckm():
    body, _ = _send_keys_capturing(True, ["Up", "Home"])
    assert body == {"id": 9, "data": "\x1bOA\x1bOH"}


def test_send_keys_no_cursor_keys_skips_lookup():
    body, paths = _send_keys_capturing(True, ["C-c", "Enter"])
    assert body == {"id": 9, "data": "\x03\r"}
    assert "/mcp/terminals" not in paths  # no DECCKM lookup without cursor keys


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

    server._client = BrowserlandClient(token="t", transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(ValueError):
            server.send_keys(9, ["Down", "Bogus"])
    finally:
        server._client = None
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
    assert m.main([]) == 2
    assert "no MCP token" in capsys.readouterr().err


# ---- tool smoke test (FastMCP server wired to a MockTransport) -----------

@pytest.mark.asyncio
async def test_tools_registered():
    from webterm.mcptool import server

    tools = await server.mcp.list_tools()
    names = sorted(t.name for t in tools)
    assert names == ["launch_terminal", "list_profiles", "list_terminals",
                     "mcp_info", "read_screen", "send_input", "send_keys"]


def test_tools_round_trip_through_http():
    from webterm.mcptool import server

    calls = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append((req.url.path, json.loads(req.content) if req.content else None))
        if req.url.path == "/mcp/read":
            return httpx.Response(200, json={"ok": True, "id": 5, "cols": 80,
                                             "rows": 24, "text": "screen"})
        return httpx.Response(200, json={"ok": True})

    # Inject a stubbed client into the module-level slot the tools delegate to.
    server._client = BrowserlandClient(token="t", transport=httpx.MockTransport(handler))
    try:
        assert server.read_screen(5)["text"] == "screen"
        # The tool maps the logical Enter ("\n") to a carriage return so it
        # submits on PowerShell (#13); the wire payload is therefore "ls\r".
        assert server.send_input(5, "ls\n") == {"ok": True}
    finally:
        server._client = None

    assert calls == [
        ("/mcp/read", {"id": 5}),
        ("/mcp/input", {"id": 5, "data": "ls\r"}),
    ]
