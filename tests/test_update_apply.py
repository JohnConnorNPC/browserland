"""The update-apply feature (#182 Part 2): preconditions (A24), the deploy
journal seam (A23), dependency-delta detection (A25), and now the apply route
plus its git-mutation engine (A27).

The precondition half is pure predicates over an injected snapshot -- no
network, no Sanic. The impure pieces (``collect_apply_snapshot``, the A27
``perform_apply`` engine) are tested by monkeypatching the module's own git
helpers, never by touching the real checkout or the network; the route tests
fake the engine the same way test_update_check fakes ``run_check``.

The load-bearing rules, mirrored from the check engine's honesty rules:

* Writability is ADVISORY, never a refusal: it cannot be pre-proved,
  especially on Windows, where a file that opens now can be locked by another
  handle at apply time (review point R5).
* Untracked files do NOT trip dirty-tree -- an untracked scratch file must not
  block an update; a genuinely colliding one surfaces at merge time and is
  reported then.
* Unknown ancestry is ``state-unknown``, its own code -- never guessed into
  ``ahead-or-diverged`` (the compare-404 trap, already refuted in Part 1).
* Refusals ACCUMULATE: the UI shows every unmet condition, not just the first.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path

import pytest

import webterm.broker.app as app_mod
from webterm.broker import supervise, update
from webterm.broker.app import create_app

from .auth_helpers import TEST_TOKEN, authed, authed_reusable, with_token


SHA = "a" * 40
TARGET = "b" * 40
OTHER = "c" * 40


def _snap(**over):
    """A snapshot that PASSES every precondition; tests override one field at
    a time so each refusal is proved to fire alone."""
    base = dict(local_sha=SHA, dirty=False, check_state=update.STATE_BEHIND,
                target_sha=TARGET, ahead_by=0, behind_by=5,
                restart_available=True, restart_reason=None,
                apply_enabled=True, writable=True, dependency_delta=None)
    base.update(over)
    return update.ApplySnapshot(**base)


def _codes(snap):
    return [r.reason_code for r in update.apply_preconditions(snap)]


# ---- the clean path ---------------------------------------------------------

def test_a_clean_snapshot_yields_zero_refusals():
    assert update.apply_preconditions(_snap()) == []


def test_evaluate_apply_reports_ok_on_a_clean_snapshot():
    out = update.evaluate_apply(_snap())
    assert out["ok"] is True
    assert out["refusals"] == []


def test_snapshot_defaults_fail_closed():
    """A field nobody filled in must read as 'not safe to apply', never as
    permission: an EMPTY snapshot refuses, it does not proceed."""
    assert update.apply_preconditions(update.ApplySnapshot()) != []


# ---- each refusal fires alone ------------------------------------------------

def test_a_wheel_install_refuses_not_a_checkout():
    """Issue non-goal: updating a pip/wheel install is out of scope. No local
    sha means no checkout, and git has nothing to update."""
    assert _codes(_snap(local_sha=None)) == [update.APPLY_NOT_A_CHECKOUT]


def test_tracked_modifications_refuse_dirty_tree():
    assert _codes(_snap(dirty=True)) == [update.APPLY_DIRTY_TREE]


def test_local_commits_refuse_ahead_or_diverged():
    """4445 is exactly this machine (Part 1's own tests say so): a dev
    checkout with unpushed commits must refuse, never be updated over."""
    refusals = update.apply_preconditions(
        _snap(check_state=update.STATE_AHEAD, ahead_by=3))
    assert [r.reason_code for r in refusals] == [update.APPLY_AHEAD_OR_DIVERGED]
    assert "3" in refusals[0].message


@pytest.mark.parametrize("over", [
    {"check_state": None},                    # a check never ran
    {"check_state": update.STATE_UNKNOWN},    # it ran and could not answer
    {"target_sha": None},                     # no named target sha
    {"ahead_by": None},                       # ancestry never established
    {"behind_by": None},
])
def test_an_unestablished_check_refuses_state_unknown(over):
    """An apply must be preceded by an established check result naming the
    target sha -- anything less is its own refusal, not a guess."""
    assert _codes(_snap(**over)) == [update.APPLY_STATE_UNKNOWN]


def test_unknown_ancestry_is_state_unknown_never_ahead():
    """The Part 1 correction carried forward: when ahead/behind was never
    established the refusal says so. It is not guessed into ahead-or-diverged
    the way the issue once guessed a compare 404 was."""
    codes = _codes(_snap(ahead_by=None))
    assert update.APPLY_STATE_UNKNOWN in codes
    assert update.APPLY_AHEAD_OR_DIVERGED not in codes


def test_behind_zero_refuses_already_current():
    codes = _codes(_snap(check_state=update.STATE_CURRENT, behind_by=0))
    assert codes == [update.APPLY_ALREADY_CURRENT]


def test_restart_unavailable_refuses_and_names_the_underlying_reason():
    """The caller passes the restart capability and its reason_code into the
    snapshot; the human sentence must carry that underlying reason through."""
    refusals = update.apply_preconditions(
        _snap(restart_available=False, restart_reason="no-supervisor"))
    assert [r.reason_code for r in refusals] == [
        update.APPLY_RESTART_UNAVAILABLE]
    assert "no-supervisor" in refusals[0].message


def test_restart_unavailable_without_a_reason_still_reads_as_a_sentence():
    refusals = update.apply_preconditions(_snap(restart_available=False))
    assert refusals[0].reason_code == update.APPLY_RESTART_UNAVAILABLE
    assert len(refusals[0].message) > 20


def test_the_apply_gate_off_refuses_apply_disabled():
    assert _codes(_snap(apply_enabled=False)) == [update.APPLY_DISABLED]


def test_a_dependency_delta_refuses_via_the_reserved_seam():
    """The DETECTION is a later atom; the seam is 'a truthy field refuses'.
    Falsy shapes must not, or the seam becomes a default refusal."""
    codes = _codes(_snap(dependency_delta={"requirements.txt": "changed"}))
    assert codes == [update.APPLY_DEPENDENCY_DELTA]
    assert _codes(_snap(dependency_delta=None)) == []
    assert _codes(_snap(dependency_delta={})) == []


# ---- accumulation and stability ---------------------------------------------

def test_multiple_failures_are_all_reported():
    """The UI shows every unmet condition in one pass, so the evaluator must
    not stop at the first refusal."""
    codes = _codes(_snap(dirty=True, restart_available=False,
                         apply_enabled=False))
    assert set(codes) == {update.APPLY_DIRTY_TREE,
                          update.APPLY_RESTART_UNAVAILABLE,
                          update.APPLY_DISABLED}
    assert len(codes) == 3


def test_every_reason_code_is_distinct_and_pinned():
    """These strings are an API the UI and peer brokers will switch on.
    Pinned literally, so a rename fails a test instead of silently orphaning
    a client."""
    pinned = {
        update.APPLY_NOT_A_CHECKOUT: "not-a-checkout",
        update.APPLY_DIRTY_TREE: "dirty-tree",
        update.APPLY_AHEAD_OR_DIVERGED: "ahead-or-diverged",
        update.APPLY_STATE_UNKNOWN: "state-unknown",
        update.APPLY_ALREADY_CURRENT: "already-current",
        update.APPLY_RESTART_UNAVAILABLE: "restart-unavailable",
        update.APPLY_DISABLED: "apply-disabled",
        update.APPLY_DEPENDENCY_DELTA: "dependency-delta",
    }
    for const, literal in pinned.items():
        assert const == literal
    assert len(set(pinned.values())) == 8, "codes must be distinct"


def test_every_refusal_carries_its_own_human_sentence():
    """Each reason_code must be reachable and must carry a distinct,
    non-placeholder sentence -- a shrug string shared between codes would be
    indistinguishable to the person reading it."""
    failing = [
        _snap(local_sha=None),
        _snap(dirty=True),
        _snap(check_state=update.STATE_AHEAD, ahead_by=2),
        _snap(check_state=None),
        _snap(check_state=update.STATE_CURRENT, behind_by=0),
        _snap(restart_available=False),
        _snap(apply_enabled=False),
        _snap(dependency_delta=True),
    ]
    seen = {}
    for snap in failing:
        for r in update.apply_preconditions(snap):
            assert isinstance(r.message, str) and len(r.message) > 20
            seen[r.reason_code] = r.message
    assert len(seen) == 8, "every reason_code must be reachable"
    assert len(set(seen.values())) == 8, "sentences must be distinct too"


# ---- the untracked-file rule (collector) ------------------------------------

def _fake_git(status_output):
    """Stand-in for update._git, capturing its argument tuples."""
    calls = []

    def fake(*args, timeout=5.0):
        calls.append(args)
        if args and args[0] == "status":
            return status_output
        return ""
    return fake, calls


def test_untracked_files_do_not_trip_dirty_tree(monkeypatch):
    """An untracked scratch file must not block an update. Structural, not
    filtered: the collector asks git for tracked files only, so untracked
    entries never even reach the predicate."""
    monkeypatch.setattr(update, "local_sha", lambda: SHA)
    monkeypatch.setattr(update, "probe_writability", lambda: True)
    fake, calls = _fake_git("")     # tracked-only status: nothing to report
    monkeypatch.setattr(update, "_git", fake)
    snap = update.collect_apply_snapshot()
    assert snap.dirty is False
    status_calls = [c for c in calls if c and c[0] == "status"]
    assert status_calls, "the collector must actually ask git"
    assert "--untracked-files=no" in status_calls[0], (
        "untracked files must be excluded at the git level, structurally")


def test_tracked_modifications_are_what_the_collector_reports(monkeypatch):
    monkeypatch.setattr(update, "local_sha", lambda: SHA)
    monkeypatch.setattr(update, "probe_writability", lambda: True)
    fake, _ = _fake_git(" M webterm/broker/update.py")
    monkeypatch.setattr(update, "_git", fake)
    assert update.collect_apply_snapshot().dirty is True


def test_the_collector_maps_a_check_result_into_the_snapshot(monkeypatch):
    """The snapshot's check fields come from run_check's cached output shape
    (state / aheadBy / behindBy / upstream.sha), so the evaluator and the
    check engine can never disagree about what was established."""
    monkeypatch.setattr(update, "local_sha", lambda: SHA)
    monkeypatch.setattr(update, "probe_writability", lambda: True)
    fake, _ = _fake_git("")
    monkeypatch.setattr(update, "_git", fake)
    check = {"state": update.STATE_BEHIND, "aheadBy": 0, "behindBy": 4,
             "upstream": {"sha": TARGET, "branch": "main"}}
    snap = update.collect_apply_snapshot(check_result=check,
                                         restart_available=True,
                                         apply_enabled=True)
    assert snap.check_state == update.STATE_BEHIND
    assert snap.target_sha == TARGET
    assert snap.ahead_by == 0 and snap.behind_by == 4
    assert update.apply_preconditions(snap) == []


def test_a_non_checkout_collects_no_dirty_measurement(monkeypatch):
    """Without a checkout there is no tree to measure: dirty stays None (not
    False -- unmeasured is not clean) and git status is never run."""
    monkeypatch.setattr(update, "local_sha", lambda: None)
    monkeypatch.setattr(update, "probe_writability", lambda: None)
    called = []
    monkeypatch.setattr(update, "_git",
                        lambda *a, **k: called.append(a) or "")
    snap = update.collect_apply_snapshot()
    assert snap.local_sha is None and snap.dirty is None
    assert not called, "no checkout means no git status to run"
    assert update.APPLY_NOT_A_CHECKOUT in [
        r.reason_code for r in update.apply_preconditions(snap)]


# ---- writability: advisory only, never a refusal (R5) ------------------------

@pytest.mark.parametrize("writable", [True, False, None])
def test_writability_never_appears_as_a_refusal(writable):
    """R5: writability cannot be pre-proved, especially on Windows, so even a
    FAILED probe refuses nothing -- it is reported alongside, honestly."""
    snap = _snap(writable=writable)
    assert update.apply_preconditions(snap) == []
    out = update.evaluate_apply(snap)
    assert out["ok"] is True
    assert out["refusals"] == []
    advisory = out["writability_advisory"]
    assert advisory["writable"] is writable
    assert "pre-proved" in advisory["message"]


def test_no_reason_code_exists_for_writability():
    """Structural: there is no code a regression could reach for."""
    for name in dir(update):
        if name.startswith("APPLY_"):
            assert "writ" not in getattr(update, name)


def test_the_advisory_is_honest_about_windows():
    """Even a SUCCESSFUL probe must say the quiet part: Windows cannot
    pre-prove writability, so success now proves nothing about apply time."""
    msg = update.writability_advisory(True)["message"]
    assert "Windows" in msg and "pre-proved" in msg


def test_the_probe_is_best_effort_and_never_raises():
    assert update.probe_writability() in (True, False, None)


# ---- the preview -------------------------------------------------------------

def test_the_preview_names_old_target_behind_and_a_compare_url():
    p = update.evaluate_apply(_snap())["preview"]
    assert p["oldSha"] == SHA
    assert p["targetSha"] == TARGET
    assert p["behindBy"] == 5
    assert p["compareUrl"] == update.compare_url(
        update.UPSTREAM_REPO, SHA, TARGET)
    assert SHA in p["compareUrl"] and TARGET in p["compareUrl"]


def test_the_preview_degrades_honestly_without_shas():
    p = update.evaluate_apply(_snap(local_sha=None,
                                    target_sha=None))["preview"]
    assert p["oldSha"] is None and p["targetSha"] is None
    assert p["compareUrl"] is None


def test_evaluate_apply_always_returns_the_full_shape():
    """One output shape, whatever the verdict -- the later route atom serves
    this verbatim, so a key that comes and goes would be a client-side bug."""
    for snap in (_snap(), _snap(local_sha=None, apply_enabled=False)):
        out = update.evaluate_apply(snap)
        for key in ("ok", "refusals", "writability_advisory", "preview"):
            assert key in out
        for key in ("oldSha", "targetSha", "behindBy", "compareUrl"):
            assert key in out["preview"]
        for r in out["refusals"]:
            assert set(r) == {"reason", "message"}


# ---- the deploy journal seam (A23) -------------------------------------------
#
# begin_deploy is the WORKER half of the deploy seam: it journals the deploy
# (outside the worktree, in the supervisor's run dir) before the apply-restart
# is requested. The supervisor half -- adjudication of the next generation --
# is tested in test_supervisor.py; here we prove the writer produces exactly
# what the supervisor's reader accepts, and that it fails closed.

def test_begin_deploy_writes_a_journal_the_supervisor_can_read(tmp_path):
    run_dir = tmp_path / "run"
    ok = update.begin_deploy(
        SHA, TARGET,
        {"brokerId": "b-1", "configPath": "broker_config.json", "port": 4445},
        "op-42", run_dir=str(run_dir))
    assert ok is True
    record = supervise.read_pending_deploy(str(run_dir))
    assert record is not None
    assert record["oldSha"] == SHA
    assert record["targetSha"] == TARGET
    assert record["operationId"] == "op-42"
    # expected_identity is journal DATA (for A28 and the audit trail),
    # preserved verbatim -- adjudication never keys on it.
    assert record["expectedIdentity"] == {
        "brokerId": "b-1", "configPath": "broker_config.json", "port": 4445}


def test_begin_deploy_defaults_to_the_supervisors_run_dir(tmp_path,
                                                          monkeypatch):
    """The same environment default as arm_restart, so the apply route can
    call it bare inside a supervised worker."""
    monkeypatch.setenv(supervise.ENV_RUN_DIR, str(tmp_path / "envrun"))
    assert update.begin_deploy(SHA, TARGET, None, "op-env") is True
    record = supervise.read_pending_deploy(str(tmp_path / "envrun"))
    assert record is not None
    assert record["operationId"] == "op-env"


def test_begin_deploy_without_a_supervisor_refuses(monkeypatch):
    """No run dir means no supervisor to adjudicate: a False is the caller's
    cue that the apply must not proceed to a restart."""
    monkeypatch.delenv(supervise.ENV_RUN_DIR, raising=False)
    assert update.begin_deploy(SHA, TARGET, None, "op") is False


def test_begin_deploy_refuses_anything_but_full_shas(tmp_path):
    """Short shas and refs are the ambiguity local_sha() already refuses; a
    journal carrying one could be adjudicated against the wrong commit."""
    run_dir = tmp_path / "run"
    for old, target in ((SHA[:7], TARGET), (SHA, "main"),
                        (None, TARGET), (SHA, None)):
        assert update.begin_deploy(old, target, None, "op",
                                   run_dir=str(run_dir)) is False, (old, target)
    assert not run_dir.exists(), "a refused begin_deploy must write nothing"


# ---- git mutations, structurally scoped to ONE marked section ----------------
#
# A27 consciously rescoped the old whole-file ban: the apply engine NEEDS
# `fetch` and `merge`, so those two verbs are now permitted INSIDE the single
# marked mutation section at the END of update.py -- and remain banned as
# quoted argument literals everywhere before it. Every other mutation verb
# stays banned in the whole file, and `pull` is banned even in the mutation
# section, because there is no safe form of it here (it follows the branch's
# CONFIGURED upstream, not the constant the check verified).

MUTATION_MARKER = "==== THE APPLY MUTATION SECTION (A27) ===="


def _update_source():
    with open(update.__file__, "r", encoding="utf-8") as fh:
        return fh.read()


def test_git_mutations_live_only_in_the_marked_section():
    """Asserted as QUOTED argument literals, so prose in a docstring naming a
    forbidden command cannot trip it -- same trick as the original ban."""
    body = _update_source()
    assert body.count(MUTATION_MARKER) == 1, (
        "exactly one mutation section: the guard splits the file on it")
    head, tail = body.split(MUTATION_MARKER)
    for verb in ('"pull"', '"merge"', '"reset"', '"clean"', '"stash"',
                 '"checkout"', '"fetch"'):
        assert verb not in head, (
            "%s must never appear as a git argument outside the marked "
            "mutation section of update.py" % verb)
    for verb in ('"pull"', '"reset"', '"clean"', '"stash"', '"checkout"'):
        assert verb not in tail, (
            "%s is not one of the two verbs an apply needs; it must not "
            "appear even inside the mutation section" % verb)


def test_the_permitted_mutations_are_pinned_to_their_safe_forms():
    """fetch and merge are allowed in, but only in the shape the design
    demands: ff-only, and hooks disabled via a path that can never be a
    directory on either platform."""
    tail = _update_source().split(MUTATION_MARKER)[1]
    assert '"--ff-only"' in tail
    assert '"core.hooksPath=" + os.devnull' in tail


def test_git_pull_appears_nowhere_as_an_argument():
    """The inherited correction, held forever: `git pull` fetches whatever
    `origin` happens to point at on this machine -- a fork, a renamed remote,
    an arbitrary re-point -- not the constant the check verified."""
    assert '"pull"' not in _update_source()


# ---- dependency-delta detection over a broad file set (A25) -------------------
#
# #182 trap 7: a git pull updates code only; a build that needs a new
# dependency then fails to import, and the box is down along with the UI you
# would fix it with. Decision D4: NEVER auto-install -- a delta only ever
# REFUSES the apply. Three honest outcomes: established-empty (falsy),
# established-non-empty (refuse, naming files), and UNKNOWN (the diff could
# not be taken -- refuses, never passes).


@pytest.mark.parametrize("path", [
    # python project metadata
    "pyproject.toml", "setup.py", "setup.cfg",
    # requirements/constraints variants, incl. the requirements/ dir shape
    "requirements.txt", "requirements-dev.txt", "requirements_extra.txt",
    "requirements/dev.txt", "tools/requirements/prod.txt",
    "constraints.txt", "constraints-ci.txt",
    # lockfiles
    "poetry.lock", "uv.lock", "Pipfile", "Pipfile.lock",
    # conda
    "environment.yml", "environment.yaml", "conda-lock.yml",
    # vendored wheels, at any depth
    "wheels/httpx-0.27.0-py3-none-any.whl",
    # node manifests/locks: none exist in this repo today (vendored JS is
    # checked in and served as-is) -- see update.py's A25 section comment for
    # why the pattern is kept anyway
    "package.json", "package-lock.json", "npm-shrinkwrap.json",
    "yarn.lock", "pnpm-lock.yaml",
    # nested at depth: root-only would miss a future sub-package manifest
    "sub/pkg/pyproject.toml", "deep/er/requirements.txt",
    # case handling: manifests are conventionally cased, filesystems vary
    "PYPROJECT.TOML", "Requirements-Dev.TXT", "PIPFILE",
])
def test_every_pattern_family_fires(path):
    rep = update.dependency_delta(["webterm/broker/app.py", path])
    assert rep.known is True
    assert rep.matched == [path]
    assert bool(rep) is True, "a non-empty delta must be truthy (refuse)"


def test_unrelated_paths_do_not_fire():
    rep = update.dependency_delta([
        "webterm/broker/update.py", "wiki/Home.md", "10_css_root.css",
        "webterm/broker/vendor/codemirror/state.mjs", "media/shot.png",
        "notes/requirements.md",       # requirements-named, but not a .txt
        "setup.pyc",                   # not setup.py
        "tests/test_update_apply.py",
    ])
    assert rep.known is True and rep.matched == []
    assert bool(rep) is False, "an established empty delta must be falsy"


def test_an_empty_change_list_is_no_delta():
    rep = update.dependency_delta([])
    assert rep.known is True and rep.matched == []
    assert bool(rep) is False
    assert update.apply_preconditions(_snap(dependency_delta=rep)) == []


def test_families_say_why_each_path_matched():
    rep = update.dependency_delta(
        ["uv.lock", "requirements/dev.txt", "package.json",
         "vendored/x-1.0-py3-none-any.whl"])
    assert rep.families["uv.lock"] == "python-lock"
    assert rep.families["requirements/dev.txt"] == "requirements"
    assert rep.families["package.json"] == "node"
    assert rep.families["vendored/x-1.0-py3-none-any.whl"] == "wheel"


# ---- unknown refuses, never passes -------------------------------------------

def test_an_unknown_report_is_truthy_and_refuses():
    """The honesty rule: a diff that could not be taken must refuse -- a
    failed diff silently passing is exactly the trap-7 outage."""
    rep = update.DeltaReport(known=False, reason="target-object-absent")
    assert bool(rep) is True
    refusals = update.apply_preconditions(_snap(dependency_delta=rep))
    assert [r.reason_code for r in refusals] == [update.APPLY_DEPENDENCY_DELTA]
    assert "could not be established" in refusals[0].message
    assert "target-object-absent" in refusals[0].message


def test_the_classifier_never_launders_no_data_into_no_delta():
    """None, a bare string (whose CHARS would iterate and match nothing), and
    outright junk all come back unknown -- truthy, so they refuse."""
    for junk in (None, "pyproject.toml", b"pyproject.toml", 42):
        rep = update.dependency_delta(junk)
        assert rep.known is False, junk
        assert bool(rep) is True, junk


# ---- the refusal sentence ----------------------------------------------------

def test_the_delta_sentence_names_files_and_states_the_rule():
    rep = update.dependency_delta(["pyproject.toml", "uv.lock"])
    refusals = update.apply_preconditions(_snap(dependency_delta=rep))
    msg = refusals[0].message
    assert "pyproject.toml" in msg and "uv.lock" in msg
    assert "never installs" in msg, "the rule must be stated"
    assert "restart" in msg, "the by-hand procedure must be stated"


def test_the_sentence_is_bounded_for_a_fifty_file_delta():
    paths = ["requirements/extra-%02d.txt" % i for i in range(50)]
    rep = update.dependency_delta(paths)
    assert len(rep.matched) == 50
    msg = update.apply_preconditions(_snap(dependency_delta=rep))[0].message
    assert "requirements/extra-00.txt" in msg      # the first few are named...
    assert "requirements/extra-49.txt" not in msg  # ...but never all fifty
    assert "(+45 more)" in msg                     # the rest are counted
    assert len(msg) < 600


# ---- evaluator integration ---------------------------------------------------

def test_a_real_delta_report_fires_alongside_other_refusals():
    rep = update.dependency_delta(["uv.lock"])
    codes = _codes(_snap(dependency_delta=rep, dirty=True,
                         apply_enabled=False))
    assert set(codes) == {update.APPLY_DEPENDENCY_DELTA,
                          update.APPLY_DIRTY_TREE,
                          update.APPLY_DISABLED}
    out = update.evaluate_apply(_snap(dependency_delta=rep))
    assert out["ok"] is False
    assert [r["reason"] for r in out["refusals"]] == [
        update.APPLY_DEPENDENCY_DELTA]
    assert "uv.lock" in out["refusals"][0]["message"]


def test_a_no_delta_report_lets_a_clean_snapshot_proceed():
    rep = update.dependency_delta(["webterm/broker/update.py", "wiki/Home.md"])
    assert update.apply_preconditions(_snap(dependency_delta=rep)) == []


# ---- the impure collector (read-only git, injected) --------------------------

def _fake_delta_git(diff_output, *, have_target=True):
    """Stand-in for update._git covering the collector's two calls."""
    calls = []

    def fake(*args, timeout=5.0):
        calls.append(args)
        if args and args[0] == "cat-file":
            return "" if have_target else None
        if args and args[0] == "diff":
            return diff_output
        return ""
    return fake, calls


def test_the_collector_diffs_old_to_target_read_only(monkeypatch):
    fake, calls = _fake_delta_git("pyproject.toml\nwebterm/broker/app.py\n")
    monkeypatch.setattr(update, "_git", fake)
    rep = update.collect_dependency_delta(SHA, TARGET)
    assert rep.known is True and rep.matched == ["pyproject.toml"]
    diff_calls = [c for c in calls if c and c[0] == "diff"]
    assert diff_calls == [("diff", "--name-only",
                           "%s..%s" % (SHA, TARGET))], (
        "one name-only diff over old..target, and nothing else")


def test_an_absent_target_object_is_unknown(monkeypatch):
    """Pre-fetch, or a garbage-collected sha: the local store does not hold
    the target's objects, so the diff CANNOT be taken."""
    fake, _ = _fake_delta_git("", have_target=False)
    monkeypatch.setattr(update, "_git", fake)
    rep = update.collect_dependency_delta(SHA, TARGET)
    assert rep.known is False and bool(rep) is True
    assert rep.reason == "target-object-absent"


def test_a_failed_diff_is_unknown_not_empty(monkeypatch):
    fake, _ = _fake_delta_git(None)
    monkeypatch.setattr(update, "_git", fake)
    rep = update.collect_dependency_delta(SHA, TARGET)
    assert rep.known is False and bool(rep) is True
    assert rep.reason == "diff-failed"


def test_an_empty_diff_is_established_no_delta(monkeypatch):
    """'' from a zero-exit git is a REAL answer (no files changed) and must
    never be confused with the None a failed git returns."""
    fake, _ = _fake_delta_git("")
    monkeypatch.setattr(update, "_git", fake)
    rep = update.collect_dependency_delta(SHA, TARGET)
    assert rep.known is True and rep.matched == []
    assert bool(rep) is False


def test_the_collector_refuses_anything_but_full_shas(monkeypatch):
    """Same rule as begin_deploy: a short sha or ref name could name a
    different commit than the check established, and full-hex is also what
    makes option injection structurally impossible. Nothing reaches git."""
    called = []
    monkeypatch.setattr(update, "_git",
                        lambda *a, **k: called.append(a) or "")
    for old, target in ((SHA[:7], TARGET), (SHA, "main"), (None, TARGET),
                        (SHA, None), ("HEAD", TARGET)):
        rep = update.collect_dependency_delta(old, target)
        assert rep.known is False and bool(rep) is True, (old, target)
        assert rep.reason == "bad-ref", (old, target)
    assert not called, "a refused ref must never reach git"


# ---- the git phase (perform_apply, A27) --------------------------------------
#
# The engine is driven entirely through fakes of update.py's own git helpers
# (the same seam the collector tests above use): one shared `events` list per
# test, so the ordering guarantees are PROVED, not inferred. The journal half
# uses the REAL begin_deploy/finalize against tmp_path, because "pending" and
# "finalized" are on-disk facts the supervisor reads.


def _phase_fakes(monkeypatch, events, *, fetch_rc=0, merge_rc=0,
                 merge_stderr="", have_target=True, ff_possible=True,
                 target_upstream=True, fetched=TARGET, delta=None,
                 begin=None, head_after_merge=TARGET):
    shas = {"head": SHA}

    def fake_local_sha():
        return shas["head"]

    def fake_mutation(args, timeout=None):
        events.append(("mut", tuple(args)))
        if args and args[0] == "fetch":
            return {"returncode": fetch_rc, "stdout": "",
                    "stderr": ("fatal: could not read from remote"
                               if fetch_rc else "")}
        if merge_rc == 0:
            shas["head"] = head_after_merge
        return {"returncode": merge_rc, "stdout": "", "stderr": merge_stderr}

    def fake_ro(*args, timeout=5.0):
        events.append(("ro", args))
        if args and args[0] == "cat-file":
            return "" if have_target else None
        if args and args[0] == "rev-parse":
            return fetched
        if args and args[0] == "merge-base":
            if args[2] == "HEAD":
                return "" if ff_possible else None
            return "" if target_upstream else None
        return ""

    def fake_delta(old, target):
        events.append(("delta", old, target))
        return delta if delta is not None else update.DeltaReport(known=True)

    real_begin = update.begin_deploy

    def fake_begin(old, target, identity, op, run_dir=None):
        events.append(("journal", old, target, op))
        if begin is not None:
            return begin
        return real_begin(old, target, identity, op, run_dir=run_dir)

    monkeypatch.setattr(update, "local_sha", fake_local_sha)
    monkeypatch.setattr(update, "_git_mutation", fake_mutation)
    monkeypatch.setattr(update, "_git", fake_ro)
    monkeypatch.setattr(update, "collect_dependency_delta", fake_delta)
    monkeypatch.setattr(update, "begin_deploy", fake_begin)


def test_perform_apply_orders_fetch_verify_delta_journal_merge(tmp_path,
                                                               monkeypatch):
    """The order IS the contract: fetch before any verification can mean
    anything, delta before the journal, the journal before the one mutation
    of the worktree."""
    events = []
    _phase_fakes(monkeypatch, events)
    out = update.perform_apply(target_sha=TARGET, operation_id="op-1",
                               run_dir=str(tmp_path / "run"))
    assert out["ok"] is True, out
    kinds = [e[0] for e in events]
    assert events[0] == ("mut", ("fetch",
                                 "https://github.com/JohnConnorNPC/"
                                 "browserland.git", "main"))
    delta_at = kinds.index("delta")
    journal_at = kinds.index("journal")
    merge_at = [i for i, e in enumerate(events)
                if e[0] == "mut" and "merge" in e[1]][0]
    assert delta_at < journal_at < merge_at
    # every verify read happens after the fetch and before the delta
    for i, e in enumerate(events):
        if e[0] == "ro":
            assert 0 < i < delta_at, events


def test_the_fetch_names_the_pinned_url_never_a_remote_or_pull(tmp_path,
                                                               monkeypatch):
    """The correction this atom encodes: `git pull` follows the branch's
    CONFIGURED upstream, so the engine fetches the explicit https URL derived
    from UPSTREAM_REPO plus the explicit ref -- a fork's `origin` is
    structurally irrelevant, and `pull` appears in no argv at all."""
    events = []
    _phase_fakes(monkeypatch, events)
    update.perform_apply(target_sha=TARGET, operation_id="op-2",
                         run_dir=str(tmp_path / "run"))
    all_args = [a for e in events if e[0] == "mut" for a in e[1]]
    all_args += [a for e in events if e[0] == "ro" for a in e[1]]
    assert "origin" not in all_args
    assert not any("pull" in str(a) for a in all_args)
    assert update.upstream_git_url() == \
        "https://github.com/JohnConnorNPC/browserland.git"


def test_the_merge_is_ff_only_hooks_disabled_exact_sha(tmp_path, monkeypatch):
    events = []
    _phase_fakes(monkeypatch, events)
    update.perform_apply(target_sha=TARGET, operation_id="op-3",
                         run_dir=str(tmp_path / "run"))
    merge = [e[1] for e in events if e[0] == "mut" and "merge" in e[1]][0]
    assert merge[-1] == TARGET, "the merge target is the EXACT previewed sha"
    assert "--ff-only" in merge
    hook_at = merge.index("-c")
    assert merge[hook_at + 1] == "core.hooksPath=" + os.devnull
    assert hook_at < merge.index("merge"), \
        "-c must precede the verb or git reads it as a pathspec"


def test_success_leaves_the_journal_pending_for_the_supervisor(tmp_path,
                                                               monkeypatch):
    """A successful apply's journal is deliberately NOT consumed here: it is
    what makes the supervisor adjudicate the next generation."""
    events = []
    _phase_fakes(monkeypatch, events)
    run_dir = str(tmp_path / "run")
    out = update.perform_apply(target_sha=TARGET, operation_id="op-4",
                               expected_identity={"brokerId": "b-1"},
                               run_dir=run_dir)
    assert out["ok"] is True
    assert out["old_sha"] == SHA and out["target_sha"] == TARGET
    record = supervise.read_pending_deploy(run_dir)
    assert record is not None
    assert record["oldSha"] == SHA and record["targetSha"] == TARGET
    assert record["operationId"] == "op-4"
    assert record["expectedIdentity"] == {"brokerId": "b-1"}


def test_a_failed_fetch_aborts_before_any_journal_or_merge(tmp_path,
                                                           monkeypatch):
    events = []
    _phase_fakes(monkeypatch, events, fetch_rc=128)
    run_dir = str(tmp_path / "run")
    out = update.perform_apply(target_sha=TARGET, operation_id="op-5",
                               run_dir=run_dir)
    assert out["ok"] is False and out["stage"] == "fetch"
    assert [r["reason"] for r in out["refusals"]] == \
        [update.APPLY_FETCH_FAILED]
    assert "could not read" in out["stderr"]
    kinds = [e[0] for e in events]
    assert "journal" not in kinds and "delta" not in kinds
    assert len([e for e in events if e[0] == "mut"]) == 1, \
        "the merge must never run after a failed fetch"
    assert supervise.read_pending_deploy(run_dir) is None


@pytest.mark.parametrize("over,code", [
    ({"have_target": False}, update.APPLY_TARGET_MISSING),
    ({"ff_possible": False}, update.APPLY_NOT_FAST_FORWARD),
    ({"target_upstream": False}, update.APPLY_TARGET_NOT_UPSTREAM),
    ({"fetched": None}, update.APPLY_TARGET_NOT_UPSTREAM),
])
def test_verification_failures_each_refuse_by_name(tmp_path, monkeypatch,
                                                   over, code):
    """Object present, ff-possible, and reachable-from-the-fetched-head are
    three different facts with three different fixes; a sha upstream does not
    have must never be applied merely because someone possesses 40 hex
    characters."""
    events = []
    _phase_fakes(monkeypatch, events, **over)
    out = update.perform_apply(target_sha=TARGET, operation_id="op-6",
                               run_dir=str(tmp_path / "run"))
    assert out["ok"] is False and out["stage"] == "verify"
    assert [r["reason"] for r in out["refusals"]] == [code]
    kinds = [e[0] for e in events]
    assert "journal" not in kinds
    assert len([e for e in events if e[0] == "mut"]) == 1, \
        "a failed verification must abort before the merge"


def test_a_dependency_delta_post_fetch_aborts_before_the_journal(tmp_path,
                                                                 monkeypatch):
    events = []
    delta = update.dependency_delta(["pyproject.toml"])
    _phase_fakes(monkeypatch, events, delta=delta)
    out = update.perform_apply(target_sha=TARGET, operation_id="op-7",
                               run_dir=str(tmp_path / "run"))
    assert out["ok"] is False and out["stage"] == "delta"
    assert [r["reason"] for r in out["refusals"]] == \
        [update.APPLY_DEPENDENCY_DELTA]
    assert "pyproject.toml" in out["refusals"][0]["message"]
    assert "journal" not in [e[0] for e in events]


def test_an_unknown_delta_refuses_the_merge_too(tmp_path, monkeypatch):
    """A diff that could not be taken refuses -- post-fetch this time, same
    honesty rule as the A25 classifier."""
    events = []
    _phase_fakes(monkeypatch, events,
                 delta=update.DeltaReport(known=False, reason="diff-failed"))
    out = update.perform_apply(target_sha=TARGET, operation_id="op-7b",
                               run_dir=str(tmp_path / "run"))
    assert out["ok"] is False and out["stage"] == "delta"
    assert len([e for e in events if e[0] == "mut"]) == 1


def test_an_unwritable_journal_refuses_before_the_merge(tmp_path,
                                                        monkeypatch):
    """No journal means the supervisor adjudicates nothing -- a deploy whose
    failure nobody observes -- so the merge must never run unjournalled."""
    events = []
    _phase_fakes(monkeypatch, events, begin=False)
    out = update.perform_apply(target_sha=TARGET, operation_id="op-8",
                               run_dir=str(tmp_path / "run"))
    assert out["ok"] is False and out["stage"] == "journal"
    assert [r["reason"] for r in out["refusals"]] == \
        [update.APPLY_NO_JOURNAL]
    assert len([e for e in events if e[0] == "mut"]) == 1


def test_a_failed_merge_is_tree_suspect_and_finalizes_the_journal(
        tmp_path, monkeypatch):
    """Trap 12. A merge that fails partway on Windows file locking leaves a
    tree nobody can vouch for: report tree-suspect with the git stderr, do
    NOT restart -- and finalize the journal, because a journal left pending
    with no restart coming would be adjudicated against whatever manual
    restart happens hours later."""
    events = []
    stderr = ("error: unable to unlink old 'webterm/broker/app.py': "
              "Permission denied")
    _phase_fakes(monkeypatch, events, merge_rc=1, merge_stderr=stderr)
    run_dir = str(tmp_path / "run")
    out = update.perform_apply(target_sha=TARGET, operation_id="op-9",
                               run_dir=run_dir)
    assert out["ok"] is False and out["stage"] == "merge"
    assert out["tree_suspect"] is True
    assert [r["reason"] for r in out["refusals"]] == \
        [update.APPLY_TREE_SUSPECT]
    assert "unable to unlink" in out["stderr"]
    assert out["journal_cancelled"] is True
    # consumed, never adjudicated -- and the outcome file says why
    assert supervise.read_pending_deploy(run_dir) is None
    outcome = json.loads(
        (Path(run_dir) / supervise.DEPLOY_OUTCOME_FILENAME).read_text(
            encoding="utf-8"))
    assert outcome["outcome"] == update.DEPLOY_CANCELLED
    assert outcome["record"]["operationId"] == "op-9"


def test_a_merge_that_lands_off_target_is_tree_suspect(tmp_path, monkeypatch):
    """rc 0 is not proof: HEAD must actually BE the previewed commit
    afterwards, or the tree cannot be vouched for."""
    events = []
    _phase_fakes(monkeypatch, events, head_after_merge=SHA)  # HEAD never moved
    run_dir = str(tmp_path / "run")
    out = update.perform_apply(target_sha=TARGET, operation_id="op-10",
                               run_dir=run_dir)
    assert out["ok"] is False and out["tree_suspect"] is True
    assert out["journal_cancelled"] is True
    assert supervise.read_pending_deploy(run_dir) is None


def test_the_reported_stderr_is_bounded(tmp_path, monkeypatch):
    """A failed checkout can name every locked file in the tree; the JSON
    error body must not carry all of it."""
    events = []
    _phase_fakes(monkeypatch, events, merge_rc=1, merge_stderr="x" * 10_000)
    out = update.perform_apply(target_sha=TARGET, operation_id="op-11",
                               run_dir=str(tmp_path / "run"))
    assert out["stderr"] is not None
    assert len(out["stderr"]) <= update.MUTATION_STDERR_CAP + 20


def test_perform_apply_refuses_anything_but_a_full_sha(monkeypatch):
    """Same rule as begin_deploy and the delta collector: full lowercase
    40-hex only, checked before ANY argv is built."""
    called = []
    monkeypatch.setattr(
        update, "_git_mutation",
        lambda *a, **k: called.append(a) or {"returncode": 0, "stdout": "",
                                             "stderr": ""})
    for bad in (TARGET[:7], "main", None, "HEAD~1", "B" * 40):
        out = update.perform_apply(target_sha=bad, operation_id="op")
        assert out["ok"] is False, bad
        assert [r["reason"] for r in out["refusals"]] == \
            [update.APPLY_BAD_TARGET], bad
    assert called == [], "a refused target must never reach git"


def test_cancel_pending_deploy_consumes_and_records(tmp_path, monkeypatch):
    monkeypatch.setattr(update, "local_sha", lambda: SHA)
    run_dir = str(tmp_path / "run")
    assert update.begin_deploy(SHA, TARGET, None, "op-c",
                               run_dir=run_dir) is True
    assert update.cancel_pending_deploy("testing the cancel",
                                        run_dir=run_dir) is True
    assert supervise.read_pending_deploy(run_dir) is None
    outcome = json.loads(
        (Path(run_dir) / supervise.DEPLOY_OUTCOME_FILENAME).read_text(
            encoding="utf-8"))
    assert outcome["outcome"] == update.DEPLOY_CANCELLED
    assert outcome["detail"] == "testing the cancel"
    # with nothing pending it is a no-op, never an invented outcome
    assert update.cancel_pending_deploy("again", run_dir=run_dir) is False


# ---- the last-deploy outcome accessor (A28) -----------------------------------
#
# The surfacing half of the rollback atom: the supervisor finalizes every
# deploy into ONE current outcome file, and last_deploy_outcome is the
# read-only accessor the route layer uses so a browser can render "apply
# failed, rolled back" AFTER the restart that did it. Tolerant of absence and
# corruption -> None, because "no outcome to show" must never take down the
# response that would have carried it.

def test_last_deploy_outcome_roundtrips_a_finalized_outcome(tmp_path):
    run_dir = str(tmp_path / "run")
    record = supervise.build_deploy_record(SHA, TARGET, None, "op-ld")
    outcome = supervise.classify_deploy_outcome(
        record, ready=False, observed_sha=None, detail="worker-exited-1")
    assert supervise.finalize_deploy(run_dir, record, outcome, now=99.0)
    out = update.last_deploy_outcome(run_dir=run_dir)
    assert out is not None
    assert out["outcome"] == supervise.DEPLOY_NEVER_CAME_UP
    assert out["detail"] == "worker-exited-1"
    assert out["record"]["oldSha"] == SHA
    assert out["record"]["targetSha"] == TARGET, \
        "the failed target sha must stay visible to the UI"
    assert out["finalizedAt"] == 99.0


def test_last_deploy_outcome_carries_a_rolled_back_verdict(tmp_path):
    """The shape the UI atom will render: rolled-back, with the original
    failure in the detail."""
    run_dir = str(tmp_path / "run")
    record = supervise.build_deploy_record(SHA, TARGET, None, "op-rb")
    assert supervise.finalize_deploy(
        run_dir, record,
        {"outcome": supervise.DEPLOY_ROLLED_BACK, "observedSha": None,
         "detail": "restart-budget-exhausted"})
    out = update.last_deploy_outcome(run_dir=run_dir)
    assert out["outcome"] == supervise.DEPLOY_ROLLED_BACK
    assert out["detail"] == "restart-budget-exhausted"


def test_last_deploy_outcome_defaults_to_the_supervisors_run_dir(
        tmp_path, monkeypatch):
    """Same environment default as begin_deploy, so the route can call it
    bare inside a supervised worker."""
    run_dir = str(tmp_path / "envrun")
    record = supervise.build_deploy_record(SHA, TARGET, None, "op-env2")
    assert supervise.finalize_deploy(
        run_dir, record,
        {"outcome": supervise.DEPLOY_READY_ON_TARGET, "observedSha": TARGET,
         "detail": None})
    monkeypatch.setenv(supervise.ENV_RUN_DIR, run_dir)
    out = update.last_deploy_outcome()
    assert out is not None
    assert out["outcome"] == supervise.DEPLOY_READY_ON_TARGET


def test_last_deploy_outcome_tolerates_absence_and_corruption(tmp_path,
                                                              monkeypatch):
    monkeypatch.delenv(supervise.ENV_RUN_DIR, raising=False)
    assert update.last_deploy_outcome() is None            # unsupervised
    assert update.last_deploy_outcome(
        run_dir=str(tmp_path / "never-existed")) is None   # absent dir
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    assert update.last_deploy_outcome(run_dir=str(run_dir)) is None  # no file
    path = run_dir / supervise.DEPLOY_OUTCOME_FILENAME
    for junk in ("{not json", "[]", '"a string"', "42",
                 json.dumps({"no": "outcome key"}),
                 json.dumps({"outcome": 42})):
        path.write_text(junk, encoding="utf-8")
        assert update.last_deploy_outcome(run_dir=str(run_dir)) is None, junk


# ---- the preview-mismatch refusal (pure) --------------------------------------

def test_preview_mismatch_fires_only_when_both_facts_exist_and_differ():
    """Absence of either sha is state-unknown's job; agreement refuses
    nothing; a difference is its own named refusal -- the human previewed
    stale facts and must re-preview, never have another sha silently
    applied."""
    assert update.preview_mismatch_refusal(TARGET, TARGET) is None
    assert update.preview_mismatch_refusal(None, TARGET) is None
    assert update.preview_mismatch_refusal(TARGET, None) is None
    assert update.preview_mismatch_refusal("", TARGET) is None
    ref = update.preview_mismatch_refusal(TARGET, OTHER)
    assert ref is not None
    assert ref.reason_code == update.APPLY_PREVIEW_MISMATCH == \
        "preview-sha-mismatch"
    assert TARGET[:12] in ref.message and OTHER[:12] in ref.message


# ---- the route: POST /update/apply (A27) --------------------------------------
#
# Route tests fake the layers underneath exactly the way test_restart.py
# does: collect_apply_snapshot / perform_apply are recording fakes resolved
# through the module object, and request_restart is a spy so no server is
# ever actually stopped. Nothing here touches the network or the real
# checkout.

_route_seq = 0


def _apply_app(tmp_path, monkeypatch, *, apply_enabled=True,
               restart_enabled=True, **cfg):
    global _route_seq
    _route_seq += 1
    monkeypatch.delenv("WEB_TERMINAL_TOKEN", raising=False)
    base = {"auth_token": TEST_TOKEN,
            "state_path": str(tmp_path / "webterm_state.json"),
            "update_apply_enabled": apply_enabled,
            "restart_enabled": restart_enabled}
    base.update(cfg)
    app = create_app(base, name=f"webterm-apply-route-{_route_seq}")
    # The documented per-app capability seam (see test_restart.py): the route
    # must see a restartable broker without probing this machine.
    app.ctx.restart_capability = {"supported": True,
                                  "mechanism": supervise.MECHANISM_SUPERVISOR,
                                  "reason_code": None}
    return app


def _patch_collect(monkeypatch, snap):
    calls = []

    def fake(**kwargs):
        calls.append(kwargs)
        return snap

    monkeypatch.setattr(update, "collect_apply_snapshot", fake)
    return calls


def _ok_apply(**kwargs):
    return {"ok": True, "stage": None, "old_sha": SHA,
            "target_sha": kwargs.get("target_sha"),
            "operation_id": kwargs.get("operation_id"),
            "refusals": [], "tree_suspect": False,
            "journal_cancelled": None, "stderr": None}


def _patch_perform(monkeypatch, result=None, explode=False):
    calls = []

    def fake(**kwargs):
        calls.append(kwargs)
        if explode:
            raise AssertionError(
                "the git phase was reached when it must not be")
        if result is not None:
            out = dict(result)
            out.setdefault("target_sha", kwargs.get("target_sha"))
            out.setdefault("operation_id", kwargs.get("operation_id"))
            return out
        return _ok_apply(**kwargs)

    monkeypatch.setattr(update, "perform_apply", fake)
    return calls


_RESTART_OK = {"ok": True, "lifecycle": "restart_ready", "reason": None,
               "waited_for": 0, "finished": 0, "timed_out": 0,
               "aborted_uploads": [], "aborted_recordings": [],
               "elapsed": 0.01, "armed": True, "stopping": True,
               "mechanism": supervise.MECHANISM_SUPERVISOR}


def _spy_restart(monkeypatch, result=None):
    calls = []
    payload = dict(_RESTART_OK if result is None else result)

    async def fake(app, **kwargs):
        calls.append(kwargs)
        if payload.get("ok"):
            app.ctx.lifecycle = app_mod.LIFECYCLE_RESTART_READY
        else:
            app_mod.resume_from_quiesce(app)     # what the real one does
        return dict(payload)

    monkeypatch.setattr(app_mod, "request_restart", fake)
    return calls


def test_apply_401s_unauthenticated(tmp_path, monkeypatch):
    """The shared gate, first and always; test_auth_mandatory's live-router
    walk pins the same thing for this route by enumeration."""
    performs = _patch_perform(monkeypatch, explode=True)
    app = _apply_app(tmp_path, monkeypatch)
    # RAW client on purpose: no token.
    _, r = app.test_client.post("/update/apply", json={"target_sha": TARGET})
    assert r.status == 401
    assert r.json["error"] == "auth_required"
    assert performs == []


def test_apply_is_post_only(tmp_path, monkeypatch):
    app = _apply_app(tmp_path, monkeypatch)
    _, r = authed(app).get("/update/apply")
    assert r.status == 405, r.status


def test_apply_answers_its_preflight(tmp_path, monkeypatch):
    """Registered in the explicit preflight list: route resolution precedes
    request middleware, so an unrouted OPTIONS would 405 and a cross-origin
    operator's browser would report an opaque network error."""
    app = _apply_app(tmp_path, monkeypatch)
    _, r = app.test_client.options("/update/apply")
    assert r.status == 204
    assert r.headers.get("Access-Control-Allow-Origin") == "*"


def test_apply_503s_when_the_gate_is_off(tmp_path, monkeypatch):
    performs = _patch_perform(monkeypatch, explode=True)
    app = _apply_app(tmp_path, monkeypatch, apply_enabled=False)
    _, r = authed(app).post("/update/apply", json={"target_sha": TARGET})
    assert r.status == 503, r.json
    assert r.json["error"] == "update_apply_disabled"
    assert performs == []
    assert app.ctx.update_apply_claim is None


def test_a_cross_origin_apply_is_refused(tmp_path, monkeypatch):
    """Same rationale as POST /restart: _cors_headers answers ACAO:* to
    everyone, so for a route that replaces the running code the token cannot
    be the only gate."""
    performs = _patch_perform(monkeypatch, explode=True)
    app = _apply_app(tmp_path, monkeypatch)
    for origin in ("http://evil.example", "https://evil.example:8443",
                   "null"):
        _, r = authed(app).post("/update/apply",
                                json={"target_sha": TARGET},
                                headers={"Origin": origin})
        assert r.status == 403, f"{origin} answered {r.status}"
        assert r.json["error"] == "forbidden_origin"
    assert performs == []


@pytest.mark.parametrize("body,error", [
    ([1, 2], "bad_json"),                                # not an object
    ({}, "bad_target_sha"),                              # missing
    ({"target_sha": "b" * 39}, "bad_target_sha"),        # short
    ({"target_sha": "B" * 40}, "bad_target_sha"),        # not lowercase hex
    ({"target_sha": "main"}, "bad_target_sha"),          # a ref, not a sha
    ({"target_sha": 42}, "bad_target_sha"),              # not a string
])
def test_a_malformed_target_is_a_400(tmp_path, monkeypatch, body, error):
    performs = _patch_perform(monkeypatch, explode=True)
    app = _apply_app(tmp_path, monkeypatch)
    _, r = authed(app).post("/update/apply", json=body)
    assert r.status == 400, r.json
    assert r.json["error"] == error
    assert performs == []


def test_precondition_refusals_all_come_back(tmp_path, monkeypatch):
    """Server-side preconditions re-run FRESH, and every failing one is in
    the 409 -- the evaluator accumulates so the human fixes the list once."""
    performs = _patch_perform(monkeypatch, explode=True)
    _patch_collect(monkeypatch, _snap(dirty=True, restart_available=False))
    app = _apply_app(tmp_path, monkeypatch)
    _, r = authed(app).post("/update/apply", json={"target_sha": TARGET})
    assert r.status == 409, r.json
    assert r.json["error"] == "apply_refused"
    assert set(r.json["reason_codes"]) == {update.APPLY_DIRTY_TREE,
                                           update.APPLY_RESTART_UNAVAILABLE}
    for refusal in r.json["refusals"]:
        assert refusal["message"]
    assert performs == []
    assert app.ctx.update_apply_claim is None
    assert app.ctx.lifecycle == app_mod.LIFECYCLE_RUNNING


def test_a_stale_preview_is_its_own_named_refusal(tmp_path, monkeypatch):
    """Upstream moved between the preview and the click: the check no longer
    names the sha the human saw, and the server must never silently apply a
    different one."""
    performs = _patch_perform(monkeypatch, explode=True)
    _patch_collect(monkeypatch, _snap())        # the check names TARGET
    app = _apply_app(tmp_path, monkeypatch)
    _, r = authed(app).post("/update/apply", json={"target_sha": OTHER})
    assert r.status == 409, r.json
    assert r.json["reason_codes"] == [update.APPLY_PREVIEW_MISMATCH]
    msg = r.json["refusals"][0]["message"]
    assert TARGET[:12] in msg and OTHER[:12] in msg
    assert performs == []


def test_a_second_apply_gets_409_with_the_same_operation_id(tmp_path,
                                                            monkeypatch):
    """Mirrors the restart CAS: the loser is told the truth -- one is under
    way -- and told WHICH, so a retrying client can recognise its own work."""
    performs = _patch_perform(monkeypatch, explode=True)
    app = _apply_app(tmp_path, monkeypatch)
    app.ctx.update_apply_claim = {"operation_id": "apply-first"}
    _, r = authed(app).post("/update/apply", json={"target_sha": TARGET})
    assert r.status == 409, r.json
    assert r.json["error"] == "apply_in_progress"
    assert r.json["reason_code"] == "apply-in-progress"
    assert r.json["operation_id"] == "apply-first"
    assert performs == []
    assert app.ctx.update_apply_claim == {"operation_id": "apply-first"}, \
        "the loser must not release the winner's claim"


def test_two_concurrent_applies_produce_one_operation(tmp_path, monkeypatch):
    """The CAS under real concurrency: the winner is genuinely in flight
    (queued on update_lock, which the test holds) when the loser arrives, and
    the loser is answered IMMEDIATELY with the winner's operation id rather
    than queued behind a fetch."""
    performs = _patch_perform(monkeypatch)
    _patch_collect(monkeypatch, _snap())
    restarts = _spy_restart(monkeypatch)
    app = _apply_app(tmp_path, monkeypatch)
    with authed_reusable(app) as client:
        url = with_token(
            f"http://{client.host}:{client.port}/update/apply",
            app.ctx.auth_token)

        async def _drive():
            await app.ctx.update_lock.acquire()
            first = asyncio.ensure_future(
                client._session.post(url, json={"target_sha": TARGET}))
            for _ in range(500):
                if app.ctx.update_apply_claim is not None:
                    break
                await asyncio.sleep(0.01)
            assert app.ctx.update_apply_claim is not None, \
                "the first request never claimed the apply"
            op = app.ctx.update_apply_claim["operation_id"]
            second = await client._session.post(
                url, json={"target_sha": TARGET})
            assert second.status_code == 409, second.text
            assert second.json()["operation_id"] == op
            assert second.json()["reason_code"] == "apply-in-progress"
            app.ctx.update_lock.release()
            winner = await first
            return op, winner

        op, winner = client._loop.run_until_complete(_drive())

    assert winner.status_code == 202, winner.text
    assert winner.json()["operation_id"] == op
    assert len(performs) == 1, "two requests must produce ONE git phase"
    assert len(restarts) == 1


def test_a_revoked_gate_is_honoured_inside_the_lock(tmp_path, monkeypatch):
    """The revoke-TOCTOU twin of the check gate's rule: a revoke that lands
    while the request is queued on update_lock must win -- checking only at
    the top of the handler would leave a window exactly as wide as the lock
    is held."""
    performs = _patch_perform(monkeypatch, explode=True)
    _patch_collect(monkeypatch, _snap())
    app = _apply_app(tmp_path, monkeypatch)
    with authed_reusable(app) as client:
        url = with_token(
            f"http://{client.host}:{client.port}/update/apply",
            app.ctx.auth_token)

        async def _drive():
            await app.ctx.update_lock.acquire()
            req = asyncio.ensure_future(
                client._session.post(url, json={"target_sha": TARGET}))
            for _ in range(500):
                if app.ctx.update_apply_claim is not None:
                    break
                await asyncio.sleep(0.01)
            assert app.ctx.update_apply_claim is not None, \
                "the request never passed the top gate"
            app.ctx.update_apply_enabled = False   # the revoke lands here
            app.ctx.update_lock.release()
            return await req

        r = client._loop.run_until_complete(_drive())

    assert r.status_code == 503, r.text
    assert r.json()["error"] == "update_apply_disabled"
    assert performs == []
    assert app.ctx.update_apply_claim is None


def test_a_successful_apply_answers_202_and_arms_the_restart(tmp_path,
                                                             monkeypatch):
    """202, never 200, and never the word "updated": the response is written
    by the process being replaced. The client proves the update by a CHANGED
    bootId and a fresh check reporting current."""
    performs = _patch_perform(monkeypatch)
    _patch_collect(monkeypatch, _snap())
    restarts = _spy_restart(monkeypatch)
    app = _apply_app(tmp_path, monkeypatch)
    _, r = authed(app).post("/update/apply", json={"target_sha": TARGET})
    assert r.status == 202, r.json
    assert r.json["ok"] is True
    assert r.json["bootId"] == app_mod.BOOT_ID
    assert r.json["old_sha"] == SHA and r.json["target_sha"] == TARGET
    op = r.json["operation_id"]
    assert op.startswith("apply-")
    assert r.headers.get("Cache-Control") == "no-store"
    assert len(performs) == 1
    assert performs[0]["target_sha"] == TARGET
    assert performs[0]["repo"] == update.UPSTREAM_REPO
    assert performs[0]["branch"] == update.UPSTREAM_BRANCH
    assert performs[0]["operation_id"] == op
    assert len(restarts) == 1, "the apply-aware restart was not armed"
    # The claim rides to process death on purpose: a follow-up apply must be
    # told THIS operation is in flight, not start a second one.
    assert app.ctx.update_apply_claim == {"operation_id": op}


def test_the_audit_precedes_the_mutation_which_precedes_the_arm(
        tmp_path, monkeypatch, caplog):
    """Who/when/old/new is written BEFORE the git phase (a merge that wedges
    must still leave a record of who asked), and the restart is armed only
    after the merge reported ok."""
    caplog.set_level(logging.WARNING, logger=app_mod.LOGGER.name)
    events = []

    def fake_perform(**kwargs):
        events.append("mutate")
        assert any("UPDATE APPLY" in rec.getMessage()
                   for rec in caplog.records), \
            "the audit line must be written BEFORE the mutation"
        return _ok_apply(**kwargs)

    monkeypatch.setattr(update, "perform_apply", fake_perform)
    _patch_collect(monkeypatch, _snap())

    async def fake_restart(app, **kwargs):
        events.append("restart")
        app.ctx.lifecycle = app_mod.LIFECYCLE_RESTART_READY
        return dict(_RESTART_OK)

    monkeypatch.setattr(app_mod, "request_restart", fake_restart)
    app = _apply_app(tmp_path, monkeypatch)
    _, r = authed(app).post("/update/apply", json={"target_sha": TARGET})
    assert r.status == 202, r.json
    assert events == ["mutate", "restart"], events
    audit = [rec.getMessage() for rec in caplog.records
             if "UPDATE APPLY" in rec.getMessage()]
    assert audit and SHA in audit[0] and TARGET in audit[0]


def test_a_tree_suspect_merge_reports_and_does_not_restart(tmp_path,
                                                           monkeypatch):
    """Failure honesty (trap 12): the tree is suspect, the journal was
    finalized by the engine, and NOTHING may restart onto a tree nobody can
    vouch for."""
    _patch_collect(monkeypatch, _snap())
    restarts = _spy_restart(monkeypatch)
    _patch_perform(monkeypatch, result={
        "ok": False, "stage": "merge", "old_sha": SHA,
        "refusals": [{"reason": update.APPLY_TREE_SUSPECT,
                      "message": "the merge failed partway"}],
        "tree_suspect": True, "journal_cancelled": True,
        "stderr": "error: unable to unlink old 'x': Permission denied"})
    app = _apply_app(tmp_path, monkeypatch)
    _, r = authed(app).post("/update/apply", json={"target_sha": TARGET})
    assert r.status == 409, r.json
    assert r.json["error"] == "apply_failed"
    assert r.json["tree_suspect"] is True
    assert r.json["journal_cancelled"] is True
    assert r.json["reason_codes"] == [update.APPLY_TREE_SUSPECT]
    assert "unable to unlink" in r.json["stderr"]
    assert restarts == [], "a suspect tree must NOT be restarted onto"
    assert app.ctx.lifecycle == app_mod.LIFECYCLE_RUNNING, \
        "a failed apply left the broker refusing new work"
    assert app.ctx.update_apply_claim is None


def test_an_unarmable_restart_cancels_the_journal(tmp_path, monkeypatch):
    """The merge landed but no restart will follow: the journal must be
    finalized NOW (a pending journal with no restart coming would be
    adjudicated against whatever manual restart happens hours later), and the
    body must say honestly that the tree IS updated while the old code still
    runs."""
    _patch_collect(monkeypatch, _snap())
    _patch_perform(monkeypatch)
    _spy_restart(monkeypatch,
                 result=dict(_RESTART_OK, ok=False, armed=False,
                             stopping=False,
                             reason="critical_sections_timed_out"))
    cancels = []
    monkeypatch.setattr(update, "cancel_pending_deploy",
                        lambda reason, **kw: cancels.append(reason) or True)
    app = _apply_app(tmp_path, monkeypatch)
    _, r = authed(app).post("/update/apply", json={"target_sha": TARGET})
    assert r.status == 503, r.json
    assert r.json["error"] == "apply_incomplete"
    assert r.json["tree_updated"] is True, \
        "the merge DID land; the body must say so honestly"
    assert r.json["journal_cancelled"] is True
    assert cancels and "critical_sections_timed_out" in cancels[0]
    assert app.ctx.lifecycle == app_mod.LIFECYCLE_RUNNING
    assert app.ctx.update_apply_claim is None
