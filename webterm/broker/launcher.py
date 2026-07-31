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

Which of those paths a spawn actually took is RECORDED per agent (#183): a
self-restart is only tolerable because agents outlive it, so the broker has to
be able to say how many LIVE sessions would die with it — and on Windows the
answer differs agent by agent, because the breakaway can be refused for one
spawn (we silently fall back, and that child then shares the broker's lifetime)
and granted for the next. See SpawnResult and Launcher.continuity_for.
"""

from __future__ import annotations

import asyncio
import logging
import os
import secrets
import subprocess
import sys
from typing import Any, Dict, List, NamedTuple, Optional, Tuple

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

# Restart continuity of ONE agent, as observed at spawn time. These exact
# strings are the wire values other broker code reports, so they are constants
# here rather than literals scattered across call sites.
CONTINUITY_GUARANTEED = "guaranteed"   # detached from the broker's lifetime
CONTINUITY_AT_RISK = "at_risk"         # dies with the broker (breakaway refused)
CONTINUITY_UNKNOWN = "unknown"         # not established — never assume better
_CONTINUITY_VALUES = (CONTINUITY_GUARANTEED, CONTINUITY_AT_RISK,
                      CONTINUITY_UNKNOWN)


class SpawnResult(NamedTuple):
    """What ``_spawn_detached`` DID, not what it was asked to do.

    ``continuity`` has to be observed here and carried out, because it cannot be
    recovered afterwards: on Windows an extra parent in the process tree changes
    job-object membership, so two agents spawned by the same broker on the same
    machine can land on different paths. Deriving it later from ``os.name`` or
    from the install's shape would be a guess dressed as a fact."""

    proc: subprocess.Popen
    continuity: str


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
        # #183: observed restart continuity per AGENT, keyed by the window id
        # this launcher allocated. Per-agent and not per-install on purpose —
        # "a replacement broker can be launched" and "your sessions survive it"
        # are different claims, and on Windows the second one can differ between
        # two agents of the same broker. Only ever written from a spawn that
        # actually happened (see _note_continuity); an agent that RECONNECTS
        # after a restart is therefore absent from a fresh broker's map and
        # reads "unknown", which is exactly right: the new process did not spawn
        # it and cannot know how it was created.
        self._continuity: Dict[int, str] = {}

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
        #
        # spawn_env() shells out to the Windows registry and _spawn_detached is
        # a full process-creation syscall (not fast when the profile cwd is a
        # network path), and the broker runs single_process=True — one event
        # loop for every handler and every live terminal WebSocket. So both run
        # together in ONE worker-thread hop below. Only inert data crosses the
        # boundary (argv, cwd, token, port); self._pending and the registry
        # waiter are loop-affine and stay on the loop.
        #
        # NOT asyncio.create_subprocess_exec: that binds a child watcher to a
        # process this module deliberately breaks away from the job object (see
        # the module docstring) so agents outlive a broker restart.
        profile_cwd = profile.get("cwd")
        token = self._token
        broker_url = f"ws://127.0.0.1:{self._broker_port}/browserland"

        def _build_env_and_spawn() -> SpawnResult:
            env = spawn_env()
            # The agent honors $BROWSERLAND_BROKER_URL above its --broker-url
            # flag; pin it so an inherited value can't redirect our child.
            env[BROKER_URL_ENV] = broker_url
            # Token via env only — argv is visible in process lists.
            if token:
                env[TOKEN_ENV] = token
            else:
                env.pop(TOKEN_ENV, None)
            return _spawn_detached(argv, cwd=profile_cwd, env=env)

        # The offload MUST stay AFTER add_waiter + the _pending bump: those
        # register the pending launch, and a child that reaches /browserland
        # before anyone is waiting for it is a launch nobody ever collects.
        waiter = self._registry.add_waiter(window_id)
        self._pending += 1
        try:
            try:
                # No wait_for: a running executor future is not cancellable, so
                # a deadline here would 504 the request and still leave the
                # thread wedged mid-CreateProcess.
                spawned = await asyncio.get_running_loop().run_in_executor(
                    None, _build_env_and_spawn)
            except Exception as exc:
                raise LaunchError(500, f"spawn_failed: {exc}")
            proc = spawned.proc
            # Record what THIS spawn did before anything can go wrong later:
            # the window may never register, but if it does register it must
            # already carry the truth about how it was created (#183).
            self._note_continuity(window_id, spawned.continuity)
            LOGGER.info("launched profile %r as window %d (agent pid %s, "
                        "restart continuity %s)",
                        name, window_id, proc.pid, spawned.continuity)

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

    # -- restart continuity (#183) ------------------------------------------

    def _note_continuity(self, window_id: int, continuity: str) -> None:
        """Record one spawn's observed continuity, pruning dead entries first.

        A window's DEPARTURE has no hook here — registry.deregister() is called
        from the producer session, which knows nothing about the launcher — so
        the map is pruned lazily instead: an entry is dropped the first time we
        look and find its window neither registered nor still pending. That
        bounds the map at (live + in-flight) agents rather than one entry per
        launch for the broker's whole life. Prune BEFORE the insert so the row
        we are writing can never be the one collected.

        The lazy prune can also drop a record whose agent registers unusually
        late (a launch that already 202'd): it then reads "unknown". That is the
        safe direction — under-claiming survival, never over-claiming it."""
        if continuity not in _CONTINUITY_VALUES:
            continuity = CONTINUITY_UNKNOWN
        self._prune_continuity()
        self._continuity[int(window_id)] = continuity

    def _prune_continuity(self) -> None:
        # is_pending() covers the gap between the spawn and the agent's hello:
        # launch() adds the waiter before spawning and drops it in its finally,
        # so a just-spawned window is never collected out from under itself.
        #
        # Pure bookkeeping, so it must never be the reason a launch fails: a
        # registry that cannot answer just means these rows live one round
        # longer, which costs a few dict entries and no correctness.
        try:
            stale = [wid for wid in self._continuity
                     if wid not in self._registry
                     and not self._registry.is_pending(wid)]
        except Exception:
            LOGGER.debug("continuity prune skipped", exc_info=True)
            return
        for wid in stale:
            self._continuity.pop(wid, None)

    def continuity_for(self, window_id: int) -> str:
        """Whether THAT agent survives a broker restart: "guaranteed",
        "at_risk" or "unknown".

        Read-only, and "unknown" for anything this launcher did not itself
        spawn — including every agent that reconnected after a restart, because
        the map lives in the broker process that did the spawning and a fresh
        broker starts empty. That is by construction, not by accident: there is
        no path that writes this map from a hello, so a reconnect can never be
        mistaken for a launch we watched."""
        try:
            recorded = self._continuity.get(int(window_id))
        except (TypeError, ValueError):
            return CONTINUITY_UNKNOWN
        if recorded in (CONTINUITY_GUARANTEED, CONTINUITY_AT_RISK):
            return recorded
        return CONTINUITY_UNKNOWN

    def continuity_summary(self) -> Dict[str, int]:
        """Counts over the agents CURRENTLY registered — what a restart
        confirmation shows an operator ("2 of 5 sessions will not survive").

        Never raises and never over-claims: a registered window with no record
        counts as unknown, and if the registry cannot even be enumerated we
        report zeros rather than a reassuring guess. Only registered windows are
        counted, so a record whose terminal has already exited cannot inflate
        the total."""
        counts = {CONTINUITY_GUARANTEED: 0, CONTINUITY_AT_RISK: 0,
                  CONTINUITY_UNKNOWN: 0}
        try:
            # session_summaries() is the registry's public enumeration; this
            # runs on the loop like every other registry read, so the snapshot
            # cannot tear underneath us.
            window_ids = [int(s["id"])
                          for s in self._registry.session_summaries()]
            self._prune_continuity()
        except Exception:
            LOGGER.debug("continuity_summary: registry unreadable",
                         exc_info=True)
            return counts
        for wid in window_ids:
            counts[self.continuity_for(wid)] += 1
        return counts


def _spawn_detached(argv: List[str], cwd: Optional[str],
                    env: Dict[str, str]) -> SpawnResult:
    """Spawn the agent detached and report which path actually worked.

    The flags and the fallback below are unchanged — they are the whole reason
    agents outlive a broker restart. All that is new is that the caller now
    learns which of them this spawn ended up on (#183)."""
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
            proc = subprocess.Popen(argv, creationflags=flags, **kwargs)
        except OSError:
            # Parent job may forbid breakaway (ERROR_ACCESS_DENIED) — the
            # agent then shares the broker's lifetime, which beats failing.
            # That shared lifetime IS the at-risk case: a restart takes this
            # agent (and its terminal) down with the broker, so say so instead
            # of letting the fallback pass for a normal launch.
            flags = (subprocess.DETACHED_PROCESS
                     | subprocess.CREATE_NEW_PROCESS_GROUP)
            return SpawnResult(
                subprocess.Popen(argv, creationflags=flags, **kwargs),
                CONTINUITY_AT_RISK)
        # Breakaway accepted: the child is out of the broker's job object, so
        # nothing kills it when the broker exits.
        return SpawnResult(proc, CONTINUITY_GUARANTEED)
    proc = subprocess.Popen(argv, start_new_session=True, **kwargs)
    # start_new_session is setsid(2): the child leads a NEW session and process
    # group, so it neither dies with the broker nor sees signals aimed at the
    # broker's group. Claim that only where the flag means that — on some other
    # os.name we genuinely do not know what we just did, and "unknown" is the
    # honest answer rather than the flattering one.
    return SpawnResult(proc, CONTINUITY_GUARANTEED if os.name == "posix"
                       else CONTINUITY_UNKNOWN)
