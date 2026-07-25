"""Foreground-agent classifier.

Given a process's executable name and argv, decide whether it *is* one of the
agents we care about (``claude`` / ``grok`` / ``codex`` / ``opencode`` /
``hermes``). Used by the PTY backends' ``foreground_command()`` to label the
live session.

Deliberately strict — we match the executable name, ``argv[0]``, and (for
interpreter wrappers like node/python/pwsh) the *first* script-like argument
only. We do NOT scan arbitrary arguments, so ``rg codex`` or ``cat claude.md``
never trip a false positive.

Detection matches **basenames** (``claude.exe`` -> ``claude``); the configurable
launch *paths* in the front-end Control Panel affect launching, not detection.

Last-resort fallback: some agents ship a generically-named launcher (the
reported case is grok installed as ``…\.grok\bin\agent.exe`` — basename
``agent`` matches nothing). When every name/argv rule misses we look for a
known per-vendor install directory (``.grok`` / ``.claude`` / ``.codex`` /
``.opencode``) in the *executable's own path* (or argv[0]). That stays strict:
it only ever inspects the program being run, never arbitrary arguments, so
``rg codex`` is still safe.

``hermes`` is deliberately NOT in that map (#156): no ``.hermes`` install layout
was found to attribute, and the map exists to rescue a vendor's *generically*
named launcher — a plainly named ``hermes`` is already covered by the name rule.
Add an entry only against a real install, never speculatively.

Known caveat for ``hermes``: the name is not exclusive — Meta's React Native JS
engine also ships a ``hermes`` binary — so a build running it is labelled as a
foreground agent. Accepted: the consequences are attribution-only (a taskbar
chip, and ``cwd()`` preferring that process's dir), and ``hermes`` is NOT in the
browser's BRACKET_GAP_AGENTS, so no paste/input behaviour rides the label.
"""

from __future__ import annotations

import os
from typing import List, Optional, Sequence

# DRIFT: duplicated as ``_AGENTS`` in ``webterm/broker/registry.py`` (the broker
# side that WHITELISTS these labels off the wire) — keep both in step; a label
# missing there collapses to "" no matter what this classifier returns.
# ``tests/test_registry_agent.py::test_agent_whitelists_do_not_drift`` enforces it.
_AGENTS = ("claude", "grok", "codex", "opencode", "hermes")
_INTERP = ("node", "node.exe", "python", "python.exe", "python3",
           "pwsh", "pwsh.exe", "powershell", "powershell.exe")
_STRIP = (".exe", ".cmd", ".ps1", ".bat", ".js", ".mjs", ".cjs", ".py")
# Per-vendor install dirs -> agent. Matched against path components of the
# executable, so a vendor's generically-named launcher (e.g. grok's
# ``agent.exe`` under ``.grok\bin``) is still attributed correctly.
_VENDOR_DIRS = {
    ".grok": "grok",
    ".claude": "claude",
    ".codex": "codex",
    ".opencode": "opencode",
}


def _safe_exe(proc) -> Optional[str]:
    """``proc.exe()`` or None — psutil raises AccessDenied/ZombieProcess for it
    often. A best-effort hint for ``classify_proc``'s exe/vendor-dir rules; the
    PTY backends pass it when walking the session's process tree."""
    try:
        return proc.exe()
    except Exception:
        return None


def _norm(token: str) -> str:
    """Basename, lowercased, with a single known launcher extension stripped."""
    b = os.path.basename(str(token)).lower()
    for ext in _STRIP:
        if b.endswith(ext):
            b = b[:-len(ext)]
            break
    return b


def _vendor_from_path(path: Optional[str]) -> Optional[str]:
    """If a known per-vendor dir appears anywhere in ``path``'s components,
    return that agent, else None. Slash-agnostic and case-insensitive so it
    works for Windows and POSIX paths alike."""
    if not path:
        return None
    norm = str(path).replace("\\", "/").lower()
    for part in norm.split("/"):
        agent = _VENDOR_DIRS.get(part)
        if agent:
            return agent
    return None


def classify_proc(name: str, cmdline: Sequence[str],
                  exe: Optional[str] = None) -> Optional[str]:
    """Return 'claude' | 'grok' | 'codex' | 'opencode' | 'hermes' if this process
    is that agent, else None. ``name`` is the process executable name; ``cmdline`` is its
    argv; ``exe`` is the process's full executable path (optional, best-effort —
    callers pass ``proc.exe()`` when available)."""
    # 1) executable name itself (claude.exe, codex)
    if _norm(name) in _AGENTS:
        return _norm(name)
    cmd: List[str] = list(cmdline or [])
    # 2) argv[0] basename
    if cmd and _norm(cmd[0]) in _AGENTS:
        return _norm(cmd[0])
    # 3) interpreter wrappers: first script-like (non-option) arg only
    if cmd:
        interp = {_norm(x) for x in _INTERP}
        if _norm(cmd[0]) in interp:
            for arg in cmd[1:]:
                if arg.startswith("-"):
                    continue
                if _norm(arg) in _AGENTS:
                    return _norm(arg)
                break  # only the first script arg
    # 4) last resort: a known vendor install dir in the executable's own path
    # (or argv[0]). Inspects only the program being run -> stays strict.
    return (_vendor_from_path(exe)
            or _vendor_from_path(cmd[0] if cmd else None))
