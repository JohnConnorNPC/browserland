"""GH#183 end to end: does an agent actually outlive a broker restart?

``launcher.py``'s docstring says agents survive a broker restart because they
are spawned detached and their reconnect loop reattaches. That claim is the
whole reason a self-restart is tolerable rather than catastrophic, and until
this file nothing in the suite checked it -- it was a comment, and a comment is
not evidence.

There is a second, sharper reason to OBSERVE it rather than reason about it:
#183 puts an extra parent (the supervisor) in the process tree, and on Windows
lifetime follows job-object membership and handles rather than parent-child
death. ``launcher.py``'s own fallback says as much -- when a job forbids
CREATE_BREAKAWAY_FROM_JOB it retries without the flag, and that agent then
shares the broker's lifetime. So the answer can differ per machine and even per
spawn, and the only honest test is one that runs the real thing.

What is graded here is therefore the broker's HONESTY, not the outcome we would
like:

  * ``GET /info``'s ``restart.continuity`` reports, per live agent,
    ``guaranteed`` / ``at_risk`` / ``unknown`` -- taken from the path the spawn
    actually took.
  * an agent the broker called ``guaranteed`` MUST come back after a restart,
    as the same OS process. Losing one is a bug and fails this test.
  * an agent it called ``at_risk`` (or ``unknown``) was never promised, so
    losing one is correct behaviour and passes -- loudly, via a warning that
    names the platform's real answer, so a weakened claim can never be mistaken
    for a green tick.

Everything here is real: a real supervisor, a real worker, a real detached
agent, a real ``POST /restart``. That makes this file SLOW (tens of seconds --
two broker boots plus one restart) and it spawns processes that are detached by
design, so every path reaps what it started, failures included.

(No ``@pytest.mark.slow``: this repo registers no custom markers, and an
unregistered one would add a PytestUnknownMarkWarning to every run of the whole
suite. The cost is documented here instead.)
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
import warnings
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
TOKEN = "restart-e2e-token"
LAUNCH_PROFILE = "cmd" if os.name == "nt" else "bash"
_AUTH = {"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"}

#: Bounded, but generously: a broker boot imports Sanic and the whole app, and
#: CI boxes are slower than this one. A hang must still end in a message.
BOOT_TIMEOUT = 90.0
#: bootId is the ONLY honest proof a restart happened (see app.BOOT_ID): the
#: response to POST /restart is written by the process being replaced.
RESTART_TIMEOUT = 120.0
#: The agent's reconnect backoff is 500 ms doubling to a 10 s cap, so a
#: reconnect is not instantaneous even once the port is back.
RECONNECT_TIMEOUT = 90.0

# The launch profiles are "cmd" on Windows and "bash" elsewhere; without a
# shell to spawn there is no agent to observe. A genuine environmental
# impossibility, and the only skip in this file.
requires_a_shell = pytest.mark.skipif(
    os.name != "nt" and shutil.which("bash") is None,
    reason="no bash on this box, so POST /launch cannot spawn a real agent")


# --------------------------------------------------------------------------- #
# plumbing (shaped after tests/test_broker_e2e.py's broker_proc fixture)
# --------------------------------------------------------------------------- #

def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _http(method: str, url: str, body: bytes = None, headers: dict = None,
          timeout: float = 10):
    request = urllib.request.Request(url, data=body, method=method,
                                     headers=headers or {})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"{}")


class _Broker:
    """One supervised broker: the supervisor process, its base URL, and its log.

    ``proc`` is ``python -m webterm.broker`` WITHOUT ``--worker`` -- i.e. the
    supervisor, which spawns the actual server as its child. That is the whole
    point of this file: the restart path only exists under the supervisor, and
    the extra parent is exactly what could change the answer on Windows."""

    def __init__(self, proc, base: str, port: int, log_path: Path, handle):
        self.proc = proc
        self.base = base
        self.port = port
        self.log_path = log_path
        self._handle = handle

    def tail(self, limit: int = 3000) -> str:
        """The end of the broker's own stderr, for failure messages. The
        supervisor logs its decisions there ("restarting the broker as
        requested", "restart budget exhausted", ...), which is usually the
        difference between a diagnosable failure and a mystery."""
        try:
            text = self.log_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return "<no broker log>"
        return text[-limit:]

    def info(self):
        return _http("GET", f"{self.base}/info?token={TOKEN}")

    def sessions(self):
        return _http("GET", f"{self.base}/sessions?token={TOKEN}")

    def stop(self) -> None:
        try:
            if self.proc.poll() is None:
                self.proc.terminate()
                try:
                    self.proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    self.proc.kill()
                    self.proc.wait(timeout=10)
        finally:
            try:
                self._handle.close()
            except OSError:
                pass


def _write_config(work: Path, *, restart_enabled) -> Path:
    """A broker config in ``work``. ``restart_enabled=None`` OMITS the key --
    which is the shape the gate test needs (absent, not merely false)."""
    config = {
        "auth_token": TOKEN,
        # Everything the broker persists goes in the throwaway dir, never
        # beside the source tree.
        "state_path": str(work / "webterm_state.json"),
        "recordings_dir": str(work / "recordings"),
        "editor_root": str(work),
        # The post-boot cooldown (#183, R6) defaults to 90s, and this file
        # restarts a broker moments after it boots — 0 is the documented off
        # switch for exactly this. The cooldown has its own tests in
        # test_restart.py; what is graded here is survival, not pacing.
        "restart_cooldown_seconds": 0,
    }
    if restart_enabled is not None:
        config["restart_enabled"] = restart_enabled
    path = work / "broker_config.json"
    path.write_text(json.dumps(config, indent=2), encoding="utf-8")
    return path


def _start_supervised_broker(work: Path, *, restart_enabled) -> _Broker:
    """Start a real broker through the supervisor and wait until it answers.

    ``--port`` is always an explicitly-picked free port: a port-less broker
    would land on 4445 and collide with the live development one."""
    work.mkdir(parents=True, exist_ok=True)
    config = _write_config(work, restart_enabled=restart_enabled)
    port = _free_port()

    env = dict(os.environ)
    # webterm must resolve to THIS tree even though cwd is the throwaway dir
    # (an installed/shadowing copy elsewhere on sys.path would silently be the
    # thing under test).
    env["PYTHONPATH"] = str(REPO) + os.pathsep + env.get("PYTHONPATH", "")
    # The config file is the only source of the token and of the restart gate:
    # $WEB_TERMINAL_TOKEN would OUTRANK auth_token (auth.resolve_token), and an
    # inherited $WEB_TERMINAL_CONFIG must not shadow the one we just wrote.
    env.pop("WEB_TERMINAL_TOKEN", None)
    env.pop("WEB_TERMINAL_CONFIG", None)
    env.pop("BROWSERLAND_BROKER_URL", None)

    log_path = work / "broker.log"
    handle = open(log_path, "wb")
    proc = subprocess.Popen(
        # NO --worker: this process is the supervisor (#183). --headless keeps
        # the boot cheap; only JSON/WS surfaces are exercised here.
        [sys.executable, "-m", "webterm.broker", "--headless",
         "--config", str(config),
         "--host", "127.0.0.1", "--port", str(port)],
        cwd=str(work), env=env, stdout=handle, stderr=subprocess.STDOUT,
    )
    broker = _Broker(proc, f"http://127.0.0.1:{port}", port, log_path, handle)
    deadline = time.time() + BOOT_TIMEOUT
    while True:
        try:
            status, _ = broker.sessions()
            if status == 200:
                return broker
            # A 401 here means the config never reached the worker: the token
            # we are using came from that same file.
            if status == 401:
                broker.stop()
                raise RuntimeError(
                    "the broker answered 401 with the configured token: it did "
                    f"not load {config}\n{broker.tail()}")
        except OSError:
            pass                      # not listening yet
        if proc.poll() is not None:
            handle.close()
            raise RuntimeError(f"broker exited early ({proc.returncode}):\n"
                               f"{broker.tail()}")
        if time.time() > deadline:
            broker.stop()
            raise RuntimeError(f"broker did not come up:\n{broker.tail()}")
        time.sleep(0.2)


def _pid_alive(pid) -> bool:
    """Is that OS process still running?

    NOT ``os.kill(pid, 0)``: on Windows ``os.kill`` is implemented with
    TerminateProcess, so the "is it alive" probe would KILL the very agent it
    is asking about -- and this test would then prove the opposite of what it
    claims to."""
    if not pid:
        return False
    pid = int(pid)
    if os.name == "nt":
        out = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/NH", "/FO", "CSV"],
            capture_output=True, text=True)
        return f'"{pid}"' in (out.stdout or "")
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True                   # alive, just not ours to signal
    return True


def _reap_agent(agent_pid, shell_pid) -> None:
    """Kill an agent and its shell. Agents are spawned DETACHED and their
    reconnect loop never gives up, so one left behind outlives the test run."""
    pids = [p for p in (agent_pid, shell_pid) if p]
    if os.name == "nt":
        for pid in pids:
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                           capture_output=True)
        return
    for pid in pids:
        # Agent AND shell are both session leaders (start_new_session in
        # _spawn_detached and in the PTY backend), so killing one does not take
        # the other down.
        try:
            os.kill(int(pid), signal.SIGKILL)
        except OSError:
            pass


def _launch_agent(broker: _Broker):
    """POST /launch a real detached agent; return (window_id, agent_pid)."""
    status, payload = _http(
        "POST", f"{broker.base}/launch",
        body=json.dumps({"profile": LAUNCH_PROFILE,
                         "cols": 90, "rows": 25}).encode(),
        headers=_AUTH, timeout=30)
    assert status == 200, f"{status} {payload}\n{broker.tail()}"
    assert payload["ok"] is True and payload["registered"] is True, payload
    return int(payload["id"]), int(payload["agent_pid"])


def _session(broker: _Broker, window_id: int, timeout: float = 0.0):
    """The /sessions entry for ``window_id``, or None. With ``timeout`` > 0,
    poll for it to (re)appear -- a reconnect is not instantaneous."""
    deadline = time.time() + max(0.0, timeout)
    while True:
        try:
            status, sessions = broker.sessions()
            if status == 200:
                for entry in sessions:
                    if entry.get("id") == window_id:
                        return entry
        except OSError:
            pass                      # still down, or coming back
        if time.time() >= deadline:
            return None
        time.sleep(0.25)


def _wait_for_new_boot(broker: _Broker, previous: str, timeout: float):
    """Poll /info until ``restart.bootId`` differs from ``previous``.

    This is the only trustworthy "it restarted" signal: broker_id is durable
    across restarts BY DESIGN (#64), and the 202 is written by the process that
    is about to be replaced."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            status, info = broker.info()
            if status == 200:
                boot = info.get("restart", {}).get("bootId")
                if boot and boot != previous:
                    return info
        except OSError:
            pass                      # the port is down between processes
        if broker.proc.poll() is not None:
            return None               # the supervisor itself gave up
        time.sleep(0.25)
    return None


# --------------------------------------------------------------------------- #
# E20: agents outlive a restart -- or the broker said up front that they would
#      not
# --------------------------------------------------------------------------- #

@requires_a_shell
def test_an_agent_survives_a_broker_restart(tmp_path):
    """The claim in launcher.py's docstring, actually run.

    Launch a real agent, record what the broker PROMISES about it, restart the
    broker for real, and then check the promise against what happened. SLOW
    (~30-60 s): two process boots and a live restart.
    """
    broker = _start_supervised_broker(tmp_path / "restartable",
                                      restart_enabled=True)
    agent_pid = shell_pid = None
    try:
        # -- the restart must be available BEFORE anything else is claimed ----
        status, info = broker.info()
        assert status == 200, info
        before = info["restart"]
        assert before["configured"] is True, before
        assert before["available"] is True, (
            "the broker was started through the supervisor with "
            "restart_enabled, so a restart must be available; it refused with "
            f"reason_code={before['reason_code']!r} "
            f"(mechanism={before['mechanism']!r})\n{broker.tail()}")
        assert before["mechanism"] == "supervisor", before
        assert before["lifecycle"] == "running", before
        boot_before = before["bootId"]
        broker_id_before = info["broker_id"]

        # -- a real detached agent -------------------------------------------
        window_id, agent_pid = _launch_agent(broker)
        entry = _session(broker, window_id, timeout=10)
        assert entry is not None, f"the agent never registered\n{broker.tail()}"
        assert entry["kind"] == "agent", entry
        shell_pid = entry["pid"]
        assert shell_pid > 0, entry
        assert _pid_alive(agent_pid), "the agent process is already gone"

        # -- what does the broker PROMISE about THIS agent? -------------------
        # continuity is a summary over live agents; ours is the only one on a
        # broker this fresh, so the single non-zero bucket is its verdict.
        status, info = broker.info()
        assert status == 200, info
        continuity = info["restart"]["continuity"]
        assert sum(continuity.values()) == 1, (
            "expected exactly one live agent to account for, got "
            f"{continuity}\n{broker.tail()}")
        promised = next(k for k, v in continuity.items() if v == 1)
        assert promised in ("guaranteed", "at_risk", "unknown"), continuity

        # -- restart, for real ------------------------------------------------
        status, payload = _http("POST", f"{broker.base}/restart", body=b"{}",
                                headers=_AUTH, timeout=60)
        assert status == 202, f"{status} {payload}\n{broker.tail()}"
        assert payload["ok"] is True, payload
        assert payload["mechanism"] == "supervisor", payload
        # 202, never 200: the answering process is the one being replaced, so
        # it hands back the bootId to watch instead of claiming success.
        assert payload["bootId"] == boot_before, payload

        info_after = _wait_for_new_boot(broker, boot_before, RESTART_TIMEOUT)
        assert info_after is not None, (
            f"restart.bootId never changed from {boot_before!r} within "
            f"{RESTART_TIMEOUT:.0f}s -- the broker did not come back "
            f"(supervisor exit: {broker.proc.poll()})\n{broker.tail()}")
        # Same install, new process: broker_id is durable across restarts by
        # design, so this pins that we are talking to the same broker rather
        # than to something else that grabbed the port.
        assert info_after["broker_id"] == broker_id_before, info_after
        assert info_after["restart"]["lifecycle"] == "running", info_after

        # -- did the agent live through it? -----------------------------------
        entry_after = _session(broker, window_id, timeout=RECONNECT_TIMEOUT)
        survived = entry_after is not None and _pid_alive(agent_pid)

        if promised == "guaranteed":
            assert entry_after is not None, (
                "the broker reported restart continuity 'guaranteed' for this "
                "agent and then LOST it: it never reappeared in /sessions "
                f"within {RECONNECT_TIMEOUT:.0f}s of the restart. Either "
                "agents do not survive a restart on this platform (and the "
                "broker must stop claiming they do), or the reconnect is "
                f"broken.\n{broker.tail()}")
            # SAME process, not a replacement: /launch spawned this agent once
            # and nothing relaunched it, so its pid -- and the pid of the shell
            # it is hosting -- must be unchanged.
            assert _pid_alive(agent_pid), (
                f"the agent's window {window_id} is registered again but its "
                f"process ({agent_pid}) is gone: something RESPAWNED it "
                "instead of it surviving")
            assert entry_after["pid"] == shell_pid, (
                "the reconnected session is hosting a different shell "
                f"({entry_after['pid']} was {shell_pid}): this is a new agent, "
                "not the one that survived")
            assert entry_after["kind"] == "agent", entry_after
            # ...and the NEW broker must not flatter itself about an agent it
            # did not spawn: continuity is recorded at spawn time, in a process
            # that is now gone, so a reconnected agent reads "unknown".
            # Read FRESH: info_after was captured the moment the port came
            # back, possibly before the agent had reconnected.
            status, info_now = broker.info()
            assert status == 200, info_now
            after_continuity = info_now["restart"]["continuity"]
            assert after_continuity["guaranteed"] == 0, (
                "a broker that did not spawn this agent cannot know how it was "
                f"created, yet it claims {after_continuity}")
            assert after_continuity["unknown"] >= 1, after_continuity
        else:
            # The broker did NOT promise survival, so losing the agent here is
            # the documented behaviour rather than a bug (launcher.py falls
            # back to a spawn without CREATE_BREAKAWAY_FROM_JOB when a job
            # object forbids breakaway, and that agent shares the broker's
            # lifetime). What must never happen is the opposite pairing -- a
            # confident "guaranteed" followed by a lost agent -- and the branch
            # above is what fails on that.
            #
            # Said out loud, every run, so a weakened claim can never pass for
            # a green tick.
            warnings.warn(
                "this platform cannot guarantee that agents survive a broker "
                f"restart: the broker reported continuity={promised!r} for the "
                "launched agent before the restart, and the agent "
                f"{'DID' if survived else 'did NOT'} survive it. The broker's "
                "report is honest either way (it never promised survival), but "
                "a restart here can cost live terminals.",
                stacklevel=1)
            assert promised != "guaranteed"
            if survived:
                assert entry_after["pid"] == shell_pid, entry_after
    finally:
        _reap_agent(agent_pid, shell_pid)
        broker.stop()


def test_a_restart_is_refused_without_the_gate(tmp_path):
    """Being authenticated is not being allowed.

    The same token that gets a 202 out of the broker above gets a 503 here, and
    the only difference is the absent ``restart_enabled`` key -- so it is the
    gate that permits a restart, not the credential. The mechanism is still
    reported as ``supervisor``: this broker COULD restart, it is simply not
    allowed to."""
    broker = _start_supervised_broker(tmp_path / "gated-off",
                                      restart_enabled=None)
    try:
        status, info = broker.info()
        assert status == 200, (
            "the token must genuinely authenticate here, or the 503 below "
            f"proves nothing: {status} {info}")
        gate = info["restart"]
        assert gate["configured"] is False, gate
        assert gate["available"] is False, gate
        assert gate["reason_code"] == "restart-disabled", gate
        assert gate["mechanism"] == "supervisor", (
            "this broker runs under the supervisor, so the ONLY thing refusing "
            f"the restart must be the gate: {gate}\n{broker.tail()}")
        boot_before = gate["bootId"]

        status, payload = _http("POST", f"{broker.base}/restart", body=b"{}",
                                headers=_AUTH, timeout=30)
        assert status == 503, f"{status} {payload}\n{broker.tail()}"
        assert payload["error"] == "restart_disabled", payload
        assert payload["reason_code"] == "restart-disabled", payload
        assert payload["restart"]["configured"] is False, payload

        # A refusal must not be a stealth restart: same process, same bootId,
        # and the broker is not left quiesced.
        time.sleep(1.0)
        status, info = broker.info()
        assert status == 200, info
        assert info["restart"]["bootId"] == boot_before, info
        assert info["restart"]["lifecycle"] == "running", info
        assert broker.proc.poll() is None, "the refused restart stopped it"
    finally:
        broker.stop()
