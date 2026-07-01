"""Profiles-only agent launcher behind ``POST /launch``.

Clients pick a named profile; they can never supply a command, cwd, or env
(that would be RCE-by-design). The broker spawns a detached
``python -m webterm.agent`` pointed back at its own loopback /browserland
endpoint and waits for the hello to land in the registry.

Windows detached-spawn flags carried over from an earlier broker's
detached-spawn helper: DETACHED_PROCESS + CREATE_NEW_PROCESS_GROUP
alone is not enough when the parent sits in a job object that kills its
members on parent exit; CREATE_BREAKAWAY_FROM_JOB removes the child from
the job so agents survive a broker restart (their reconnect loop reattaches).
"""

from __future__ import annotations

import asyncio
import logging
import os
import secrets
import subprocess
import sys
from typing import Any, Dict, List, Optional, Tuple

from ..agent.config import BROKER_URL_ENV
from ..agent.env_util import spawn_env
from .auth import TOKEN_ENV
from .registry import BrokerRegistry

LOGGER = logging.getLogger(__name__)

MAX_PENDING = 8
REGISTER_TIMEOUT = 10.0
_POLL_INTERVAL = 0.25

# Win32: exclude the child from the parent's job object (see module doc).
_CREATE_BREAKAWAY_FROM_JOB = 0x01000000


def default_profiles() -> Dict[str, Any]:
    if os.name == "nt":
        return {
            "default_profile": "cmd",
            "profiles": {
                "cmd": {"command": [os.environ.get("COMSPEC", "cmd.exe")],
                        "title": "cmd"},
                "powershell": {"command": ["powershell.exe", "-NoLogo"],
                               "title": "powershell"},
            },
        }
    return {
        "default_profile": "bash",
        "profiles": {
            "bash": {"command": ["bash", "-l"], "title": "bash"},
            "sh": {"command": ["sh"], "title": "sh"},
        },
    }


class LaunchError(Exception):
    def __init__(self, status: int, error: str, **extra: Any) -> None:
        super().__init__(error)
        self.status = status
        self.payload = {"ok": False, "error": error, **extra}


class Launcher:
    def __init__(
        self,
        registry: BrokerRegistry,
        agent_config: Optional[Dict[str, Any]],
        broker_port: int,
        token: Optional[str],
    ) -> None:
        self._registry = registry
        cfg = dict(agent_config or {})
        defaults = default_profiles()
        self._profiles: Dict[str, Any] = cfg.get("profiles") or defaults["profiles"]
        self._default_profile: str = (
            cfg.get("default_profile") or defaults["default_profile"])
        self._python: str = cfg.get("python") or sys.executable
        self._broker_port = broker_port
        self._token = token
        self._pending = 0

    @property
    def profiles(self) -> Dict[str, Any]:
        return self._profiles

    @property
    def default_profile(self) -> str:
        return self._default_profile

    def set_profiles(self, profiles: Dict[str, Any],
                     default_profile: str) -> None:
        """Live-swap the profile set (Control Panel edit, no restart — #70).

        Rebinds ``self._profiles`` / ``self._default_profile`` to brand-new
        objects the caller owns; it NEVER mutates the current dict in place. A
        single attribute rebind is atomic under the GIL, so a ``launch()``
        already past ``self._profiles.get(name)`` finishes against the snapshot
        it read while a new launch sees the fresh set — no torn read, no lock
        needed on the read path. The caller (POST /profiles/config) validates
        the payload and serializes writes under ``profiles_lock`` before calling
        this; here we only rebind. The ``profiles``/``default_profile``
        properties read the live attributes, so /profiles and every launch pick
        up the change immediately."""
        self._profiles = dict(profiles)
        self._default_profile = str(default_profile or "")

    async def launch(
        self,
        profile_name: Optional[str],
        cols: int = 80,
        rows: int = 24,
        title: Optional[str] = None,
        cwd: Optional[str] = None,
    ) -> Tuple[int, Dict[str, Any]]:
        """Returns (http_status, response_payload).

        ``cwd`` (todo2 task 7) is the client-chosen starting directory for the
        spawned shell. It MUST already be validated (existing dir) and gated by
        the caller — it is passed through to the agent as ``--cwd`` (data, not a
        command), which sets the shell's working dir."""
        # Snapshot the live profile set once (#70 live-swap): set_profiles
        # rebinds self._profiles / self._default_profile to fresh objects, so
        # read them into locals here and use only the locals below. The one
        # residual window — a no-explicit-profile launch racing an edit that
        # renames the default — yields at most a self-correcting 400, never a
        # torn command: the argv comes wholly from one snapshot's entry.
        profiles = self._profiles
        name = profile_name or self._default_profile
        profile = profiles.get(name)
        if profile is None or not profile.get("command"):
            raise LaunchError(400, "unknown_profile", profile=name)

        if self._pending >= MAX_PENDING:
            raise LaunchError(429, "too_many_pending_launches")

        window_id = self._allocate_window_id()
        cols = max(1, min(int(cols), 1000))
        rows = max(1, min(int(rows), 1000))
        effective_title = title or profile.get("title") or name

        argv: List[str] = [
            self._python, "-m", "webterm.agent",
            "--window-id", str(window_id),
            "--broker-url", f"ws://127.0.0.1:{self._broker_port}/browserland",
            "--cols", str(cols),
            "--rows", str(rows),
            "--title", str(effective_title),
        ]
        if cwd:
            argv += ["--cwd", str(cwd)]
        # #115: pass the resolved profile NAME so the agent re-announces it in
        # every hello (it survives a broker restart via the agent's reconnect,
        # exactly like cwd/title). The browser resolves the per-profile default
        # terminal color from it at seed time. Data, never a command.
        argv += ["--profile", str(name)]
        argv += ["--"] + [str(part) for part in profile["command"]]

        # Start from a registry-fresh PATH (todo task 17) so the detached agent
        # — and the shell it spawns — find programs installed since the broker
        # logged in, without a re-login. No-op copy of os.environ off Windows.
        env = spawn_env()
        # The agent honors $BROWSERLAND_BROKER_URL above its --broker-url flag;
        # pin it so an inherited value can't redirect our child.
        env[BROKER_URL_ENV] = f"ws://127.0.0.1:{self._broker_port}/browserland"
        # Token via env only — argv is visible in process lists.
        if self._token:
            env[TOKEN_ENV] = self._token
        else:
            env.pop(TOKEN_ENV, None)

        waiter = self._registry.add_waiter(window_id)
        self._pending += 1
        try:
            try:
                proc = _spawn_detached(argv, cwd=profile.get("cwd"), env=env)
            except Exception as exc:
                raise LaunchError(500, f"spawn_failed: {exc}")
            LOGGER.info("launched profile %r as window %d (agent pid %s)",
                        name, window_id, proc.pid)

            deadline = asyncio.get_running_loop().time() + REGISTER_TIMEOUT
            while True:
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    return 202, {"ok": True, "id": window_id,
                                 "registered": False, "agent_pid": proc.pid}
                try:
                    await asyncio.wait_for(
                        waiter.wait(), timeout=min(_POLL_INTERVAL, remaining))
                except asyncio.TimeoutError:
                    pass
                if waiter.is_set():
                    return 200, {"ok": True, "id": window_id,
                                 "registered": True, "agent_pid": proc.pid}
                code = proc.poll()
                if code is not None:
                    raise LaunchError(500, "agent_exited_early",
                                      returncode=code)
        finally:
            self._pending -= 1
            self._registry.remove_waiter(window_id)

    def _allocate_window_id(self) -> int:
        """Above any HWND/XID (>= 2**52), below 2**53 so the picker's JS
        compares it exactly. Re-roll on the unlikely collision."""
        while True:
            window_id = (1 << 52) | secrets.randbits(32)
            if window_id not in self._registry and \
                    not self._registry.is_pending(window_id):
                return window_id


def _spawn_detached(argv: List[str], cwd: Optional[str],
                    env: Dict[str, str]) -> subprocess.Popen:
    kwargs: Dict[str, Any] = dict(
        cwd=cwd or None,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
    )
    if os.name == "nt":
        flags = (subprocess.DETACHED_PROCESS
                 | subprocess.CREATE_NEW_PROCESS_GROUP
                 | _CREATE_BREAKAWAY_FROM_JOB)
        try:
            return subprocess.Popen(argv, creationflags=flags, **kwargs)
        except OSError:
            # Parent job may forbid breakaway (ERROR_ACCESS_DENIED) — the
            # agent then shares the broker's lifetime, which beats failing.
            flags = (subprocess.DETACHED_PROCESS
                     | subprocess.CREATE_NEW_PROCESS_GROUP)
            return subprocess.Popen(argv, creationflags=flags, **kwargs)
    return subprocess.Popen(argv, start_new_session=True, **kwargs)
