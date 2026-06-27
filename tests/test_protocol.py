"""Frame shapes must match the literal JSON the broker parses
(relay origin: xterm-py browser/broker.py + websocket_handler.py,
a separate codebase at https://github.com/JohnConnorNPC/xterm-py)."""

import json

from webterm import protocol


def test_hello_minimal_shape():
    frame = json.loads(protocol.hello_frame(42, 1234, "bash", 80, 24))
    assert frame == {
        "type": "hello",
        "window_id": 42,
        "pid": 1234,
        "title": "bash",
        "cols": 80,
        "rows": 24,
    }
    # Ints must be JSON numbers, not strings.
    assert isinstance(frame["window_id"], int)
    assert isinstance(frame["pid"], int)
    assert isinstance(frame["cols"], int)
    assert isinstance(frame["rows"], int)


def test_hello_optional_fields():
    frame = json.loads(protocol.hello_frame(
        1, 2, "t", 80, 24, host="box1", kind="agent", version="0.1.0+abc"))
    assert frame["host"] == "box1"
    assert frame["kind"] == "agent"
    assert frame["version"] == "0.1.0+abc"   # build id for stale detection (#22)
    # Absent when not supplied — non-agent producers omit them.
    bare = json.loads(protocol.hello_frame(1, 2, "t", 80, 24))
    assert ("host" not in bare and "kind" not in bare
            and "version" not in bare)


def test_build_version_starts_with_package_version():
    import webterm
    v = webterm.build_version()
    assert isinstance(v, str) and v.startswith(webterm.__version__)


def test_build_version_falls_back_when_git_unavailable(monkeypatch):
    import subprocess

    import webterm
    monkeypatch.setattr(webterm, "_BUILD_VERSION", None)

    def boom(*a, **k):
        raise FileNotFoundError("git not found")

    monkeypatch.setattr(subprocess, "run", boom)
    assert webterm.build_version() == webterm.__version__   # bare version, no raise


def test_build_version_is_cached(monkeypatch):
    import subprocess

    import webterm
    monkeypatch.setattr(webterm, "_BUILD_VERSION", None)
    first = webterm.build_version()
    calls = []
    real = subprocess.run
    monkeypatch.setattr(subprocess, "run",
                        lambda *a, **k: (calls.append(1), real(*a, **k))[1])
    # Second call must serve the cache and NOT re-shell-out to git.
    assert webterm.build_version() == first and calls == []


def test_title_frame():
    assert json.loads(protocol.title_frame("vim")) == {
        "type": "title", "data": "vim"}


def test_resized_frame():
    assert json.loads(protocol.resized_frame(120, 32)) == {
        "type": "resized", "cols": 120, "rows": 32}


def test_exit_frame():
    frame = json.loads(protocol.exit_frame(0))
    assert frame == {"type": "exit", "code": 0}
    # Code must be a JSON number, and non-zero codes pass through.
    assert isinstance(frame["code"], int)
    assert json.loads(protocol.exit_frame(137)) == {"type": "exit", "code": 137}


def test_broker_to_producer_frames():
    assert json.loads(protocol.input_frame("ls\r")) == {
        "type": "input", "data": "ls\r"}
    assert json.loads(protocol.resize_frame(100, 30)) == {
        "type": "resize", "cols": 100, "rows": 30}
    assert json.loads(protocol.snapshot_please_frame()) == {
        "type": "snapshot_please"}


def test_mcp_activity_frame():
    # #33: a transient per-window MCP-touch pulse; carries only the kind (the
    # window id is implicit in the per-session browser WS).
    assert json.loads(protocol.mcp_activity_frame("read")) == {
        "type": "mcp_activity", "kind": "read"}
    assert json.loads(protocol.mcp_activity_frame("write")) == {
        "type": "mcp_activity", "kind": "write"}


def test_error_frame():
    assert json.loads(protocol.error_frame("unknown_session", 7)) == {
        "type": "error", "reason": "unknown_session", "session_id": 7}


def test_management_rpc_request_frames():
    assert json.loads(protocol.procs_please_frame(5)) == {
        "type": "procs_please", "req": 5}
    assert json.loads(protocol.kill_frame(7, 4321)) == {
        "type": "kill", "req": 7, "pid": 4321}
    assert json.loads(protocol.git_status_please_frame(9)) == {
        "type": "git_status_please", "req": 9}
    assert json.loads(protocol.reset_please_frame(11)) == {
        "type": "reset_please", "req": 11}


def test_management_rpc_reply_frames():
    procs = [{"pid": 1, "name": "sh"}]
    assert json.loads(protocol.procs_frame(5, procs)) == {
        "type": "procs", "req": 5, "procs": procs}
    # killed: error/pid only present when supplied.
    assert json.loads(protocol.killed_frame(7, True)) == {
        "type": "killed", "req": 7, "ok": True}
    assert json.loads(protocol.killed_frame(7, False, error="not_in_session",
                                            pid=99)) == {
        "type": "killed", "req": 7, "ok": False,
        "error": "not_in_session", "pid": 99}
    # git_status: the status dict is merged under the envelope.
    frame = json.loads(protocol.git_status_frame(
        3, {"ok": True, "branch": "main", "dirty": False}))
    assert frame["type"] == "git_status" and frame["req"] == 3
    assert frame["branch"] == "main" and frame["ok"] is True
    # reset_done (#27): error only present when supplied.
    assert json.loads(protocol.reset_done_frame(11, True)) == {
        "type": "reset_done", "req": 11, "ok": True}
    assert json.loads(protocol.reset_done_frame(11, False, error="boom")) == {
        "type": "reset_done", "req": 11, "ok": False, "error": "boom"}


def test_parse_round_trip():
    assert protocol.parse(protocol.input_frame("x"))["type"] == "input"
    assert protocol.parse("not json") is None
    assert protocol.parse("[1,2,3]") is None
    assert protocol.parse('"str"') is None


def test_hello_id_within_js_safe_range():
    from webterm.broker.launcher import Launcher
    from webterm.broker.registry import BrokerRegistry
    launcher = Launcher(BrokerRegistry(), None, 4445, None)
    for _ in range(100):
        wid = launcher._allocate_window_id()
        assert (1 << 52) <= wid < (1 << 53)


def test_mode_frame():
    assert json.loads(protocol.mode_frame(True)) == {
        "type": "mode", "app_cursor": True}
    assert json.loads(protocol.mode_frame(False)) == {
        "type": "mode", "app_cursor": False}


def test_screen_text_please_frame_view_lines():
    f = json.loads(protocol.screen_text_please_frame(5, "scrollback", 200))
    assert f == {"type": "screen_text_please", "req": 5,
                 "view": "scrollback", "lines": 200,
                 "wait_for_change": None, "timeout_ms": 0,
                 "wait_for_text": None, "wait_for_regex": None,
                 "wait_absent": False}
    d = json.loads(protocol.screen_text_please_frame(5))
    assert d["view"] == "screen" and d["lines"] == 0
    assert d["wait_for_change"] is None and d["timeout_ms"] == 0


def test_screen_text_please_frame_wait_for_content():
    f = json.loads(protocol.screen_text_please_frame(
        7, wait_for_text="Ready", timeout_ms=3000, wait_absent=True))
    assert f["wait_for_text"] == "Ready" and f["wait_absent"] is True
    assert f["timeout_ms"] == 3000 and f["wait_for_regex"] is None
    g = json.loads(protocol.screen_text_please_frame(
        8, wait_for_regex=r"\d+%"))
    assert g["wait_for_regex"] == r"\d+%" and g["wait_absent"] is False


def test_screen_text_frame_matched():
    # matched is present only when set (content-predicate reads); omitted for
    # immediate / wait_for_change reads so they stay unambiguous.
    plain = json.loads(protocol.screen_text_frame(1, "hi", 80, 24))
    assert "matched" not in plain
    hit = json.loads(protocol.screen_text_frame(1, "hi", 80, 24, matched=True))
    assert hit["matched"] is True
    miss = json.loads(protocol.screen_text_frame(1, "hi", 80, 24, matched=False))
    assert miss["matched"] is False


def test_screen_text_please_frame_wait_for_change():
    # #26: a baseline hash + timeout ride the frame to the agent.
    f = json.loads(protocol.screen_text_please_frame(
        9, "screen", 0, wait_for_change="abc123", timeout_ms=3000))
    assert f["wait_for_change"] == "abc123" and f["timeout_ms"] == 3000


def test_screen_text_frame_content_hash():
    # #26: content_hash rides the reply; default is the empty string.
    f = json.loads(protocol.screen_text_frame(
        7, "grid", 80, 24, content_hash="deadbeefcafef00d"))
    assert f["content_hash"] == "deadbeefcafef00d"
    assert json.loads(protocol.screen_text_frame(7, "g", 80, 24))[
        "content_hash"] == ""


def test_screen_text_frame_alt_cursor_view_fields():
    f = json.loads(protocol.screen_text_frame(
        7, "grid", 80, 24, alt_screen=True, app_cursor=True,
        cursor={"row": 3, "col": 9}, view="screen", history_lines=0))
    assert f["alt_screen"] is True and f["view"] == "screen"
    assert f["app_cursor"] is True             # #23 DECCKM
    assert f["cursor"] == {"row": 3, "col": 9} and f["history_lines"] == 0
    # default app_cursor is False (back-compat)
    assert json.loads(protocol.screen_text_frame(7, "g", 80, 24))["app_cursor"] is False
    # degraded -> cursor null, view raw
    d = json.loads(protocol.screen_text_frame(
        7, "raw", 80, 24, degraded=True, cursor=None, view="raw"))
    assert d["degraded"] is True and d["cursor"] is None and d["view"] == "raw"
