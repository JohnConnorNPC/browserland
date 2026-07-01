"""Launch-profile editor: sidecar load/merge, live-swap, and detection (#70).

The mutating and detection cases live here (in-process, isolated tmp state_path)
rather than in the module-scoped subprocess broker of test_broker_e2e.py:
  * detection MUST be monkeypatched (a subprocess broker can't be patched);
  * "sidecar owns across a restart" needs a second create_app on the same dir;
  * REPLACE-semantics edits would otherwise disturb the shared broker's set.

Each create_app needs a UNIQUE Sanic name (Sanic refuses duplicates in one
process) and points state_path at tmp_path, so the profiles/identity/state
sidecars never land in the repo. The e2e file keeps the real-subprocess auth /
shape / non-destructive live-swap smoke.
"""

from __future__ import annotations

import json
import types
from pathlib import Path

import pytest

from webterm.broker import app as brokerapp
from webterm.broker.app import (
    _detect_posix_shells,
    _detect_windows_shells,
    _load_profiles_cfg,
    _validate_profiles_post,
    create_app,
)
from webterm.broker.launcher import Launcher, default_profiles
from webterm.broker.registry import BrokerRegistry

_seq = 0


def _make_app(tmp_path, monkeypatch, token=None):
    global _seq
    _seq += 1
    monkeypatch.delenv("WEB_TERMINAL_TOKEN", raising=False)
    cfg = {"state_path": str(tmp_path / "webterm_state.json")}
    if token:
        cfg["auth_token"] = token
    return create_app(cfg, name=f"webterm-profiles-test-{_seq}")


def _post(app, body, token=None):
    path = "/profiles/config" + (f"?token={token}" if token else "")
    _, r = app.test_client.post(path, data=json.dumps(body).encode(),
                                headers={"Content-Type": "application/json"})
    return r


# ---- Launcher.set_profiles (live-swap unit) ------------------------------

def test_set_profiles_whole_reference_swap():
    lnch = Launcher(BrokerRegistry(), None, broker_port=1, token=None)
    old = lnch.profiles
    new = {"zsh": {"command": ["zsh", "-l"], "title": "zsh", "cwd": None}}
    lnch.set_profiles(new, "zsh")
    assert set(lnch.profiles) == {"zsh"}
    assert lnch.default_profile == "zsh"
    # A brand-new object was bound (never an in-place mutation of the old dict).
    assert lnch.profiles is not old
    assert lnch.profiles is not new           # set_profiles copies its input
    new["injected"] = {"command": ["x"]}
    assert "injected" not in lnch.profiles    # caller can't mutate live set


# ---- _load_profiles_cfg (sidecar-owns-once-written) ----------------------

def test_sidecar_owns_across_reload(tmp_path):
    sidecar = tmp_path / "webterm_profiles.json"
    sidecar.write_text(json.dumps({
        "profiles": {"wsl": {"command": ["wsl.exe", "-d", "Ubuntu"],
                             "title": "Ubuntu", "cwd": None}},
        "default_profile": "wsl"}), encoding="utf-8")
    cfg = _load_profiles_cfg(sidecar, {})
    assert cfg["source"] == "sidecar"
    assert set(cfg["profiles"]) == {"wsl"}
    assert cfg["default_profile"] == "wsl"


def test_corrupt_sidecar_falls_back_to_seed(tmp_path):
    sidecar = tmp_path / "webterm_profiles.json"
    sidecar.write_text("{ not valid json", encoding="utf-8")
    cfg = _load_profiles_cfg(sidecar, {})
    assert cfg["source"] == "config"              # never throws; seed used
    assert cfg["profiles"] == _load_profiles_cfg(tmp_path / "absent", {})["profiles"]


def test_empty_sidecar_falls_back_to_seed(tmp_path):
    # A sidecar with zero salvageable profiles must NOT leave the launcher empty.
    sidecar = tmp_path / "webterm_profiles.json"
    sidecar.write_text(json.dumps({"profiles": {}, "default_profile": ""}),
                       encoding="utf-8")
    cfg = _load_profiles_cfg(sidecar, {})
    assert cfg["source"] == "config"
    assert cfg["profiles"]                          # non-empty seed


def test_seed_from_config_agent(tmp_path):
    agent = {"profiles": {"only": {"command": ["only"]}},
             "default_profile": "only"}
    cfg = _load_profiles_cfg(tmp_path / "absent", {"agent": agent})
    assert set(cfg["profiles"]) == {"only"}
    assert cfg["default_profile"] == "only"


def test_dangling_default_resolved(tmp_path):
    # A sidecar default that isn't a member self-heals to a real member.
    sidecar = tmp_path / "webterm_profiles.json"
    sidecar.write_text(json.dumps({
        "profiles": {"a": {"command": ["a"]}}, "default_profile": "gone"}),
        encoding="utf-8")
    cfg = _load_profiles_cfg(sidecar, {})
    assert cfg["default_profile"] == "a"


# ---- POST /profiles/config: persist + live-swap + delete/rename ----------

def test_post_persists_and_swaps_live(tmp_path, monkeypatch):
    app = _make_app(tmp_path, monkeypatch)
    body = {"profiles": {
        "wsl-ubuntu": {"command": ["wsl.exe", "-d", "Ubuntu", "--", "bash",
                                   "-l"], "title": "Ubuntu (WSL)", "cwd": None},
        "pwsh": {"command": ["pwsh", "-NoLogo"]}},
        "default_profile": "pwsh"}
    r = _post(app, body)
    assert r.status == 200 and r.json["ok"] is True
    assert set(r.json["profiles"]) == {"wsl-ubuntu", "pwsh"}
    assert r.json["source"] == "sidecar" and r.json["default_profile"] == "pwsh"
    # The live launcher swapped WITHOUT a restart: /profiles (names-only)
    # reflects the new set immediately.
    _, names = app.test_client.get("/profiles")
    assert set(names.json["profiles"]) == {"wsl-ubuntu", "pwsh"}
    assert names.json["default"] == "pwsh"
    # ...and it was persisted to the sidecar beside the state store.
    persisted = json.loads((tmp_path / "webterm_profiles.json").read_text())
    assert set(persisted["profiles"]) == {"wsl-ubuntu", "pwsh"}


def test_post_persists_color_and_exposes_map(tmp_path, monkeypatch):
    # #115: a per-profile color round-trips through POST -> public view, the
    # sidecar, the live launcher, and the names-only /profiles color side-map.
    app = _make_app(tmp_path, monkeypatch)
    body = {"profiles": {
        "red": {"command": ["bash", "-l"], "color": "#ff0000"},
        "plain": {"command": ["sh"]}},          # no color -> None
        "default_profile": "red"}
    r = _post(app, body)
    assert r.status == 200 and r.json["ok"] is True
    # Public view (the Control-Panel editor reads this) reflects both.
    assert r.json["profiles"]["red"]["color"] == "#ff0000"
    assert r.json["profiles"]["plain"]["color"] is None
    # Live launcher set carries it (seeds the launch/summary path).
    assert app.ctx.launcher.profiles["red"]["color"] == "#ff0000"
    assert app.ctx.launcher.profiles["plain"].get("color") is None
    # Persisted to the sidecar beside the state store.
    persisted = json.loads((tmp_path / "webterm_profiles.json").read_text())
    assert persisted["profiles"]["red"]["color"] == "#ff0000"
    # Names-only /profiles: names stay an array, colors ride the side-map (only
    # profiles that set one appear).
    _, names = app.test_client.get("/profiles")
    assert set(names.json["profiles"]) == {"red", "plain"}
    assert names.json["colors"] == {"red": "#ff0000"}


def test_post_clears_color_with_empty_string(tmp_path, monkeypatch):
    # #115: an empty/absent color means "no default" (like cwd), and re-POSTing
    # without it clears a previously-set color (REPLACE semantics).
    app = _make_app(tmp_path, monkeypatch)
    _post(app, {"profiles": {"a": {"command": ["a"], "color": "#010203"}},
                "default_profile": "a"})
    r = _post(app, {"profiles": {"a": {"command": ["a"], "color": ""}},
                    "default_profile": "a"})
    assert r.status == 200 and r.json["profiles"]["a"]["color"] is None
    _, names = app.test_client.get("/profiles")
    assert names.json["colors"] == {}            # no colored profiles remain


def test_heal_drops_bad_color_keeps_entry(tmp_path):
    # #115 self-heal: a hand-edited/reloaded sidecar with a junk color keeps the
    # profile but drops only the color (never carries junk into the seed map).
    sidecar = tmp_path / "webterm_profiles.json"
    sidecar.write_text(json.dumps({
        "profiles": {"a": {"command": ["a"], "color": "not-a-color"},
                     "b": {"command": ["b"], "color": "#00ff00"}},
        "default_profile": "a"}), encoding="utf-8")
    cfg = _load_profiles_cfg(sidecar, {})
    assert set(cfg["profiles"]) == {"a", "b"}        # both entries survive
    assert cfg["profiles"]["a"]["color"] is None     # junk dropped
    assert cfg["profiles"]["b"]["color"] == "#00ff00"


def test_post_delete_via_replace(tmp_path, monkeypatch):
    app = _make_app(tmp_path, monkeypatch)
    _post(app, {"profiles": {"a": {"command": ["a"]},
                             "b": {"command": ["b"]}}, "default_profile": "a"})
    # Deleting "a" (the default) via replace re-homes the default to a member.
    r = _post(app, {"profiles": {"b": {"command": ["b"]}},
                    "default_profile": ""})
    assert r.status == 200 and set(r.json["profiles"]) == {"b"}
    assert r.json["default_profile"] == "b"
    _, names = app.test_client.get("/profiles")
    assert names.json["profiles"] == ["b"]


def test_post_rename_via_replace(tmp_path, monkeypatch):
    app = _make_app(tmp_path, monkeypatch)
    _post(app, {"profiles": {"old": {"command": ["sh"]}},
                "default_profile": "old"})
    r = _post(app, {"profiles": {"new": {"command": ["sh"]}},
                    "default_profile": "new"})
    assert r.status == 200 and set(r.json["profiles"]) == {"new"}
    _, names = app.test_client.get("/profiles")
    assert names.json["profiles"] == ["new"]


def test_post_auth_gate_with_token(tmp_path, monkeypatch):
    app = _make_app(tmp_path, monkeypatch, token="sekrit")
    _, r = app.test_client.get("/profiles/config")     # loopback but token set
    assert r.status == 401
    _, r = app.test_client.get("/profiles/config?token=sekrit")
    assert r.status == 200 and r.json["ok"] is True
    # A rejected POST changes nothing.
    r = _post(app, {"profiles": {"a/b": {"command": ["x"]}}}, token="sekrit")
    assert r.status == 400 and r.json["error"] == "bad_name"


def test_post_rejects_too_large(tmp_path, monkeypatch):
    app = _make_app(tmp_path, monkeypatch)
    big = "x" * (brokerapp.MAX_PROFILES_BYTES + 1)
    _, r = app.test_client.post("/profiles/config", data=big.encode(),
                                headers={"Content-Type": "application/json"})
    assert r.status == 413 and r.json["error"] == "too_large"


def test_mcp_profiles_stays_names_only_after_edit(tmp_path, monkeypatch):
    # The profiles-only invariant: even after the browser realm defines a new
    # profile (now optionally with a #115 color), the MCP realm still only ever
    # sees NAMES (never commands, never colors).
    app = _make_app(tmp_path, monkeypatch)
    # Enable the MCP realm with a token so /mcp/profiles is reachable at all.
    app.ctx.mcp_cfg = dict(app.ctx.mcp_cfg, enabled=True, token="mtok")
    _post(app, {"profiles": {"secret": {"command": ["donotshow", "--flag"],
                                        "color": "#abcdef"}},
                "default_profile": "secret"})
    _, r = app.test_client.get("/mcp/profiles?token=mtok")
    assert r.status == 200
    assert r.json["profiles"] == ["secret"]      # names only, never commands
    blob = json.dumps(r.json)
    assert "command" not in blob and "donotshow" not in blob
    assert "color" not in blob and "#abcdef" not in blob   # #115: no color leak
    # The BROWSER-realm names-only /profiles DOES carry the additive color map,
    # while its "profiles" stays a plain name array (the seed path reads both).
    _, pr = app.test_client.get("/profiles")
    assert pr.json["profiles"] == ["secret"]
    assert pr.json["colors"] == {"secret": "#abcdef"}
    assert "command" not in json.dumps(pr.json)  # still no command leak


# ---- validation slugs (fast, exhaustive; complements the e2e sampling) ----

@pytest.mark.parametrize("body,err", [
    ({"profiles": []}, "bad_profiles"),
    ({"profiles": {}}, "no_profiles"),
    ({"profiles": {"a\nb": {"command": ["x"]}}}, "bad_name"),
    ({"profiles": {"a": {"command": ["x\ty"]}}}, "bad_command"),
    ({"profiles": {"a": {"command": [""]}}}, "bad_command"),
    ({"profiles": {"a": {"command": [1]}}}, "bad_command"),
    ({"profiles": {"a": {"command": ["x"], "title": 5}}}, "bad_title"),
    ({"profiles": {"a": {"command": ["x"], "cwd": 5}}}, "bad_cwd"),
    # #115: strict #rrggbb — a name, a 3-digit hex, an over-long hex, a bad hex
    # digit, a trailing newline, and a non-string all reject (never coerce).
    ({"profiles": {"a": {"command": ["x"], "color": "red"}}}, "bad_color"),
    ({"profiles": {"a": {"command": ["x"], "color": "#fff"}}}, "bad_color"),
    ({"profiles": {"a": {"command": ["x"], "color": "#1234567"}}}, "bad_color"),
    ({"profiles": {"a": {"command": ["x"], "color": "#12345g"}}}, "bad_color"),
    ({"profiles": {"a": {"command": ["x"], "color": "#123456\n"}}}, "bad_color"),
    ({"profiles": {"a": {"command": ["x"], "color": 5}}}, "bad_color"),
    ({"profiles": {"a": {"command": ["x"]}}, "default_profile": "z"},
     "default_not_member"),
])
def test_validate_rejections(body, err):
    result, got = _validate_profiles_post(body)
    assert result is None and got == err


def test_validate_empty_default_resolves_to_first():
    result, err = _validate_profiles_post(
        {"profiles": {"a": {"command": ["a"]}, "b": {"command": ["b"]}},
         "default_profile": ""})
    assert err is None and result["default_profile"] == "a"


# ---- detection (monkeypatched — deterministic on any OS) ------------------

def test_detect_windows_parses_wsl_output(monkeypatch):
    monkeypatch.setattr(brokerapp, "_wsl_exe", lambda: r"C:\Windows\System32\wsl.exe")
    # UTF-16-LE with a BOM, CRLF, a blank line, and the localized no-distro
    # sentence (spaces) that MUST be dropped.
    raw = ("﻿Ubuntu\r\nkali-linux\r\n\r\n"
           "Windows Subsystem for Linux has no installed distributions.\r\n")
    monkeypatch.setattr(brokerapp.subprocess, "run",
                        lambda *a, **k: types.SimpleNamespace(
                            returncode=0, stdout=raw.encode("utf-16-le")))
    out = _detect_windows_shells()
    assert [s["name"] for s in out] == ["Ubuntu", "kali-linux"]
    assert out[0]["command"] == ["wsl.exe", "-d", "Ubuntu", "--cd", "~", "--",
                                 "bash", "-l"]
    assert out[0]["title"] == "Ubuntu (WSL)" and out[0]["exists"] is True


def test_detect_windows_no_wsl(monkeypatch):
    monkeypatch.setattr(brokerapp, "_wsl_exe", lambda: None)
    assert _detect_windows_shells() == []


def test_detect_windows_never_raises_on_error(monkeypatch):
    monkeypatch.setattr(brokerapp, "_wsl_exe", lambda: "wsl.exe")

    def boom(*a, **k):
        raise OSError("nope")
    monkeypatch.setattr(brokerapp.subprocess, "run", boom)
    assert _detect_windows_shells() == []


def test_detect_posix_allowlist(monkeypatch):
    which = {"bash": "/bin/bash", "zsh": "/usr/bin/zsh"}
    monkeypatch.setattr(brokerapp.shutil, "which",
                        lambda name: which.get(name))
    out = _detect_posix_shells()
    names = {s["name"] for s in out}
    assert {"bash", "zsh"} <= names          # /etc/shells may add more, never less
    assert "fish" not in names               # not on PATH -> not suggested
    for s in out:
        assert s["command"] == [s["name"], "-l"] and s["exists"] is True


def test_detect_endpoint_wired(tmp_path, monkeypatch):
    app = _make_app(tmp_path, monkeypatch)
    fake = [{"name": "Ubuntu", "title": "Ubuntu (WSL)",
             "command": ["wsl.exe", "-d", "Ubuntu"], "exists": True}]
    monkeypatch.setattr(brokerapp, "_detect_profile_suggestions", lambda: fake)
    _, r = app.test_client.get("/profiles/detect")
    assert r.status == 200 and r.json["ok"] is True
    assert r.json["suggestions"] == fake
