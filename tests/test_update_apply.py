"""Apply preconditions for the update-apply feature (#182 Part 2, atom A24).

Pure predicates over an injected snapshot -- no network, no Sanic, no route
(that is a later atom), and no git mutation anywhere. The one impure piece
(``collect_apply_snapshot``) is tested by monkeypatching the module's own
read-only git helper, never by mutating a repo.

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

import pytest

from webterm.broker import update


SHA = "a" * 40
TARGET = "b" * 40


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


# ---- no git mutations, structurally -----------------------------------------

def test_the_module_never_shells_out_to_a_git_mutation():
    """This atom runs no git mutations at all: the evaluator is pure and the
    collector is read-only (status / rev-parse / cat-file / rev-list).
    Asserted as QUOTED argument literals, so prose in a docstring naming the
    forbidden command cannot trip it."""
    with open(update.__file__, "r", encoding="utf-8") as fh:
        body = fh.read()
    for verb in ('"pull"', '"merge"', '"reset"', '"clean"', '"stash"',
                 '"checkout"', '"fetch"'):
        assert verb not in body, (
            "%s must never appear as a git argument in update.py" % verb)
