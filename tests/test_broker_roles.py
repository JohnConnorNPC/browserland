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
