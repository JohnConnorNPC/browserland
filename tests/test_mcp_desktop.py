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

import asyncio
import json
import os
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
import webterm.broker.app as app_mod
from webterm.broker.registry import WindowEntry
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

# ---- C2: the relay ------------------------------------------------------------
#
# In-process cells install a FAKE holder socket straight into
# ctx.control_clients (the relay only needs an async send()); the cells that
# exercise the REAL /control receive loop, the lease generation and the
# designated-socket rule run a real broker subprocess on a spare port and
# dial it with websockets, reusing _until/_lease from
# tests/test_terminal_control.py verbatim (which itself follows
# tests/test_broker_e2e.py's broker_proc).


class _FakeBrowserWS:
    """Stands in for the holder's /control socket: captures every frame sent
    and optionally resolves the pending call from inside send() (the reply
    lands on the request's own loop, like a real browser's would)."""

    def __init__(self, app, reply=None):
        self.app = app
        self.reply = reply
        self.frames = []

    async def send(self, frame):
        data = json.loads(frame)
        self.frames.append(data)
        call = self.app.ctx.desktop_rpcs.get(data.get("rid"))
        if call is not None and not call.future.done() and self.reply:
            self.reply(call.future)

    async def close(self, *a, **k):
        pass


def _holder(app, reply=None, client_id="b1", sockets=1):
    """Install a fake lease holder; returns the (single or list of) fake
    sockets. The relay timeout is shortened so refusal cells are fast."""
    app.ctx.desktop_rpc_timeout_s = 0.3
    wss = [_FakeBrowserWS(app, reply) for _ in range(sockets)]
    app.ctx.control_clients[client_id] = set(wss)
    app.ctx.active_client_id = client_id
    return wss[0] if sockets == 1 else wss


def _reply_ok(value):
    return lambda future: future.set_result({"ok": True, "value": value})


def _reply_err(code, detail=None):
    return lambda future: future.set_result(
        {"ok": False, "error": code, "detail": detail})


def _live(app, wid, pid=111, scope=None, mcp_mode=None):
    ws = _FakeBrowserWS(app)
    entry = WindowEntry(wid, pid, f"t{wid}", 80, 24, ws, kind="agent")
    entry.mcp_scope = scope
    entry.mcp_mode = mcp_mode
    app.ctx.registry._entries[wid] = entry
    return entry


def _desktop_app(tmp_path, monkeypatch, admin=None, **extra):
    return _make_app(tmp_path, monkeypatch, mcp_allow_desktop=True,
                     admin=admin, **extra)


def test_desktop_flag_off_403_desktop_disabled(tmp_path, monkeypatch):
    """#244: with allow_desktop off (the default) both desktop routes refuse
    403 desktop_disabled after the shared gate, before any relay."""
    app = _make_app(tmp_path, monkeypatch)
    _, resp = _mcp(app, "/mcp/desktop", {"op": "get"})
    assert (resp.status, resp.json["error"]) == (403, "desktop_disabled")
    _, resp = _mcp(app, "/mcp/desktop", method="GET")
    assert (resp.status, resp.json["error"]) == (403, "desktop_disabled")


def test_desktop_bad_op_and_body_400(tmp_path, monkeypatch):
    """#244: op outside the fixed allowlist is 400 bad_op; a non-object body
    bad_json; non-dict args bad_args -- all before any relay."""
    app = _desktop_app(tmp_path, monkeypatch)
    _holder(app)
    _, resp = _mcp(app, "/mcp/desktop", {"op": "explode"})
    assert (resp.status, resp.json["error"]) == (400, "bad_op")
    _, resp = app.test_client.post(
        "/mcp/desktop", data="{nope",
        headers=[("Authorization", f"Bearer {MCP_TOKEN}")])
    assert (resp.status, resp.json["error"]) == (400, "bad_json")
    _, resp = _mcp(app, "/mcp/desktop", {"op": "focus", "args": 5})
    assert (resp.status, resp.json["error"]) == (400, "bad_args")


def test_workspace_op_non_admin_403_admin_required_before_relay(
        tmp_path, monkeypatch):
    """#244 (owner ruling): workspace management is admin-only, and the
    refusal fires BEFORE the relay -- the holder sees no frame at all. With
    the credential the op relays, and w2's additive list value maps
    (columnWs/floatWs) pass through verbatim [M3]."""
    app = _desktop_app(tmp_path, monkeypatch)
    ws = _holder(app)
    _, resp = _mcp(app, "/mcp/desktop", {"op": "workspaces:list"})
    assert (resp.status, resp.json["error"]) == (403, "admin_required")
    assert ws.frames == []

    app2 = _desktop_app(tmp_path, monkeypatch, admin=ADMIN_TOKEN)
    ws2 = _holder(app2, _reply_ok({"active": 0, "items": [],
                                   "columnWs": {}, "floatWs": {}}))
    _, resp = _mcp(app2, "/mcp/desktop", {"op": "workspaces:list"},
                   admin=ADMIN_TOKEN)
    assert (resp.status, resp.json["value"]["active"]) == (200, 0)
    assert ws2.frames[0]["op"] == "workspaces:list"
    assert resp.json["value"]["columnWs"] == {}


def test_outcome_mapping_table(tmp_path, monkeypatch):
    """#244: the browser's error codes map per the wire contract --
    unknown_window answers the byte-identical shared 404, not_home and
    lost_lease 409, needs_confirm 409 with the count, last_workspace 409,
    bad_args 400, an op throw 502."""
    app = _desktop_app(tmp_path, monkeypatch, admin=ADMIN_TOKEN)
    _live(app, 5)

    cases = [
        ({"ok": False, "error": "unknown_window"}, 404,
         {"error": "unknown_or_off"}),
        ({"ok": False, "error": "not_home"}, 409, {"error": "not_home"}),
        ({"ok": False, "error": "lost_lease"}, 409,
         {"error": "lost_lease", "dispatched": True}),
        ({"ok": False, "error": "needs_confirm",
          "detail": {"count": 3}}, 409,
         {"error": "needs_confirm", "count": 3}),
        ({"ok": False, "error": "last_workspace"}, 409,
         {"error": "last_workspace"}),
        ({"ok": False, "error": "bad_args", "detail": "preset"}, 400,
         {"error": "bad_args", "detail": "preset"}),
        ({"ok": False, "error": "error", "detail": "boom"}, 502,
         {"error": "desktop_op_failed", "detail": "boom"}),
    ]
    for outcome, status, body in cases:
        ws = _holder(app, lambda f, o=outcome: f.set_result(dict(o)))
        _, resp = _mcp(app, "/mcp/desktop",
                       {"op": "workspaces:remove", "args": {"index": 1}},
                       admin=ADMIN_TOKEN)
        assert (resp.status, resp.json) == (status, body), outcome
    # byte-identity of the 404: same as the chokepoint's answer
    ws = _holder(app, _reply_err("unknown_window"))
    _, choke = _mcp(app, "/mcp/read", {"id": 424242})
    _, relayed = _mcp(app, "/mcp/desktop", {"op": "focus", "args": {"id": 5}},
                      admin=ADMIN_TOKEN)
    assert relayed.body == choke.body


def test_absent_inactive_answer_200_reason(tmp_path, monkeypatch):
    """#244: a mod that is off/disabled answers 200 {ok: false, reason} --
    the issue's 'no silent no-op' -- instead of an HTTP error."""
    app = _desktop_app(tmp_path, monkeypatch, admin=ADMIN_TOKEN)
    for reason in ("absent", "inactive"):
        _holder(app, _reply_err(reason))
        _, resp = _mcp(app, "/mcp/desktop", {"op": "workspaces:switch",
                                             "args": {"index": 0}},
                       admin=ADMIN_TOKEN)
        assert (resp.status, resp.json) == (200, {"ok": False,
                                                  "reason": reason})


def test_desktop_result_malformed_bodies_ignored(tmp_path, monkeypatch):
    """#244 [c22]: rid: true (which aliases key 1 in Python), a non-bool ok,
    a missing/non-string error and a needs_confirm without an int count are
    all dropped unanswered; the well-formed answer resolves. And a holder
    that never answers surfaces as the 504 timeout."""
    app = _desktop_app(tmp_path, monkeypatch, admin=ADMIN_TOKEN)

    class _SimpleCall:
        def __init__(self, future, client_id, generation):
            self.future = future
            self.client_id = client_id
            self.generation = generation

    loop = asyncio.new_event_loop()
    try:
        future = loop.create_future()
        app.ctx.desktop_rpcs[1] = _SimpleCall(
            future, "b1", app.ctx.lease_generation)
        validate = app.ctx._desktop_result_for_tests
        for junk in ({"rid": True, "ok": True},
                     {"rid": 1, "ok": "yes"},
                     {"rid": 1, "ok": False},
                     {"rid": 1, "ok": False, "error": 5},
                     {"rid": 1, "ok": False, "error": "needs_confirm"},
                     {"rid": 1, "ok": False, "error": "needs_confirm",
                      "detail": {"count": True}},
                     {"rid": 1, "ok": False, "error": "needs_confirm",
                      "detail": {"count": -1}}):
            validate("b1", junk)
            assert not future.done(), junk
        validate("b1", {"rid": 1, "ok": True, "value": 7})
        assert future.result() == {"ok": True, "value": 7}
    finally:
        loop.close()

    # the answer must also come from the RIGHT client: a watcher with the
    # live rid resolves nothing (the generation check has its own cell in
    # the subprocess harness).
    loop2 = asyncio.new_event_loop()
    try:
        future2 = loop2.create_future()
        app.ctx.desktop_rpcs[2] = _SimpleCall(
            future2, "b1", app.ctx.lease_generation)
        app.ctx._desktop_result_for_tests(
            "some-other-client", {"rid": 2, "ok": True, "value": "spoof"})
        assert not future2.done()
        app.ctx._desktop_result_for_tests(
            "b1", {"rid": 2, "ok": True, "value": "legit"})
        assert future2.result() == {"ok": True, "value": "legit"}
    finally:
        loop2.close()

    _holder(app)      # no reply -> the caller surfaces the timeout
    _, resp = _mcp(app, "/mcp/desktop", {"op": "workspaces:list"},
                   admin=ADMIN_TOKEN)
    assert (resp.status, resp.json["error"]) == (504, "desktop_timeout")


def test_result_with_wrong_rid_ignored_then_right_resolves(
        tmp_path, monkeypatch):
    """#244: an answer with an unknown rid changes nothing; the call it
    belongs to still resolves with the holder's real answer."""
    app = _desktop_app(tmp_path, monkeypatch, admin=ADMIN_TOKEN)
    validate = app.ctx._desktop_result_for_tests

    def reply(future):
        for rid, call in list(app.ctx.desktop_rpcs.items()):
            if call.future is future:
                validate("b1", {"rid": rid + 999, "ok": True,
                                "value": "junk"})
        future.set_result({"ok": True, "value": {"done": True}})

    _holder(app, reply)
    _, resp = _mcp(app, "/mcp/desktop", {"op": "focus", "args": {"id": 5}},
                   admin=ADMIN_TOKEN)
    assert (resp.status, resp.json["value"]) == (200, {"done": True})


def test_non_admin_relay_frame_carries_visible_ids_exactly(tmp_path,
                                                            monkeypatch):
    """#244 [M1]: a NON-admin's frame carries args.visible with exactly the
    ids it can see -- the off window and the foreign-scope window excluded
    (this applies to unscoped non-admins too); an admin's carries null."""
    app = _desktop_app(tmp_path, monkeypatch, admin=ADMIN_TOKEN)
    _live(app, 5, scope="mine", mcp_mode="readwrite")
    _live(app, 6, scope="mine", mcp_mode="off")          # off: invisible
    _live(app, 7, scope="theirs", mcp_mode="readwrite")  # foreign scope
    _live(app, 8, mcp_mode="read")                       # untagged, visible

    ws = _holder(app, _reply_ok({}))
    _, resp = app.test_client.post(
        "/mcp/desktop", json={"op": "focus", "args": {"id": 5}},
        headers=[("Authorization", f"Bearer {MCP_TOKEN}"),
                 ("X-Browserland-Scope", "mine")])
    assert resp.status == 200
    (frame,) = ws.frames
    assert frame["args"]["visible"] == [5]      # off + foreign + untagged out

    # an UNSCOPED non-admin sees every non-off window (admin_set irrelevant)
    ws1 = _holder(app, _reply_ok({}))
    _, resp = _mcp(app, "/mcp/desktop", {"op": "focus", "args": {"id": 5}})
    assert resp.status == 200
    (frame1,) = ws1.frames
    assert frame1["args"]["visible"] == [5, 7, 8]

    ws2 = _holder(app, _reply_ok({}))
    _, resp = _mcp(app, "/mcp/desktop", {"op": "focus", "args": {"id": 5}},
                   admin=ADMIN_TOKEN)
    assert resp.status == 200
    (frame2,) = ws2.frames
    assert frame2["args"]["visible"] is None


def test_desktop_out_of_scope_id_identical_404_bytes(tmp_path, monkeypatch):
    """#244: the per-named-id belt -- a scoped caller naming a foreign or off
    window gets the byte-identical shared 404 before any relay."""
    app = _desktop_app(tmp_path, monkeypatch)
    _live(app, 5, scope="mine", mcp_mode="readwrite")
    _live(app, 7, scope="theirs", mcp_mode="readwrite")
    ws = _holder(app)
    headers = [("Authorization", f"Bearer {MCP_TOKEN}"),
               ("X-Browserland-Scope", "mine")]
    _, foreign = app.test_client.post(
        "/mcp/desktop", json={"op": "focus", "args": {"id": 7}},
        headers=headers)
    _, missing = app.test_client.post(
        "/mcp/desktop", json={"op": "focus", "args": {"id": 424242}},
        headers=headers)
    assert (foreign.status, foreign.json["error"]) == (404,
                                                       "unknown_or_off")
    assert foreign.body == missing.body
    assert ws.frames == []


def test_two_id_ops_authorize_every_id_tab_into_foreign_target_404(
        tmp_path, monkeypatch):
    """#244 [c14]: tab_into names TWO ids; the foreign TARGET is refused with
    the shared 404 even though the mover is visible."""
    app = _desktop_app(tmp_path, monkeypatch)
    _live(app, 5, scope="mine", mcp_mode="readwrite")
    _live(app, 7, scope="theirs", mcp_mode="readwrite")
    ws = _holder(app)
    _, resp = app.test_client.post(
        "/mcp/desktop",
        json={"op": "tab_into", "args": {"id": 5, "target": 7}},
        headers=[("Authorization", f"Bearer {MCP_TOKEN}"),
                 ("X-Browserland-Scope", "mine")])
    assert (resp.status, resp.json["error"]) == (404, "unknown_or_off")
    assert ws.frames == []


def test_desktop_get_rebuilds_identity_from_registry(tmp_path, monkeypatch):
    """#244 [c20]: the registry is authoritative for identity -- rows for
    ids not live here are dropped, duplicate ids keep the first, titles come
    from the registry (a lying browser row cannot leak a foreign title),
    and only whitelisted layout attributes pass through [M3]."""
    app = _desktop_app(tmp_path, monkeypatch)
    e5 = _live(app, 5, scope="a", mcp_mode="readwrite")
    snap = {"viewport": {"w": 1920, "h": 1040}, "mode": "tiling",
            "focused_column": 1, "active_workspace": 0,
            "workspaces": [{"index": 0, "id": "L4", "name": "1"}],
            "home": True,
            "windows": [
                {"id": 5, "title": "LIES", "key": "local:5", "tiled": True,
                 "column": 1, "row": 0, "width_preset": "1/2",
                 "geometry": None, "workspace": 0, "kind": "terminal",
                 "minimized": False, "tabIndex": 0, "tabbed": False,
                 "split": None, "widthFrac": 0.5},
                {"id": 5, "title": "dup"},                      # duplicate
                {"id": None, "key": "edit:3", "title": "notes"},  # foreign
                {"id": 424242, "title": "ghost"},                 # not live
            ]}
    _holder(app, _reply_ok(snap))
    _, resp = _mcp(app, "/mcp/desktop", method="GET")
    assert resp.status == 200
    rows = resp.json["windows"]
    assert [r["id"] for r in rows] == [5]
    assert rows[0]["title"] == e5.title            # registry, not "LIES"
    assert rows[0]["width_preset"] == "1/2"
    assert rows[0]["widthFrac"] == 0.5             # additive extra passes
    assert rows[0]["kind"] == "terminal"
    assert resp.json["workspaces"] == [{"index": 0, "id": "L4",
                                        "name": "1"}]
    assert resp.json["home"] is True


def _get_snapshot(app, snap, admin=None):
    _holder(app, _reply_ok(snap))
    _, resp = _mcp(app, "/mcp/desktop", method="GET", admin=admin)
    assert resp.status == 200
    return resp.json


def test_262_chips_are_a_separate_typed_list(tmp_path, monkeypatch):
    """#262 (review round 1, R6): terminals that are only a taskbar chip
    arrive in their own top-level `chips` list, rebuilt like rows: an int id
    live in THIS registry, not already a row or a chip, the title from the
    registry, only {id, title}. The rows stay exactly as they would without
    any chips, and an older page that sends no `chips` gets none back."""
    app = _desktop_app(tmp_path, monkeypatch)
    for wid in (5, 6, 7):
        _live(app, wid, mcp_mode="readwrite")
    rows = [{"id": 5, "tiled": True, "column": 0, "row": 0,
             "kind": "terminal", "key": "local:5"}]
    with_chips = _get_snapshot(app, {"windows": rows, "chips": [
        {"id": 6, "title": "LIES", "extra": "dropped"},
        {"id": 6, "title": "dup"},              # duplicate chip
        {"id": 5, "title": "already a row"},    # a row wins
        {"id": 424242, "title": "ghost"},       # not live here
        {"id": True}, "junk",                   # not an int id / not a dict
        {"id": 7}]})
    assert with_chips["chips"] == [
        {"id": 6, "title": app.ctx.registry.get(6).title},
        {"id": 7, "title": app.ctx.registry.get(7).title}]
    without = _get_snapshot(app, {"windows": rows})
    assert "chips" not in without                # an older page: no key
    assert with_chips["windows"] == without["windows"]
    assert [r["id"] for r in with_chips["windows"]] == [5]
    # ...and the MCP tool's id namespacing covers the chips too.
    from webterm.mcptool import server
    out = server._namespace_snapshot("h", with_chips)
    assert [c["id"] for c in out["chips"]] == ["h:6", "h:7"]
    assert out["windows"][0]["id"] == "h:5"


def test_262_a_row_never_carries_open(tmp_path, monkeypatch):
    """Review round 2 (R13): the row whitelist stays the pre-#262 one, so a
    page that still sends a row key `open` gets it dropped."""
    app = _desktop_app(tmp_path, monkeypatch)
    _live(app, 5, mcp_mode="readwrite")
    out = _get_snapshot(app, {"windows": [
        {"id": 5, "open": False, "tiled": True, "kind": "terminal"}]})
    assert out["windows"] == [{"id": 5, "title": app.ctx.registry.get(5).title,
                               "tiled": True, "kind": "terminal"}]


def test_262_a_chip_id_must_be_a_real_int(tmp_path, monkeypatch):
    """Review round 2 (R14): with terminal 1 live, a chip id that is not a
    real int -- true (which Python treats as 1), "1", 1.5, null -- matches
    nothing and is dropped; only the int 1 names terminal 1."""
    app = _desktop_app(tmp_path, monkeypatch)
    _live(app, 1, mcp_mode="readwrite")
    out = _get_snapshot(app, {"windows": [], "chips": [
        {"id": True}, {"id": "1"}, {"id": 1.5}, {"id": None}, {}]})
    assert out["chips"] == []
    out = _get_snapshot(app, {"windows": [], "chips": [{"id": True}, {"id": 1}]})
    assert out["chips"] == [{"id": 1, "title": app.ctx.registry.get(1).title}]


_OLD_BROKER_REV = "e022ee8"           # the last broker before #262


def test_262_an_old_broker_drops_chips_and_keeps_the_rows(tmp_path):
    """Review round 2 (R15): a NEW page behind an OLD broker. The pre-#262
    broker (extracted from git at e022ee8 and run in its own interpreter)
    answers a snapshot that carries `chips` with the rows unchanged, no
    `chips` key, and no error."""
    import shutil
    import subprocess
    import sys
    import tarfile
    repo = Path(__file__).resolve().parents[1]
    if shutil.which("git") is None:
        pytest.skip("git not available")
    probe = subprocess.run(["git", "cat-file", "-e", _OLD_BROKER_REV + "^{commit}"],
                           cwd=repo, capture_output=True)
    if probe.returncode != 0:
        pytest.skip(f"{_OLD_BROKER_REV} is not in this clone")
    tar = tmp_path / "old.tar"
    subprocess.run(["git", "archive", "-o", str(tar), _OLD_BROKER_REV, "webterm"],
                   cwd=repo, check=True)
    old = tmp_path / "old"
    with tarfile.open(tar) as t:
        t.extractall(old)
    script = tmp_path / "probe.py"
    script.write_text(_OLD_BROKER_PROBE, encoding="utf-8")
    proc = subprocess.run([sys.executable, "-I", str(script), str(old),
                           str(tmp_path)], capture_output=True, text=True,
                          timeout=120)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    result = json.loads(proc.stdout.strip().splitlines()[-1])
    assert result["status"] == 200
    assert "chips" not in result["body"]
    assert result["body"]["windows"] == result["rows_without_chips"]


_OLD_BROKER_PROBE = r"""
import asyncio, json, sys
sys.path.insert(0, sys.argv[1])
from webterm.broker import app as app_mod
from webterm.broker.registry import WindowEntry

tmp = sys.argv[2]
cfg = {"state_path": tmp + "/s.json", "mcp_state_path": tmp + "/m.json",
       "auth_token": "t" * 32, "mcp_enabled": True, "mcp_token": "mcp-tok",
       "mcp_default_mode": "readwrite", "mcp_allow_desktop": True}
app = app_mod.create_app(cfg, name="old-broker-262")


class WS:
    def __init__(self, snap):
        self.snap = snap

    async def send(self, frame):
        rid = json.loads(frame).get("rid")
        call = app.ctx.desktop_rpcs.get(rid)
        if call is not None and not call.future.done():
            call.future.set_result({"ok": True, "value": self.snap})

    async def close(self, *a, **k):
        pass


for wid in (5, 6):
    app.ctx.registry._entries[wid] = WindowEntry(wid, 1, "t%d" % wid, 80, 24,
                                                 WS(None), kind="agent")
rows = [{"id": 5, "tiled": True, "kind": "terminal"}]


def get(snap):
    app.ctx.control_clients["b"] = {WS(snap)}
    app.ctx.active_client_id = "b"
    _, resp = app.test_client.get(
        "/mcp/desktop", headers={"Authorization": "Bearer mcp-tok"})
    return resp.status, resp.json


status, body = get({"windows": rows, "chips": [{"id": 6, "title": "chip"}]})
_, plain = get({"windows": rows})
print(json.dumps({"status": status, "body": body,
                  "rows_without_chips": plain["windows"]}))
"""


def test_262_chips_follow_the_callers_visibility(tmp_path, monkeypatch):
    """A non-admin keeps only chips it can see (effective mode not off),
    exactly like rows; an admin sees them all."""
    app = _desktop_app(tmp_path, monkeypatch, admin=ADMIN_TOKEN)
    _live(app, 6, mcp_mode="readwrite")
    _live(app, 7, mcp_mode="off")
    snap = {"windows": [], "chips": [{"id": 6}, {"id": 7}]}
    plain = _get_snapshot(app, snap)
    assert [c["id"] for c in plain["chips"]] == [6]
    admin = _get_snapshot(app, snap, admin=ADMIN_TOKEN)
    assert [c["id"] for c in admin["chips"]] == [6, 7]


def test_desktop_get_filters_rows_by_scope_and_mode(tmp_path, monkeypatch):
    """#244: after the identity rebuild the caller's visibility applies -- a
    scoped non-admin sees only its own windows; an admin keeps every row in
    the broker's namespace."""
    app = _desktop_app(tmp_path, monkeypatch)
    _live(app, 5, scope="mine", mcp_mode="readwrite")
    _live(app, 6, scope="mine", mcp_mode="off")
    _live(app, 7, scope="theirs", mcp_mode="readwrite")
    snap = {"windows": [{"id": wid, "title": f"w{wid}", "tiled": True}
                        for wid in (5, 6, 7)]}
    _holder(app, _reply_ok(snap))
    _, resp = app.test_client.get(
        "/mcp/desktop",
        headers=[("Authorization", f"Bearer {MCP_TOKEN}"),
                 ("X-Browserland-Scope", "mine")])
    assert [r["id"] for r in resp.json["windows"]] == [5]

    app2 = _desktop_app(tmp_path, monkeypatch, admin=ADMIN_TOKEN)
    _live(app2, 5, scope="mine", mcp_mode="readwrite")
    _live(app2, 6, scope="mine", mcp_mode="off")
    _holder(app2, _reply_ok(snap))
    _, resp = _mcp(app2, "/mcp/desktop", method="GET", admin=ADMIN_TOKEN)
    assert [r["id"] for r in resp.json["windows"]] == [5, 6]


def test_desktop_rpc_frame_shape():
    """#244: the protocol pin -- exact JSON, rid/op/args, and the caller's
    visible set riding args."""
    import webterm.protocol as protocol
    frame = json.loads(protocol.desktop_rpc_frame(
        12, "set_width", {"id": 7, "preset": "1/3", "visible": [3, 7]}))
    assert frame == {"type": "desktop_rpc", "rid": 12, "op": "set_width",
                     "args": {"id": 7, "preset": "1/3",
                              "visible": [3, 7]}}
    frame = json.loads(protocol.desktop_rpc_frame(1, "get"))
    assert frame["args"] == {}


def test_262_session_launched_frame_shape():
    """#262: the protocol pin for the launch announcement -- exact JSON:
    the new window's id, the lease generation and the broker run."""
    import webterm.protocol as protocol
    frame = json.loads(protocol.session_launched_frame(4503599627370497, 7,
                                                        "boot-abc"))
    assert frame == {"type": "session_launched", "id": 4503599627370497,
                     "gen": 7, "bootId": "boot-abc"}


def test_relay_sends_to_one_designated_holder_socket(tmp_path, monkeypatch):
    """#244 [c18]: a clientId may own several /control sockets (tabs); the
    rpc goes to exactly ONE of them -- the others never see the frame, so
    nothing can apply twice. (Which one is not pinned: control_clients is a
    set, so "first" is arbitrary-but-single; the design's insertion-order
    wording is a deviation noted in ready.md.)"""
    app = _desktop_app(tmp_path, monkeypatch, admin=ADMIN_TOKEN)
    first, second = _holder(app, _reply_ok({"done": True}), sockets=2)
    _, resp = _mcp(app, "/mcp/desktop", {"op": "focus", "args": {"id": 1}},
                   admin=ADMIN_TOKEN)
    assert resp.status == 200
    counts = sorted((len(first.frames), len(second.frames)))
    assert counts == [0, 1]


# ---- round 1: A2 / A3 / A6 ----------------------------------------------------


def test_desktop_get_rebuilds_top_level_fields(tmp_path, monkeypatch):
    """#244 round 1 A2: EVERY top-level snapshot field is rebuilt with a
    type discipline, never passed through -- a lying browser that stuffs
    foreign ids/titles into workspaces, oversized strings anywhere, or
    wrong-typed values gets them stripped or nulled, so the visibility
    promise cannot be bypassed through a field the filter never checks."""
    app = _desktop_app(tmp_path, monkeypatch)
    _live(app, 5, scope="a", mcp_mode="readwrite")
    snap = {
        "viewport": {"w": 1920, "h": "huge", "junk": 1},   # bad h -> dropped
        "mode": "t" * 33,                                    # too long
        "focused_column": True,                              # bool -> dropped
        "active_workspace": None,                            # null kept
        "home": "yes",                                       # not a bool
        "workspaces": [
            # extra keys (a foreign window list!) stripped, entry kept
            {"index": 0, "id": "L4", "name": "1",
             "windows": [{"id": 9, "title": "leak"}]},
            {"index": 1, "id": "L5", "name": "n" * 65},      # oversized name
            {"index": True, "id": "L6", "name": "ok"},       # bool index
            "junk",                                          # not a dict
        ],
        "windows": [
            {"id": 5, "title": "t", "tiled": "yes", "width_preset": "x" * 200,
             "geometry": {"x": 1.5, "y": 2, "w": 3, "h": 4},
             "widthFrac": 0.5, "kind": "terminal", "tabIndex": 0},
        ],
    }
    _holder(app, _reply_ok(snap))
    _, resp = _mcp(app, "/mcp/desktop", method="GET")
    assert resp.status == 200
    body = resp.json
    assert "viewport" not in body
    assert "mode" not in body
    assert "focused_column" not in body
    assert body["active_workspace"] is None
    assert "home" not in body
    # the foreign window list never reached the caller
    assert body["workspaces"] == [{"index": 0, "id": "L4", "name": "1"}]
    row = body["windows"][0]
    assert "tiled" not in row and "width_preset" not in row
    assert "geometry" not in row              # float x killed the whole geom
    assert row["widthFrac"] == 0.5 and row["kind"] == "terminal"
    assert row["tabIndex"] == 0


def test_stalled_sockets_cannot_stretch_the_deadline(tmp_path, monkeypatch):
    """#244 round 1 A3: ONE end-to-end deadline shared by the send loop and
    the reply wait -- N stalled sockets cost at most ONE timeout, never
    (N+1) x it."""
    class _StalledWS:
        def __init__(self):
            self.frames = []

        async def send(self, frame):
            self.frames.append(json.loads(frame))
            await asyncio.sleep(10)

    app = _desktop_app(tmp_path, monkeypatch)
    app.ctx.desktop_rpc_timeout_s = 0.3
    sockets = {_StalledWS() for _ in range(4)}
    app.ctx.control_clients["stalled"] = sockets
    app.ctx.active_client_id = "stalled"
    t0 = time.monotonic()
    _, resp = _mcp(app, "/mcp/desktop", {"op": "get"})
    elapsed = time.monotonic() - t0
    assert (resp.status, resp.json["error"]) == (504, "desktop_timeout")
    assert elapsed < 0.9, elapsed


def test_desktop_get_keeps_unnamed_workspaces(tmp_path, monkeypatch):
    """#244 round 2: a null/absent name is the UNNAMED workspace (the mod's
    default label mode) and survives the rebuild with name null -- a default
    desktop's get_desktop must not answer an empty workspaces list while
    active_workspace points into it. Only a PRESENT name that is not a
    str<=64 drops the entry."""
    app = _desktop_app(tmp_path, monkeypatch)
    snap = {"active_workspace": 1, "workspaces": [
        {"index": 0, "id": "L1", "name": None},      # unnamed
        {"index": 1, "id": "L2"},                    # name absent
        {"index": 2, "id": "L3", "name": "named"},
        {"index": 3, "id": "L4", "name": 7},         # present, wrong type
    ]}
    _holder(app, _reply_ok(snap))
    _, resp = _mcp(app, "/mcp/desktop", method="GET")
    assert resp.status == 200
    assert resp.json["workspaces"] == [
        {"index": 0, "id": "L1", "name": None},
        {"index": 1, "id": "L2", "name": None},
        {"index": 2, "id": "L3", "name": "named"},
    ]
    assert resp.json["active_workspace"] == 1


def test_desktop_result_union_enforced(tmp_path, monkeypatch):
    """#244 round 1 A6: the success/error union is ENFORCED, not just
    documented -- ok:true carries a value and no error key; ok:false one of
    the closed error codes. Anything else is dropped (the agent sees the
    timeout)."""
    app = _desktop_app(tmp_path, monkeypatch, admin=ADMIN_TOKEN)

    class _SimpleCall:
        def __init__(self, future, client_id, generation):
            self.future = future
            self.client_id = client_id
            self.generation = generation

    loop = asyncio.new_event_loop()
    try:
        future = loop.create_future()
        app.ctx.desktop_rpcs[1] = _SimpleCall(
            future, "b1", app.ctx.lease_generation)
        validate = app.ctx._desktop_result_for_tests
        for junk in ({"rid": 1, "ok": True, "error": "x", "value": 1},
                     {"rid": 1, "ok": True},
                     {"rid": 1, "ok": False, "error": "surprise"},
                     {"rid": 1, "ok": False, "error": ""}):
            validate("b1", junk)
            assert not future.done(), junk
        validate("b1", {"rid": 1, "ok": False, "error": "absent"})
        assert future.result() == {"ok": False, "error": "absent",
                                   "detail": None}
    finally:
        loop.close()


# ---- C2, real broker subprocess + websockets (M5 harness) --------------------
#
# _until/_lease are reused VERBATIM from tests/test_terminal_control.py (same
# provenance comment: that file's fixture follows test_broker_e2e.py's
# broker_proc). The broker runs HEADLESS with its state under a tempdir and
# MCP desktop enabled via --config.

REPO = Path(__file__).resolve().parents[1]
import websockets  # noqa: E402
from tests.test_terminal_control import _until  # noqa: E402  (verbatim)


async def _lease(port, client_id, token=TEST_TOKEN):
    """A /control socket for ``client_id`` that holds the lease -- the
    tests/test_terminal_control.py helper with the token PARAMETERIZED (its
    own hard-codes that module's broker token; this module's subprocess
    configures TEST_TOKEN). Otherwise verbatim."""
    ws = await websockets.connect(
        f"ws://127.0.0.1:{port}/control?clientId={client_id}&token={token}")
    status = json.loads(await asyncio.wait_for(ws.recv(), 5))
    if not status["active"]:
        await ws.send(json.dumps({"type": "become_active"}))
        await _until(ws, lambda f: f.get("type") == "status"
                     and f["active"] is True)
    return ws


def _free_port() -> int:
    import socket
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _post(url, body, headers, timeout=30):
    import urllib.request
    import urllib.error
    request = urllib.request.Request(
        url, data=json.dumps(body).encode("utf-8"), method="POST",
        headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"{}")


def _get(url, timeout=10):
    import urllib.request
    import urllib.error
    request = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"{}")


@pytest.fixture(scope="module")
def broker():
    port = _free_port()
    tmp = Path(tempfile.mkdtemp(prefix="webterm-mcp-desktop-"))
    cfg = {
        "state_path": str(tmp / "webterm_state.json"),
        "mcp_state_path": str(tmp / "webterm_mcp.json"),
        "mcp_windows_path": str(tmp / "webterm_mcp_windows.json"),
        "auth_token": TEST_TOKEN,
        "mcp_enabled": True,
        "mcp_token": MCP_TOKEN,
        "mcp_admin_token": ADMIN_TOKEN,
        "mcp_default_mode": "readwrite",
        "mcp_allow_desktop": True,
        "serve_ui": False,
    }
    cfg_path = tmp / "broker_config.json"
    cfg_path.write_text(json.dumps(cfg), encoding="utf-8")
    env = dict(os.environ)
    env["PYTHONPATH"] = str(REPO) + os.pathsep + env.get("PYTHONPATH", "")
    for key in list(env):
        if key.startswith("CLAUDE_CODE_") or key.startswith("CLAUDECODE"):
            env.pop(key, None)
    env.pop("BROWSERLAND_BROKER_URL", None)
    env.pop("WEB_TERMINAL_TOKEN", None)
    proc = subprocess.Popen(
        [sys.executable, "-m", "webterm.broker", "--host", "127.0.0.1",
         "--port", str(port), "--config", str(cfg_path), "--headless"],
        cwd=str(REPO), env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    base = f"http://127.0.0.1:{port}"
    deadline = time.time() + 20
    while True:
        try:
            status, _ = _get(f"{base}/sessions?token={TEST_TOKEN}", timeout=5)
            if status == 200:
                break
        except OSError:
            pass
        if proc.poll() is not None:
            raise RuntimeError(f"broker exited early: {proc.returncode}")
        if time.time() > deadline:
            proc.kill()
            raise RuntimeError("broker did not come up")
        time.sleep(0.2)
    yield port, base, str(tmp)
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()


AUTH = {"Authorization": f"Bearer {MCP_TOKEN}",
        "Content-Type": "application/json"}
ADMIN_AUTH = {"Authorization": f"Bearer {MCP_TOKEN}",
              MCP_ADMIN_HEADER: ADMIN_TOKEN,
              "Content-Type": "application/json"}

def test_no_holder_409_no_active_browser(broker):
    port, base, _ = broker
    status, body = _post(f"{base}/mcp/desktop",
                         {"op": "get", "args": {}}, AUTH)
    assert (status, body["error"]) == (409, "no_active_browser")


def test_desktop_roundtrip_and_visible_null_for_admin(broker):
    """A real /control holder receives the desktop_rpc frame (exact shape,
    admin => visible null) and its desktop_result resolves the call. One
    asyncio.run for the whole cell: the ws and the frame waits must share a
    loop."""
    port, base, _ = broker

    async def script():
        ws = await _lease(port, "admin-browser")
        try:
            with ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(_post, f"{base}/mcp/desktop",
                                     {"op": "get", "args": {}}, ADMIN_AUTH)
                frame = await _until(ws, lambda f: f.get("type")
                                     == "desktop_rpc")
                await ws.send(json.dumps({
                    "type": "desktop_result", "rid": frame["rid"],
                    "ok": True,
                    "value": {"viewport": {"w": 800, "h": 600},
                              "windows": []}}))
                return frame, future.result(timeout=15)
        finally:
            await ws.close()

    frame, (status, body) = asyncio.run(script())
    assert frame["op"] == "get" and frame["args"]["visible"] is None
    assert status == 200 and body["ok"] is True
    # op get answers the rebuilt snapshot: viewport flattened to the top
    assert body["viewport"] == {"w": 800, "h": 600}
    assert body["windows"] == []


def test_timeout_504_desktop_timeout(broker):
    port, base, _ = broker

    async def script():
        ws = await _lease(port, "silent-browser")
        try:
            with ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(_post, f"{base}/mcp/desktop",
                                     {"op": "get", "args": {}}, AUTH)
                await _until(ws, lambda f: f.get("type") == "desktop_rpc")
                return future.result(timeout=30)     # never answers
        finally:
            await ws.close()

    status, body = asyncio.run(script())
    assert (status, body["error"]) == (504, "desktop_timeout")


def test_result_from_non_holder_ignored(broker):
    """A second client's answer with the holder's rid resolves nothing; the
    holder's own answer still wins."""
    port, base, _ = broker

    async def script():
        holder = await _lease(port, "the-holder")
        # a SECOND client that does NOT take the lease (a plain /control
        # socket, no become_active -- _lease would flip the lease to it)
        other = await websockets.connect(
            f"ws://127.0.0.1:{port}/control?clientId=not-the-holder"
            f"&token={TEST_TOKEN}")
        json.loads(await asyncio.wait_for(other.recv(), 5))   # its status
        try:
            with ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(_post, f"{base}/mcp/desktop",
                                     {"op": "focus",
                                      "args": {"id": 1}}, ADMIN_AUTH)
                frame = await _until(holder, lambda f: f.get("type")
                                     == "desktop_rpc")
                # #250: rids start at a random offset per run, so the spoof
                # names the LIVE call's rid, sent while it is pending.
                await other.send(json.dumps({
                    "type": "desktop_result", "rid": frame["rid"],
                    "ok": True, "value": "spoofed"}))
                await asyncio.sleep(0.2)
                await holder.send(json.dumps({
                    "type": "desktop_result", "rid": frame["rid"],
                    "ok": True, "value": "real"}))
                return future.result(timeout=15)
        finally:
            await holder.close()
            await other.close()

    status, body = asyncio.run(script())
    assert (status, body["value"]) == (200, "real")


def test_stale_generation_is_lost_lease(broker):
    """The holder's late answer after ANOTHER browser took the lease is
    rejected (a new generation), and the call resolves 409 lost_lease
    [c15]; the pending call is failed INSIDE the lease lock [c16]."""
    port, base, _ = broker

    async def script():
        a = await _lease(port, "gen-a")
        b = None
        try:
            with ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(_post, f"{base}/mcp/desktop",
                                     {"op": "get", "args": {}}, AUTH)
                frame = await _until(a, lambda f: f.get("type")
                                     == "desktop_rpc")
                b = await _lease(port, "gen-b")   # takes the lease from a
                await a.send(json.dumps({          # a's LATE answer
                    "type": "desktop_result", "rid": frame["rid"],
                    "ok": True, "value": "stale"}))
                return future.result(timeout=15)
        finally:
            await a.close()
            if b is not None:
                await b.close()

    status, body = asyncio.run(script())
    assert (status, body["error"]) == (409, "lost_lease")


def test_holder_disconnect_mid_rpc_409_lost_lease(broker):
    """The holder receives the frame and goes away: the release bumps the
    generation and the pending call resolves 409 lost_lease, not a
    timeout."""
    port, base, _ = broker

    async def script():
        a = await _lease(port, "dying-holder")
        try:
            with ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(_post, f"{base}/mcp/desktop",
                                     {"op": "get", "args": {}}, AUTH)
                await _until(a, lambda f: f.get("type") == "desktop_rpc")
                await a.close()
                return future.result(timeout=15)
        finally:
            pass

    status, body = asyncio.run(script())
    assert (status, body["error"]) == (409, "lost_lease")


def test_snapshot_keeps_258_workspace_fields_typed_and_present_only(
        tmp_path, monkeypatch):
    """#258 BK1-BK3: an entry's color/stack and the top-level stacks pass the
    rebuild only when the browser SENT them, typed -- a bad value drops that
    key, never the row -- and an older page's snapshot keeps its exact
    shape (no new keys appear)."""
    app = _desktop_app(tmp_path, monkeypatch)
    snap = {"active_workspace": 0, "workspaces": [
        {"index": 0, "id": "L1", "name": None, "color": "#e96d6d", "stack": "S1"},
        {"index": 1, "id": "L2", "name": None, "color": None, "stack": None},
        {"index": 2, "id": "L3", "name": "x", "color": "red", "stack": 7},
        {"index": 3, "id": "L4", "name": None, "color": "#ABCDEF",
         "stack": "s" * 65},
    ], "stacks": [
        {"id": "S1", "name": "dev", "color": "#5fbf7f", "last": "L1"},
        {"id": "S2", "name": "n" * 65, "color": "blue", "last": 9},
        {"name": "no id"}, "junk",
    ]}
    _holder(app, _reply_ok(snap))
    _, resp = _mcp(app, "/mcp/desktop", method="GET")
    assert resp.status == 200
    assert resp.json["workspaces"] == [
        {"index": 0, "id": "L1", "name": None, "color": "#e96d6d", "stack": "S1"},
        {"index": 1, "id": "L2", "name": None, "color": None, "stack": None},
        {"index": 2, "id": "L3", "name": "x"},
        {"index": 3, "id": "L4", "name": None},
    ]
    assert resp.json["stacks"] == [
        {"id": "S1", "name": "dev", "color": "#5fbf7f", "last": "L1"},
        {"id": "S2", "name": None, "color": None, "last": None},
    ]

    old = _desktop_app(tmp_path, monkeypatch)
    _holder(old, _reply_ok({"active_workspace": 0, "workspaces": [
        {"index": 0, "id": "L1", "name": None}]}))
    _, resp = _mcp(old, "/mcp/desktop", method="GET")
    assert resp.json["workspaces"] == [{"index": 0, "id": "L1", "name": None}]
    assert "stacks" not in resp.json

    off = _desktop_app(tmp_path, monkeypatch)
    _holder(off, _reply_ok({"workspaces": None, "active_workspace": None,
                            "stacks": None}))
    _, resp = _mcp(off, "/mcp/desktop", method="GET")
    assert resp.json["stacks"] is None


def test_258_workspace_ops_are_admin_only_and_relayed(tmp_path, monkeypatch):
    """#258 BK4-BK6: move/color/stack/unstack join the admin-only workspace
    set (403 before any relay for a non-admin), relay for an admin (never
    bad_op), and a direct list value with the new fields reaches the caller
    verbatim."""
    new_ops = ("workspaces:move", "workspaces:color", "workspaces:stack",
               "workspaces:unstack")
    app = _desktop_app(tmp_path, monkeypatch)
    ws = _holder(app)
    for op in new_ops:
        _, resp = _mcp(app, "/mcp/desktop", {"op": op, "args": {"index": 0}})
        assert (resp.status, resp.json["error"]) == (403, "admin_required"), op
    assert ws.frames == []

    app2 = _desktop_app(tmp_path, monkeypatch, admin=ADMIN_TOKEN)
    value = {"active": 0, "items": [
        {"index": 0, "id": "L1", "name": None, "color": "#e96d6d", "stack": "S1"}],
        "columnWs": {}, "floatWs": {},
        "stacks": [{"id": "S1", "name": "dev", "color": None, "last": None}],
        "palette": ["#4aa3ff", "#5fbf7f"]}
    ws2 = _holder(app2, _reply_ok(value))
    for op in new_ops:
        _, resp = _mcp(app2, "/mcp/desktop", {"op": op, "args": {"index": 0}},
                       admin=ADMIN_TOKEN)
        assert resp.status == 200, (op, resp.status, resp.json)
    assert [f["op"] for f in ws2.frames] == list(new_ops)
    _, resp = _mcp(app2, "/mcp/desktop", {"op": "workspaces:list"},
                   admin=ADMIN_TOKEN)
    assert resp.status == 200 and resp.json["value"] == value
