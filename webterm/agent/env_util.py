"""Fresh-environment builder for spawned shells/agents (todo task 17).

On Windows a process inherits the PATH frozen at login; a program installed
afterward (its installer only edits the registry's Machine/User ``Path``) is
invisible to already-running shells — and to the long-lived webterm broker and
the agents it spawns — until the next logout. ``spawn_env()`` re-reads PATH
from the registry (``HKLM\\...\\Session Manager\\Environment`` +
``HKCU\\Environment``), expands and merges it the way Windows itself does
(Machine then User), and returns a full environment dict with PATH refreshed —
so a shell we launch finds a just-installed ``grok``/``claude`` without a
re-login.

The merge is append-only: every entry already on the inherited PATH is kept
(so a venv the launcher prepended, or anything the running session added,
survives) — we only ever ADD the registry entries that were missing. On
non-Windows there is no Machine/User PATH split, so ``spawn_env()`` returns a
copy of the base environment unchanged.
"""

from __future__ import annotations

import os
from typing import Dict, List, Optional

# Where Windows keeps the persistent PATH. The Machine value lives under
# HKLM; the per-user value under HKCU. Both can be REG_EXPAND_SZ (carry
# %SystemRoot%-style refs) and must be expanded before use.
_HKLM_ENV = r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment"
_HKCU_ENV = r"Environment"


def _expand(value: str) -> str:
    """Expand %VAR% references in a REG_EXPAND_SZ value against the current
    process environment (SystemRoot/ProgramFiles etc. are stable machine
    values present in any process). Falls back to os.path.expandvars."""
    if not value:
        return ""
    try:
        import ctypes

        size = ctypes.windll.kernel32.ExpandEnvironmentStringsW(value, None, 0)
        if size <= 0:
            return os.path.expandvars(value)
        buf = ctypes.create_unicode_buffer(size)
        ctypes.windll.kernel32.ExpandEnvironmentStringsW(value, buf, size)
        return buf.value or os.path.expandvars(value)
    except Exception:
        return os.path.expandvars(value)


def _read_path(root, sub) -> str:
    """The ``Path`` value under ``root\\sub``, expanded, or "" if absent."""
    try:
        import winreg
    except Exception:
        return ""
    try:
        with winreg.OpenKey(root, sub) as key:
            try:
                val, typ = winreg.QueryValueEx(key, "Path")
            except FileNotFoundError:
                return ""
            if not isinstance(val, str):
                return ""
            return _expand(val) if typ == winreg.REG_EXPAND_SZ else val
    except OSError:
        return ""


def _merge_paths(*chunks: str) -> List[str]:
    """Flatten PATH chunks into one de-duplicated list, first occurrence wins.
    Dedup is case- and separator-insensitive (Windows paths) so ``C:\\foo`` and
    ``C:\\foo\\`` collapse, but the original spelling of the winner is kept."""
    out: List[str] = []
    seen = set()
    for chunk in chunks:
        for part in (chunk or "").split(os.pathsep):
            p = part.strip().strip('"')
            if not p:
                continue
            key = os.path.normcase(os.path.normpath(p))
            if key in seen:
                continue
            seen.add(key)
            out.append(p)
    return out


def registry_path(base_path: Optional[str] = None) -> Optional[str]:
    """The merged Machine+User PATH from the registry, unioned (append-only)
    with ``base_path`` (defaults to the inherited PATH). Returns None
    off-Windows or when the registry can't be read, so callers keep the
    inherited PATH unchanged."""
    if os.name != "nt":
        return None
    try:
        import winreg
    except Exception:
        return None
    machine = _read_path(winreg.HKEY_LOCAL_MACHINE, _HKLM_ENV)
    user = _read_path(winreg.HKEY_CURRENT_USER, _HKCU_ENV)
    if not machine and not user:
        return None
    inherited = os.environ.get("PATH", "") if base_path is None else base_path
    # Machine first, then User (Windows' own ordering), then any inherited
    # entry the registry didn't list — never DROP an existing PATH dir.
    merged = _merge_paths(machine, user, inherited)
    return os.pathsep.join(merged) if merged else None


def spawn_env(base: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    """A full environment dict for a freshly-spawned child with PATH refreshed
    from the registry (Windows) or an unchanged copy of ``base``/os.environ
    (elsewhere). Never raises: any failure yields the base environment."""
    env = dict(base if base is not None else os.environ)
    try:
        fresh = registry_path(env.get("PATH"))
    except Exception:
        fresh = None
    if fresh:
        # Windows env names are case-insensitive but a dict is not: drop any
        # stale-cased 'Path'/'path' so the refreshed 'PATH' is the only one in
        # the child's environment block.
        for k in [k for k in list(env) if k.upper() == "PATH"]:
            del env[k]
        env["PATH"] = fresh
    return env
