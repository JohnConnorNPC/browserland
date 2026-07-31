"""Unit tests for the fresh-PATH builder (todo task 17)."""

from __future__ import annotations

import os

from webterm.agent import env_util


def test_merge_paths_dedupes_first_wins():
    sep = os.pathsep
    a = sep.join(["/x", "/y"])
    b = sep.join(["/y", "/z", ""])      # blank + duplicate /y
    merged = env_util._merge_paths(a, b)
    assert merged == ["/x", "/y", "/z"]


def test_merge_paths_strips_quotes_and_blanks():
    sep = os.pathsep
    merged = env_util._merge_paths(sep.join(['"/quoted"', "", "  ", "/real"]))
    assert merged == ["/quoted", "/real"]


def test_spawn_env_returns_full_environment():
    env = env_util.spawn_env()
    assert isinstance(env, dict)
    # PATH (any case) is present and it's a copy, not the live mapping.
    assert any(k.upper() == "PATH" for k in env)
    assert env is not os.environ


def test_spawn_env_accepts_base():
    base = {"FOO": "bar", "PATH": os.environ.get("PATH", "")}
    env = env_util.spawn_env(base)
    assert env["FOO"] == "bar"


def test_spawn_env_strips_supervisor_vars():
    """GH#183: the supervisor authorizes worker restarts via these three
    vars; a spawned agent (and its shell) must never see them."""
    base = {
        "BROWSERLAND_SUPERVISOR_PID": "1234",
        "BROWSERLAND_SUPERVISOR_NONCE": "secret-nonce",
        "BROWSERLAND_RUN_DIR": "/run/dir",
        "PATH": os.environ.get("PATH", ""),
    }
    env = env_util.spawn_env(base)
    for name in env_util._SUPERVISOR_ENV_VARS:
        assert name not in env


def test_spawn_env_strips_supervisor_vars_any_case():
    """Windows env names are case-insensitive; a mixed/lower spelling must
    be scrubbed just as reliably as the canonical upper-case one."""
    base = {
        "Browserland_Supervisor_Pid": "1234",
        "browserland_supervisor_nonce": "secret-nonce",
        "BROWSERLAND_RUN_dir": "/run/dir",
        "PATH": os.environ.get("PATH", ""),
    }
    env = env_util.spawn_env(base)
    assert not any(k.upper() in env_util._SUPERVISOR_ENV_VARS for k in env)


def test_spawn_env_preserves_unrelated_vars():
    base = {
        "FOO": "bar",
        "BROWSERLAND_SUPERVISOR_PID": "1234",
        "PATH": os.environ.get("PATH", ""),
    }
    env = env_util.spawn_env(base)
    assert env["FOO"] == "bar"
    assert "BROWSERLAND_SUPERVISOR_PID" not in env


def test_spawn_env_odd_environment_does_not_raise():
    # Empty base, and a base with only a supervisor var and no PATH at all —
    # spawn_env() must never raise regardless of platform.
    env_empty = env_util.spawn_env({})
    assert isinstance(env_empty, dict)
    env = env_util.spawn_env({"BROWSERLAND_SUPERVISOR_NONCE": "x"})
    assert "BROWSERLAND_SUPERVISOR_NONCE" not in env


if os.name == "nt":

    def test_registry_path_is_superset_of_inherited():
        """Append-only invariant: every dir already on the inherited PATH must
        survive the registry merge (we never DROP a session path)."""
        fresh = env_util.registry_path()
        assert fresh is not None and fresh
        fresh_keys = {os.path.normcase(os.path.normpath(p))
                      for p in fresh.split(os.pathsep) if p.strip()}
        for p in os.environ.get("PATH", "").split(os.pathsep):
            if p.strip():
                key = os.path.normcase(os.path.normpath(p.strip().strip('"')))
                assert key in fresh_keys

    def test_spawn_env_single_path_key():
        """No stale-cased duplicate 'Path'/'PATH' in the child env block."""
        env = env_util.spawn_env()
        path_keys = [k for k in env if k.upper() == "PATH"]
        assert path_keys == ["PATH"]

else:

    def test_registry_path_none_off_windows():
        assert env_util.registry_path() is None

    def test_spawn_env_passthrough_off_windows():
        env = env_util.spawn_env()
        assert env.get("PATH") == os.environ.get("PATH")
