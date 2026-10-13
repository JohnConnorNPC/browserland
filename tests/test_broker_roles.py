"""Broker roles (#253): Hub / Node and ``terminals: false``.

* ``roles.resolve`` is the one fold of the config ``role`` alias, ``--role``,
  ``--headless`` and ``--no-terminals`` into the two stored keys (``serve_ui``,
  ``terminals``). ``role`` is derived and never survives the fold.
* A node (no page) with ``terminals: false`` is refused at startup: exit 2,
  one line on stderr, before the supervisor or the worker exists. The
  one-shots (``--print-token``, ``--scan-recordings``) still run on that
  config -- they bind nothing.
* ``create_app`` refuses the same combination (ValueError), so a direct
  caller cannot build it either.
* ``GET /info`` reports the derived ``role`` and ``terminals`` on both kinds;
  the boot log names them.
"""

from __future__ import annotations

import json
import logging
import os
import re
import socket
import subprocess
import sys
from pathlib import Path

import pytest

from .auth_helpers import TEST_TOKEN, authed
from webterm.broker import roles
from webterm.broker.app import create_app

REPO = Path(__file__).resolve().parents[1]
REFUSAL = ("error: refusing to start: a node (serve_ui: false) with "
           "terminals: false has no terminals for another broker to use; "
           "set terminals: true, or serve the page (role: hub)")

_seq = 0


def _app(tmp_path, **cfg):
    global _seq
    _seq += 1
    base = {"state_path": str(tmp_path / "webterm_state.json"),
            "auth_token": TEST_TOKEN}
    base.update(cfg)
    return create_app(base, name=f"webterm-roles-{_seq}")


# ---- roles.resolve ---------------------------------------------------------

# (config in, cli kwargs, error substring or None, serve_ui after, terminals
# after -- "absent" when the key must not have been written)
RESOLVE_CASES = [
    ("default", {}, {}, None, "absent", "absent"),
    ("alias node", {"role": "node"}, {}, None, False, "absent"),
    ("alias hub", {"role": "hub"}, {}, None, True, "absent"),
    ("alias agrees", {"role": "node", "serve_ui": False}, {}, None, False,
     "absent"),
    ("alias conflict", {"role": "node", "serve_ui": True}, {},
     "contradicts serve_ui: true", None, None),
    ("alias conflict hub", {"role": "hub", "serve_ui": False}, {},
     "contradicts serve_ui: false", None, None),
    ("bad role", {"role": "desktop"}, {}, "unknown role 'desktop'", None,
     None),
    ("terminals string", {"terminals": "false"}, {},
     "terminals must be true or false", None, None),
    ("cli node beats config", {"serve_ui": True}, {"cli_role": "node"}, None,
     False, "absent"),
    ("cli hub beats config", {"serve_ui": False}, {"cli_role": "hub"}, None,
     True, "absent"),
    ("cli hub beats alias", {"role": "node"}, {"cli_role": "hub"}, None, True,
     "absent"),
    ("headless", {}, {"cli_headless": True}, None, False, "absent"),
    ("headless + role node", {}, {"cli_headless": True, "cli_role": "node"},
     None, False, "absent"),
    ("headless + role hub", {}, {"cli_headless": True, "cli_role": "hub"},
     "contradicts --role hub", None, None),
    ("no-terminals hub", {}, {"cli_no_terminals": True}, None, "absent",
     False),
    ("hub terminals off", {"terminals": False}, {}, None, "absent", False),
    ("node terminals off", {"serve_ui": False, "terminals": False}, {},
     "refusing to start", None, None),
    ("alias node terminals off", {"role": "node", "terminals": False}, {},
     "refusing to start", None, None),
    ("headless + no-terminals", {}, {"cli_headless": True,
                                     "cli_no_terminals": True},
     "refusing to start", None, None),
    ("cli hub rescues config node", {"serve_ui": False, "terminals": False},
     {"cli_role": "hub"}, None, True, False),
]


@pytest.mark.parametrize("label,cfg,cli,err,serve_ui,terminals",
                         RESOLVE_CASES, ids=[c[0] for c in RESOLVE_CASES])
def test_resolve_table(label, cfg, cli, err, serve_ui, terminals):
    config = dict(cfg)
    got = roles.resolve(config, **cli)
    if err is not None:
        assert got is not None and err in got, got
        assert got.startswith("error: ") and "\n" not in got
        # A refusal leaves the input untouched, so a retry refuses again.
        assert config == cfg
        assert roles.resolve(config, **cli) == got
        return
    assert got is None
    assert "role" not in config                 # derived, never stored
    for key, want in (("serve_ui", serve_ui), ("terminals", terminals)):
        if want == "absent":
            assert key not in config, (key, config)
        else:
            assert config[key] is want, (key, config)
    # Idempotent: create_app runs it again on the folded dict.
    again = dict(config)
    assert roles.resolve(again) is None
    assert again == config


def test_refusal_wording_is_the_one_line():
    assert roles.resolve({"serve_ui": False, "terminals": False}) == REFUSAL


def test_role_of():
    assert roles.role_of(True) == "hub"
    assert roles.role_of(False) == "node"


# ---- startup refusal (subprocess) ------------------------------------------

def _env():
    env = {k: v for k, v in os.environ.items()
           if not (k.startswith("CLAUDE_CODE_") or k == "CLAUDECODE"
                   or k.startswith("ANTHROPIC_"))}
    env["WEB_TERMINAL_TOKEN"] = TEST_TOKEN
    env["PYTHONPATH"] = str(REPO) + os.pathsep + env.get("PYTHONPATH", "")
    env.pop("BROWSERLAND_BROKER_URL", None)
    env.pop("WEB_TERMINAL_CONFIG", None)
    return env


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _connectable(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1):
            return True
    except OSError:
        return False


def _run_broker(tmp_path, config: dict, *args, timeout=30):
    """Start ``python -m webterm.broker`` on a free port with ``config`` and
    wait for it to exit. A broker that binds instead (the refusal went missing)
    is killed by PID in ``finally`` and the test fails on the timeout."""
    cfg_path = tmp_path / "broker_config.json"
    config = dict(config)
    config.setdefault("state_path", str(tmp_path / "webterm_state.json"))
    cfg_path.write_text(json.dumps(config), encoding="utf-8")
    port = _free_port()
    proc = subprocess.Popen(
        [sys.executable, "-m", "webterm.broker", "--config", str(cfg_path),
         "--host", "127.0.0.1", "--port", str(port), *args],
        cwd=str(tmp_path), env=_env(),
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        try:
            out, err = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            bound = _connectable(port)
            proc.kill()
            proc.communicate()
            pytest.fail(f"broker did not exit (port bound: {bound})")
        return proc.returncode, out, err, port
    finally:
        if proc.poll() is None:
            proc.kill()


def _assert_refused(rc, err, port, line):
    assert rc == 2, (rc, err)
    assert err.count(line) == 1, err
    assert "Traceback" not in err, err
    # Nothing else on stderr but the broker's own INFO log lines (the config
    # load is logged before the fold runs).
    others = [ln for ln in err.splitlines() if ln and ln != line]
    assert all(re.match(r"^\d{4}-\d\d-\d\d [\d:,]+ INFO ", ln)
               for ln in others), err
    assert not _connectable(port)


def test_headless_no_terminals_refused_before_bind(tmp_path):
    rc, _, err, port = _run_broker(tmp_path, {}, "--headless",
                                   "--no-terminals")
    _assert_refused(rc, err, port, REFUSAL)


def test_config_role_node_terminals_false_refused_before_bind(tmp_path):
    rc, _, err, port = _run_broker(tmp_path, {"role": "node",
                                              "terminals": False})
    _assert_refused(rc, err, port, REFUSAL)


def test_headless_with_role_hub_refused(tmp_path):
    rc, _, err, port = _run_broker(tmp_path, {}, "--headless",
                                   "--role", "hub")
    _assert_refused(rc, err, port,
                    "error: --headless is the alias of --role node; "
                    "it contradicts --role hub")


def test_one_shots_still_run_on_a_refused_role(tmp_path):
    """Q1 ruling: the fold runs before the one-shots, but only the server path
    exits 2 -- reading the token or auditing recordings binds nothing."""
    refused = {"serve_ui": False, "terminals": False}
    rc, out, err, _ = _run_broker(tmp_path, refused, "--print-token")
    assert rc == 0, err
    assert out.splitlines()[0] == TEST_TOKEN
    assert REFUSAL not in err
    rec = tmp_path / "recs"
    rec.mkdir()
    rc, out, err, _ = _run_broker(tmp_path, refused, "--scan-recordings",
                                  "--recordings-dir", str(rec))
    assert rc == 0, (out, err)
    assert REFUSAL not in err


def test_create_app_refuses_a_node_without_terminals(tmp_path):
    with pytest.raises(ValueError) as exc:
        _app(tmp_path, serve_ui=False, terminals=False)
    assert str(exc.value) == REFUSAL


# ---- /info + boot line -------------------------------------------------------

@pytest.mark.parametrize("cfg,role,terminals", [
    ({}, "hub", True),
    ({"serve_ui": False}, "node", True),
    ({"terminals": False}, "hub", False),
    ({"role": "node"}, "node", True),
])
def test_info_reports_role_and_terminals(tmp_path, cfg, role, terminals):
    app = _app(tmp_path, **cfg)
    _, r = authed(app).get("/info")
    assert r.status == 200
    assert r.json["role"] == role
    assert r.json["terminals"] is terminals
    assert r.json["serve_ui"] is (role == "hub")      # kept beside role


@pytest.mark.parametrize("cfg,line", [
    ({}, "broker role: hub - terminals on"),
    ({"serve_ui": False}, "broker role: node - terminals on"),
    ({"terminals": False}, "broker role: hub - terminals off (launches "
                           "refused)"),
])
def test_boot_line_names_the_role(tmp_path, caplog, cfg, line):
    with caplog.at_level(logging.INFO, logger="webterm.broker.app"):
        _app(tmp_path, **cfg)
    assert line in [r.getMessage() for r in caplog.records]


# ---- A2: the launch gates ----------------------------------------------------

MCP_TOKEN = "roles-mcp-token"


def _gate_app(tmp_path, monkeypatch, **cfg):
    """A broker with MCP on, and a spy in place of the launcher so a launch
    that gets past every gate is recorded instead of spawning a shell."""
    monkeypatch.delenv("WEB_TERMINAL_TOKEN", raising=False)
    monkeypatch.delenv("WEB_TERMINAL_MCP_TOKEN", raising=False)
    base = {"mcp_state_path": str(tmp_path / "webterm_mcp.json"),
            "mcp_enabled": True, "mcp_token": MCP_TOKEN,
            "mcp_default_mode": "off"}
    base.update(cfg)
    app = _app(tmp_path, **base)
    calls = []

    async def _spy_launch(profile, **kw):
        calls.append(profile)
        return 200, {"ok": True, "id": 4242}

    app.ctx.launcher.launch = _spy_launch
    return app, calls


def _mcp_headers(token=MCP_TOKEN):
    return [("Authorization", f"Bearer {token}")]


def test_launch_401_outranks_terminals_disabled(tmp_path, monkeypatch):
    app, calls = _gate_app(tmp_path, monkeypatch, terminals=False)
    # RAW client on purpose: no token.
    _, r = app.test_client.post("/launch", json={})
    assert r.status == 401
    assert r.json["error"] == "auth_required"
    assert calls == []


def test_launch_quiesce_outranks_terminals_disabled(tmp_path, monkeypatch):
    from webterm.broker import app as app_mod
    app, calls = _gate_app(tmp_path, monkeypatch, terminals=False)
    app.ctx.lifecycle = app_mod.LIFECYCLE_QUIESCING
    _, r = authed(app).post("/launch", json={})
    assert r.status == 503
    assert r.json["error"] == "restarting"
    assert calls == []


def test_launch_refused_when_terminals_off(tmp_path, monkeypatch):
    app, calls = _gate_app(tmp_path, monkeypatch, terminals=False)
    _, r = authed(app).post("/launch", json={})
    assert r.status == 403
    assert r.json == {"ok": False, "error": "terminals_disabled"}
    # Before the body parse: a malformed body gets the same answer.
    _, r = authed(app).post("/launch", data="{not json")
    assert r.json["error"] == "terminals_disabled"
    assert calls == []


def test_launch_default_config_unchanged(tmp_path, monkeypatch):
    app, calls = _gate_app(tmp_path, monkeypatch)
    _, r = authed(app).post("/launch", json={})
    assert r.status == 200, r.json
    assert r.json["id"] == 4242
    assert len(calls) == 1


def test_mcp_launch_auth_outranks_terminals_disabled(tmp_path, monkeypatch):
    app, calls = _gate_app(tmp_path, monkeypatch, terminals=False)
    _, r = app.test_client.post("/mcp/launch", json={},
                                headers=_mcp_headers("wrong"))
    assert r.status == 401
    assert r.json["error"] == "auth_required"
    assert calls == []


def test_mcp_launch_terminals_disabled_before_launch_disabled(tmp_path,
                                                              monkeypatch):
    app, calls = _gate_app(tmp_path, monkeypatch, terminals=False,
                           mcp_allow_launch=False)
    _, r = app.test_client.post("/mcp/launch", json={},
                                headers=_mcp_headers())
    assert r.status == 403
    assert r.json["error"] == "terminals_disabled"
    assert calls == []


def test_mcp_launch_disabled_unchanged_when_terminals_on(tmp_path,
                                                         monkeypatch):
    app, calls = _gate_app(tmp_path, monkeypatch, mcp_allow_launch=False)
    _, r = app.test_client.post("/mcp/launch", json={},
                                headers=_mcp_headers())
    assert r.status == 403
    assert r.json["error"] == "launch_disabled"
    assert calls == []


def test_mcp_launch_terminals_off_even_with_allow_launch(tmp_path,
                                                         monkeypatch):
    app, calls = _gate_app(tmp_path, monkeypatch, terminals=False,
                           mcp_allow_launch=True)
    _, r = app.test_client.post("/mcp/launch", json={},
                                headers=_mcp_headers())
    assert r.status == 403
    assert r.json["error"] == "terminals_disabled"
    assert calls == []


@pytest.mark.parametrize("terminals", [True, False])
def test_mcp_info_reports_terminals(tmp_path, monkeypatch, terminals):
    app, _ = _gate_app(tmp_path, monkeypatch, terminals=terminals,
                       mcp_allow_launch=True)
    _, r = app.test_client.get("/mcp/info", headers=_mcp_headers())
    assert r.status == 200
    assert r.json["terminals"] is terminals
    assert r.json["allow_launch"] is True          # unchanged beside it


def test_profile_writes_and_detect_refused_reads_answer(tmp_path,
                                                        monkeypatch):
    app, _ = _gate_app(tmp_path, monkeypatch, terminals=False)
    _, r = authed(app).post("/profiles/config", json={"profiles": {}})
    assert r.status == 403
    assert r.json["error"] == "terminals_disabled"
    _, r = authed(app).get("/profiles/detect")
    assert r.status == 403
    assert r.json["error"] == "terminals_disabled"
    # Reads stay: a hub can still show what this broker offers.
    for path in ("/profiles", "/profiles/config", "/sessions"):
        _, r = authed(app).get(path)
        assert r.status == 200, (path, r.status)
    # And the refusals still sit behind the auth gate (RAW client: no token).
    _, r = app.test_client.get("/profiles/detect")
    assert r.status == 401
    _, r = app.test_client.post("/profiles/config", json={"profiles": {}})
    assert r.status == 401
    assert r.json["error"] == "auth_required"


def test_profile_routes_unchanged_by_default(tmp_path, monkeypatch):
    app, _ = _gate_app(tmp_path, monkeypatch)
    _, r = authed(app).get("/profiles/detect")
    assert r.status == 200 and r.json["ok"] is True


def test_mcptool_names_terminals_disabled():
    import httpx
    from webterm.mcptool import client as mc
    assert "terminals_disabled" in mc._ERROR_MESSAGES
    resp = httpx.Response(
        403, json={"ok": False, "error": "terminals_disabled"},
        request=httpx.Request("POST", "http://broker/mcp/launch"))
    with pytest.raises(mc.BrowserlandError) as exc:
        mc.BrowserlandClient._raise_for_status(None, resp)
    assert exc.value.code == "terminals_disabled"
    assert "hosts no terminals" in str(exc.value)
    assert "Broker error:" not in str(exc.value)


def test_producer_hello_still_registers_on_a_terminals_off_broker(tmp_path):
    """R4: terminals: false refuses NEW launches only. /browserland keeps
    accepting agents -- one launched before the flag must reconnect."""
    import asyncio
    import time
    import urllib.error
    import urllib.request

    import websockets

    cfg_path = tmp_path / "broker_config.json"
    cfg_path.write_text(json.dumps({
        "state_path": str(tmp_path / "webterm_state.json")}),
        encoding="utf-8")
    port = _free_port()
    # --no-terminals on the CLI, so the supervisor's argv replay to its worker
    # is what carries the flag (the worker re-folds it).
    proc = subprocess.Popen(
        [sys.executable, "-m", "webterm.broker", "--config", str(cfg_path),
         "--host", "127.0.0.1", "--port", str(port), "--no-terminals"],
        cwd=str(tmp_path), env=_env(),
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    base = f"http://127.0.0.1:{port}"

    def get(path):
        with urllib.request.urlopen(f"{base}{path}?token={TEST_TOKEN}",
                                    timeout=5) as r:
            return json.loads(r.read())

    try:
        deadline = time.time() + 30
        while True:
            try:
                info = get("/info")
                break
            except OSError:
                pass
            assert proc.poll() is None, f"broker exited: {proc.returncode}"
            assert time.time() < deadline, "broker did not come up"
            time.sleep(0.2)
        assert info["terminals"] is False and info["role"] == "hub"
        req = urllib.request.Request(
            f"{base}/launch?token={TEST_TOKEN}", data=b"{}", method="POST",
            headers={"Content-Type": "application/json"})
        try:
            urllib.request.urlopen(req, timeout=5)
            raise AssertionError("/launch answered 2xx on --no-terminals")
        except urllib.error.HTTPError as exc:
            assert exc.code == 403
            assert json.loads(exc.read())["error"] == "terminals_disabled"

        async def scenario():
            ws = await websockets.connect(
                f"ws://127.0.0.1:{port}/browserland?token={TEST_TOKEN}",
                max_size=None)
            try:
                await ws.send(json.dumps({
                    "type": "hello", "window_id": 553001, "pid": 1,
                    "title": "pre-flag agent", "cols": 80, "rows": 24,
                    "kind": "agent"}))
                loop = asyncio.get_running_loop()
                for _ in range(50):
                    sessions = await loop.run_in_executor(
                        None, get, "/sessions")
                    if any(s["id"] == 553001 for s in sessions):
                        return True
                    await asyncio.sleep(0.1)
                return False
            finally:
                await ws.close()

        assert asyncio.run(scenario()), "hello did not register"
    finally:
        proc.kill()
        proc.wait(timeout=10)


def test_role_node_flag_serves_a_node(tmp_path):
    """--role node, not only the legacy --headless, through the supervisor."""
    import time
    import urllib.request

    cfg_path = tmp_path / "broker_config.json"
    cfg_path.write_text(json.dumps({
        "state_path": str(tmp_path / "webterm_state.json")}),
        encoding="utf-8")
    port = _free_port()
    proc = subprocess.Popen(
        [sys.executable, "-m", "webterm.broker", "--config", str(cfg_path),
         "--host", "127.0.0.1", "--port", str(port), "--role", "node"],
        cwd=str(tmp_path), env=_env(),
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    base = f"http://127.0.0.1:{port}"

    def get(path):
        with urllib.request.urlopen(f"{base}{path}", timeout=5) as r:
            return json.loads(r.read())

    try:
        deadline = time.time() + 30
        while True:
            try:
                info = get(f"/info?token={TEST_TOKEN}")
                break
            except OSError:
                pass
            assert proc.poll() is None, f"broker exited: {proc.returncode}"
            assert time.time() < deadline, "broker did not come up"
            time.sleep(0.2)
        assert info["role"] == "node" and info["serve_ui"] is False
        assert info["terminals"] is True
        assert get("/") == {"ui": False, "role": "node"}
    finally:
        proc.kill()
        proc.wait(timeout=10)


def test_every_launcher_call_sits_in_a_gated_handler():
    """The gate lives at the handlers, so a NEW call to the launcher anywhere
    else would launch on a terminals-off broker. Pin the enclosing handler of
    every app.ctx.launcher.launch( call -- only _launch and _mcp_launch -- and
    that each of those calls _refuse_if_no_terminals."""
    src = (REPO / "webterm" / "broker" / "app.py").read_text(encoding="utf-8")
    handlers = list(re.finditer(r"^    async def (_\w+)\(", src, re.M))
    calls = list(re.finditer(r"app\.ctx\.launcher\.launch\(", src))
    assert calls, "no launcher call found: retarget this test"
    seen = set()
    for m in calls:
        enclosing = [h for h in handlers if h.start() < m.start()][-1]
        seen.add(enclosing.group(1))
    assert seen == {"_launch", "_mcp_launch"}, seen
    for name in seen:
        start = src.index(f"    async def {name}(")
        nxt = [h.start() for h in handlers if h.start() > start]
        body = src[start:nxt[0] if nxt else len(src)]
        assert "_refuse_if_no_terminals(app)" in body, name
