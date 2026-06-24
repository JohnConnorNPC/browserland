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
        1, 2, "t", 80, 24, host="box1", kind="agent"))
    assert frame["host"] == "box1"
    assert frame["kind"] == "agent"
    # Absent when not supplied — non-agent producers omit them.
    bare = json.loads(protocol.hello_frame(1, 2, "t", 80, 24))
    assert "host" not in bare and "kind" not in bare


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
