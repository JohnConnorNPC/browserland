"""Stable broker identity + /info endpoint (#64).

The same broker reached through several URLs (127.0.0.1 / localhost / Tailscale
100.x) was getting added as multiple browser-local host records, spawning
duplicate taskbar chips and making Terminate silently fail against a stale twin.
``/info`` exposes a stable, non-secret ``broker_id`` so the UI can detect "same
broker, different URL" and gate the terminate fallback. These cover the
persisted-id contract and the (mandatory, #142) token gate.

The route tests use the in-process Sanic test client (each create_app needs a
UNIQUE name — Sanic refuses two apps sharing a name in one process) and point
state_path at a tmp dir so the identity file never lands in the repo.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from .auth_helpers import TEST_TOKEN, authed
from webterm.broker.app import _load_or_create_broker_id, create_app

REPO = Path(__file__).resolve().parents[1]

IDENTITY = "webterm_identity.json"


# ---- _load_or_create_broker_id (persisted-id contract) -------------------

def test_broker_id_stable_same_dir(tmp_path):
    p = tmp_path / IDENTITY
    first = _load_or_create_broker_id(p)
    second = _load_or_create_broker_id(p)
    assert isinstance(first, str) and first
    assert first == second                         # immutable across "restarts"
    assert json.loads(p.read_text())["broker_id"] == first   # persisted on disk


def test_broker_id_differs_across_dirs(tmp_path):
    d1, d2 = tmp_path / "one", tmp_path / "two"
    d1.mkdir()
    d2.mkdir()
    assert _load_or_create_broker_id(d1 / IDENTITY) != \
        _load_or_create_broker_id(d2 / IDENTITY)


def test_broker_id_self_heals_corrupt_file(tmp_path):
    # A hand-edited / truncated identity file must re-mint, never break startup.
    p = tmp_path / IDENTITY
    p.write_text("{ not valid json")
    minted = _load_or_create_broker_id(p)
    assert isinstance(minted, str) and minted
    assert json.loads(p.read_text())["broker_id"] == minted   # rewritten clean
    assert _load_or_create_broker_id(p) == minted             # now stable


# ---- /info route gating + CORS -------------------------------------------

_app_seq = 0


def _make_app(tmp_path, monkeypatch, token=None):
    """Build a broker app with state_path in tmp_path (identity file lands there,
    not the repo) and a UNIQUE Sanic name. Env token would override config, so
    clear it and set auth_token explicitly when a token is wanted."""
    global _app_seq
    _app_seq += 1
    monkeypatch.delenv("WEB_TERMINAL_TOKEN", raising=False)
    cfg = {"state_path": str(tmp_path / "webterm_state.json")}
    cfg["auth_token"] = token or TEST_TOKEN
    return create_app(cfg, name=f"webterm-info-test-{_app_seq}")


def test_info_with_token_returns_id_and_version(tmp_path, monkeypatch):
    # Was test_info_loopback_ok_no_token: loopback used to pass the gate
    # unauthenticated. Since #142 there is no loopback exemption anywhere, so
    # the token is what gets us the broker_id + version.
    app = _make_app(tmp_path, monkeypatch, token=None)
    _, response = authed(app).get("/info")
    assert response.status == 200
    body = response.json
    assert body["ok"] is True
    assert isinstance(body["broker_id"], str) and body["broker_id"]
    assert body["broker_id"] == app.ctx.broker_id
    assert body["version"] == app.ctx.version


def test_info_reports_mods_enabled_default_true(tmp_path, monkeypatch):
    # #71: the mod-system master switch defaults ON and is surfaced via /info so
    # the frontend loader can gate at runtime (fail-open / default-on).
    app = _make_app(tmp_path, monkeypatch, token=None)
    assert app.ctx.mods_enabled is True
    _, response = authed(app).get("/info")
    assert response.status == 200
    assert response.json["mods_enabled"] is True


def test_info_reports_mods_enabled_false_when_configured(tmp_path, monkeypatch):
    global _app_seq
    _app_seq += 1
    monkeypatch.delenv("WEB_TERMINAL_TOKEN", raising=False)
    cfg = {"state_path": str(tmp_path / "webterm_state.json"),
           "auth_token": TEST_TOKEN,
           "mods_enabled": False}
    app = create_app(cfg, name=f"webterm-info-test-{_app_seq}")
    assert app.ctx.mods_enabled is False
    _, response = authed(app).get("/info")
    assert response.status == 200
    assert response.json["mods_enabled"] is False


# ---- headless mode / serve_ui (#87) --------------------------------------

def test_serve_ui_default_true_serves_page_and_help(tmp_path, monkeypatch):
    # Default (serve_ui unset): UI mode. /info reports serve_ui true; the desktop
    # page and the help corpus are both served, exactly as before #87.
    app = _make_app(tmp_path, monkeypatch, token=None)
    assert app.ctx.serve_ui is True
    _, response = authed(app).get("/info")
    assert response.status == 200
    assert response.json["serve_ui"] is True
    _, page = authed(app).get("/")
    assert page.status == 200
    assert "<title>Browserland</title>" in page.body.decode("utf-8")
    _, corpus = authed(app).get("/help-corpus.json")
    assert corpus.status == 200


def test_headless_serves_api_but_not_page_or_help(tmp_path, monkeypatch):
    # serve_ui False: headless. GET / is a self-describing 200 {"ui": false},
    # /help-corpus.json is unregistered (404), but the JSON API still answers.
    global _app_seq
    _app_seq += 1
    monkeypatch.delenv("WEB_TERMINAL_TOKEN", raising=False)
    cfg = {"state_path": str(tmp_path / "webterm_state.json"),
           "auth_token": TEST_TOKEN,
           "serve_ui": False}
    app = create_app(cfg, name=f"webterm-info-test-{_app_seq}")
    assert app.ctx.serve_ui is False
    _, response = authed(app).get("/info")
    assert response.status == 200
    assert response.json["serve_ui"] is False
    _, page = authed(app).get("/")
    assert page.status == 200
    assert page.json == {"ui": False}            # self-describing, not the page
    _, corpus = authed(app).get("/help-corpus.json")
    assert corpus.status == 404                  # route not registered
    _, sessions = authed(app).get("/sessions")
    assert sessions.status == 200                # JSON/WS API unaffected
    assert isinstance(sessions.json, list)


def _create_app_in_subprocess(serve_ui: bool, tmp_path) -> str:
    """Run create_app(serve_ui=...) in a FRESH interpreter and report which of
    webterm.broker.{ui,help_corpus} ended up in sys.modules. Must be a clean
    process: other tests import those modules directly and pollute sys.modules,
    so an in-process check could not prove the gate skipped the assembly.
    cwd=tmp_path keeps state/identity files out of the repo; PYTHONPATH=REPO
    lets the broker import and (in UI mode) read its packaged assets."""
    # Importing __main__ too (its top level pulls in app) catches a regression
    # where someone re-adds a top-level UI import on the actual CLI entrypoint —
    # importing as a module doesn't trip the `if __name__ == "__main__"` guard.
    code = (
        "import sys\n"
        "import webterm.broker.__main__  # noqa: F401 (CLI import path)\n"
        "from webterm.broker.app import create_app\n"
        # Explicit auth_token so the child never mints a token sidecar.
        f"create_app({{'serve_ui': {serve_ui!r}, "
        f"'auth_token': 'subproc-token'}}, name='h')\n"
        "print('ui' if 'webterm.broker.ui' in sys.modules else '-',\n"
        "      'help' if 'webterm.broker.help_corpus' in sys.modules else '-')\n"
    )
    env = dict(os.environ)
    env["PYTHONPATH"] = str(REPO) + os.pathsep + env.get("PYTHONPATH", "")
    out = subprocess.run([sys.executable, "-c", code], cwd=str(tmp_path),
                         env=env, capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    return out.stdout.strip()


def test_headless_skips_ui_and_help_assembly(tmp_path):
    # The core invariant (#87): headless never imports — and so never assembles —
    # webterm.broker.ui (INDEX_HTML) or webterm.broker.help_corpus (HELP_CORPUS).
    # Guards against someone re-adding a top-level import, which every other test
    # would still pass while silently defeating the feature.
    assert _create_app_in_subprocess(False, tmp_path) == "- -"


def test_ui_mode_imports_ui_and_help(tmp_path):
    # The inverse: UI mode DOES assemble both at create_app time (loud-at-startup).
    assert _create_app_in_subprocess(True, tmp_path) == "ui help"


def test_info_requires_token_when_configured(tmp_path, monkeypatch):
    app = _make_app(tmp_path, monkeypatch, token="sekrit")
    # No / wrong token -> 401. RAW client on purpose: authed() would
    # append the very token this test needs to be missing.
    _, response = app.test_client.get("/info")
    assert response.status == 401
    _, response = app.test_client.get("/info?token=nope")
    assert response.status == 401
    # Correct token (the path appendHostToken uses) -> 200 with the id.
    _, response = authed(app).get("/info?token=sekrit")
    assert response.status == 200
    assert response.json["broker_id"] == app.ctx.broker_id


def test_info_options_preflight_cors(tmp_path, monkeypatch):
    # Even with a token configured, the preflight is answered with ACAO:* (the
    # cross-origin add-time probe depends on it).
    app = _make_app(tmp_path, monkeypatch, token="sekrit")
    _, response = authed(app).options("/info")
    assert response.status == 204
    assert response.headers.get("Access-Control-Allow-Origin") == "*"
    assert response.headers.get("Access-Control-Allow-Methods") == \
        "GET, POST, PUT, OPTIONS"
