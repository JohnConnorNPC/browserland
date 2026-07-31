"""#183 restart continuity: every agent spawn records whether THAT agent will
survive a broker restart, taken from the path the spawn actually used.

The self-restart is only tolerable because agents outlive it, so the claim has
to be observed rather than assumed — on Windows the job object can refuse
CREATE_BREAKAWAY_FROM_JOB for one spawn and allow it for the next, and the
launcher's fallback (which drops the flag) leaves that agent sharing the
broker's lifetime.

No real processes here: subprocess.Popen and os.name are monkeypatched, so the
case that matters most — breakaway REFUSED, where a restart eats the operator's
terminals — is covered on every platform, not only on a machine that happens to
sit inside a restrictive job object.
"""

from __future__ import annotations

import asyncio
import os
import subprocess

import pytest

from webterm.broker import launcher as launcher_mod
from webterm.broker.launcher import (CONTINUITY_AT_RISK, CONTINUITY_GUARANTEED,
                                     CONTINUITY_UNKNOWN,
                                     _CREATE_BREAKAWAY_FROM_JOB, Launcher,
                                     _spawn_detached)
from webterm.broker.registry import BrokerRegistry

# The real Win32 values. subprocess omits them off Windows, so the tests below
# inject them and assert against them; test_windows_flag_constants_match_the_
# platform pins the pair to the genuine article on a Windows runner so the
# stand-ins can never drift into testing a fiction.
_DETACHED_PROCESS = 0x00000008
_CREATE_NEW_PROCESS_GROUP = 0x00000200

_ZERO_COUNTS = {"guaranteed": 0, "at_risk": 0, "unknown": 0}


class _FakeProc:
    """A spawned agent as far as launch() cares: a pid and a poll() that keeps
    saying 'still running' (a None poll is what stops launch() declaring
    agent_exited_early)."""

    _next_pid = 40000

    def __init__(self, argv):
        self.argv = list(argv)
        _FakeProc._next_pid += 1
        self.pid = _FakeProc._next_pid

    def poll(self):
        return None


class _FakeSpawner:
    """subprocess.Popen stand-in that can play a job object forbidding
    breakaway: while ``refuse_breakaway`` is set, any attempt carrying
    CREATE_BREAKAWAY_FROM_JOB raises OSError exactly as CreateProcess does with
    ERROR_ACCESS_DENIED, and only the flag-less retry succeeds."""

    def __init__(self):
        self.calls = []            # one entry per Popen attempt, in order
        self.refuse_breakaway = False

    def __call__(self, argv, **kwargs):
        self.calls.append(dict(kwargs, argv=list(argv)))
        if self.refuse_breakaway and (
                kwargs.get("creationflags", 0) & _CREATE_BREAKAWAY_FROM_JOB):
            raise OSError(5, "Access is denied")
        return _FakeProc(argv)


class _StubWS:
    """Producer socket stand-in; register() only stores it on the entry."""

    async def send(self, payload):
        pass

    async def close(self, *a, **k):
        pass


def _use_windows(monkeypatch, spawner):
    """Make the launcher take its Windows path on any host."""
    monkeypatch.setattr(launcher_mod.os, "name", "nt")
    monkeypatch.setattr(subprocess, "DETACHED_PROCESS", _DETACHED_PROCESS,
                        raising=False)
    monkeypatch.setattr(subprocess, "CREATE_NEW_PROCESS_GROUP",
                        _CREATE_NEW_PROCESS_GROUP, raising=False)
    monkeypatch.setattr(subprocess, "Popen", spawner)


def _make_launcher(registry):
    return Launcher(registry,
                    {"profiles": {"p": {"command": ["noop"], "title": "t"}},
                     "default_profile": "p"},
                    4444, None)


# -- _spawn_detached: what path did we actually take? ------------------------


def test_posix_spawn_records_guaranteed(monkeypatch):
    spawner = _FakeSpawner()
    monkeypatch.setattr(launcher_mod.os, "name", "posix")
    monkeypatch.setattr(subprocess, "Popen", spawner)

    result = _spawn_detached(["agent"], cwd=None, env={"PATH": ""})
    proc, continuity = result                      # tuple-compatible for callers

    assert continuity == CONTINUITY_GUARANTEED
    assert proc is result.proc and isinstance(proc, _FakeProc)
    assert len(spawner.calls) == 1
    call = spawner.calls[0]
    assert call["start_new_session"] is True       # setsid: new session + group
    assert "creationflags" not in call             # not a Windows spawn


def test_unclassifiable_platform_records_unknown(monkeypatch):
    """Neither nt nor posix: the spawn still happens (behaviour unchanged), but
    a path nobody has reasoned about must not pass for 'guaranteed'."""
    spawner = _FakeSpawner()
    monkeypatch.setattr(launcher_mod.os, "name", "java")
    monkeypatch.setattr(subprocess, "Popen", spawner)

    result = _spawn_detached(["agent"], cwd=None, env={})

    assert result.continuity == CONTINUITY_UNKNOWN
    assert isinstance(result.proc, _FakeProc)


def test_windows_breakaway_accepted_records_guaranteed(monkeypatch):
    spawner = _FakeSpawner()
    _use_windows(monkeypatch, spawner)

    result = _spawn_detached(["agent"], cwd=None, env={})

    assert result.continuity == CONTINUITY_GUARANTEED
    assert len(spawner.calls) == 1                 # no fallback attempt
    assert spawner.calls[0]["creationflags"] == (
        _DETACHED_PROCESS | _CREATE_NEW_PROCESS_GROUP
        | _CREATE_BREAKAWAY_FROM_JOB)


def test_windows_breakaway_refused_records_at_risk(monkeypatch):
    """The case the whole atom exists for: the parent job denies breakaway, the
    launcher silently retries without it, and that agent now dies with the
    broker. The retry must still hand back a WORKING process — failing the
    launch outright would be worse than a mortal agent — and the flags on both
    attempts must be exactly what they always were."""
    spawner = _FakeSpawner()
    spawner.refuse_breakaway = True
    _use_windows(monkeypatch, spawner)

    result = _spawn_detached(["agent"], cwd=None, env={})

    assert result.continuity == CONTINUITY_AT_RISK
    assert isinstance(result.proc, _FakeProc)
    assert result.proc.pid > 0 and result.proc.poll() is None
    assert len(spawner.calls) == 2                 # refused, then the fallback
    assert spawner.calls[0]["creationflags"] & _CREATE_BREAKAWAY_FROM_JOB
    assert spawner.calls[1]["creationflags"] == (
        _DETACHED_PROCESS | _CREATE_NEW_PROCESS_GROUP)
    assert not spawner.calls[1]["creationflags"] & _CREATE_BREAKAWAY_FROM_JOB


@pytest.mark.skipif(os.name != "nt",
                    reason="Win32 creation flags only exist on Windows")
def test_windows_flag_constants_match_the_platform():
    """Pin the stand-in constants used above to the real ones (Windows only)."""
    assert subprocess.DETACHED_PROCESS == _DETACHED_PROCESS
    assert subprocess.CREATE_NEW_PROCESS_GROUP == _CREATE_NEW_PROCESS_GROUP


# -- Launcher: per-agent bookkeeping ----------------------------------------


def test_continuity_summary_is_zeros_before_any_launch():
    """Nothing launched, nothing registered — counts, not an exception."""
    launcher = _make_launcher(BrokerRegistry())
    assert launcher.continuity_summary() == _ZERO_COUNTS


def test_unrecorded_window_reads_unknown():
    launcher = _make_launcher(BrokerRegistry())
    assert launcher.continuity_for((1 << 52) | 1234) == CONTINUITY_UNKNOWN
    # Junk ids answer the same way instead of blowing up a status handler.
    assert launcher.continuity_for("nonsense") == CONTINUITY_UNKNOWN
    assert launcher.continuity_for(None) == CONTINUITY_UNKNOWN


def test_continuity_summary_never_raises_on_a_broken_registry():
    """It backs a restart confirmation, so an unreadable registry must degrade
    to 'we established nothing', never to an error and never to guaranteed."""

    class _Exploding:
        def session_summaries(self, *a, **k):
            raise RuntimeError("boom")

        def is_pending(self, window_id):
            raise RuntimeError("boom")

        def __contains__(self, window_id):
            raise RuntimeError("boom")

    launcher = _make_launcher(_Exploding())
    assert launcher.continuity_summary() == _ZERO_COUNTS


async def _launch_and_register(launcher, reg, spawner):
    """Drive one launch through to a registered window: start it, read the
    window id off the argv the spawner saw, then register that window as the
    agent's hello would. Returns (window_id, status, payload)."""
    before = len(spawner.calls)
    task = asyncio.create_task(launcher.launch("p"))
    for _ in range(400):
        if len(spawner.calls) > before:
            break
        await asyncio.sleep(0.005)
    assert len(spawner.calls) > before, "launch never reached the spawner"
    argv = spawner.calls[before]["argv"]
    window_id = int(argv[argv.index("--window-id") + 1])
    await reg.register(_StubWS(), {"window_id": window_id, "pid": 1,
                                   "title": "t", "cols": 80, "rows": 24,
                                   "kind": "agent"})
    status, payload = await asyncio.wait_for(task, 5)
    return window_id, status, payload


def test_launch_records_continuity_per_agent_and_summarises(monkeypatch):
    """Two agents of the SAME broker on the SAME machine can land on different
    paths, so the record is per agent; the summary counts only what is live and
    calls everything it did not watch 'unknown'."""
    spawner = _FakeSpawner()
    _use_windows(monkeypatch, spawner)
    # Keep the spawn hermetic and fast: spawn_env() otherwise shells out to the
    # Windows registry for a fresh PATH.
    monkeypatch.setattr(launcher_mod, "spawn_env", lambda: {"PATH": ""})

    async def scenario():
        reg = BrokerRegistry()
        launcher = _make_launcher(reg)

        wid_ok, status, payload = await _launch_and_register(
            launcher, reg, spawner)
        assert status == 200 and payload["registered"] is True
        assert payload["id"] == wid_ok
        assert launcher.continuity_for(wid_ok) == CONTINUITY_GUARANTEED

        # Next agent, same launcher: the job now refuses breakaway.
        spawner.refuse_breakaway = True
        wid_risk, status, _ = await _launch_and_register(launcher, reg, spawner)
        assert status == 200
        assert launcher.continuity_for(wid_risk) == CONTINUITY_AT_RISK
        # The earlier agent's verdict is untouched by the later refusal.
        assert launcher.continuity_for(wid_ok) == CONTINUITY_GUARANTEED

        assert launcher.continuity_summary() == {
            "guaranteed": 1, "at_risk": 1, "unknown": 0}

        # An agent that reconnected ACROSS a restart: registered here, spawned
        # by a broker process that is gone. It must read unknown — a fresh
        # broker cannot know how it was created.
        await reg.register(_StubWS(), {"window_id": (1 << 52) | 7, "pid": 2,
                                       "title": "t", "cols": 80, "rows": 24})
        assert launcher.continuity_summary() == {
            "guaranteed": 1, "at_risk": 1, "unknown": 1}

        # The at-risk terminal exits: its record must not outlive the window,
        # neither in the counts nor in the map.
        await reg.deregister(wid_risk)
        assert launcher.continuity_summary() == {
            "guaranteed": 1, "at_risk": 0, "unknown": 1}
        assert wid_risk not in launcher._continuity      # pruned, not hidden
        assert wid_ok in launcher._continuity            # live one survives

    asyncio.run(scenario())
