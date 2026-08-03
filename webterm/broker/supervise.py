"""GH#183 -- the broker supervises itself.

``python -m webterm.broker`` is no longer the server. It is a small, stable
*supervisor* that spawns ``python -m webterm.broker --worker`` as a direct
child and relaunches it when the worker asks. The server lives in the child;
the supervisor imports nothing that can crash it and does no I/O beyond its
sentinel and deploy-journal files (plus one read-only git query when a deploy
is being adjudicated -- see the deploy journal section).

Why here, and not in the launcher scripts. The obvious design -- wrap
``launchers/run-broker.sh`` / ``run-broker.ps1`` in a relaunch loop -- was
rejected in review, for reasons that are worth keeping written down:

* under systemd a *waiting shell parent* becomes ``$MAINPID``. That silently
  retargets ``KillMode``, ``ExecReload`` and watchdog attribution at the shell
  instead of at the broker: the unit contract changes without the unit file
  changing.
* the same fragile loop would have to exist twice, in bash and in PowerShell,
  and stay in agreement.
* and it still would not cover the fourth start path -- somebody simply typing
  ``python -m webterm.broker``.

Putting the loop in Python fixes all three: the launchers keep their ``exec``
(so the supervisor IS the main PID and the unit contract is unchanged), there
is one implementation, and every start path -- systemd, Windows scheduled task,
shell, bare python -- gets restart support for free.

Load-bearing decisions:

* **Exit 75 is an authorization request, not a fact.** Any crash that happens
  to exit 75 would otherwise be honoured as a deliberate restart. So the worker
  must ALSO write an intent sentinel containing this supervisor boot's nonce
  into ``$BROWSERLAND_RUN_DIR``; the supervisor relaunches only when that file
  is present AND matches, and deletes it after reading. An unarmed 75 is a
  crash and is reported as one. The nonce is per-supervisor-boot, so a sentinel
  left behind by an earlier broker cannot authorize anything.

* **The failure budget resets on "a worker came up", never on "a worker was
  spawned".** Resetting per spawn lets a crash-loop refill its own budget
  forever, which is exactly the failure mode a budget exists to stop.

* **Capability is reported by the WORKER, with a PPID check.** The three
  environment variables are inherited by every agent and every shell those
  agents spawn, so a broker a user starts by hand from inside one of those
  shells would inherit them and claim it is supervised -- and then die on the
  first restart. ``os.getppid() == $BROWSERLAND_SUPERVISOR_PID`` is what makes
  the claim unspoofable, because only a real child of this supervisor has it.

* **``$INVOCATION_ID`` alone does not mean systemd will restart us.** A unit
  with ``Restart=no`` sets it too, and would claim support and then stay dead.
  The unit's real policy is read with ``systemctl show``, and only ``always``
  and ``on-failure`` count (75 is non-zero, so on-failure does respawn).

* **Nothing here ever raises.** A capability probe that throws is strictly
  worse than one that says "no".

Two limits, stated plainly so nobody has to rediscover them:

* the sentinel guards against ACCIDENT, not against a hostile local process.
  Anything running as the broker's own user can read the worker's environment
  and write the file. That is the same trust boundary the broker already has
  (it launches shells as that user), and the threat the sentinel actually
  removes is the realistic one: a crash, a library, or a wrapper that happens
  to exit 75 being mistaken for a deliberate restart.

* **a restart replaces the worker's code, never the supervisor's.** After an
  update on disk, the new worker runs under the OLD supervisor, for as long as
  that supervisor lives. So the contract between them -- the exit codes, the
  three variable names, the sentinel format -- has to stay backward-compatible,
  and picking up a change to THIS file means restarting the service itself
  (``systemctl restart``, the scheduled task, the shell). That is the price of
  a parent stable enough to be trusted to restart things, and it is why this
  module stays small and dependency-free.
"""

from __future__ import annotations

import ctypes
import errno
import json
import os
import re
import secrets
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from collections import deque
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence

# ---- exit-code vocabulary ---------------------------------------------------
#
# Deliberately outside the codes this repo already uses (0 ok, 1 error, 2 audit
# incomplete, 130 Ctrl-C), so none of them can be mistaken for one of these.

#: The worker is asking to be relaunched. Honoured ONLY with a matching intent
#: sentinel -- see ``arm_restart`` / ``consume_restart_intent``.
EXIT_RESTART = 75

#: The worker could not bind its port -- it is held by somebody else, or the OS
#: refused the address outright. The supervisor must NOT relaunch on this one:
#: retrying a bind that already failed for a structural reason is an infinite
#: loop that never becomes a working broker.
EXIT_ADDR_IN_USE = 78

#: The supervisor gave up: the restart budget is exhausted, or the worker could
#: not be spawned at all. Non-zero on purpose -- under ``Restart=always``
#: systemd then applies its OWN start-limit logic on top of ours.
EXIT_SUPERVISOR_FAILED = 70

# ---- the supervisor -> worker environment -----------------------------------
#
# These exact names are also scrubbed from agent/terminal environments: a shell
# the broker launches must not inherit them (see the PPID note in the module
# docstring for why inheriting them is not a security hole, only a lie).

ENV_SUPERVISOR_PID = "BROWSERLAND_SUPERVISOR_PID"
ENV_SUPERVISOR_NONCE = "BROWSERLAND_SUPERVISOR_NONCE"
ENV_RUN_DIR = "BROWSERLAND_RUN_DIR"

#: systemd sets this for every service it starts. Presence proves systemd
#: started us; it proves NOTHING about whether systemd will restart us.
ENV_INVOCATION_ID = "INVOCATION_ID"

# ---- budget + backoff -------------------------------------------------------

#: At most this many relaunches inside ``RESTART_WINDOW`` seconds.
MAX_RESTARTS = 5
RESTART_WINDOW = 60.0

#: A worker that stayed alive this long is treated as "came up", which is what
#: clears the budget. This is a PROXY for a real readiness handshake, and it is
#: adequate because the ways a broker fails to come up are all fast and fatal:
#: an import error, a bad config, or a port it cannot bind kill it in well under
#: a second. The failures it cannot see -- a broker that binds and then wedges
#: -- would not be caught by a startup handshake either; they need a liveness
#: probe, which is a different feature.
READY_SECONDS = 10.0

#: Backoff between relaunches, doubling per CONSECUTIVE fast death. A restart
#: from a healthy broker waits not at all (the operator is watching).
BACKOFF_BASE = 0.5
BACKOFF_MAX = 30.0
#: Doubling stops here; BACKOFF_MAX has long since won, and an unclamped
#: exponent is an OverflowError waiting for a long-running crash loop.
_BACKOFF_MAX_SHIFT = 20

#: How long the child gets after a forwarded SIGTERM/SIGINT before it is killed.
#: systemd's TimeoutStopSec is 90s by default; ours is shorter because the only
#: thing between the signal and exit is a drain.
STOP_GRACE = 10.0

#: The child is waited on in short slices rather than one blocking wait, because
#: on Windows a blocking WaitForSingleObject is NOT interrupted by a Python
#: signal handler -- Ctrl-C would sit unhandled until the child happened to
#: exit. Polling costs nothing measurable and makes both platforms behave alike.
POLL_INTERVAL = 0.25

INTENT_FILENAME = "restart.intent"

# ---- capability vocabulary --------------------------------------------------
#
# The UI renders these strings, so they are API: rename one and a client shows a
# blank reason. Add new ones rather than repurposing old ones.

MECHANISM_SUPERVISOR = "supervisor"
MECHANISM_SYSTEMD = "systemd"
MECHANISM_NONE = "none"

#: No supervisor variables at all -- a bare ``--worker`` run, or an old broker.
REASON_NO_SUPERVISOR = "no-supervisor"
#: The variables are set but our parent is not the supervisor they name: we
#: INHERITED them (an agent shell, a terminal opened from the desktop). Distinct
#: from no-supervisor because it is the case that would otherwise lie.
REASON_PPID_MISMATCH = "supervisor-ppid-mismatch"
#: systemd started us, but the unit will not restart us (Restart=no, on-abort,
#: on-abnormal, on-watchdog, on-success -- none of which respawn on a plain
#: non-zero exit).
REASON_SYSTEMD_NO_RESTART = "systemd-restart-disabled"
#: systemd started us and we could not find out what its restart policy is --
#: no unit name in the cgroup, no systemctl, or systemctl failed. Unsupported,
#: because "we could not check" must never render as "yes".
REASON_SYSTEMD_POLICY_UNKNOWN = "systemd-policy-unreadable"
#: The probe itself blew up. Should be unreachable; it exists so that a bug in
#: this module degrades to "restart unsupported" instead of taking down the
#: route that calls it.
REASON_PROBE_FAILED = "probe-failed"

#: The only two policies under which a non-zero exit brings the unit back.
SYSTEMD_RESTART_OK = frozenset({"always", "on-failure"})

# ---- address-in-use detection (A19) -----------------------------------------

#: errno values, never message text: the string is localized, differs per
#: platform, and changes between Python versions.
#:
#: ``errno.EADDRINUSE`` is whatever this build calls it (98 Linux, 48 BSD/macOS,
#: 100 on Windows); the literals cover the case where the error travelled from
#: another platform's convention -- notably Winsock's 10048, which is what a
#: Windows socket error actually carries.
ADDR_IN_USE_ERRNOS = frozenset({errno.EADDRINUSE, 48, 98, 10048})

#: The OTHER way a bind permanently fails, and on Windows the LIKELIER one.
#: Measured, not assumed: a second bind to a taken port only reports 10048 when
#: the binding socket has no SO_REUSEADDR. Sanic's ``bind_socket`` always sets
#: it, so an occupied port shows up as WSAEACCES 10013 instead. On POSIX the
#: same errno is what binding a privileged port as a normal user gives. Both are
#: structural: the next attempt fails identically, so neither may be retried.
BIND_DENIED_ERRNOS = frozenset({errno.EACCES, 13, 10013})

#: Reasons ``bind_failure_reason`` can return.
BIND_IN_USE = "in-use"
BIND_DENIED = "denied"

_WSAEADDRINUSE = 10048
_WSAEACCES = 10013


def bind_failure_reason(exc: BaseException) -> Optional[str]:
    """``"in-use"``, ``"denied"``, or None -- classify a startup exception.

    The chain walk is the point. Sanic does not let the ``OSError`` from
    ``bind()`` out: ``configure_socket`` catches it and raises its own
    ``ServerError`` with advice about ``__main__`` guards. Because that raise
    happens inside the ``except`` block, Python attaches the original as
    ``__context__``, so the errno is still reachable -- just not on the
    exception the caller sees. Checking only the top exception would classify
    every port clash as a generic crash, and a generic crash is exactly the
    thing that must not be retried forever.

    errno, never message text: the string is localized, differs per platform,
    and changes between Python versions.
    """
    seen = set()
    # BOTH links, not `__cause__ or __context__`: an explicit `raise X from Y`
    # sets __cause__ while __context__ still points at whatever was being
    # handled, and the errno can be down either branch.
    pending = [exc]
    while pending:
        current = pending.pop()
        if current is None or id(current) in seen:
            continue
        seen.add(id(current))
        if isinstance(current, OSError):
            code = getattr(current, "errno", None)
            # Windows sockets can carry the Winsock code in .winerror with a
            # translated (or absent) .errno.
            win = getattr(current, "winerror", None)
            if code in ADDR_IN_USE_ERRNOS or win == _WSAEADDRINUSE:
                return BIND_IN_USE
            if code in BIND_DENIED_ERRNOS or win == _WSAEACCES:
                return BIND_DENIED
        pending.append(current.__cause__)
        pending.append(current.__context__)
    return None


def is_addr_in_use(exc: BaseException) -> bool:
    """True only for a genuine address-already-in-use error. ``denied`` is a
    different fact and gets a different message, even though both stop the
    supervisor with the same exit code."""
    return bind_failure_reason(exc) == BIND_IN_USE


# ---- the intent sentinel ----------------------------------------------------

def arm_restart(run_dir: Optional[str] = None,
                nonce: Optional[str] = None) -> bool:
    """Authorize ONE relaunch. Called in the worker, just before it stops.

    Returns True when the sentinel is on disk, False when this worker is not
    supervised or the write failed -- and a False return is a caller's cue that
    exiting 75 will simply stop the broker, so it should not.

    Both arguments default to the environment the supervisor set, so the caller
    in ``app.py`` is a bare ``arm_restart()``.

    Written to a temp name and renamed, so the supervisor can never read a
    half-written nonce. (A partial read would be refused anyway -- the compare
    is exact -- but "refused" and "restart silently didn't happen" are the same
    observable, and that is a bad thing to leave to timing.)
    """
    run_dir = run_dir or os.environ.get(ENV_RUN_DIR)
    nonce = nonce or os.environ.get(ENV_SUPERVISOR_NONCE)
    if not run_dir or not nonce:
        return False
    try:
        directory = Path(run_dir)
        directory.mkdir(parents=True, exist_ok=True)
        tmp = directory / (INTENT_FILENAME + ".tmp")
        # 0600: the sentinel is an authorization token for "restart the
        # broker". (A no-op on Windows, where the run dir's inherited ACL is
        # what protects it -- see default_run_dir.)
        fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="ascii") as handle:
            handle.write(nonce)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(str(tmp), str(directory / INTENT_FILENAME))
        return True
    except (OSError, ValueError):
        return False


def restart_armed(run_dir: Optional[str] = None,
                  nonce: Optional[str] = None) -> bool:
    """Would the supervisor accept a restart right now? Read-only -- it does
    not consume.

    Checks the CONTENT, not merely that a file exists: a stale or corrupt
    sentinel that the supervisor will reject must not convince the worker that
    exiting 75 is safe. Answering "yes" on a file the supervisor is about to
    refuse turns sentinel debris into a broker that shuts down and stays
    down."""
    run_dir = run_dir or os.environ.get(ENV_RUN_DIR)
    nonce = nonce or os.environ.get(ENV_SUPERVISOR_NONCE)
    if not run_dir or not nonce:
        return False
    try:
        raw = (Path(run_dir) / INTENT_FILENAME).read_text(
            encoding="ascii", errors="replace").strip()
    except (OSError, ValueError):
        return False
    return bool(raw) and secrets.compare_digest(raw, str(nonce))


def consume_restart_intent(run_dir: str, nonce: str) -> bool:
    """Read, DESTROY, and validate the sentinel. True only if it matched AND
    the file is now gone.

    Destroying unconditionally is deliberate: one sentinel authorizes exactly
    one relaunch, and a mismatched file must not sit there waiting for the boot
    whose nonce it happens to equal.

    The "and the file is now gone" half matters just as much. If the unlink
    fails -- on Windows another process can hold the file open without delete
    sharing -- a surviving sentinel would authorize the NEXT exit 75 as well,
    and a worker that crashes 75 twice would be relaunched on the strength of
    an intent it expressed once. So a sentinel that cannot be removed is
    blanked instead, and if even that fails the restart is refused."""
    try:
        path = Path(run_dir) / INTENT_FILENAME
    except (TypeError, ValueError):
        return False
    raw = None
    try:
        raw = path.read_text(encoding="ascii", errors="replace").strip()
    except (OSError, ValueError):
        raw = None
    destroyed = True
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    except OSError:
        try:
            with open(str(path), "w", encoding="ascii"):
                pass          # an empty sentinel matches no nonce
        except OSError:
            destroyed = False
    if not raw or not nonce or not destroyed:
        return False
    return secrets.compare_digest(raw, str(nonce))


def default_run_dir() -> str:
    """A private directory this supervisor owns, removed when it exits.

    ``mkdtemp`` rather than a fixed path under the state dir: two brokers on one
    box (dev on 4445, deploy on 8445) must not share a sentinel, and 0700 comes
    for free on POSIX. On Windows the per-user TEMP directory's ACL is the
    equivalent protection."""
    return tempfile.mkdtemp(prefix="browserland-supervisor-")


def worker_env(base: Optional[Dict[str, str]], pid: int, nonce: str,
               run_dir: str) -> Dict[str, str]:
    """The child's environment: ours, plus the three supervisor variables.

    NOT ``agent/env_util.py::spawn_env``. That function is written for agent
    shells -- it strips inherited tool variables that would confuse a nested
    CLI -- and the broker legitimately needs some of what it removes. Copying
    the broker's own environment verbatim is the only behaviour that keeps a
    supervised broker identical to an unsupervised one."""
    env = dict(os.environ if base is None else base)
    env[ENV_SUPERVISOR_PID] = str(pid)
    env[ENV_SUPERVISOR_NONCE] = str(nonce)
    env[ENV_RUN_DIR] = str(run_dir)
    return env


# ---- the deploy journal (A23) -----------------------------------------------
#
# GH#182 Part 2. A process that has exited cannot probe its replacement, so the
# question "did the new build come back?" can only be answered by the one
# component that outlives the restart: this supervisor. The WORKER writes a
# journal (via ``update.begin_deploy``) before it requests its apply-restart;
# the SUPERVISOR consumes it and adjudicates the next worker generation into
# exactly one of three outcomes. The journal lives in the run dir -- the same
# place as the intent sentinel, OUTSIDE the worktree -- so no git operation on
# the checkout (a rollback's reset included) can delete or alter it, and a
# mid-pull inconsistent tree cannot affect it.
#
# Lifecycle: one current journal (pending), one current outcome file
# (finalized; deliberately not a growing log), one quarantine name for a
# corrupt journal (preserved as evidence, never silently deleted). The record
# format is a worker -> supervisor contract exactly like the sentinel: the
# supervisor may be OLDER than the worker that wrote the journal, so the
# format is versioned and anything this build cannot validate is quarantined
# rather than guessed at.

DEPLOY_JOURNAL_FILENAME = "deploy.journal"
DEPLOY_OUTCOME_FILENAME = "deploy.outcome"
DEPLOY_QUARANTINE_FILENAME = "deploy.journal.corrupt"
DEPLOY_JOURNAL_VERSION = 1

# The three adjudications. The UI / rollback atom (A28) switch on these
# strings, so they are API: add new ones rather than repurposing old ones.
DEPLOY_READY_ON_TARGET = "came-up-ready-on-target"
DEPLOY_WRONG_SHA = "came-up-on-wrong-sha"
DEPLOY_NEVER_CAME_UP = "never-came-up"

_DEPLOY_SHA_RE = re.compile(r"[0-9a-f]{40}")


def encode_deploy_record(record: Dict[str, Any]) -> str:
    """One canonical serialization (sorted keys). Raises TypeError on an
    unserializable value -- ``build_deploy_record`` turns that into a None."""
    return json.dumps(record, sort_keys=True)


def decode_deploy_record(text: Any) -> Optional[Dict[str, Any]]:
    """A validated journal record, or None for ANYTHING else. Pure.

    Strict on purpose: the shas must be full 40-hex (short shas are the
    ambiguity ``update.local_sha`` already refuses), the operation id must be a
    non-empty string, and an unknown version is invalid -- an older supervisor
    must quarantine a newer journal rather than misread it."""
    try:
        obj = json.loads(text)
    except (TypeError, ValueError):
        return None
    if not isinstance(obj, dict):
        return None
    if obj.get("version") != DEPLOY_JOURNAL_VERSION:
        return None
    old = obj.get("oldSha")
    target = obj.get("targetSha")
    operation = obj.get("operationId")
    if not (isinstance(old, str) and _DEPLOY_SHA_RE.fullmatch(old)):
        return None
    if not (isinstance(target, str) and _DEPLOY_SHA_RE.fullmatch(target)):
        return None
    if not (isinstance(operation, str) and operation.strip()):
        return None
    return obj


def build_deploy_record(old_sha: Any, target_sha: Any, expected_identity: Any,
                        operation_id: Any, *,
                        now: Optional[float] = None
                        ) -> Optional[Dict[str, Any]]:
    """A validated version-1 journal record, or None when the inputs cannot
    make one (bad shas, empty operation id, unserializable identity). Pure.

    Validated by round-tripping through ``decode_deploy_record`` -- the
    READER'S own rules -- so the writer can never produce a journal the
    supervisor would quarantine.

    ``expected_identity`` is journal DATA for the rollback atom (A28) and the
    audit trail: the broker identity the deploy expects to survive (broker_id
    when the caller has one, else the config path + port). Adjudication keys
    on generation + sha, never on it."""
    try:
        record = {
            "version": DEPLOY_JOURNAL_VERSION,
            "operationId": operation_id,
            "oldSha": old_sha,
            "targetSha": target_sha,
            "expectedIdentity": expected_identity,
            "createdAt": time.time() if now is None else float(now),
        }
        return decode_deploy_record(encode_deploy_record(record))
    except (TypeError, ValueError):
        return None


def classify_deploy_outcome(record: Dict[str, Any], *, ready: bool,
                            observed_sha: Optional[str],
                            detail: Optional[str] = None) -> Dict[str, Any]:
    """One (journal, observation) pair -> one outcome dict. Pure.

        {"outcome": DEPLOY_*, "observedSha": str|None, "detail": str|None}

    The honesty rules (R10):

    * success REQUIRES a fresh generation that reported ready AND carries the
      journalled target sha. "A process is listening" is never success, and a
      build id alone is never success.
    * a sha that could not be read while the worker IS up classifies as
      ``came-up-on-wrong-sha`` with detail ``sha-unreadable``, never as
      success: a deploy that cannot be verified must not be reported as
      verified.
    """
    if not ready:
        return {"outcome": DEPLOY_NEVER_CAME_UP, "observedSha": None,
                "detail": detail or "worker-never-ready"}
    if observed_sha and observed_sha == record.get("targetSha"):
        return {"outcome": DEPLOY_READY_ON_TARGET,
                "observedSha": observed_sha, "detail": detail}
    return {"outcome": DEPLOY_WRONG_SHA, "observedSha": observed_sha,
            "detail": detail or (None if observed_sha else "sha-unreadable")}


def write_deploy_journal(run_dir: Optional[str],
                         record: Optional[Dict[str, Any]]) -> bool:
    """Atomically put one pending journal on disk. True only when the rename
    landed -- the same temp + ``os.replace`` discipline as ``arm_restart``, so
    the supervisor can never read a half-written record."""
    if not run_dir or not isinstance(record, dict):
        return False
    try:
        directory = Path(run_dir)
        directory.mkdir(parents=True, exist_ok=True)
        tmp = directory / (DEPLOY_JOURNAL_FILENAME + ".tmp")
        fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(encode_deploy_record(record))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(str(tmp), str(directory / DEPLOY_JOURNAL_FILENAME))
        return True
    except (OSError, TypeError, ValueError):
        return False


def _quarantine_deploy_journal(path: Path, log: Callable[[str], None]) -> None:
    try:
        os.replace(str(path), str(path.parent / DEPLOY_QUARANTINE_FILENAME))
    except OSError as exc:
        log("could not quarantine the deploy journal (%s); leaving it in "
            "place -- it will be reported again but never adjudicated" % exc)


def read_pending_deploy(run_dir: Optional[str],
                        log: Optional[Callable[[str], None]] = None
                        ) -> Optional[Dict[str, Any]]:
    """The pending deploy journal as a validated record, or None.

    None means NO PENDING DEPLOY, covering both "no journal" (silent) and
    "corrupt/unreadable journal" (loud). A corrupt journal is preserved under
    the quarantine name, never silently deleted: adjudicating garbage would
    invent a deploy outcome, and deleting it would destroy the evidence.
    Reading does NOT consume -- only ``finalize_deploy`` does."""
    log = log or _stderr_log
    if not run_dir:
        return None
    try:
        path = Path(run_dir) / DEPLOY_JOURNAL_FILENAME
    except (TypeError, ValueError):
        return None
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError as exc:
        log("deploy journal unreadable (%s); quarantining it and treating "
            "it as no pending deploy" % exc)
        _quarantine_deploy_journal(path, log)
        return None
    record = decode_deploy_record(raw)
    if record is None:
        log("deploy journal is corrupt (undecodable or invalid); moving it "
            "to %s and treating it as no pending deploy"
            % DEPLOY_QUARANTINE_FILENAME)
        _quarantine_deploy_journal(path, log)
        return None
    return record


def finalize_deploy(run_dir: str, record: Dict[str, Any],
                    outcome: Dict[str, Any], *, now: Optional[float] = None,
                    log: Optional[Callable[[str], None]] = None) -> bool:
    """Persist ONE current outcome file (atomically) and consume the journal.

    The outcome file is the audit trail the rollback atom (A28) and operators
    read; one current file, deliberately not a growing log. The journal is
    destroyed the way the intent sentinel is -- unlink, then blank when
    Windows refuses the unlink -- because a journal that survives its
    adjudication would be adjudicated AGAIN on the next generation, and one
    deploy gets exactly one outcome. (A blanked journal fails validation and
    is quarantined on the next read, never re-adjudicated.)"""
    log = log or _stderr_log
    ok = True
    try:
        directory = Path(run_dir)
        directory.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": DEPLOY_JOURNAL_VERSION,
            "record": record,
            "outcome": outcome.get("outcome"),
            "observedSha": outcome.get("observedSha"),
            "detail": outcome.get("detail"),
            "finalizedAt": time.time() if now is None else float(now),
        }
        tmp = directory / (DEPLOY_OUTCOME_FILENAME + ".tmp")
        fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, sort_keys=True))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(str(tmp), str(directory / DEPLOY_OUTCOME_FILENAME))
    except (OSError, TypeError, ValueError) as exc:
        log("could not persist the deploy outcome: %s" % exc)
        ok = False
    journal: Optional[Path] = None
    try:
        journal = Path(run_dir) / DEPLOY_JOURNAL_FILENAME
        journal.unlink()
    except FileNotFoundError:
        pass
    except (OSError, TypeError, ValueError):
        try:
            if journal is not None:
                with open(str(journal), "w", encoding="utf-8"):
                    pass
        except OSError:
            ok = False
            log("could not consume the deploy journal; it may be "
                "adjudicated again")
    return ok


def _worktree_sha() -> Optional[str]:
    """The full commit sha of the checkout this module was imported from, or
    None. Twin of ``update.local_sha`` -- deliberately NOT imported from
    update.py: that module is part of the code an update REPLACES, and the
    supervisor must stay correct with nothing but itself (see the
    backward-compat note in the module docstring). Same guard: only trusted
    when the package's parent holds the .git. Read-only, never raises."""
    try:
        root = Path(__file__).resolve().parent.parent.parent
        if not (root / ".git").exists():
            return None
        out = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"],
                             capture_output=True, text=True, timeout=5)
        sha = (out.stdout or "").strip()
        if out.returncode == 0 and re.fullmatch(r"[0-9a-f]{40}", sha):
            return sha
    except Exception:  # noqa: BLE001
        pass
    return None


def _conclude_deploy(run_dir: str, record: Dict[str, Any],
                     outcome: Dict[str, Any], log: Callable[[str], None],
                     hook: Optional[Callable[[Dict[str, Any],
                                              Dict[str, Any]], None]]) -> None:
    """Finalize + log one adjudicated deploy, then hand it to the hook.

    ``hook`` (the ``deploy_hook`` parameter of ``supervise``) is an OBSERVER
    seam: it receives ``(record, outcome)`` exactly once per deploy
    generation -- exactly-once is structural, because ``finalize_deploy``
    consumes the journal before the hook runs and an absent journal
    adjudicates nothing. A hook that raises is logged and contained: the
    supervisor's loop survives its plugins.

    The A28 rollback is deliberately NOT this hook. The revert has to change
    the LOOP's control flow (respawn on the old code instead of returning)
    and has to decide the outcome string that gets finalized, and a void
    observer that runs after the outcome file is already written can express
    neither. The rollback is wired inside ``supervise`` itself (see
    ``_rollback_after_failure`` there); this hook then observes whatever
    outcome that wiring finalized -- ``rolled-back`` included."""
    finalize_deploy(run_dir, record, outcome, log=log)
    log("deploy %s adjudicated: %s (old %s -> target %s, observed %s%s)"
        % (record.get("operationId"), outcome.get("outcome"),
           record.get("oldSha"), record.get("targetSha"),
           outcome.get("observedSha"),
           ("; " + str(outcome["detail"])) if outcome.get("detail") else ""))
    if hook is None:
        return
    try:
        hook(record, outcome)
    except Exception as exc:  # noqa: BLE001
        log("deploy outcome hook failed: %r" % (exc,))


# ---- rollback through the deploy coordinator (A28) ---------------------------
#
# GH#182 Part 2, the acceptance line this section delivers: "A build that
# fails to start is rolled back to the recorded sha automatically, and the
# failure is visible afterwards." The DECISION is one pure table over the
# adjudicated outcome; the REVERT is one bounded git command run by the
# SUPERVISOR (the worker is dead when a never-came-up is adjudicated, so only
# this process can revert -- same argument as the adjudication itself); the
# visibility is the outcome file, which records what happened in every case:
# rolled back, refused, or failed. R10 carries through untouched: what proves
# the failure is the fresh worker generation this supervisor itself spawned,
# never a port probe or a build id.

#: The revert landed: the checkout is back on the journalled oldSha and the
#: loop respawns the OLD code with a fresh (bounded) budget. The detail keeps
#: the ORIGINAL failure and the record keeps the failed target sha, so the
#: outcome file still says why -- the failure stays visible after the revert.
DEPLOY_ROLLED_BACK = "rolled-back"
#: The revert was refused BEFORE git ran: the journalled oldSha is not one
#: full 40-hex sha, or the checkout root has no .git to revert. Visible in
#: the outcome file; never retried.
DEPLOY_ROLLBACK_IMPOSSIBLE = "rollback-impossible"
#: git itself failed or could not run; the detail carries bounded stderr.
#: Visible; never retried -- the supervisor exits on the original failure
#: path rather than looping over a revert that already failed.
DEPLOY_ROLLBACK_FAILED = "rollback-failed"

#: ``rollback_decision``'s two verdicts.
ROLLBACK_REVERT = "revert"
ROLLBACK_LEAVE = "leave"

#: The revert only rewrites the worktree from objects already on disk; it
#: never touches the network, so a minute is generous.
ROLLBACK_TIMEOUT = 60.0
#: Same bound and reason as update.py's MUTATION_STDERR_CAP (not imported --
#: this module stays dependency-free): a failed tree rewrite can name every
#: locked file, and this text lands in a small JSON outcome file.
ROLLBACK_STDERR_CAP = 2000

#: never-came-up details that PROVE the new build failed to start. Everything
#: else leaves the tree alone:
#: * ``stopped-by-signal`` -- an operator stop is not a failed build;
#: * ``unauthorized-restart-request`` -- a forged or crashed 75 proves
#:   nothing about the build;
#: * ``supervisor-exited`` / ``worker-never-ready`` / anything unrecognized
#:   -- an UNKNOWN cause must never trigger a mutation of the checkout.
_ROLLBACK_STARTUP_FAILURES = frozenset({
    "restart-budget-exhausted", "bind-failed", "spawn-failed"})
_ROLLBACK_EXIT_PREFIX = "worker-exited-"


def rollback_decision(outcome: Any, detail: Any) -> str:
    """outcome + detail -> ``revert`` | ``leave``. Pure; the ONLY authority
    on whether an adjudicated deploy reverts the checkout.

    Revert ONLY on ``never-came-up`` whose detail names a genuine startup
    failure. ``came-up-on-wrong-sha`` deliberately never reverts: that worker
    is ALIVE and healthy, and killing a live worker to fix a bookkeeping
    surprise is worse than finalizing loudly as wrong-sha (which A23 already
    does, and which stays visible). Success obviously never reverts, and an
    unknown detail is treated as unknown -- never guessed into a revert."""
    if outcome != DEPLOY_NEVER_CAME_UP:
        return ROLLBACK_LEAVE
    if not isinstance(detail, str):
        return ROLLBACK_LEAVE
    if detail in _ROLLBACK_STARTUP_FAILURES:
        return ROLLBACK_REVERT
    if detail.startswith(_ROLLBACK_EXIT_PREFIX):
        return ROLLBACK_REVERT
    return ROLLBACK_LEAVE


def rollback_argv(root: str, old_sha: str) -> List[str]:
    """The ONE argv a rollback may run: a hard reset to the exact journalled
    sha, hooks disabled the same way the apply's ff-only merge disables them
    (``core.hooksPath=os.devnull`` -- a device that can never be a directory,
    on both platforms, so no repository-local hook can run code out of the
    tree being reverted). Never a network verb: the old sha's objects are
    necessarily on disk already (HEAD descends from it), so nothing needs
    fetching, and this loop must never generate egress."""
    return ["git", "-C", str(root), "-c", "core.hooksPath=" + os.devnull,
            "reset", "--hard", old_sha]


def _rollback_checkout_root() -> Optional[Path]:
    """The checkout this module was imported from -- the same derivation as
    ``_worktree_sha``, kept separate so each stays a one-screen read."""
    try:
        return Path(__file__).resolve().parent.parent.parent
    except Exception:  # noqa: BLE001
        return None


def _bounded_rollback_text(text: Any, cap: int = ROLLBACK_STDERR_CAP) -> str:
    s = str(text or "").strip()
    return s if len(s) <= cap else s[:cap] + " ...[truncated]"


def _run_rollback_git(argv: Sequence[str]) -> Any:
    """``(returncode|None, stderr text)``; None means git did not run at all
    (missing binary, timeout, OS refusal). Never raises."""
    try:
        out = subprocess.run(list(argv), capture_output=True, text=True,
                             timeout=ROLLBACK_TIMEOUT)
        return out.returncode, out.stderr or ""
    except Exception as exc:  # noqa: BLE001
        return None, "git did not run: %r" % (exc,)


def perform_rollback(old_sha: Any, *, expect_head: Optional[str] = None,
                     root: Optional[Any] = None,
                     git_runner: Optional[Callable[[Sequence[str]], Any]]
                     = None) -> Dict[str, Any]:
    """Revert the checkout to the journalled pre-deploy sha. Never raises.

        {"ok": bool,
         "outcome": DEPLOY_ROLLED_BACK | DEPLOY_ROLLBACK_IMPOSSIBLE
                    | DEPLOY_ROLLBACK_FAILED,
         "detail": str|None}

    WHY a hard reset is safe here, encoded once: the apply's preconditions
    proved a CLEAN tree (no tracked modifications) and its merge was ff-only,
    so the journalled old sha is an ancestor of HEAD and the reset only
    unwinds the tracked changes that merge introduced; untracked files are
    not touched by a reset at all. Bounded, not assumed: a malformed old sha
    or a root with no .git refuses BEFORE any argv is built, and a git
    failure is reported with bounded stderr -- every one of those lands in
    the outcome file, so a rollback that could not happen is exactly as
    visible as one that did."""
    try:
        if not (isinstance(old_sha, str)
                and _DEPLOY_SHA_RE.fullmatch(old_sha)):
            return {"ok": False, "outcome": DEPLOY_ROLLBACK_IMPOSSIBLE,
                    "detail": "old-sha-not-full-hex"}
        checkout = _rollback_checkout_root() if root is None else Path(root)
        if checkout is None or not (checkout / ".git").exists():
            return {"ok": False, "outcome": DEPLOY_ROLLBACK_IMPOSSIBLE,
                    "detail": "no-git-checkout"}
        if expect_head is not None:
            # A hard reset is only vouched-for while the tree still sits
            # where the deploy left it. If HEAD moved (an operator committed
            # or reset by hand between the failed boot and this revert), or
            # cannot be read at all, refuse VISIBLY rather than erase work
            # the journal knows nothing about.
            try:
                probe = subprocess.run(
                    ["git", "-C", str(checkout), "rev-parse", "HEAD"],
                    capture_output=True, text=True, timeout=5)
                seen = (probe.stdout.strip()
                        if probe.returncode == 0 else None)
            except Exception:  # noqa: BLE001 -- probe failure = unprovable
                seen = None
            if seen != expect_head:
                return {"ok": False, "outcome": DEPLOY_ROLLBACK_IMPOSSIBLE,
                        "detail": "tree-moved-since-deploy: HEAD is %s, "
                                  "the deploy left %s"
                                  % (seen or "unreadable", expect_head)}
        runner = git_runner or _run_rollback_git
        rc, stderr = runner(rollback_argv(str(checkout), old_sha))
        if rc == 0:
            return {"ok": True, "outcome": DEPLOY_ROLLED_BACK, "detail": None}
        return {"ok": False, "outcome": DEPLOY_ROLLBACK_FAILED,
                "detail": _bounded_rollback_text(stderr)
                or ("git exited %s" % rc)}
    except Exception as exc:  # noqa: BLE001 -- must never take down the loop
        return {"ok": False, "outcome": DEPLOY_ROLLBACK_FAILED,
                "detail": "rollback machinery failed: %r" % (exc,)}


# ---- capability reporting (A13) ---------------------------------------------

_UNIT_RE = re.compile(r"([^/\s]+\.service)")


def _read_self_cgroup() -> Optional[str]:
    try:
        return Path("/proc/self/cgroup").read_text(
            encoding="utf-8", errors="replace")
    except (OSError, ValueError):
        return None


def _run_systemctl(argv: Sequence[str]) -> Optional[str]:
    """Stripped stdout, or None on any failure at all. Never raises."""
    try:
        out = subprocess.run(list(argv), capture_output=True, text=True,
                             timeout=5)
    except Exception:  # noqa: BLE001 -- missing binary, timeout, permissions
        return None
    if out.returncode != 0:
        return None
    return (out.stdout or "").strip()


def systemd_unit(cgroup_text: Optional[str]) -> Optional[str]:
    """Our unit name out of /proc/self/cgroup, or None.

    v2 gives ``0::/system.slice/webterm-broker.service``; v1 gives several
    lines that all end in the same path; a user unit nests under
    ``user@1000.service/app.slice/``. Taking the LAST ``*.service`` segment is
    correct for all three -- the outer ``user@1000.service`` is a slice we are
    inside, not the unit we are."""
    if not cgroup_text:
        return None
    found = _UNIT_RE.findall(cgroup_text)
    return found[-1] if found else None


def systemd_is_user_unit(cgroup_text: Optional[str]) -> bool:
    """Does our cgroup path run through a per-user systemd manager?

    ``systemctl show`` without ``--user`` asks the SYSTEM manager, which has
    never heard of a user unit and answers for a stub with ``Restart=no`` --
    a false "cannot restart" for anyone running the broker as a user service.
    The nesting under ``user@<uid>.service`` is what distinguishes the two."""
    return bool(cgroup_text) and "/user@" in (cgroup_text or "")


def _cap(supported: bool, mechanism: str,
         reason_code: Optional[str]) -> Dict[str, Any]:
    return {"supported": supported, "mechanism": mechanism,
            "reason_code": reason_code}


def worker_capability(*, env: Optional[Dict[str, str]] = None,
                      getppid: Optional[Callable[[], int]] = None,
                      runner: Optional[Callable[[Sequence[str]],
                                                Optional[str]]] = None,
                      cgroup_reader: Optional[Callable[[], Optional[str]]]
                      = None) -> Dict[str, Any]:
    """Can THIS process be restarted by exiting? Called in the worker.

    ``{"supported": bool, "mechanism": "supervisor"|"systemd"|"none",
       "reason_code": str|None}``

    Every probe is injectable so the systemd branch is testable on a box with
    no systemd -- which includes every Windows developer and CI runner we have.
    """
    try:
        env = os.environ if env is None else env
        getppid = getppid or os.getppid
        runner = runner or _run_systemctl
        cgroup_reader = cgroup_reader or _read_self_cgroup

        pid_raw = env.get(ENV_SUPERVISOR_PID)
        nonce = env.get(ENV_SUPERVISOR_NONCE)
        # BOTH, or neither: a half-set pair is not a supervisor, it is debris.
        if pid_raw and nonce:
            try:
                claimed = int(str(pid_raw).strip())
            except (TypeError, ValueError):
                claimed = None
            actual = None
            try:
                actual = int(getppid())
            except Exception:  # noqa: BLE001
                actual = None
            if claimed is not None and actual is not None and claimed == actual:
                return _cap(True, MECHANISM_SUPERVISOR, None)
            # Inherited, not ours. Saying "unsupported" is not enough here: the
            # operator needs to know the variables are present but stale, or
            # they will go looking for a supervisor that never existed.
            return _cap(False, MECHANISM_NONE, REASON_PPID_MISMATCH)

        if env.get(ENV_INVOCATION_ID):
            cgroup = cgroup_reader()
            unit = systemd_unit(cgroup)
            if not unit:
                return _cap(False, MECHANISM_NONE,
                            REASON_SYSTEMD_POLICY_UNKNOWN)
            scope = ["--user"] if systemd_is_user_unit(cgroup) else []
            policy = runner(["systemctl"] + scope
                            + ["show", "-p", "Restart", "--value", unit])
            if policy is None or not policy.strip():
                return _cap(False, MECHANISM_NONE,
                            REASON_SYSTEMD_POLICY_UNKNOWN)
            if policy.strip().lower() in SYSTEMD_RESTART_OK:
                return _cap(True, MECHANISM_SYSTEMD, None)
            return _cap(False, MECHANISM_NONE, REASON_SYSTEMD_NO_RESTART)

        return _cap(False, MECHANISM_NONE, REASON_NO_SUPERVISOR)
    except Exception:  # noqa: BLE001 -- a probe must never take down its caller
        return _cap(False, MECHANISM_NONE, REASON_PROBE_FAILED)


# ---- keeping the worker from outliving the supervisor -----------------------
#
# If the supervisor is hard-killed (TerminateProcess on Windows, SIGKILL on
# POSIX) it cannot forward anything, and the worker would survive holding the
# port -- a broker nobody can stop and nothing can replace. Before this module
# existed, killing `python -m webterm.broker` killed the server, and that has to
# stay true.
#
# On Linux the shipped path is systemd, whose default KillMode=control-group
# already takes the whole cgroup down with the main process, so there is nothing
# to add. On Windows there is no equivalent, so the child goes into a job object
# marked kill-on-close: when the supervisor dies by ANY means the handle closes
# and Windows terminates the job. Agents are unaffected -- launcher.py already
# spawns them with CREATE_BREAKAWAY_FROM_JOB precisely so they survive a broker
# restart.

_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000

#: MANDATORY alongside kill-on-close, and the reason is not obvious: without it
#: Windows FAILS every CreateProcess that passes CREATE_BREAKAWAY_FROM_JOB with
#: ERROR_ACCESS_DENIED. ``launcher.py`` passes exactly that flag to keep agents
#: alive across a broker restart, and it falls back to spawning WITHOUT
#: breakaway when the call fails -- so omitting this does not break /launch
#: loudly, it silently puts every agent inside the job and kills them all the
#: next time the supervisor dies. A quiet reversal of documented behaviour is
#: worse than a crash.
_JOB_OBJECT_LIMIT_BREAKAWAY_OK = 0x00000800

_JobObjectExtendedLimitInformation = 9


class _IO_COUNTERS(ctypes.Structure):
    _fields_ = [("ReadOperationCount", ctypes.c_ulonglong),
                ("WriteOperationCount", ctypes.c_ulonglong),
                ("OtherOperationCount", ctypes.c_ulonglong),
                ("ReadTransferCount", ctypes.c_ulonglong),
                ("WriteTransferCount", ctypes.c_ulonglong),
                ("OtherTransferCount", ctypes.c_ulonglong)]


class _JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64),
                ("PerJobUserTimeLimit", ctypes.c_int64),
                ("LimitFlags", ctypes.c_uint32),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", ctypes.c_uint32),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", ctypes.c_uint32),
                ("SchedulingClass", ctypes.c_uint32)]


class _JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [("BasicLimitInformation", _JOBOBJECT_BASIC_LIMIT_INFORMATION),
                ("IoInfo", _IO_COUNTERS),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t)]


_KERNEL32: Any = None


def _kernel32():
    """One cached kernel32 binding. Loading it per spawn would take a library
    reference each time and never give one back."""
    global _KERNEL32
    if _KERNEL32 is None and os.name == "nt":
        _KERNEL32 = ctypes.WinDLL("kernel32", use_last_error=True)
    return _KERNEL32


def _create_kill_on_close_job():
    """A Windows job handle whose closure kills its members, or None.

    None on any failure, and the caller carries on without it: a supervisor
    that cannot make a job object is still a working supervisor, just one whose
    worker can be orphaned by a hard kill."""
    if os.name != "nt":
        return None
    try:
        k32 = _kernel32()
        # restype MUST be set: the default c_int truncates a 64-bit HANDLE, and
        # the truncated value then fails every call that uses it.
        k32.CreateJobObjectW.restype = ctypes.c_void_p
        k32.CreateJobObjectW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p]
        k32.SetInformationJobObject.argtypes = [
            ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32]
        job = k32.CreateJobObjectW(None, None)
        if not job:
            return None
        info = _JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        info.BasicLimitInformation.LimitFlags = (
            _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            | _JOB_OBJECT_LIMIT_BREAKAWAY_OK)
        ok = k32.SetInformationJobObject(
            ctypes.c_void_p(job), _JobObjectExtendedLimitInformation,
            ctypes.byref(info), ctypes.sizeof(info))
        if not ok:
            k32.CloseHandle(ctypes.c_void_p(job))
            return None
        return job
    except Exception:  # noqa: BLE001
        return None


def _assign_to_job(job, proc) -> None:
    if not job or os.name != "nt":
        return
    try:
        handle = getattr(proc, "_handle", None)
        if handle is None:
            return
        k32 = _kernel32()
        k32.AssignProcessToJobObject.argtypes = [ctypes.c_void_p,
                                                 ctypes.c_void_p]
        k32.AssignProcessToJobObject(ctypes.c_void_p(job),
                                     ctypes.c_void_p(int(handle)))
    except Exception:  # noqa: BLE001
        pass


def _close_job(job) -> None:
    if not job or os.name != "nt":
        return
    try:
        _kernel32().CloseHandle(ctypes.c_void_p(job))
    except Exception:  # noqa: BLE001
        pass


# ---- the loop ---------------------------------------------------------------

def _stderr_log(message: str) -> None:
    try:
        sys.stderr.write("[supervisor] %s\n" % message)
        sys.stderr.flush()
    except Exception:  # noqa: BLE001 -- a closed stderr must not be fatal
        pass


def _forward(proc, signum: int) -> None:
    """Pass a stop signal down to the worker. Never raises -- this runs inside
    a signal handler, where an exception surfaces somewhere unrelated.

    Windows deliberately forwards NOTHING. The only signal a Python handler
    actually sees there is a console Ctrl-C, and the OS has already delivered
    that to the whole process group -- the worker has it and is shutting down
    gracefully. The only forwarding primitive Windows offers is
    ``TerminateProcess``, which would abort that shutdown mid-flight and
    guarantee the ungraceful stop we were trying to avoid. The STOP_GRACE
    escalation in ``_wait_for`` is the backstop for a worker that ignores it.
    """
    if os.name == "nt":
        return
    try:
        proc.send_signal(signum)
    except Exception:  # noqa: BLE001
        pass


def _wait_for(proc, state: Dict[str, Any], *, poll: float = POLL_INTERVAL,
              grace: float = STOP_GRACE,
              clock: Callable[[], float] = time.monotonic,
              ready_after: Optional[float] = None,
              on_ready: Optional[Callable[[], None]] = None) -> Optional[int]:
    """Wait in slices so signal handlers actually run (see POLL_INTERVAL), and
    escalate to a kill if the worker ignores the stop signal.

    ``on_ready`` fires AT MOST ONCE, the first time the child has stayed alive
    ``ready_after`` seconds -- i.e. while the process is STILL RUNNING, which
    is what lets a pending deploy be adjudicated for a worker that came up and
    never exits (the success case). It is the same uptime proxy the budget
    uses (READY_SECONDS), observed live instead of post-mortem. Deliberately
    NOT fired once a stop signal has arrived: a worker that is being shut down
    is not "up"."""
    signalled_at: Optional[float] = None
    last_kill: Optional[float] = None
    started = clock()
    ready_fired = False
    while True:
        try:
            return proc.wait(timeout=poll)
        except subprocess.TimeoutExpired:
            if state.get("signum") and signalled_at is None:
                signalled_at = clock()
            if (on_ready is not None and not ready_fired
                    and ready_after is not None and signalled_at is None
                    and clock() - started >= ready_after):
                ready_fired = True
                try:
                    on_ready()
                except Exception:  # noqa: BLE001 -- adjudication must never
                    pass           # take down the wait loop
            if signalled_at is None:
                continue
            now = clock()
            # Retried, not fired once: a kill can fail transiently, and a
            # single ignored failure would leave this polling forever with no
            # further escalation -- a supervisor that hangs on shutdown.
            due = (last_kill is None and now - signalled_at > grace) or \
                  (last_kill is not None and now - last_kill > grace)
            if due:
                last_kill = now
                try:
                    proc.kill()
                except Exception:  # noqa: BLE001
                    pass


def _exit_code_for(returncode: Optional[int], signum: int) -> int:
    """One integer for ``sys.exit``.

    A signal we were told to stop on IS the reason we stopped, so it wins over
    whatever the child managed to return -- and 128+signum is the shell
    convention this repo already speaks (130 for Ctrl-C). A negative returncode
    is POSIX for "killed by signal N" and would otherwise become a nonsense
    exit status."""
    if signum:
        return 128 + int(signum)
    if returncode is None:
        return 0
    if returncode < 0:
        return 128 + (-returncode)
    return int(returncode)


def supervise(args: Optional[Sequence[str]] = None, *,
              child_cmd: Optional[Sequence[str]] = None,
              env: Optional[Dict[str, str]] = None,
              run_dir: Optional[str] = None,
              nonce: Optional[str] = None,
              popen: Optional[Callable[..., Any]] = None,
              clock: Optional[Callable[[], float]] = None,
              sleeper: Optional[Callable[[float], None]] = None,
              log: Optional[Callable[[str], None]] = None,
              max_restarts: int = MAX_RESTARTS,
              window: float = RESTART_WINDOW,
              ready_seconds: float = READY_SECONDS,
              backoff_base: float = BACKOFF_BASE,
              backoff_max: float = BACKOFF_MAX,
              install_signals: bool = True,
              deploy_hook: Optional[Callable[[Dict[str, Any],
                                              Dict[str, Any]], None]] = None,
              sha_reader: Optional[Callable[[], Optional[str]]] = None,
              rollback: Optional[Callable[[Any], Dict[str, Any]]]
              = None) -> int:
    """Run the worker, relaunch it when it asks, and return an exit code.

    ``args`` are this process's own CLI arguments; they are replayed to the
    worker verbatim after ``--worker`` so the child is configured identically.
    Everything else is injectable so the loop can be driven by a trivial fake
    child instead of a real broker.

    ``deploy_hook`` observes every adjudicated deploy (see
    ``_conclude_deploy``); ``sha_reader`` is how a ready worker's build is
    identified for deploy adjudication (default: this checkout's HEAD via
    ``_worktree_sha``); ``rollback`` is the A28 revert (default:
    ``perform_rollback`` against the real checkout -- tests MUST inject a
    fake, or a pending journal makes the loop run git against the repo the
    tests live in)."""
    args = list(sys.argv[1:] if args is None else args)
    popen = popen or subprocess.Popen
    clock = clock or time.monotonic
    sleeper = sleeper or time.sleep
    log = log or _stderr_log
    nonce = nonce or secrets.token_hex(16)

    owned_dir: Optional[str] = None
    if run_dir is None:
        try:
            run_dir = owned_dir = default_run_dir()
        except OSError as exc:
            log("cannot create a run directory (%s); restarts unavailable"
                % exc)
            return EXIT_SUPERVISOR_FAILED

    if child_cmd is None:
        # A fresh interpreter, not an in-process re-exec: Sanic's state is not
        # reusable after a stop, and a new process is also the only thing that
        # picks up code changed on disk -- which is the whole point of a
        # restart after an update.
        child_cmd = [sys.executable, "-m", "webterm.broker", "--worker"] + args
    child_cmd = list(child_cmd)
    child_env = worker_env(env, os.getpid(), nonce, run_dir)

    # Mutable box shared with the signal handler; a handler cannot rebind a
    # closure local. pending_deploy / deploy_detail belong to the deploy
    # journal (A23): the per-spawn journal snapshot, and why the loop exited.
    state: Dict[str, Any] = {"signum": 0, "child": None,
                             "pending_deploy": None, "deploy_detail": None}
    sha_read = sha_reader or _worktree_sha
    revert = perform_rollback if rollback is None else rollback

    def _deploy_ready() -> None:
        # Runs from inside _wait_for while the CURRENT worker -- a fresh pid
        # this supervisor itself spawned -- is still alive past ready_seconds.
        # That conjunction (fresh generation + ready + sha) is the R10 proof:
        # never "something is listening on the port", never a build id alone.
        record = state.get("pending_deploy")
        if not record:
            return
        state["pending_deploy"] = None
        try:
            observed = sha_read()
        except Exception:  # noqa: BLE001 -- an injected reader must not
            observed = None            # take down the loop
        outcome = classify_deploy_outcome(record, ready=True,
                                          observed_sha=observed)
        _conclude_deploy(run_dir, record, outcome, log, deploy_hook)

    def _rollback_after_failure(detail: str) -> bool:
        """The A28 wiring, called at each return site whose ``detail`` marks
        the just-spawned generation as never-came-up. True ONLY when the
        checkout was actually reverted -- the caller then clears the budget
        (the same bounded reset a worker that came up earns; the failed
        new-code spawns already charged it) and continues the loop, so the
        OLD code respawns with one fresh, bounded chance. If the old code
        cannot come up either, the budget exhausts again with NO journal left
        to revert, and the supervisor exits: never an infinite loop. In every
        other case the caller falls through to its original return.

        Exactly-once still holds structurally: every path through here that
        adjudicates also consumes the journal (finalize inside
        ``_conclude_deploy``) and clears the snapshot, so neither the finally
        block nor a later generation can adjudicate this deploy again. A
        LEAVE decision adjudicates nothing here -- the finally block then
        finalizes never-came-up exactly as it did before A28."""
        record = state.get("pending_deploy")
        if record is None:
            record = read_pending_deploy(run_dir, log)
        if record is None:
            return False
        if rollback_decision(DEPLOY_NEVER_CAME_UP, detail) != ROLLBACK_REVERT:
            return False
        try:
            if revert is perform_rollback:
                # The real revert gets the tree-moved guard; an injected fake
                # keeps its one-argument contract.
                result = revert(record.get("oldSha"),
                                expect_head=record.get("targetSha"))
            else:
                result = revert(record.get("oldSha"))
        except Exception as exc:  # noqa: BLE001 -- an injected revert must
            result = {"ok": False,   # not take down the loop
                      "outcome": DEPLOY_ROLLBACK_FAILED,
                      "detail": "the revert callable raised: %r" % (exc,)}
        if not isinstance(result, dict):
            result = {"ok": False, "outcome": DEPLOY_ROLLBACK_FAILED,
                      "detail": "the revert callable returned %r" % (result,)}
        why = result.get("detail")
        outcome = {
            "outcome": result.get("outcome") or DEPLOY_ROLLBACK_FAILED,
            # No ready worker observed anything; observedSha stays honest.
            "observedSha": None,
            # The ORIGINAL failure stays visible ahead of the revert's own
            # note; the failed target sha rides in the record alongside.
            "detail": detail if not why else "%s; %s" % (detail, why),
        }
        state["pending_deploy"] = None
        _conclude_deploy(run_dir, record, outcome, log, deploy_hook)
        if not result.get("ok"):
            return False
        log("rolled the checkout back to %s after the new build never came "
            "up (%s); relaunching on the old code with a fresh budget"
            % (record.get("oldSha"), detail))
        return True

    def _handler(signum, _frame):
        state["signum"] = signum
        child = state.get("child")
        if child is not None:
            _forward(child, signum)

    installed: List[Any] = []
    if install_signals:
        # SIGTERM and SIGINT only. Anything else (SIGHUP, a console close) keeps
        # its default disposition, which is what the pre-supervisor broker had.
        for name in ("SIGTERM", "SIGINT"):
            sig = getattr(signal, name, None)
            if sig is None:
                continue                      # not on this platform
            try:
                installed.append((sig, signal.signal(sig, _handler)))
            except (ValueError, OSError, RuntimeError):
                # ValueError: not the main thread (a test, an embedded run).
                pass

    job = _create_kill_on_close_job()
    attempts: "deque" = deque()   # relaunch timestamps inside the window
    consecutive = 0               # fast deaths since the last worker that came up
    try:
        while True:
            # The pending deploy journal is snapshotted PER SPAWN: the
            # generation it adjudicates is the one about to be spawned, never
            # the (still-running) worker that wrote it. A journal written
            # mid-flight is therefore only picked up by the NEXT generation.
            state["pending_deploy"] = read_pending_deploy(run_dir, log)
            started = clock()
            try:
                proc = popen(child_cmd, env=child_env)
            except OSError as exc:
                log("could not start the broker worker: %s" % exc)
                if _rollback_after_failure("spawn-failed"):
                    attempts.clear()
                    consecutive = 0
                    continue
                state["deploy_detail"] = "spawn-failed"
                return EXIT_SUPERVISOR_FAILED
            state["child"] = proc
            _assign_to_job(job, proc)
            # A signal that arrived between installing the handler and storing
            # the child would have found state["child"] empty; deliver it now.
            if state.get("signum"):
                _forward(proc, state["signum"])
            raw = _wait_for(proc, state, clock=clock,
                            ready_after=ready_seconds, on_ready=_deploy_ready)
            state["child"] = None
            uptime = clock() - started

            if state.get("signum"):
                log("stopping on signal %d; not relaunching"
                    % state["signum"])
                state["deploy_detail"] = "stopped-by-signal"
                return _exit_code_for(raw, state["signum"])

            if raw == EXIT_ADDR_IN_USE:
                # A pending deploy first: the bind may have failed BECAUSE of
                # the new build (a changed default, a config it now reads
                # differently), so the reverted code gets ONE fresh chance. A
                # purely environmental clash then simply fails the same way
                # once more -- with no journal left -- and stops below.
                if _rollback_after_failure("bind-failed"):
                    attempts.clear()
                    consecutive = 0
                    continue
                # The worker has already printed WHICH kind of bind failure it
                # was (taken vs refused) on the inherited stderr; repeating a
                # guess here would contradict it.
                log("the broker could not bind its port (see the message "
                    "above). Not relaunching: a bind that failed for a "
                    "structural reason fails identically next time.")
                state["deploy_detail"] = "bind-failed"
                return EXIT_ADDR_IN_USE

            if raw != EXIT_RESTART:
                detail = "worker-exited-%s" % raw
                if _rollback_after_failure(detail):
                    attempts.clear()
                    consecutive = 0
                    continue
                state["deploy_detail"] = detail
                return _exit_code_for(raw, 0)

            # ---- exit 75: a restart REQUEST, to be authorized ----
            if not consume_restart_intent(run_dir, nonce):
                log("worker exited %d without arming a restart (no intent "
                    "sentinel, or the wrong nonce). Treating it as a crash "
                    "and stopping -- an unauthorized 75 is not a restart."
                    % EXIT_RESTART)
                state["deploy_detail"] = "unauthorized-restart-request"
                return EXIT_RESTART

            # NOTE (#183): an authorized restart is still charged to the CRASH
            # budget -- restarting a broker that had been up for less than
            # READY_SECONDS counts against it. The worker-side restart cooldown
            # (`restart_cooldown_seconds`, app.py, default 90 > the 60 s budget
            # window) now bounds AUTHORIZED restarts to at most one per window,
            # so deliberate clicking can no longer exhaust the budget; only a
            # crash loop can, which is what the budget is for.
            #
            # Setting `came_up = True` here is NOT the fix: it clears the budget
            # on every authorized restart, which removes the only backstop, and
            # a worker that exits 75 with a fresh sentinel every time then
            # relaunches forever. test_a_worker_that_always_exits_75_hits_the_
            # budget and test_the_budget_resets_only_for_a_worker_that_came_up
            # both hang on it rather than fail, which is how it went unnoticed.
            # Whatever replaces this needs a SEPARATE allowance for authorized
            # restarts, so a runaway is still caught while a handful of
            # deliberate ones are not charged to the crash budget.
            came_up = uptime >= ready_seconds
            now = clock()
            while attempts and now - attempts[0] > window:
                attempts.popleft()
            if came_up:
                # The ONLY thing that clears the budget. Not the spawn: a
                # crash-loop that reset on spawn would restart forever.
                attempts.clear()
                consecutive = 0
            else:
                consecutive += 1
            if len(attempts) >= max_restarts:
                log("restart budget exhausted: %d relaunches in under %.0fs "
                    "and no worker stayed up %.0fs. Giving up -- the broker "
                    "is crash-looping, and restarting it again would only "
                    "hide that." % (len(attempts), window, ready_seconds))
                if _rollback_after_failure("restart-budget-exhausted"):
                    attempts.clear()
                    consecutive = 0
                    continue
                state["deploy_detail"] = "restart-budget-exhausted"
                return EXIT_SUPERVISOR_FAILED
            attempts.append(now)

            delay = 0.0
            if not came_up:
                # The exponent is clamped, not just the result. A worker that
                # dies slowly enough to stay inside the budget (say every 9s,
                # under READY_SECONDS but too rarely to fill the window) loops
                # indefinitely by design -- that is what "N per M seconds"
                # means -- and `consecutive` would climb forever with it, until
                # `backoff_base * 2 ** consecutive` overflowed and took the
                # supervisor down with an unrelated traceback.
                delay = min(backoff_max,
                            backoff_base * (2 ** min(max(0, consecutive - 1),
                                                     _BACKOFF_MAX_SHIFT)))
                log("worker exited after %.1fs (under the %.0fs it takes to "
                    "count as up); relaunching in %.1fs [%d/%d]"
                    % (uptime, ready_seconds, delay, len(attempts),
                       max_restarts))
            else:
                log("restarting the broker as requested")
            # Sliced, and abandoned the moment a stop arrives. A single
            # sleeper(30) would keep the supervisor alive and unresponsive for
            # half a minute after a SIGTERM, and then -- worse -- spawn a fresh
            # worker it immediately has to stop.
            remaining = delay
            while remaining > 0 and not state.get("signum"):
                slice_ = min(POLL_INTERVAL, remaining)
                sleeper(slice_)
                remaining -= slice_
            if state.get("signum"):
                log("stopping on signal %d; not relaunching"
                    % state["signum"])
                state["deploy_detail"] = "stopped-by-signal"
                return _exit_code_for(0, state["signum"])
    finally:
        for sig, previous in installed:
            try:
                signal.signal(sig, previous)
            except (ValueError, OSError, RuntimeError, TypeError):
                pass
        _close_job(job)
        # A deploy still pending when the loop is over means the new build
        # NEVER CAME BACK: no generation reached ready before the supervisor
        # gave up (budget, bind failure, a plain crash, a stop signal --
        # deploy_detail says which). Adjudicated HERE because a process that
        # has exited cannot probe its replacement; the supervisor is the only
        # component positioned to observe this. The file is re-read as a
        # fallback so a journal written mid-flight by a worker that then never
        # restarted is finalized too, not left pending in a dying run dir.
        record = state.get("pending_deploy")
        if record is None:
            record = read_pending_deploy(run_dir, log)
        if record is not None:
            outcome = classify_deploy_outcome(
                record, ready=False, observed_sha=None,
                detail=state.get("deploy_detail") or "supervisor-exited")
            _conclude_deploy(run_dir, record, outcome, log, deploy_hook)
        if owned_dir:
            shutil.rmtree(owned_dir, ignore_errors=True)
