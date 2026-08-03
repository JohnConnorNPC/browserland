"""The broker's own supervisor (GH#183).

``python -m webterm.broker`` is now a stable parent that spawns
``python -m webterm.broker --worker`` and relaunches it on request. What is
worth testing here is not "does a broker start" -- test_broker_e2e already
spawns real brokers through this exact path, and its staying green is the proof
that the ``--worker`` split did not change normal startup -- but the four
properties the loop exists for:

* **authorization.** Exit 75 is a REQUEST. Honoured only with an intent
  sentinel carrying this supervisor boot's nonce; an unarmed 75, or one armed
  with the wrong nonce, is a crash and is treated as one.
* **the budget.** A worker that always exits 75 must terminate the supervisor,
  visibly, instead of spinning; and the budget must reset only after a worker
  that actually stayed up, never merely on spawn.
* **capability.** Every "cannot restart" answer carries its own reason code,
  including the one that matters most -- inherited supervisor variables with a
  PPID that does not match, which is the case that would otherwise lie.
* **the bind failure.** A port clash stops the supervisor rather than becoming
  an infinite relaunch loop.

The loop is driven by a trivial fake child (a ~30 line script), never a real
broker: a real broker would make each of these tests a multi-second startup and
would test Sanic rather than the supervisor. The one place a real broker IS
spawned is the orphan test, because "kill the supervisor, does the worker go
too" cannot be faked.
"""

from __future__ import annotations

import json
import os
import re
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]

from webterm.broker import supervise  # noqa: E402 (after the sys.path fixup)


# --------------------------------------------------------------------------- #
# the fake worker
# --------------------------------------------------------------------------- #

# argv: <counter> <limit> <sleep_s> <arm: good|none|bad> <final_exit_code>
#
# Exits EXIT_RESTART for the first <limit> runs, then <final_exit_code>. The
# counter file is what makes "how many times was this relaunched" observable
# from the test; it is written BEFORE the sleep so a killed child still counts.
_FAKE_CHILD = '''\
import json
import os
import sys
import time

from webterm.broker import supervise as sv

counter, limit, sleep_s, arm, final = (
    sys.argv[1], int(sys.argv[2]), float(sys.argv[3]), sys.argv[4],
    int(sys.argv[5]))

n = 0
try:
    with open(counter) as fh:
        n = int(fh.read().strip() or "0")
except (OSError, ValueError):
    pass
n += 1
with open(counter, "w") as fh:
    fh.write(str(n))

if n == 1:
    # One report of what the supervisor actually handed us. This is the only
    # way to see the real env + PPID contract from inside a real child.
    run_dir = os.environ.get(sv.ENV_RUN_DIR)
    with open(counter + ".cap", "w") as fh:
        json.dump({
            "cap": sv.worker_capability(),
            "ppid": os.getppid(),
            "pid_env": os.environ.get(sv.ENV_SUPERVISOR_PID),
            "nonce_len": len(os.environ.get(sv.ENV_SUPERVISOR_NONCE) or ""),
            "run_dir": run_dir,
            "run_dir_exists": bool(run_dir) and os.path.isdir(run_dir),
        }, fh)

if sleep_s:
    time.sleep(sleep_s)

if n > limit:
    sys.exit(final)

if arm == "good":
    if not sv.arm_restart():
        sys.exit(9)          # the writer failed; fail loudly rather than loop
elif arm == "bad":
    # A sentinel that exists but carries somebody else's nonce.
    with open(os.path.join(os.environ[sv.ENV_RUN_DIR], sv.INTENT_FILENAME),
              "w") as fh:
        fh.write("0" * 32)
sys.exit(sv.EXIT_RESTART)
'''


def _child_env() -> dict:
    env = dict(os.environ)
    # The fake child imports webterm.broker.supervise (only stdlib underneath,
    # so it stays a ~100ms process).
    env["PYTHONPATH"] = str(REPO) + os.pathsep + env.get("PYTHONPATH", "")
    return env


def _drive(tmp_path, *, limit: int, arm: str = "good", sleep: float = 0.0,
           final: int = 0, **kwargs):
    """Run the real supervisor loop against the fake child.

    Returns ``(exit_code, spawn_count, log_lines)``."""
    script = tmp_path / "fake_worker.py"
    script.write_text(_FAKE_CHILD, encoding="utf-8")
    counter = tmp_path / "count"
    cmd = [sys.executable, str(script), str(counter), str(limit), str(sleep),
           arm, str(final)]
    logs = []
    kwargs.setdefault("backoff_base", 0.0)     # no sleeping in the unit tests
    code = supervise.supervise(
        [], child_cmd=cmd, env=_child_env(), log=logs.append,
        # pytest owns the process's signal handlers; installing ours in-process
        # is tested separately and deliberately off here.
        install_signals=False, **kwargs)
    try:
        count = int(counter.read_text(encoding="utf-8").strip() or "0")
    except (OSError, ValueError):
        count = 0
    return code, count, logs


def _cap_report(tmp_path):
    return json.loads((tmp_path / "count.cap").read_text(encoding="utf-8"))


# --------------------------------------------------------------------------- #
# the exit-code vocabulary
# --------------------------------------------------------------------------- #

def test_exit_codes_are_distinct_and_out_of_the_way():
    codes = {supervise.EXIT_RESTART, supervise.EXIT_ADDR_IN_USE,
             supervise.EXIT_SUPERVISOR_FAILED}
    assert len(codes) == 3
    assert supervise.EXIT_RESTART == 75
    assert supervise.EXIT_ADDR_IN_USE == 78
    # The codes this repo already uses: a restart request must never collide
    # with an ordinary error, a failed audit, or a Ctrl-C.
    assert codes.isdisjoint({0, 1, 2, 130})


# --------------------------------------------------------------------------- #
# authorization: exit 75 is a request, not a fact
# --------------------------------------------------------------------------- #

def test_armed_75_is_relaunched(tmp_path):
    code, count, _ = _drive(tmp_path, limit=1, arm="good")
    assert count == 2          # the first run asked; the second ran and stopped
    assert code == 0


def test_unarmed_75_is_a_crash_and_is_not_relaunched(tmp_path):
    """The authorization property. Without this, ANY crash that happens to exit
    75 -- a library, a signal, a wrapper -- becomes a restart nobody asked
    for."""
    code, count, logs = _drive(tmp_path, limit=5, arm="none")
    assert count == 1                       # never relaunched
    assert code == supervise.EXIT_RESTART   # propagated, not swallowed
    assert any("without arming" in line for line in logs), logs


def test_sentinel_with_the_wrong_nonce_is_refused(tmp_path):
    """A sentinel from an earlier supervisor boot (or from anything else that
    can write the file) must not authorize this one's relaunch."""
    code, count, logs = _drive(tmp_path, limit=5, arm="bad")
    assert count == 1
    assert code == supervise.EXIT_RESTART
    assert any("without arming" in line for line in logs), logs


def test_a_consumed_sentinel_authorizes_exactly_one_relaunch(tmp_path):
    """Two runs of the fake child, one sentinel each: the sentinel is deleted
    on read, so run 2 has to arm again to be relaunched again."""
    code, count, _ = _drive(tmp_path, limit=2, arm="good")
    assert count == 3
    assert code == 0


# --------------------------------------------------------------------------- #
# the budget (A10)
# --------------------------------------------------------------------------- #

def test_a_worker_that_always_exits_75_hits_the_budget(tmp_path):
    code, count, logs = _drive(tmp_path, limit=10 ** 6, arm="good",
                               max_restarts=3, ready_seconds=30.0)
    # One initial spawn plus exactly max_restarts relaunches, then it gives up.
    assert count == 4
    assert code == supervise.EXIT_SUPERVISOR_FAILED
    assert code != 0                       # the failure is visible to the caller
    assert any("budget exhausted" in line for line in logs), logs


def test_the_budget_resets_only_for_a_worker_that_came_up(tmp_path):
    """Two runs of the same scenario, differing ONLY in how long the worker
    lives. The long-lived one is allowed more relaunches than the budget; the
    short-lived one is not. If the reset happened on spawn instead, the second
    half of this test would never terminate on its own."""
    long_lived = tmp_path / "up"
    long_lived.mkdir()
    code, count, _ = _drive(long_lived, limit=2, arm="good", sleep=1.2,
                            max_restarts=1, ready_seconds=0.8)
    assert count == 3, "a worker that stayed up should refill the budget"
    assert code == 0

    crash_loop = tmp_path / "down"
    crash_loop.mkdir()
    code, count, logs = _drive(crash_loop, limit=10 ** 6, arm="good", sleep=0.0,
                               max_restarts=1, ready_seconds=0.8)
    assert count == 2, "a fast death must NOT refill the budget"
    assert code == supervise.EXIT_SUPERVISOR_FAILED
    assert any("budget exhausted" in line for line in logs), logs


def test_backoff_grows_between_fast_deaths(tmp_path):
    """The backoff doubles per consecutive fast death and then caps. It is
    slept in slices (so a stop signal can cut it short), hence the assertion on
    the announced delay plus the total actually slept."""
    slept = []
    _, _, logs = _drive(tmp_path, limit=10 ** 6, arm="good", max_restarts=4,
                        ready_seconds=30.0, backoff_base=0.5, backoff_max=2.0,
                        sleeper=slept.append)
    announced = [float(m) for m in
                 re.findall(r"relaunching in ([0-9.]+)s", "\n".join(logs))]
    assert announced == [0.5, 1.0, 2.0, 2.0]
    assert sum(slept) == pytest.approx(5.5)
    assert max(slept) <= supervise.POLL_INTERVAL   # sliced, not one long nap


def test_a_stop_signal_during_backoff_prevents_the_next_spawn(tmp_path):
    """A SIGTERM/SIGINT that lands while the supervisor is backing off must
    stop it there. Sleeping the delay out and THEN spawning a worker only to
    kill it is both slower and wrong."""
    import signal as signal_mod

    script = tmp_path / "fake_worker.py"
    script.write_text(_FAKE_CHILD, encoding="utf-8")
    counter = tmp_path / "count"
    fired = []

    def sleeper(_seconds):
        if not fired:
            fired.append(True)
            # Delivered to THIS process, so the supervisor's own handler runs
            # exactly as it would for a real stop.
            signal_mod.raise_signal(signal_mod.SIGINT)

    logs = []
    code = supervise.supervise(
        [], child_cmd=[sys.executable, str(script), str(counter),
                       "1000000", "0", "good", "0"],
        env=_child_env(), log=logs.append, install_signals=True,
        backoff_base=1.0, sleeper=sleeper)
    assert code == 128 + int(signal_mod.SIGINT)      # 130, the shell's Ctrl-C
    assert counter.read_text(encoding="utf-8").strip() == "1"
    assert any("stopping on signal" in line for line in logs), logs


# --------------------------------------------------------------------------- #
# other exit codes
# --------------------------------------------------------------------------- #

def test_addr_in_use_stops_instead_of_relaunching(tmp_path):
    """A port clash must never become an infinite restart loop: the port is
    taken, and the next attempt fails identically."""
    code, count, logs = _drive(tmp_path, limit=0,
                               final=supervise.EXIT_ADDR_IN_USE)
    assert count == 1
    assert code == supervise.EXIT_ADDR_IN_USE
    assert any("bind" in line for line in logs), logs


def test_an_ordinary_exit_code_is_propagated(tmp_path):
    for expected in (0, 1, 2, 3):
        directory = tmp_path / f"rc{expected}"
        directory.mkdir()
        code, count, _ = _drive(directory, limit=0, final=expected)
        assert (code, count) == (expected, 1)


def test_a_spawn_failure_is_reported_not_retried(tmp_path):
    logs = []
    code = supervise.supervise(
        [], child_cmd=[str(tmp_path / "definitely-not-an-executable")],
        env=_child_env(), log=logs.append, install_signals=False)
    assert code == supervise.EXIT_SUPERVISOR_FAILED
    assert any("could not start" in line for line in logs), logs


# --------------------------------------------------------------------------- #
# the supervisor -> worker contract
# --------------------------------------------------------------------------- #

def test_the_worker_is_a_direct_child_with_the_three_variables(tmp_path):
    """The env the worker sees, reported by the worker itself: our pid, a fresh
    nonce, a run dir that exists -- and a capability probe that says
    ``supervisor`` because the PPID genuinely matches."""
    code, count, _ = _drive(tmp_path, limit=0, final=0)
    assert (code, count) == (0, 1)
    report = _cap_report(tmp_path)
    assert report["pid_env"] == str(os.getpid())
    assert report["ppid"] == os.getpid(), "the worker must be a DIRECT child"
    assert report["nonce_len"] == 32          # secrets.token_hex(16)
    assert report["run_dir_exists"] is True
    assert report["cap"] == {"supported": True, "mechanism": "supervisor",
                             "reason_code": None}


def test_the_run_dir_is_removed_when_the_supervisor_exits(tmp_path):
    _drive(tmp_path, limit=0, final=0)
    run_dir = _cap_report(tmp_path)["run_dir"]
    assert run_dir
    assert not os.path.isdir(run_dir)


def test_each_supervisor_boot_gets_a_fresh_nonce(tmp_path):
    first = tmp_path / "a"
    first.mkdir()
    second = tmp_path / "b"
    second.mkdir()
    _drive(first, limit=0)
    _drive(second, limit=0)
    # Only the length is observable from the child report, so compare the run
    # dirs too: distinct dirs and distinct nonces are the same invariant --
    # nothing from one boot can authorize the next.
    assert _cap_report(first)["run_dir"] != _cap_report(second)["run_dir"]


def test_signal_handlers_are_restored(tmp_path):
    import signal as signal_mod

    before = {name: signal_mod.getsignal(getattr(signal_mod, name))
              for name in ("SIGTERM", "SIGINT")
              if getattr(signal_mod, name, None) is not None}
    script = tmp_path / "fake_worker.py"
    script.write_text(_FAKE_CHILD, encoding="utf-8")
    supervise.supervise(
        [], child_cmd=[sys.executable, str(script), str(tmp_path / "count"),
                       "0", "0", "none", "0"],
        env=_child_env(), log=lambda _m: None, install_signals=True)
    after = {name: signal_mod.getsignal(getattr(signal_mod, name))
             for name in before}
    assert after == before


# --------------------------------------------------------------------------- #
# the intent sentinel, directly
# --------------------------------------------------------------------------- #

def test_arm_and_consume_roundtrip(tmp_path):
    run_dir = str(tmp_path / "run")
    assert supervise.arm_restart(run_dir, "abc123") is True
    assert supervise.restart_armed(run_dir, "abc123") is True
    assert supervise.consume_restart_intent(run_dir, "abc123") is True
    # Consumed: the file is gone, and a second read authorizes nothing.
    assert supervise.restart_armed(run_dir, "abc123") is False
    assert supervise.consume_restart_intent(run_dir, "abc123") is False


def test_consume_deletes_even_a_mismatched_sentinel(tmp_path):
    run_dir = str(tmp_path / "run")
    supervise.arm_restart(run_dir, "the-wrong-nonce")
    assert supervise.consume_restart_intent(run_dir, "the-right-nonce") is False
    # It must not sit there waiting for the boot whose nonce it happens to be.
    assert not (Path(run_dir) / supervise.INTENT_FILENAME).exists()


def test_restart_armed_checks_the_content_not_just_the_file(tmp_path):
    """A worker asks this before choosing to exit 75. Answering yes on a
    sentinel the supervisor will reject turns debris into a broker that shuts
    down and stays down."""
    run_dir = Path(tmp_path / "run")
    run_dir.mkdir()
    (run_dir / supervise.INTENT_FILENAME).write_text("someone-elses-nonce",
                                                     encoding="ascii")
    assert supervise.restart_armed(str(run_dir), "mine") is False
    (run_dir / supervise.INTENT_FILENAME).write_text("", encoding="ascii")
    assert supervise.restart_armed(str(run_dir), "mine") is False
    supervise.arm_restart(str(run_dir), "mine")
    assert supervise.restart_armed(str(run_dir), "mine") is True


def _no_unlink(_self, *_a, **_k):
    raise PermissionError("held open by something else")


def test_an_undeletable_sentinel_is_blanked_instead(tmp_path, monkeypatch):
    """A sentinel that survives the read would authorize the NEXT 75 too, and a
    worker that crashed 75 twice would be relaunched on an intent it expressed
    once. Windows can refuse the unlink (another process holding the file
    without delete sharing), so blanking is the fallback -- an empty sentinel
    matches no nonce, which is the same guarantee."""
    run_dir = str(tmp_path / "run")
    supervise.arm_restart(run_dir, "abc123")
    monkeypatch.setattr(Path, "unlink", _no_unlink)
    assert supervise.consume_restart_intent(run_dir, "abc123") is True
    monkeypatch.undo()
    assert supervise.restart_armed(run_dir, "abc123") is False
    assert supervise.consume_restart_intent(run_dir, "abc123") is False


def test_a_sentinel_that_cannot_be_destroyed_at_all_is_refused(tmp_path,
                                                               monkeypatch):
    """Neither removable nor blankable: refuse the restart rather than accept
    an authorization that stays valid forever."""
    import builtins

    run_dir = str(tmp_path / "run")
    supervise.arm_restart(run_dir, "abc123")
    real_open = builtins.open

    def no_writes(file, mode="r", *args, **kwargs):
        if "w" in mode or "a" in mode:
            raise PermissionError("read-only")
        return real_open(file, mode, *args, **kwargs)

    # Path.read_text goes through io.open, so the READ still works and only the
    # destroy path is broken -- which is the case under test.
    monkeypatch.setattr(builtins, "open", no_writes)
    monkeypatch.setattr(Path, "unlink", _no_unlink)
    assert supervise.consume_restart_intent(run_dir, "abc123") is False


def test_arm_restart_without_a_supervisor_says_so(monkeypatch):
    monkeypatch.delenv(supervise.ENV_RUN_DIR, raising=False)
    monkeypatch.delenv(supervise.ENV_SUPERVISOR_NONCE, raising=False)
    # False is the caller's cue that exiting 75 would simply stop the broker.
    assert supervise.arm_restart() is False
    assert supervise.restart_armed() is False


def test_consume_on_a_missing_run_dir_is_false_not_an_error(tmp_path):
    assert supervise.consume_restart_intent(
        str(tmp_path / "nope"), "abc") is False


# --------------------------------------------------------------------------- #
# capability reporting (A13)
# --------------------------------------------------------------------------- #

def test_capability_without_the_variables():
    cap = supervise.worker_capability(env={}, getppid=lambda: 1,
                                      runner=lambda argv: "always",
                                      cgroup_reader=lambda: None)
    assert cap == {"supported": False, "mechanism": "none",
                   "reason_code": supervise.REASON_NO_SUPERVISOR}


def test_capability_with_inherited_variables_and_a_foreign_ppid():
    """The case that makes the PPID check load-bearing: an agent shell inherits
    the variables, and a broker started by hand inside it would otherwise claim
    it is supervised and then die on the first restart."""
    env = {supervise.ENV_SUPERVISOR_PID: "4242",
           supervise.ENV_SUPERVISOR_NONCE: "deadbeef"}
    cap = supervise.worker_capability(env=env, getppid=lambda: 99,
                                      runner=lambda argv: "always",
                                      cgroup_reader=lambda: None)
    assert cap["supported"] is False
    assert cap["reason_code"] == supervise.REASON_PPID_MISMATCH
    # Distinct from "no supervisor at all" -- the operator needs to know the
    # variables are present but stale.
    assert cap["reason_code"] != supervise.REASON_NO_SUPERVISOR


def test_capability_with_a_matching_ppid():
    env = {supervise.ENV_SUPERVISOR_PID: "4242",
           supervise.ENV_SUPERVISOR_NONCE: "deadbeef"}
    cap = supervise.worker_capability(env=env, getppid=lambda: 4242,
                                      cgroup_reader=lambda: None)
    assert cap == {"supported": True, "mechanism": "supervisor",
                   "reason_code": None}


def test_capability_needs_both_variables():
    """Half a pair is debris, not a supervisor."""
    for env in ({supervise.ENV_SUPERVISOR_PID: "4242"},
                {supervise.ENV_SUPERVISOR_NONCE: "deadbeef"}):
        cap = supervise.worker_capability(env=env, getppid=lambda: 4242,
                                          cgroup_reader=lambda: None)
        assert cap["supported"] is False
        assert cap["reason_code"] == supervise.REASON_NO_SUPERVISOR


_CGROUP_V2 = "0::/system.slice/webterm-broker.service\n"
_CGROUP_V1 = ("11:devices:/system.slice/webterm-broker.service\n"
              "1:name=systemd:/system.slice/webterm-broker.service\n")
_CGROUP_USER = ("0::/user.slice/user-1000.slice/user@1000.service/app.slice/"
                "webterm-broker.service\n")


@pytest.mark.parametrize("policy", ["always", "on-failure", "ALWAYS"])
def test_capability_under_systemd_with_a_restarting_policy(policy):
    seen = []

    def runner(argv):
        seen.append(list(argv))
        return policy

    cap = supervise.worker_capability(
        env={supervise.ENV_INVOCATION_ID: "b7f0"}, getppid=lambda: 1,
        runner=runner, cgroup_reader=lambda: _CGROUP_V2)
    assert cap == {"supported": True, "mechanism": "systemd",
                   "reason_code": None}
    # The policy is read for the unit we resolved, not guessed.
    assert seen == [["systemctl", "show", "-p", "Restart", "--value",
                     "webterm-broker.service"]]


@pytest.mark.parametrize("policy", ["no", "on-abort", "on-abnormal",
                                    "on-watchdog", "on-success"])
def test_capability_under_systemd_that_will_not_restart_us(policy):
    """``$INVOCATION_ID`` proves systemd started us and nothing more. A unit
    with any of these policies does NOT come back from a non-zero exit, and
    claiming otherwise leaves the broker dead after a 'restart'."""
    cap = supervise.worker_capability(
        env={supervise.ENV_INVOCATION_ID: "b7f0"}, getppid=lambda: 1,
        runner=lambda argv: policy, cgroup_reader=lambda: _CGROUP_V2)
    assert cap["supported"] is False
    assert cap["reason_code"] == supervise.REASON_SYSTEMD_NO_RESTART


@pytest.mark.parametrize("runner,cgroup", [
    (lambda argv: None, lambda: _CGROUP_V2),      # systemctl missing/failed
    (lambda argv: "", lambda: _CGROUP_V2),        # empty answer
    (lambda argv: "always", lambda: None),        # no cgroup file
    (lambda argv: "always", lambda: "0::/\n"),    # no unit in the cgroup path
])
def test_capability_when_the_systemd_policy_is_unreadable(runner, cgroup):
    """"We could not check" must never render as "yes"."""
    cap = supervise.worker_capability(
        env={supervise.ENV_INVOCATION_ID: "b7f0"}, getppid=lambda: 1,
        runner=runner, cgroup_reader=cgroup)
    assert cap["supported"] is False
    assert cap["reason_code"] == supervise.REASON_SYSTEMD_POLICY_UNKNOWN


def test_capability_never_raises():
    """A probe that throws is worse than one that says no."""
    def boom(*_args, **_kwargs):
        raise RuntimeError("probe exploded")

    cap = supervise.worker_capability(
        env={supervise.ENV_INVOCATION_ID: "b7f0"}, getppid=boom,
        runner=boom, cgroup_reader=boom)
    assert cap["supported"] is False
    assert cap["reason_code"] == supervise.REASON_PROBE_FAILED
    assert cap["mechanism"] == supervise.MECHANISM_NONE


def test_every_unsupported_cause_has_its_own_reason_code():
    reasons = {supervise.REASON_NO_SUPERVISOR,
               supervise.REASON_PPID_MISMATCH,
               supervise.REASON_SYSTEMD_NO_RESTART,
               supervise.REASON_SYSTEMD_POLICY_UNKNOWN,
               supervise.REASON_PROBE_FAILED}
    assert len(reasons) == 5
    assert all(isinstance(r, str) and r for r in reasons)


def test_a_user_unit_is_queried_on_the_user_bus():
    """``systemctl show`` without ``--user`` asks the SYSTEM manager, which has
    never heard of a user unit and answers for a stub with Restart=no -- a
    false "cannot restart" for anyone running the broker as a user service."""
    seen = []

    def runner(argv):
        seen.append(list(argv))
        return "always"

    cap = supervise.worker_capability(
        env={supervise.ENV_INVOCATION_ID: "b7f0"}, getppid=lambda: 1,
        runner=runner, cgroup_reader=lambda: _CGROUP_USER)
    assert cap["supported"] is True
    assert seen == [["systemctl", "--user", "show", "-p", "Restart",
                     "--value", "webterm-broker.service"]]


def test_a_system_unit_is_not_queried_on_the_user_bus():
    seen = []
    supervise.worker_capability(
        env={supervise.ENV_INVOCATION_ID: "b7f0"}, getppid=lambda: 1,
        runner=lambda argv: seen.append(list(argv)) or "always",
        cgroup_reader=lambda: _CGROUP_V2)
    assert "--user" not in seen[0]


@pytest.mark.parametrize("text,expected", [
    (_CGROUP_V2, "webterm-broker.service"),
    (_CGROUP_V1, "webterm-broker.service"),
    # Nested under user@1000.service: the LAST segment is the unit we are, the
    # outer one is a slice we are inside.
    (_CGROUP_USER, "webterm-broker.service"),
    ("0::/system.slice/webterm\\x2dbroker.service\n",
     "webterm\\x2dbroker.service"),
    ("0::/\n", None),
    ("", None),
    (None, None),
])
def test_systemd_unit_resolution(text, expected):
    assert supervise.systemd_unit(text) == expected


# --------------------------------------------------------------------------- #
# bind-failure classification (A19)
# --------------------------------------------------------------------------- #

class _WrappedServerError(Exception):
    """Stand-in for sanic.exceptions.ServerError, which is what actually
    escapes ``app.run()`` -- the errno is only in the context chain."""


def _wrapped(errno_value):
    try:
        raise OSError(errno_value, "boom")
    except OSError:
        try:
            raise _WrappedServerError("Sanic server could not start")
        except _WrappedServerError as exc:
            return exc


def test_bind_failure_is_found_through_the_exception_chain():
    for value in (48, 98, 10048):
        assert supervise.bind_failure_reason(_wrapped(value)) \
            == supervise.BIND_IN_USE, value
        assert supervise.is_addr_in_use(_wrapped(value)) is True, value


def test_a_refused_address_is_a_bind_failure_too():
    """Measured on Windows: because Sanic's listening socket sets SO_REUSEADDR,
    an occupied port reports WSAEACCES (10013), not WSAEADDRINUSE. It is still
    a bind that will fail identically next time, so it still must not be
    retried -- but it is a different fact and gets its own message."""
    for value in (13, 10013):
        assert supervise.bind_failure_reason(_wrapped(value)) \
            == supervise.BIND_DENIED, value
        assert supervise.is_addr_in_use(_wrapped(value)) is False, value


def test_an_unrelated_error_is_not_a_bind_failure():
    assert supervise.bind_failure_reason(_wrapped(2)) is None    # ENOENT
    assert supervise.bind_failure_reason(RuntimeError("nope")) is None
    assert supervise.bind_failure_reason(_WrappedServerError("bare")) is None


def test_the_chain_walk_survives_a_cycle():
    first = RuntimeError("a")
    second = RuntimeError("b")
    first.__context__ = second
    second.__context__ = first
    assert supervise.bind_failure_reason(first) is None


def test_a_real_worker_reports_the_bind_failure_as_its_exit_code(tmp_path):
    """End to end through the actual entrypoint: hold the port, start a real
    ``--worker``, and require the dedicated exit code rather than a traceback
    and a generic 1 (which the supervisor could not tell from a crash)."""
    holder = socket.socket()
    holder.bind(("127.0.0.1", 0))
    holder.listen(5)
    port = holder.getsockname()[1]
    env = _child_env()
    # Explicit token: a broker with none configured MINTS one into its cwd.
    env["WEB_TERMINAL_TOKEN"] = "supervisor-test-token"
    env.pop("BROWSERLAND_BROKER_URL", None)
    try:
        out = subprocess.run(
            [sys.executable, "-m", "webterm.broker", "--worker", "--headless",
             "--host", "127.0.0.1", "--port", str(port)],
            cwd=str(tmp_path), env=env, capture_output=True, text=True,
            timeout=120)
    finally:
        holder.close()
    assert out.returncode == supervise.EXIT_ADDR_IN_USE, out.stderr[-2000:]
    assert "cannot bind" in out.stderr


# --------------------------------------------------------------------------- #
# the real entrypoint
# --------------------------------------------------------------------------- #

def test_worker_is_not_advertised_in_help():
    """--worker is internal: a hand-run worker is a broker that cannot restart
    itself, which is the state the supervisor exists to remove."""
    out = subprocess.run([sys.executable, "-m", "webterm.broker", "--help"],
                         cwd=str(REPO), env=_child_env(), capture_output=True,
                         text=True, timeout=120)
    assert out.returncode == 0, out.stderr[-2000:]
    assert "--worker" not in out.stdout
    assert "--print-token" in out.stdout     # the real flags are still there


def test_print_token_answers_without_spawning_anything(tmp_path):
    """The one-shot subcommands run BEFORE the supervisor split: asking for a
    token must not start a broker, and must not hang waiting on one."""
    env = _child_env()
    env.pop("WEB_TERMINAL_TOKEN", None)
    env.pop("WEB_TERMINAL_CONFIG", None)
    out = subprocess.run(
        [sys.executable, "-m", "webterm.broker", "--print-token"],
        cwd=str(tmp_path), env=env, capture_output=True, text=True, timeout=120)
    # No token anywhere in this empty cwd -> the documented "nothing yet" exit,
    # and crucially NOT a mint, a server, or a supervisor.
    assert out.returncode == 1, out.stdout + out.stderr
    assert "no auth token yet" in out.stderr
    assert not list(tmp_path.iterdir()), "--print-token must not mint anything"


def _wait_for_port(port: int, *, open_: bool, timeout: float) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                if open_:
                    return True
        except OSError:
            if not open_:
                return True
        time.sleep(0.2)
    return False


_BREAKAWAY_CHILD = '''\
import subprocess
import sys
import time

# What launcher.py does for every agent: detached, own process group, and OUT
# of the parent's job object so it survives a broker restart.
CREATE_BREAKAWAY_FROM_JOB = 0x01000000
flags = (subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
         | CREATE_BREAKAWAY_FROM_JOB)
marker, started = sys.argv[1], sys.argv[2]
grandchild = [sys.executable, "-c",
              "import sys, time; time.sleep(3); "
              "open(sys.argv[1], 'w').write('alive')", marker]
try:
    subprocess.Popen(grandchild, creationflags=flags)
except OSError:
    # The fallback launcher.py takes when breakaway is DENIED. Recorded as a
    # different marker so the test can tell the two apart.
    open(started + ".nobreakaway", "w").write("1")
    subprocess.Popen(grandchild, creationflags=(
        subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP))
open(started, "w").write("1")
time.sleep(300)
'''


@pytest.mark.skipif(os.name != "nt", reason="job objects are Windows-only")
def test_an_agent_can_still_break_out_of_the_supervisors_job(tmp_path):
    """The job object must not quietly cancel CREATE_BREAKAWAY_FROM_JOB.

    Without JOB_OBJECT_LIMIT_BREAKAWAY_OK, Windows fails that CreateProcess
    with ERROR_ACCESS_DENIED -- and launcher.py's fallback then spawns the
    agent WITHOUT breakaway, so it lands inside the job and dies with the
    supervisor. Agents surviving a broker restart is documented behaviour, and
    this is the test that it survived being supervised."""
    script = tmp_path / "breakaway.py"
    script.write_text(_BREAKAWAY_CHILD, encoding="utf-8")
    marker = tmp_path / "grandchild-alive"
    started = tmp_path / "worker-started"
    driver = (
        "import sys\n"
        "from webterm.broker import supervise as sv\n"
        "sys.exit(sv.supervise([], child_cmd=%r))\n"
        % ([sys.executable, str(script), str(marker), str(started)],))
    proc = subprocess.Popen([sys.executable, "-c", driver], cwd=str(tmp_path),
                            env=_child_env(), stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL)
    try:
        deadline = time.time() + 60
        while not started.exists() and time.time() < deadline:
            assert proc.poll() is None, "the driver exited early"
            time.sleep(0.1)
        assert started.exists(), "the fake worker never started"
        assert not (tmp_path / "worker-started.nobreakaway").exists(), \
            "CREATE_BREAKAWAY_FROM_JOB was DENIED: the job object is missing " \
            "JOB_OBJECT_LIMIT_BREAKAWAY_OK, so every agent would be killed " \
            "with the supervisor"
        # Kill the supervisor outright; the job takes the worker, and the
        # broken-away grandchild must live on to write its marker.
        proc.kill()
        proc.wait(timeout=30)
        deadline = time.time() + 30
        while not marker.exists() and time.time() < deadline:
            time.sleep(0.2)
        assert marker.exists(), \
            "the broken-away grandchild died with the supervisor's job"
    finally:
        if proc.poll() is None:
            proc.kill()


def test_killing_the_supervisor_takes_the_worker_with_it(tmp_path):
    """Before #183, killing ``python -m webterm.broker`` killed the server.
    That has to stay true: a worker that outlives its supervisor is a broker
    holding the port that nothing can stop or replace.

    On Linux the shipped path is systemd, whose KillMode=control-group already
    takes the whole cgroup down; the supervisor also forwards SIGTERM. On
    Windows there is no equivalent, so the worker is put in a kill-on-close job
    object -- and this is the test of that."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    env = _child_env()
    env["WEB_TERMINAL_TOKEN"] = "supervisor-test-token"
    env.pop("BROWSERLAND_BROKER_URL", None)
    proc = subprocess.Popen(
        [sys.executable, "-m", "webterm.broker", "--headless",
         "--host", "127.0.0.1", "--port", str(port)],
        cwd=str(tmp_path), env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        assert _wait_for_port(port, open_=True, timeout=60), \
            "the supervised broker never came up"
        # kill(), not terminate(): the hard case, where nothing can be
        # forwarded and only the OS can clean up.
        proc.kill()
        proc.wait(timeout=30)
        assert _wait_for_port(port, open_=False, timeout=30), \
            "the worker outlived its supervisor and still holds the port"
    finally:
        if proc.poll() is None:
            proc.kill()


# --------------------------------------------------------------------------- #
# the deploy journal (A23)
# --------------------------------------------------------------------------- #
#
# The seam under test: the WORKER journals a deploy (update.begin_deploy)
# before its apply-restart, and the SUPERVISOR -- the only process that
# outlives the restart -- adjudicates the next worker generation into exactly
# one of came-up-ready-on-target / came-up-on-wrong-sha / never-came-up, then
# hands (record, outcome) to the deploy_hook exactly once. The hook is where
# a later atom (A28) attaches the actual rollback; here it only records.

OLD_SHA = "a" * 40
NEW_SHA = "b" * 40


# ---- pure record + classification --------------------------------------------

def test_deploy_record_roundtrip():
    record = supervise.build_deploy_record(
        OLD_SHA, NEW_SHA, {"brokerId": "b1", "port": 4445}, "op-1", now=123.0)
    assert record is not None
    decoded = supervise.decode_deploy_record(
        supervise.encode_deploy_record(record))
    assert decoded == record
    assert decoded["version"] == supervise.DEPLOY_JOURNAL_VERSION
    assert decoded["oldSha"] == OLD_SHA
    assert decoded["targetSha"] == NEW_SHA
    assert decoded["expectedIdentity"] == {"brokerId": "b1", "port": 4445}
    assert decoded["createdAt"] == 123.0


def test_build_deploy_record_refuses_bad_input():
    good = dict(old_sha=OLD_SHA, target_sha=NEW_SHA,
                expected_identity=None, operation_id="op")
    assert supervise.build_deploy_record(**good) is not None
    for over in (dict(old_sha="HEAD"), dict(old_sha=OLD_SHA[:7]),
                 dict(old_sha=OLD_SHA.upper()), dict(old_sha=None),
                 dict(target_sha="main"), dict(target_sha=None),
                 dict(operation_id=""), dict(operation_id="   "),
                 dict(operation_id=None), dict(operation_id=7),
                 dict(expected_identity=object())):
        bad = dict(good)
        bad.update(over)
        assert supervise.build_deploy_record(**bad) is None, over


def test_decode_deploy_record_rejects_garbage():
    for text in ("", "not json", "[]", "42", '"str"', "{}", None,
                 json.dumps({"version": 2, "oldSha": OLD_SHA,
                             "targetSha": NEW_SHA, "operationId": "x"}),
                 json.dumps({"version": 1, "oldSha": "xyz",
                             "targetSha": NEW_SHA, "operationId": "x"}),
                 json.dumps({"version": 1, "oldSha": OLD_SHA,
                             "targetSha": NEW_SHA, "operationId": ""})):
        assert supervise.decode_deploy_record(text) is None, text


def test_classify_deploy_outcomes():
    record = supervise.build_deploy_record(OLD_SHA, NEW_SHA, None, "op")
    ready_on_target = supervise.classify_deploy_outcome(
        record, ready=True, observed_sha=NEW_SHA)
    assert ready_on_target["outcome"] == supervise.DEPLOY_READY_ON_TARGET
    assert ready_on_target["observedSha"] == NEW_SHA
    wrong = supervise.classify_deploy_outcome(record, ready=True,
                                              observed_sha=OLD_SHA)
    assert wrong["outcome"] == supervise.DEPLOY_WRONG_SHA
    assert wrong["observedSha"] == OLD_SHA
    never = supervise.classify_deploy_outcome(record, ready=False,
                                              observed_sha=None)
    assert never["outcome"] == supervise.DEPLOY_NEVER_CAME_UP


def test_an_unreadable_sha_is_never_reported_as_success():
    """R10: success requires the observed sha to equal the target. A worker
    that is up but whose sha cannot be read is NOT a verified deploy."""
    record = supervise.build_deploy_record(OLD_SHA, NEW_SHA, None, "op")
    out = supervise.classify_deploy_outcome(record, ready=True,
                                            observed_sha=None)
    assert out["outcome"] == supervise.DEPLOY_WRONG_SHA
    assert out["detail"] == "sha-unreadable"


def test_deploy_outcome_names_are_pinned():
    """A28 and the UI will switch on these strings; a rename must fail a test
    rather than silently orphan a consumer."""
    assert supervise.DEPLOY_READY_ON_TARGET == "came-up-ready-on-target"
    assert supervise.DEPLOY_WRONG_SHA == "came-up-on-wrong-sha"
    assert supervise.DEPLOY_NEVER_CAME_UP == "never-came-up"


# ---- the journal on disk -----------------------------------------------------

def test_journal_write_read_roundtrip_on_disk(tmp_path):
    record = supervise.build_deploy_record(OLD_SHA, NEW_SHA, "identity", "op")
    assert supervise.write_deploy_journal(str(tmp_path), record) is True
    assert not list(tmp_path.glob("*.tmp")), "no temp debris"
    logs = []
    assert supervise.read_pending_deploy(str(tmp_path), logs.append) == record
    assert logs == []
    # Reading does not consume: still pending until finalized.
    assert supervise.read_pending_deploy(str(tmp_path), logs.append) == record


def test_a_missing_journal_is_quietly_absent(tmp_path):
    logs = []
    assert supervise.read_pending_deploy(str(tmp_path), logs.append) is None
    assert logs == []
    assert not (tmp_path / supervise.DEPLOY_QUARANTINE_FILENAME).exists()


def test_a_corrupt_journal_is_quarantined_loudly_and_reads_absent(tmp_path):
    journal = tmp_path / supervise.DEPLOY_JOURNAL_FILENAME
    journal.write_text("{not json", encoding="utf-8")
    logs = []
    assert supervise.read_pending_deploy(str(tmp_path), logs.append) is None
    assert any("corrupt" in line for line in logs), logs
    assert not journal.exists()
    quarantined = tmp_path / supervise.DEPLOY_QUARANTINE_FILENAME
    assert quarantined.read_text(encoding="utf-8") == "{not json", \
        "the corrupt journal must be preserved as evidence, never deleted"


def test_a_wrong_shape_journal_is_also_quarantined(tmp_path):
    """Valid JSON that fails validation (unknown version, bad shas) is just as
    unadjudicable as garbage bytes -- an older supervisor must quarantine a
    newer journal rather than misread it."""
    journal = tmp_path / supervise.DEPLOY_JOURNAL_FILENAME
    journal.write_text(json.dumps({"version": 99}), encoding="utf-8")
    logs = []
    assert supervise.read_pending_deploy(str(tmp_path), logs.append) is None
    assert (tmp_path / supervise.DEPLOY_QUARANTINE_FILENAME).exists()
    assert any("corrupt" in line for line in logs), logs


def test_the_journal_write_is_atomic_no_file_on_failure(tmp_path, monkeypatch):
    record = supervise.build_deploy_record(OLD_SHA, NEW_SHA, None, "op")

    def boom(_src, _dst):
        raise OSError("simulated replace failure")

    monkeypatch.setattr(supervise.os, "replace", boom)
    assert supervise.write_deploy_journal(str(tmp_path), record) is False
    assert not (tmp_path / supervise.DEPLOY_JOURNAL_FILENAME).exists(), \
        "a failed write must leave NO journal, partial or otherwise"


def test_finalize_persists_one_outcome_and_consumes_the_journal(tmp_path):
    record = supervise.build_deploy_record(OLD_SHA, NEW_SHA, None, "op-9")
    assert supervise.write_deploy_journal(str(tmp_path), record)
    outcome = supervise.classify_deploy_outcome(record, ready=True,
                                                observed_sha=NEW_SHA)
    assert supervise.finalize_deploy(str(tmp_path), record, outcome,
                                     now=456.0) is True
    assert not (tmp_path / supervise.DEPLOY_JOURNAL_FILENAME).exists(), \
        "finalize must consume the journal: one deploy, one adjudication"
    saved = json.loads((tmp_path / supervise.DEPLOY_OUTCOME_FILENAME)
                       .read_text(encoding="utf-8"))
    assert saved["outcome"] == supervise.DEPLOY_READY_ON_TARGET
    assert saved["record"] == record
    assert saved["finalizedAt"] == 456.0
    # ONE current outcome file: a second deploy overwrites, never appends.
    second = supervise.build_deploy_record(OLD_SHA, NEW_SHA, None, "op-10")
    supervise.finalize_deploy(
        str(tmp_path), second,
        supervise.classify_deploy_outcome(second, ready=False,
                                          observed_sha=None))
    saved = json.loads((tmp_path / supervise.DEPLOY_OUTCOME_FILENAME)
                       .read_text(encoding="utf-8"))
    assert saved["record"]["operationId"] == "op-10"


# ---- adjudication through the real loop --------------------------------------

# argv: <counter> <sleep_s> <old_sha> <target_sha>
#
# Run 1 is the pre-update worker: it journals the deploy through the REAL
# production writer (update.begin_deploy), arms, and exits 75 -- the order the
# apply route will use. Every later run is "the new build": it stays up
# sleep_s and exits 0.
_DEPLOY_CHILD = '''\
import os
import sys
import time

from webterm.broker import supervise as sv
from webterm.broker import update

counter, sleep_s, old_sha, target_sha = (
    sys.argv[1], float(sys.argv[2]), sys.argv[3], sys.argv[4])

n = 0
try:
    with open(counter) as fh:
        n = int(fh.read().strip() or "0")
except (OSError, ValueError):
    pass
n += 1
with open(counter, "w") as fh:
    fh.write(str(n))

if n == 1:
    if not update.begin_deploy(old_sha, target_sha,
                               {"brokerId": "test-broker", "port": 0},
                               "op-test-1"):
        sys.exit(9)
    if not sv.arm_restart():
        sys.exit(9)
    sys.exit(sv.EXIT_RESTART)

time.sleep(sleep_s)
sys.exit(0)
'''


def _drive_deploy(tmp_path, *, observed, sleep=1.5, ready=0.5):
    """One full deploy through the real supervisor loop: journal -> armed 75
    -> next generation -> adjudication. ``observed`` is what the injected
    sha_reader reports at ready-time."""
    script = tmp_path / "deploy_worker.py"
    script.write_text(_DEPLOY_CHILD, encoding="utf-8")
    counter = tmp_path / "count"
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    hook_calls = []
    logs = []
    code = supervise.supervise(
        [], child_cmd=[sys.executable, str(script), str(counter), str(sleep),
                       OLD_SHA, NEW_SHA],
        env=_child_env(), log=logs.append, install_signals=False,
        run_dir=str(run_dir), ready_seconds=ready, backoff_base=0.0,
        sha_reader=lambda: observed,
        deploy_hook=lambda record, outcome: hook_calls.append(
            (record, outcome)))
    try:
        count = int(counter.read_text(encoding="utf-8").strip() or "0")
    except (OSError, ValueError):
        count = 0
    return code, count, logs, hook_calls, run_dir


def test_a_deploy_that_comes_back_ready_on_target_is_success(tmp_path):
    """R10 end to end: the SECOND generation -- a fresh pid the supervisor
    itself spawned -- stayed up past ready and reports the target sha."""
    code, count, logs, hooks, run_dir = _drive_deploy(tmp_path,
                                                      observed=NEW_SHA)
    assert (code, count) == (0, 2)
    assert len(hooks) == 1, "the hook fires exactly once per deploy"
    record, outcome = hooks[0]
    assert record["oldSha"] == OLD_SHA
    assert record["targetSha"] == NEW_SHA
    assert record["operationId"] == "op-test-1"
    assert record["expectedIdentity"]["brokerId"] == "test-broker"
    assert outcome["outcome"] == supervise.DEPLOY_READY_ON_TARGET
    assert outcome["observedSha"] == NEW_SHA
    # Finalized: outcome persisted, journal consumed.
    assert not (run_dir / supervise.DEPLOY_JOURNAL_FILENAME).exists()
    saved = json.loads((run_dir / supervise.DEPLOY_OUTCOME_FILENAME)
                       .read_text(encoding="utf-8"))
    assert saved["outcome"] == supervise.DEPLOY_READY_ON_TARGET
    assert saved["record"]["targetSha"] == NEW_SHA
    assert any("adjudicated" in line for line in logs), logs


def test_a_deploy_that_comes_back_on_the_wrong_sha(tmp_path):
    """The worker came up -- but not on the build the journal named. Never
    reported as success; the outcome names what was actually observed."""
    code, count, _, hooks, run_dir = _drive_deploy(tmp_path,
                                                   observed=OLD_SHA)
    assert (code, count) == (0, 2)
    assert len(hooks) == 1
    record, outcome = hooks[0]
    assert record["targetSha"] == NEW_SHA
    assert outcome["outcome"] == supervise.DEPLOY_WRONG_SHA
    assert outcome["observedSha"] == OLD_SHA
    assert not (run_dir / supervise.DEPLOY_JOURNAL_FILENAME).exists()


def test_a_deploy_whose_worker_never_comes_up_is_never_came_up(tmp_path):
    """A journal is pending and every generation exits 75 (armed) without ever
    staying up: the budget runs out, and the supervisor -- the only process
    still alive to observe it -- adjudicates never-came-up."""
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    record = supervise.build_deploy_record(OLD_SHA, NEW_SHA,
                                           {"brokerId": "t"}, "op-nvr")
    assert supervise.write_deploy_journal(str(run_dir), record)
    hooks = []
    code, count, logs = _drive(tmp_path, limit=10 ** 6, arm="good",
                               max_restarts=2, ready_seconds=30.0,
                               run_dir=str(run_dir),
                               sha_reader=lambda: NEW_SHA,
                               deploy_hook=lambda r, o: hooks.append((r, o)))
    assert code == supervise.EXIT_SUPERVISOR_FAILED
    assert len(hooks) == 1, "exactly one adjudication per deploy"
    got_record, outcome = hooks[0]
    assert got_record["operationId"] == "op-nvr"
    assert outcome["outcome"] == supervise.DEPLOY_NEVER_CAME_UP
    assert outcome["detail"] == "restart-budget-exhausted"
    assert not (run_dir / supervise.DEPLOY_JOURNAL_FILENAME).exists()
    saved = json.loads((run_dir / supervise.DEPLOY_OUTCOME_FILENAME)
                       .read_text(encoding="utf-8"))
    assert saved["outcome"] == supervise.DEPLOY_NEVER_CAME_UP


def test_a_plain_crash_during_a_deploy_is_never_came_up(tmp_path):
    """The post-deploy generation dies with an ordinary exit code before
    reaching ready: the supervisor stops, and the deploy is adjudicated
    never-came-up with the exit recorded in the detail."""
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    record = supervise.build_deploy_record(OLD_SHA, NEW_SHA, None, "op-crash")
    assert supervise.write_deploy_journal(str(run_dir), record)
    hooks = []
    code, count, _ = _drive(tmp_path, limit=0, final=1,
                            run_dir=str(run_dir), ready_seconds=30.0,
                            deploy_hook=lambda r, o: hooks.append((r, o)))
    assert (code, count) == (1, 1)
    assert len(hooks) == 1
    assert hooks[0][1]["outcome"] == supervise.DEPLOY_NEVER_CAME_UP
    assert hooks[0][1]["detail"] == "worker-exited-1"


def test_a_non_deploy_restart_adjudicates_nothing(tmp_path):
    """An ordinary armed restart with NO journal must not invent a deploy: no
    hook call, no outcome file, no quarantine."""
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    hooks = []
    code, count, _ = _drive(tmp_path, limit=1, arm="good",
                            run_dir=str(run_dir),
                            deploy_hook=lambda r, o: hooks.append((r, o)))
    assert (code, count) == (0, 2)
    assert hooks == []
    assert not (run_dir / supervise.DEPLOY_OUTCOME_FILENAME).exists()
    assert not (run_dir / supervise.DEPLOY_QUARANTINE_FILENAME).exists()
