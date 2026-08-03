"""GH#182 Part 1 -- is this build current with upstream?

Pure logic plus ONE hardened outbound primitive. No Sanic, no app.ctx: the whole
module is unit-testable by injecting ``fetcher``. The route that exposes it lives
in ``app.py`` (GET /update/check) and is gated twice -- by the browser token and
by a server-side operator switch (``update_check_enabled``) -- so a broker that
was never opted in makes NO outbound request at all.

Design notes that are load-bearing, each from the adversarial review of the plan:

* **A compare 404 is NOT an ahead/diverged signal.** The issue's original design
  said it was. It is not: that endpoint also 404s for a garbage-collected or
  unknown sha, a repo that moved or went private, a malformed ref, and
  unstably around a force-push -- the same checkout can read ``diverged`` now
  and 404 an hour later. So ahead/behind are derived ONLY from a 200, and every
  404 is ``unknown(compare-unavailable)``. Guessing here is exactly the failure
  a version checker cannot afford.

* **The local sha comes from ``git rev-parse HEAD``, at full length.**
  ``build_version()`` is a display string carrying a SHORT sha; short shas are
  ambiguity-prone and that string is not an API.

* **An unauthenticated 304 still costs rate-limit quota.** GitHub only promises
  conditional requests are free when the request is authenticated. ETags are
  kept here for bandwidth, never as a quota strategy -- the quota strategy is a
  DAILY ttl with jitter, single-flight, and negative caching until the reset.

* **The upstream repo is a constant.** Deriving it from ``git remote get-url``
  would make a fork check itself and report ``current`` forever.
"""

from __future__ import annotations

import json
import os
import random
import re
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from . import supervise

# ---- constants --------------------------------------------------------------

# The upstream this build is measured against. A CONSTANT on purpose (see the
# module docstring); config may override it for someone genuinely tracking their
# own fork, but it is never derived at runtime.
UPSTREAM_REPO = "JohnConnorNPC/browserland"
UPSTREAM_BRANCH = "main"
GITHUB_API = "https://api.github.com"
GITHUB_WEB = "https://github.com"

FETCH_TIMEOUT = 6.0
# Two caps, not one. /releases/latest is a small object; /compare carries commit
# data, file metadata and patches for up to 300 changed files, so the 512 KB that
# is generous for a status page rejects perfectly ordinary comparisons.
RELEASE_MAX_BYTES = 512 * 1024
COMPARE_MAX_BYTES = 8 * 1024 * 1024

# Daily, not hourly. 60 requests/hour is shared across every process on the
# source IP -- CI, developers, other brokers behind the same NAT. An update check
# has no business spending that. Jitter keeps a fleet from synchronising.
CHECK_TTL = 24 * 60 * 60
CHECK_TTL_JITTER = 60 * 60

STATE_CURRENT = "current"
STATE_BEHIND = "behind"
STATE_AHEAD = "ahead-or-diverged"
STATE_UNKNOWN = "unknown"

# Every unknown carries one of these, so the UI can say WHY rather than shrug.
REASON_NO_GIT = "no-git"
REASON_COMPARE_UNAVAILABLE = "compare-unavailable"
REASON_RATE_LIMITED = "rate-limited"
REASON_OFFLINE = "offline"
REASON_BAD_RESPONSE = "bad-response"
REASON_TOO_LARGE = "too-large"


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Refuse every redirect, so a redirecting or compromised upstream cannot
    bounce this fetch at an internal address (SSRF). Twin of the handler in
    ``app.py`` for /status/fetch; deliberately NOT imported from there, because
    app.py imports this module and the cycle would be worse than the duplication.
    If you harden one, harden both."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.HTTPError(
            req.full_url, code, "redirect refused (update check)", headers, fp)


# Built once at import: our no-redirect handler (a HTTPRedirectHandler subclass,
# so build_opener drops the permissive default) plus an EMPTY ProxyHandler, so a
# broker-process HTTP(S)_PROXY/ALL_PROXY cannot re-route this egress. The only
# outbound destination is api.github.com, dialed direct.
_OPENER = urllib.request.build_opener(
    _NoRedirectHandler, urllib.request.ProxyHandler({}))


# ---- the outbound primitive (A1) --------------------------------------------

def fetch_json(path: str, *, max_bytes: int,
               etag: Optional[str] = None) -> Dict[str, Any]:
    """GET one api.github.com path. Returns a dict -- never raises, never
    retries. Shape:

        {"status": int|None, "data": Any|None, "etag": str|None,
         "reset_at": float|None, "error": str|None}

    ``status`` is None when nothing was received at all (DNS, TCP, timeout,
    refused redirect). ``reset_at`` is a unix timestamp parsed from a 403/429 so
    the caller can negative-cache until then instead of hammering a limit that
    is already exhausted -- this function NEVER retries a 403/429 itself.
    """
    url = GITHUB_API.rstrip("/") + path
    # Belt and suspenders against the constant ever being edited to something
    # non-https: the allowlist is structural, this is the assertion.
    if urllib.parse.urlsplit(url).scheme != "https":
        return {"status": None, "data": None, "etag": None,
                "reset_at": None, "error": "non_https"}
    headers = {
        "User-Agent": "browserland-update/1.0 (+#182)",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if etag:
        # Bandwidth only. An unauthenticated 304 still costs quota -- see the
        # module docstring. Do not "optimise" the ttl on the strength of this.
        headers["If-None-Match"] = etag
    req = urllib.request.Request(url, headers=headers)
    try:
        with _OPENER.open(req, timeout=FETCH_TIMEOUT) as resp:
            body = resp.read(max_bytes + 1)
            if len(body) > max_bytes:
                return {"status": resp.status, "data": None,
                        "etag": None, "reset_at": None,
                        "error": REASON_TOO_LARGE}
            try:
                data = json.loads(body.decode("utf-8", "replace"))
            except ValueError:
                return {"status": resp.status, "data": None, "etag": None,
                        "reset_at": None, "error": REASON_BAD_RESPONSE}
            return {"status": resp.status, "data": data,
                    "etag": resp.headers.get("ETag"),
                    "reset_at": None, "error": None}
    except urllib.error.HTTPError as exc:
        reset_at = None
        if exc.code in (403, 429):
            reset_at = _parse_reset(exc.headers)
        # 304 is a success with no body: the caller keeps what it cached.
        return {"status": exc.code, "data": None,
                "etag": exc.headers.get("ETag") if exc.headers else None,
                "reset_at": reset_at,
                "error": None if exc.code == 304 else "http_%d" % exc.code}
    except Exception:  # noqa: BLE001 -- URLError, socket.timeout, ssl, anything
        return {"status": None, "data": None, "etag": None,
                "reset_at": None, "error": REASON_OFFLINE}


def _parse_reset(headers: Any) -> Optional[float]:
    """Unix timestamp when a 403/429 stops applying, from Retry-After (seconds)
    or X-RateLimit-Reset (absolute). None when neither is usable."""
    if headers is None:
        return None
    retry_after = headers.get("Retry-After")
    if retry_after:
        try:
            return time.time() + max(0.0, float(str(retry_after).strip()))
        except (TypeError, ValueError):
            pass
    reset = headers.get("X-RateLimit-Reset")
    if reset:
        try:
            return float(str(reset).strip())
        except (TypeError, ValueError):
            pass
    return None


# ---- local build identity ---------------------------------------------------

def local_sha() -> Optional[str]:
    """The FULL local commit sha, or None when this is not a git checkout.

    Guarded exactly like ``build_version()``: only trusted when the package's
    parent holds the .git, so a wheel installed inside an unrelated repo never
    reports that repo's commit. Never raises."""
    try:
        pkg_dir = Path(__file__).resolve().parent.parent      # <root>/webterm
        if not (pkg_dir.parent / ".git").exists():
            return None
        out = subprocess.run(
            ["git", "-C", str(pkg_dir), "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=5)
        sha = out.stdout.strip()
        if out.returncode == 0 and re.fullmatch(r"[0-9a-f]{40}", sha):
            return sha
    except Exception:  # noqa: BLE001
        pass
    return None


def _git(*args: str, timeout: float = 5.0) -> Optional[str]:
    """Run a read-only git command in the package's repo. None on any failure."""
    try:
        pkg_dir = Path(__file__).resolve().parent.parent
        if not (pkg_dir.parent / ".git").exists():
            return None
        out = subprocess.run(["git", "-C", str(pkg_dir)] + list(args),
                             capture_output=True, text=True, timeout=timeout)
        if out.returncode != 0:
            return None
        return out.stdout.strip()
    except Exception:  # noqa: BLE001
        return None


def local_ancestry(upstream_sha: str) -> Optional[Dict[str, int]]:
    """``{"aheadBy": n, "behindBy": m}`` computed LOCALLY, or None when this
    repo does not have the upstream commit on disk.

    This exists because the interesting case -- a development checkout with
    commits that were never pushed -- is exactly the case GitHub's compare
    endpoint cannot answer: it 404s on a sha it has never seen. Asking git
    instead turns "GitHub could not tell us" into a real, correct answer
    whenever the upstream object is already in the local object store (i.e.
    anyone who has ever fetched), and costs no request at all.

    It is a strict improvement, never a guess: when the object is absent we
    return None and the caller falls back to the API, which may in turn fail to
    ``unknown``. Nothing here invents an ancestry it did not measure."""
    if not re.fullmatch(r"[0-9a-f]{40}", upstream_sha or ""):
        return None
    # Is the commit actually here? Without this, rev-list would fail and be
    # indistinguishable from "no divergence".
    if _git("cat-file", "-e", upstream_sha + "^{commit}") is None:
        return None
    # left...right: left = reachable from upstream but not HEAD (we are behind
    # by that many), right = reachable from HEAD but not upstream (ahead).
    counts = _git("rev-list", "--left-right", "--count",
                  upstream_sha + "...HEAD")
    if not counts:
        return None
    parts = counts.split()
    if len(parts) != 2:
        return None
    try:
        behind, ahead = int(parts[0]), int(parts[1])
    except ValueError:
        return None
    return {"aheadBy": ahead, "behindBy": behind}


# ---- semver (A4) ------------------------------------------------------------

_SEMVER_RE = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)")


def parse_version(tag: Any) -> Optional[tuple]:
    """``"v0.10.0"`` -> ``(0, 10, 0)``. None when it does not look like semver.

    Numeric, not lexical: ``"0.10.0" < "0.9.0"`` is true as strings and false as
    versions, and that inversion is invisible until the day a minor hits 10."""
    if not isinstance(tag, str):
        return None
    m = _SEMVER_RE.match(tag.strip())
    if not m:
        return None
    return tuple(int(g) for g in m.groups())


# ---- mode selection (A2) ----------------------------------------------------

def select_mode(release_result: Dict[str, Any]) -> str:
    """``"release"`` only on a 200; anything else falls back to ``"commit"``.

    Data-driven on purpose: the repo has no releases today, so publishing the
    first one flips this with no edit, no redeploy, and no window where a
    forgotten flag has the mod checking the wrong thing. The fallback is also
    what keeps it correct for a fork that never cuts releases.

    Note this keys on 200 and not on "404", so a 403, a timeout or a malformed
    body degrade to commit mode rather than being mistaken for "releases exist"."""
    return "release" if release_result.get("status") == 200 else "commit"


# ---- state derivation (A3) --------------------------------------------------

def _unknown(reason: str, **extra: Any) -> Dict[str, Any]:
    out = {"state": STATE_UNKNOWN, "reason": reason}
    out.update(extra)
    return out


def _from_counts(ahead: int, behind: int, upstream: Dict[str, Any],
                 source: str) -> Dict[str, Any]:
    """One ahead/behind pair -> one state. Shared by the local-git path and the
    API compare path so they can never disagree about what the numbers mean."""
    common = {"reason": None, "aheadBy": ahead, "behindBy": behind,
              "upstream": upstream, "ancestrySource": source}
    if ahead > 0:
        # Local commits upstream has never seen: a development checkout, not a
        # stale one. Reported whether or not it is ALSO behind -- "diverged" is
        # the honest word when both are non-zero.
        return dict(common, state=STATE_AHEAD)
    if behind > 0:
        return dict(common, state=STATE_BEHIND)
    return dict(common, state=STATE_CURRENT)


def compute_state(*, mode: str, local_version: Optional[str],
                  sha: Optional[str],
                  release_result: Optional[Dict[str, Any]] = None,
                  compare_result: Optional[Dict[str, Any]] = None,
                  ancestry: Optional[Dict[str, int]] = None,
                  upstream_sha: Optional[str] = None,
                  ) -> Dict[str, Any]:
    """Reduce the fetch results to one state. The honesty rules live here:

    * No local sha -> ``unknown(no-git)``. NEVER compare a bare ``0.8.0``
      against ``0.8.0`` and call it current -- two different builds share that
      string, which is what the ``build_version()`` docstring warns about.
    * A compare that is not a 200 -> ``unknown``, with the reason distinguishing
      rate-limited / offline / compare-unavailable. Ahead and behind are read
      from the 200 body and nowhere else.
    """
    if mode == "release":
        data = (release_result or {}).get("data") or {}
        latest = parse_version(data.get("tag_name"))
        mine = parse_version(local_version)
        if latest is None or mine is None:
            return _unknown(REASON_BAD_RESPONSE,
                            upstream=data.get("tag_name"))
        upstream = {"tag": data.get("tag_name"),
                    "url": data.get("html_url"),
                    "publishedAt": data.get("published_at")}
        if mine >= latest:
            return {"state": STATE_CURRENT, "reason": None,
                    "upstream": upstream}
        return {"state": STATE_BEHIND, "reason": None, "behindBy": None,
                "upstream": upstream}

    # ---- commit mode ----
    if not sha:
        return _unknown(REASON_NO_GIT)

    # Preferred path: git already knows. Costs no request, and answers the one
    # case the API cannot -- an unpushed local commit.
    if ancestry is not None:
        up = {"sha": upstream_sha, "branch": UPSTREAM_BRANCH,
              "url": (compare_url(UPSTREAM_REPO, UPSTREAM_BRANCH, sha)
                      if upstream_sha else None)}
        return _from_counts(ancestry["aheadBy"], ancestry["behindBy"], up,
                            "local-git")

    res = compare_result or {}
    status = res.get("status")
    # Checked BEFORE the status branch: an oversized body arrives with a 200,
    # so testing status first drops it into the parse path and mislabels it
    # bad-response. It is not a malformed answer, it is an answer we refused
    # to read.
    if res.get("error") == REASON_TOO_LARGE:
        return _unknown(REASON_TOO_LARGE)
    if status != 200:
        if status in (403, 429):
            return _unknown(REASON_RATE_LIMITED, resetAt=res.get("reset_at"))
        if status is None:
            return _unknown(REASON_OFFLINE)
        # 404 lands here WITH EVERYTHING ELSE, and that is the point: it does
        # not mean "ahead", it means GitHub could not answer the question.
        return _unknown(REASON_COMPARE_UNAVAILABLE, httpStatus=status)
    data = res.get("data") or {}
    ahead = data.get("ahead_by")
    behind = data.get("behind_by")
    if not isinstance(ahead, int) or not isinstance(behind, int):
        return _unknown(REASON_BAD_RESPONSE)
    base = data.get("base_commit") or {}
    upstream = {"sha": base.get("sha") or upstream_sha,
                "url": data.get("html_url"),
                "branch": UPSTREAM_BRANCH}
    return _from_counts(ahead, behind, upstream, "github-compare")


# ---- the whole check --------------------------------------------------------

_UNSET = object()      # "caller said nothing" vs "caller said None"


def run_check(*, repo: str = UPSTREAM_REPO, branch: str = UPSTREAM_BRANCH,
              local_version: Optional[str] = None,
              sha: Any = _UNSET,
              fetcher: Optional[Callable[..., Dict[str, Any]]] = None,
              ancestor_fn: Optional[Callable[[str], Optional[Dict[str, int]]]]
              = None,
              ) -> Dict[str, Any]:
    """One complete check. ``fetcher`` is injectable so every path above can be
    tested without a network; production passes None and gets ``fetch_json``.

    Costs at most two upstream requests: /releases/latest, then (only when that
    is not a 200) one compare. The compare's ``base_commit`` doubles as the
    upstream head, so there is no third request just to name it."""
    get = fetcher or fetch_json
    ancestor_fn = ancestor_fn or local_ancestry
    # A sentinel, not `is None`: a caller passing sha=None means "this install
    # has no sha", and resolving it from disk anyway would make the no-git path
    # untestable on a machine that IS a checkout.
    sha = local_sha() if sha is _UNSET else sha

    release_result = get("/repos/%s/releases/latest" % repo,
                         max_bytes=RELEASE_MAX_BYTES)
    mode = select_mode(release_result)

    compare_result = None
    ancestry = None
    upstream_sha = None
    if mode == "commit":
        if not sha:
            # No sha means no question to ask -- do not spend a request to
            # learn we cannot answer.
            out = _unknown(REASON_NO_GIT)
            out.update({"mode": mode, "local": {"version": local_version,
                                                "sha": None},
                        "checkedAt": int(time.time())})
            return out
        # Name upstream's head first. It is a small object, and knowing the sha
        # is what lets git answer locally -- including for a commit GitHub has
        # never seen, which is precisely where the compare endpoint gives up.
        head_result = get("/repos/%s/commits/%s" % (repo, branch),
                          max_bytes=RELEASE_MAX_BYTES)
        if head_result.get("status") == 200:
            upstream_sha = ((head_result.get("data") or {}).get("sha")
                            if isinstance(head_result.get("data"), dict)
                            else None)
        if upstream_sha:
            ancestry = ancestor_fn(upstream_sha)
        if ancestry is None:
            # git could not answer (no local object, or not a checkout) -- ask
            # GitHub, and accept that it may not be able to either.
            compare_result = get(
                "/repos/%s/compare/%s...%s" % (repo, branch, sha),
                max_bytes=COMPARE_MAX_BYTES)
            if (compare_result.get("status") not in (200, 403, 429)
                    and compare_result.get("status") is not None
                    and head_result.get("status") not in (200, None)):
                # Neither call worked; report the head failure, which is the
                # more informative of the two.
                compare_result = head_result

    out = compute_state(mode=mode, local_version=local_version, sha=sha,
                        release_result=release_result,
                        compare_result=compare_result,
                        ancestry=ancestry, upstream_sha=upstream_sha)
    out.update({
        "mode": mode,
        "local": {"version": local_version, "sha": sha},
        "repo": repo,
        "checkedAt": int(time.time()),
        # The working tree is deliberately NOT verified: build ids carry no
        # dirty marker (two different dirty trees share a hash), so the UI must
        # say so rather than imply the tree was checked.
        "treeVerified": False,
    })
    if "upstream" not in out:
        out["upstream"] = None
    return out


def compare_url(repo: str, branch: str, sha: str) -> str:
    """Human-facing compare page for the detail window."""
    return "%s/%s/compare/%s...%s" % (GITHUB_WEB.rstrip("/"), repo, branch, sha)


def next_ttl(rand: Optional[Callable[[], float]] = None) -> float:
    """Daily, plus up to an hour of jitter so a fleet behind one NAT does not
    all expire in the same second."""
    r = rand or random.random
    return CHECK_TTL + r() * CHECK_TTL_JITTER


# ---- GH#182 Part 2: apply preconditions (A24) --------------------------------
#
# "May an update be APPLIED here?" -- answered with the same split the check
# engine above uses: a PURE evaluator over an injectable snapshot
# (``apply_preconditions`` / ``evaluate_apply``), and a separate IMPURE
# collector (``collect_apply_snapshot``) that is the only thing allowed to
# touch git. The collector runs read-only git commands exclusively (rev-parse
# via ``local_sha()`` and ``status --porcelain``); the mutation that actually
# applies an update belongs to a later atom and to a route that does not exist
# yet. Nothing in this section makes a network request.

# Refusal reason codes. Kebab-case like the check engine's REASON_* codes, and
# STABLE: the UI and peer brokers will switch on these strings, so they are an
# API -- renaming one is a breaking change.
APPLY_NOT_A_CHECKOUT = "not-a-checkout"
APPLY_DIRTY_TREE = "dirty-tree"
# Deliberately the same word as STATE_AHEAD: the refusal and the check state
# describe the same fact.
APPLY_AHEAD_OR_DIVERGED = "ahead-or-diverged"
APPLY_STATE_UNKNOWN = "state-unknown"
APPLY_ALREADY_CURRENT = "already-current"
APPLY_RESTART_UNAVAILABLE = "restart-unavailable"
APPLY_DISABLED = "apply-disabled"
# The detection lives in the A25 section below (``dependency_delta`` /
# ``collect_dependency_delta``); the evaluator refuses whenever the snapshot
# carries a truthy delta -- and an UNKNOWN ``DeltaReport`` is truthy on
# purpose, so "could not diff" refuses instead of silently passing.
APPLY_DEPENDENCY_DELTA = "dependency-delta"


@dataclass
class Refusal:
    """One unmet apply precondition: a stable machine ``reason_code`` plus one
    human sentence. Writability is deliberately NOT representable as a Refusal
    -- it rides alongside as an advisory (see ``writability_advisory``),
    because it cannot be pre-proved (review point R5)."""
    reason_code: str
    message: str


@dataclass
class ApplySnapshot:
    """Everything the evaluator needs, gathered by the impure collector (or
    constructed directly by a test). Defaults fail CLOSED: a field nobody
    filled in reads as 'not safe to apply', never as permission."""
    local_sha: Optional[str] = None        # None => not a git checkout
    dirty: Optional[bool] = None           # TRACKED modifications; None = unmeasured
    check_state: Optional[str] = None      # a STATE_* string; None = never ran
    target_sha: Optional[str] = None       # upstream sha the check named
    ahead_by: Optional[int] = None
    behind_by: Optional[int] = None
    restart_available: bool = False        # the caller's restart capability...
    restart_reason: Optional[str] = None   # ...and WHY it is unavailable
    apply_enabled: bool = False            # per-instance update_apply_enabled gate
    writable: Optional[bool] = None        # ADVISORY only, never a refusal
    dependency_delta: Any = None           # a DeltaReport (A25); None = never measured


def apply_preconditions(snap: ApplySnapshot) -> List[Refusal]:
    """Pure. One predicate set; returns EVERY failing precondition, not just
    the first, so the UI can show the whole list of unmet conditions in one
    pass. An empty list means the apply may proceed. No I/O, no git, no clock.

    The honesty rule carried forward from the check engine: when ahead/behind
    was never ESTABLISHED, the refusal is ``state-unknown`` -- it is never
    guessed into ``ahead-or-diverged`` (the compare-404 trap, refuted in
    Part 1). Ahead/behind here derive only from established data."""
    refusals: List[Refusal] = []

    if not snap.local_sha:
        refusals.append(Refusal(
            APPLY_NOT_A_CHECKOUT,
            "This install is not a git checkout (a pip/wheel install), so "
            "there is nothing here for git to update; updating such an "
            "install is out of scope for this broker."))

    if snap.dirty:
        refusals.append(Refusal(
            APPLY_DIRTY_TREE,
            "Tracked files carry local modifications (staged or unstaged); "
            "an update could clobber that work, so commit, stash or revert "
            "those changes first."))

    if isinstance(snap.ahead_by, int) and snap.ahead_by > 0:
        refusals.append(Refusal(
            APPLY_AHEAD_OR_DIVERGED,
            "This checkout carries %d local commit(s) upstream does not "
            "have; applying would have to merge or discard work upstream "
            "has never seen, and that is a human decision." % snap.ahead_by))

    established = (snap.check_state in (STATE_CURRENT, STATE_BEHIND,
                                        STATE_AHEAD)
                   and bool(snap.target_sha)
                   and isinstance(snap.ahead_by, int)
                   and isinstance(snap.behind_by, int))
    if not established:
        refusals.append(Refusal(
            APPLY_STATE_UNKNOWN,
            "No established update check names a target commit for this "
            "apply; run a check that succeeds first, so the apply knows "
            "exactly which sha it is applying."))

    if isinstance(snap.behind_by, int) and snap.behind_by == 0:
        refusals.append(Refusal(
            APPLY_ALREADY_CURRENT,
            "This checkout is already at the upstream head (0 commits "
            "behind); there is nothing to apply."))

    if not snap.restart_available:
        detail = (" (%s)" % snap.restart_reason) if snap.restart_reason else ""
        refusals.append(Refusal(
            APPLY_RESTART_UNAVAILABLE,
            "The broker cannot restart itself on this install%s; applying "
            "without a restart would leave the old code running while "
            "claiming the update succeeded." % detail))

    if not snap.apply_enabled:
        refusals.append(Refusal(
            APPLY_DISABLED,
            "The update_apply_enabled gate is off for this broker; applying "
            "updates is opt-in per instance, and this instance has not "
            "opted in."))

    if snap.dependency_delta:
        # Truthiness is the seam: an established non-empty DeltaReport AND an
        # UNKNOWN one both land here (the A25 section explains why unknown
        # must refuse); the message helper distinguishes them for the human.
        refusals.append(Refusal(
            APPLY_DEPENDENCY_DELTA,
            _dependency_delta_message(snap.dependency_delta)))

    return refusals


def writability_advisory(writable: Optional[bool]) -> Dict[str, Any]:
    """The advisory that rides ALONGSIDE the refusals, never among them
    (review point R5): writability cannot be pre-proved -- on Windows
    especially, a file that opens now can be exclusively locked by another
    handle at apply time -- so even a failed probe refuses nothing. It informs;
    the actual write failure, if one happens, is reported when it happens."""
    caveat = ("this is advisory only, because writability cannot be "
              "pre-proved -- on Windows especially, a file that opens now "
              "can be locked by another handle at apply time.")
    if writable is True:
        msg = ("An append probe under the checkout succeeded, but " + caveat)
    elif writable is False:
        msg = ("An append probe under the checkout failed, so the apply is "
               "likely to fail at write time; " + caveat)
    else:
        msg = ("Writability was not measured; " + caveat)
    return {"writable": writable, "message": msg}


def probe_writability() -> Optional[bool]:
    """Best-effort, impure, ADVISORY: append-open a file WE own under the
    checkout's .git directory (a name git ignores, so the probe can never
    dirty the tree), then remove it. True/False is a hint; None means the
    probe could not even run (not a checkout, or .git is a worktree pointer
    file). Never raises."""
    try:
        pkg_dir = Path(__file__).resolve().parent.parent      # <root>/webterm
        git_dir = pkg_dir.parent / ".git"
        if not git_dir.is_dir():
            return None
        probe = git_dir / "browserland-apply-write-probe.tmp"
        with open(probe, "a", encoding="utf-8"):
            pass
        try:
            probe.unlink()
        except OSError:
            pass
        return True
    except OSError:
        return False
    except Exception:  # noqa: BLE001
        return None


def collect_apply_snapshot(*, check_result: Optional[Dict[str, Any]] = None,
                           restart_available: bool = False,
                           restart_reason: Optional[str] = None,
                           apply_enabled: bool = False,
                           dependency_delta: Any = None) -> ApplySnapshot:
    """The IMPURE half: gather the facts, decide nothing. Runs READ-ONLY git
    only -- ``local_sha()`` (rev-parse) and ``status --porcelain
    --untracked-files=no``. Untracked files are excluded STRUCTURALLY, at the
    git level: an untracked scratch file must not block an update, and a
    genuinely colliding one surfaces at merge time and is reported then.

    ``check_result`` is the cached output of ``run_check``; restart
    availability and the apply gate are the caller's facts (app.ctx), passed
    through untouched. ``dependency_delta`` is the A25 ``DeltaReport``, which
    the route computes via ``collect_dependency_delta`` only POST-fetch (the
    diff needs the target's objects on disk); before that it stays None --
    unmeasured, covered by the state-unknown refusal until a check
    establishes a target. The route that calls this is a later atom."""
    sha = local_sha()
    dirty: Optional[bool] = None
    if sha is not None:
        status = _git("status", "--porcelain", "--untracked-files=no")
        if status is not None:
            dirty = bool(status)
    check = check_result if isinstance(check_result, dict) else {}
    upstream = check.get("upstream")
    target = upstream.get("sha") if isinstance(upstream, dict) else None
    ahead = check.get("aheadBy")
    behind = check.get("behindBy")
    return ApplySnapshot(
        local_sha=sha,
        dirty=dirty,
        check_state=check.get("state"),
        target_sha=target if isinstance(target, str) else None,
        ahead_by=ahead if isinstance(ahead, int) else None,
        behind_by=behind if isinstance(behind, int) else None,
        restart_available=bool(restart_available),
        restart_reason=restart_reason,
        apply_enabled=bool(apply_enabled),
        writable=probe_writability(),
        dependency_delta=dependency_delta,
    )


def apply_preview(snap: ApplySnapshot) -> Dict[str, Any]:
    """What an apply WOULD do, for the route to serve and the UI to render:
    old sha, target sha, how far behind, and a human compare page. Reuses
    ``compare_url`` with the local sha as the left ref, so the page shows
    exactly the commits the apply would bring in."""
    url = None
    if snap.local_sha and snap.target_sha:
        url = compare_url(UPSTREAM_REPO, snap.local_sha, snap.target_sha)
    return {"oldSha": snap.local_sha,
            "targetSha": snap.target_sha,
            "behindBy": snap.behind_by,
            "compareUrl": url}


def evaluate_apply(snap: ApplySnapshot) -> Dict[str, Any]:
    """One predicate set, ONE output shape -- the structure a later route atom
    serves verbatim. Refusals accumulate; writability rides alongside as an
    advisory and never appears inside ``refusals``."""
    refusals = apply_preconditions(snap)
    return {
        "ok": not refusals,
        "refusals": [{"reason": r.reason_code, "message": r.message}
                     for r in refusals],
        "writability_advisory": writability_advisory(snap.writable),
        "preview": apply_preview(snap),
    }


# ---- GH#182 Part 2: the deploy journal seam (A23) ----------------------------

def begin_deploy(old_sha: Any, target_sha: Any, expected_identity: Any,
                 operation_id: Any, *, run_dir: Optional[str] = None) -> bool:
    """Journal a deploy BEFORE its apply-restart is requested. True when the
    journal is on disk; False when it is not -- and a False is the caller's
    cue that the apply must NOT proceed to a restart, because without a
    journal the supervisor adjudicates nothing, and a deploy nobody
    adjudicates is a deploy whose failure nobody observes.

    Written OUTSIDE the worktree, into the supervisor's run dir -- the same
    place the restart-intent sentinel lives (``$BROWSERLAND_RUN_DIR``,
    defaulted from the environment exactly like ``supervise.arm_restart``) --
    so no git operation on the checkout can delete or alter it, and a
    mid-pull inconsistent tree cannot affect it. The record format and the
    atomic write live in ``supervise`` because they are a worker->supervisor
    contract, like the sentinel: this worker WRITES the journal, and the
    supervisor (the only process that outlives the restart) CONSUMES it and
    adjudicates the next worker generation into came-up-ready-on-target /
    came-up-on-wrong-sha / never-came-up, running the A28 rollback decision
    over that verdict (``supervise.rollback_decision``) before handing the
    final outcome to ``deploy_hook``.

    Runs no git at all: ``old_sha`` and ``target_sha`` are the evaluator's
    already-established facts (``ApplySnapshot.local_sha`` / ``target_sha``),
    full 40-hex, passed in. ``expected_identity`` is the broker identity the
    deploy expects to survive (broker_id when the route has one, else config
    path + port); it is journal data for A28 and the audit trail, never
    adjudicated here."""
    run_dir = run_dir or os.environ.get(supervise.ENV_RUN_DIR)
    if not run_dir:
        return False
    if supervise.read_pending_deploy(run_dir) is not None:
        # Never overwrite a journal nobody has adjudicated yet: a second
        # apply accepted before the previous generation reached ready (only
        # possible with the restart cooldown disabled) would otherwise
        # clobber the pending record and orphan the earlier deploy.
        return False
    record = supervise.build_deploy_record(
        old_sha, target_sha, expected_identity, operation_id)
    if record is None:
        return False
    return supervise.write_deploy_journal(run_dir, record)


# ---- GH#182 Part 2: dependency-delta detection (A25) -------------------------
#
# #182 trap 7: a newer build may need dependencies the old environment does
# not have. A git pull updates CODE only; the new process then fails to
# import, and the box is down along with the UI you would have fixed it with
# -- the single most likely way an apply breaks a remote machine. Decision D4:
# the updater NEVER installs anything. This section only DETECTS: a heuristic
# over the paths changed between old and target sha, whose one power is to
# make the apply refuse (APPLY_DEPENDENCY_DELTA above). There is deliberately
# no code path from a detected delta to any install, ever.
#
# Same split as the rest of this module: a PURE classifier
# (``dependency_delta``) over an injected path list, and an IMPURE collector
# (``collect_dependency_delta``) that is the only piece allowed to run git --
# one read-only ``diff --name-only`` plus a cat-file existence probe, nothing
# that mutates. The diff can only be taken once the target's objects are in
# the local store (post-fetch, which is when the apply route calls it); before
# that the honest answer is UNKNOWN, and unknown REFUSES -- never treat a
# failed diff as "no delta".

# Basename -> family, matched case-insensitively at ANY depth. This repo keeps
# its one manifest (pyproject.toml) at the root and has no requirements.txt at
# all today, but the boundary being guarded is upstream's FUTURE shape, so
# root-only would be a silent hole: a sub-package manifest added later must be
# caught, and a false positive here merely refuses with named files -- the
# safe direction for a heuristic guarding remote boxes.
#
# The node family is included although this repo has NO package.json anywhere
# today and serves its vendored JS from checked-in files only: everything
# under webterm/broker/vendor/** is git-tracked, byte-for-byte published
# assets, and vendor_codemirror.py is a by-hand re-vendor tool whose OUTPUT is
# committed -- so a pull delivers ready-to-serve JS with no build step. That
# is exactly why the patterns stay: they cannot fire on today's upstream, and
# if one ever appears in a diff it means upstream introduced a Node-side
# dependency or build boundary a pull cannot satisfy -- precisely the case to
# refuse and hand to a human.
_DEP_FAMILIES: Dict[str, str] = {
    "pyproject.toml": "python-project",
    "setup.py": "python-project",
    "setup.cfg": "python-project",
    "poetry.lock": "python-lock",
    "uv.lock": "python-lock",
    "pipfile": "python-lock",
    "pipfile.lock": "python-lock",
    "environment.yml": "conda",
    "environment.yaml": "conda",
    "conda-lock.yml": "conda",
    "conda-lock.yaml": "conda",
    "package.json": "node",
    "package-lock.json": "node",
    "npm-shrinkwrap.json": "node",
    "yarn.lock": "node",
    "pnpm-lock.yaml": "node",
}

# How many matched files the refusal sentence names before switching to a
# count: enough for the operator to know what to install by hand, not a wall.
_DELTA_NAME_CAP = 5


@dataclass
class DeltaReport:
    """The classifier's verdict over one old..target changed-path set.

    Three honest outcomes, not two:

    * established, empty      -> ``known=True``, no matches -- FALSY
    * established, non-empty  -> ``known=True``, matches    -- truthy (refuse)
    * could not be diffed     -> ``known=False``            -- truthy (refuse)

    Truthiness IS the evaluator seam (``if snap.dependency_delta``), so the
    one thing this type must never do is let UNKNOWN read as falsy: a failed
    diff that silently passed the precondition would be exactly the trap-7
    outage this section exists to prevent."""
    known: bool
    matched: List[str] = field(default_factory=list)       # in diff order
    families: Dict[str, str] = field(default_factory=dict)  # path -> family
    reason: Optional[str] = None       # why unknown, when known is False

    def __bool__(self) -> bool:
        return (not self.known) or bool(self.matched)


def _classify_dep_path(path: Any) -> Optional[str]:
    """The dependency family one changed path belongs to, or None.
    Case-insensitive on the whole path (manifests are conventionally cased --
    Pipfile -- and Browserland runs on case-insensitive filesystems too);
    separators normalised so a Windows-shaped input cannot dodge a match."""
    p = str(path).replace("\\", "/").strip().strip("/").lower()
    if not p:
        return None
    parts = p.split("/")
    base = parts[-1]
    fam = _DEP_FAMILIES.get(base)
    if fam is not None:
        return fam
    if base.endswith(".whl"):
        # A vendored wheel changing IS a dependency changing, wherever it sits.
        return "wheel"
    if base.endswith(".txt"):
        if base.startswith("requirements") or base.startswith("constraints"):
            return "requirements"
        if "requirements" in parts[:-1]:
            # The requirements/*.txt convention: the DIRECTORY names the
            # family and the files inside are free-form (dev.txt, prod.txt).
            return "requirements"
    return None


def dependency_delta(changed_paths: Any) -> DeltaReport:
    """PURE classifier: the paths changed between old and target sha ->
    ``DeltaReport``. No I/O, no git.

    Only a real iterable of paths can establish "no delta": ``None`` -- and
    any junk that is not a path list, including a bare string, whose chars
    would iterate cleanly and match nothing -- comes back UNKNOWN, so no
    caller can accidentally launder "no data" into "no delta". The matching
    itself is a heuristic: broad families, any depth, case-insensitive,
    because its false positives cost one named refusal while a false negative
    costs a remote box."""
    if changed_paths is None or isinstance(changed_paths, (str, bytes)):
        return DeltaReport(known=False, reason="paths-unavailable")
    matched: List[str] = []
    families: Dict[str, str] = {}
    try:
        for raw in changed_paths:
            fam = _classify_dep_path(raw)
            if fam is not None:
                name = str(raw)
                matched.append(name)
                families[name] = fam
    except TypeError:
        return DeltaReport(known=False, reason="paths-unavailable")
    return DeltaReport(known=True, matched=matched, families=families)


def collect_dependency_delta(old_sha: Any, target_sha: Any) -> DeltaReport:
    """IMPURE collector: what changed between two commits, from LOCAL
    read-only git (``diff --name-only`` over old..target). It can only answer
    once the target's objects are on disk -- post-fetch, which is when the
    apply route calls it; for a sha the local store has never seen, the
    honest answer is an UNKNOWN report, which refuses.

    Full 40-hex shas only, the same rule as ``begin_deploy``: a ref name here
    could name a different commit than the one the check established, and the
    strictness also makes option injection structurally impossible."""
    for sha in (old_sha, target_sha):
        if not (isinstance(sha, str) and re.fullmatch(r"[0-9a-f]{40}", sha)):
            return DeltaReport(known=False, reason="bad-ref")
    # Distinguish "the object is not here" (pre-fetch, or garbage-collected)
    # from a diff that failed some other way -- the operator sentences differ.
    if _git("cat-file", "-e", target_sha + "^{commit}") is None:
        return DeltaReport(known=False, reason="target-object-absent")
    out = _git("diff", "--name-only", "%s..%s" % (old_sha, target_sha))
    if out is None:
        return DeltaReport(known=False, reason="diff-failed")
    # "" from a zero-exit git is a REAL answer (no files changed); only None
    # means the command failed. _git strips, so splitlines of "" is [].
    paths = [line.strip() for line in out.splitlines() if line.strip()]
    return dependency_delta(paths)


def _dependency_delta_message(delta: Any) -> str:
    """The refusal's human sentence. Names the matched files (bounded: the
    first ``_DELTA_NAME_CAP`` plus a count) so the operator knows what to
    install by hand, and always states the rule."""
    rule = ("The updater never installs dependencies (decision D4): update "
            "this box by hand -- git pull, install what changed, then "
            "restart -- so a human sees any install failure while the old "
            "broker is still serving.")
    if isinstance(delta, DeltaReport):
        if not delta.known:
            return ("Whether this update changes dependency files could not "
                    "be established (%s); refusing rather than guessing, "
                    "because a missing dependency takes the new process down "
                    "along with the UI you would fix it with. %s"
                    % (delta.reason or "diff unavailable", rule))
        shown = delta.matched[:_DELTA_NAME_CAP]
        rest = len(delta.matched) - len(shown)
        names = ", ".join(shown) + (" (+%d more)" % rest if rest else "")
        return ("This update changes dependency files -- %s -- and a git "
                "pull updates code only, installing nothing; the new process "
                "could fail to import and take the box down. %s"
                % (names, rule))
    # A truthy non-DeltaReport (the pre-A25 seam shape): the generic sentence.
    return ("This update changes dependency requirements, which an automatic "
            "apply cannot reconcile on its own; update the dependencies by "
            "hand first. " + rule)


# ---- GH#182 Part 2: route-phase apply support (A27) --------------------------
#
# Pure helpers the POST /update/apply route uses AROUND the git phase. Nothing
# here mutates; the mutation section is the clearly-marked block at the end of
# this file, and the structural tests in tests/test_update_apply.py hold that
# line.

# The check's established target no longer matches the sha the human previewed:
# upstream moved between the preview and the click. Same stability contract as
# every other APPLY_* code -- the UI switches on it, so it is API.
APPLY_PREVIEW_MISMATCH = "preview-sha-mismatch"
# The git phase's own refusal codes (details in perform_apply).
APPLY_BAD_TARGET = "bad-target-sha"
APPLY_FETCH_FAILED = "fetch-failed"
APPLY_TARGET_MISSING = "target-object-missing"
APPLY_NOT_FAST_FORWARD = "not-fast-forward"
APPLY_TARGET_NOT_UPSTREAM = "target-not-upstream"
# "no-deploy-journal", not "journal-unwritable": the R5 guard pins that no
# APPLY_* code may contain "writ" (writability is an advisory, never a
# refusal), and the fact here is the JOURNAL's absence, not the tree's
# writability.
APPLY_NO_JOURNAL = "no-deploy-journal"
APPLY_TREE_SUSPECT = "tree-suspect"

#: ``deploy.outcome`` verdict for a journal finalized because NO restart will
#: follow (merge failed, or the restart could not be armed). Lives here rather
#: than in ``supervise`` because the supervisor never writes it -- only the
#: worker cancels its own deploy; the supervisor's three adjudications describe
#: a restart that actually happened. Same add-never-repurpose rule.
DEPLOY_CANCELLED = "cancelled-before-restart"


def preview_mismatch_refusal(established_target: Any,
                             requested_target: Any) -> Optional[Refusal]:
    """A ``Refusal`` when the live check names a DIFFERENT upstream sha than
    the one the client previewed, else None.

    The server never applies "whatever is newest": it applies exactly what the
    human saw. When upstream moved between the preview and the click, the
    honest answer is "re-preview and decide again", never a silent apply of a
    sha nobody looked at. Absence of either sha returns None on purpose --
    that case is already the ``state-unknown`` refusal, and doubling it up
    would show one fact twice."""
    if not (isinstance(established_target, str) and established_target):
        return None
    if not (isinstance(requested_target, str) and requested_target):
        return None
    if established_target == requested_target:
        return None
    return Refusal(
        APPLY_PREVIEW_MISMATCH,
        "The current check names upstream commit %s, not the %s this apply "
        "previewed; upstream moved since the preview was taken, so re-run "
        "the preview and decide again from current facts -- this broker "
        "never silently applies a commit the human did not see."
        % (established_target[:12], requested_target[:12]))


def cancel_pending_deploy(reason: str, *,
                          run_dir: Optional[str] = None) -> bool:
    """Finalize (consume) a pending deploy journal that NO restart will
    follow. True when a pending journal was found and finalized.

    The invariant this enforces is A23's: a journal must never be left pending
    without a restart coming, because the next generation -- whenever an
    operator happens to bounce the process, hours later -- would be
    adjudicated against a deploy that was already abandoned, and A28's hook
    would act on a verdict about the wrong boot. ``finalize_deploy`` is the
    same consume path the supervisor uses, so one deploy still gets exactly
    one outcome; the outcome string is ``DEPLOY_CANCELLED`` with ``reason`` as
    its detail, and ``observedSha`` records where HEAD actually is so the
    audit trail says what state the cancel left behind."""
    run_dir = run_dir or os.environ.get(supervise.ENV_RUN_DIR)
    if not run_dir:
        return False
    record = supervise.read_pending_deploy(run_dir)
    if record is None:
        return False
    outcome = {"outcome": DEPLOY_CANCELLED, "observedSha": local_sha(),
               "detail": str(reason)}
    return supervise.finalize_deploy(run_dir, record, outcome)


def last_deploy_outcome(run_dir: Optional[str] = None
                        ) -> Optional[Dict[str, Any]]:
    """The most recent finalized deploy outcome, or None. Read-only.

    The A28 surfacing seam. Every deploy is finalized into ONE current
    outcome file in the supervisor's run dir -- by the supervisor
    (ready-on-target / wrong-sha / never-came-up / rolled-back /
    rollback-impossible / rollback-failed) or by the worker's own cancel path
    (cancelled-before-restart) -- and this accessor is how the route layer
    lets a browser render "apply failed, rolled back" AFTER the restart that
    did it. The run dir defaults from the environment exactly like
    ``begin_deploy``, so an unsupervised broker simply answers None.

    Tolerant by design, never raises: an absent file, an unreadable file, or
    one that does not decode to a dict carrying a string ``outcome`` all read
    as None, because "no outcome to show" must never take down the response
    that would have carried it."""
    run_dir = run_dir or os.environ.get(supervise.ENV_RUN_DIR)
    if not run_dir:
        return None
    try:
        raw = (Path(run_dir)
               / supervise.DEPLOY_OUTCOME_FILENAME).read_text(encoding="utf-8")
        data = json.loads(raw)
    except (OSError, TypeError, ValueError):
        return None
    if not (isinstance(data, dict) and isinstance(data.get("outcome"), str)):
        return None
    return data


# =============================================================================
# ==== THE APPLY MUTATION SECTION (A27) =======================================
#
# The ONLY place in this module -- and, by policy, in this codebase -- that may
# run a git command that MUTATES the checkout. The structural tests in
# tests/test_update_apply.py split this file on the marker line above: every
# quoted git-mutation argument is banned BEFORE the marker, and `git pull` is
# banned everywhere INCLUDING here. Keep any future mutation inside this
# section, and keep it to exactly the two verbs an apply needs.
#
# WHY NEVER `git pull` (the inherited correction this section exists to
# encode): pull follows the branch's CONFIGURED upstream -- whatever `origin`
# happens to point at on this machine -- not the constant the check verified.
# On a fork that is a different repository; after a remote rename it is a dead
# one; on a machine someone re-pointed it is an arbitrary one. So the git
# phase names the pinned https URL derived from the same constant the check
# used, plus the explicit ref, and then fast-forwards to the EXACT sha the
# human previewed. A remote NAME never appears in any argv here.
#
# WHY core.hooksPath=os.devnull on the merge: merge can run repository-local
# hooks (post-merge in particular, which is how `git pull` runs code straight
# out of the tree being replaced, authored by whoever last wrote the hook).
# os.devnull is "nul" on Windows and "/dev/null" on POSIX -- in both cases a
# device that can never be a DIRECTORY, so git's hook lookup
# (<hooksPath>/<hook-name>) can never resolve to an executable file and every
# hook is skipped, identically on both platforms. An EMPTY hooksPath is
# deliberately not used: git's treatment of "" is version-dependent, and
# anything that falls back to the repo's own .git/hooks re-opens the hole.

MUTATION_FETCH_TIMEOUT = 120.0     # a fetch pulls objects; give it real time
MUTATION_MERGE_TIMEOUT = 60.0      # an ff merge only rewrites the worktree
# How much captured git stderr a failure report carries. Bounded because a
# failed checkout can name every locked file in the tree, and the transport
# for this text is a JSON error body.
MUTATION_STDERR_CAP = 2000


def upstream_git_url(repo: str = UPSTREAM_REPO) -> str:
    """The EXPLICIT https fetch URL, derived from the same constant the check
    verified. Never a remote name: a fork's `origin` must be irrelevant."""
    return "%s/%s.git" % (GITHUB_WEB.rstrip("/"), repo)


def _bounded_stderr(text: Any, cap: int = MUTATION_STDERR_CAP) -> Optional[str]:
    if not text:
        return None
    s = str(text).strip()
    if not s:
        return None
    return s if len(s) <= cap else s[:cap] + " ...[truncated]"


def _git_mutation(args: List[str], *, timeout: float) -> Dict[str, Any]:
    """Run ONE mutating git command in the package's repo. Argv only, never a
    shell; stdout/stderr captured so a failure can be REPORTED rather than
    lost. Never raises: ``returncode`` None means the command did not run at
    all (no checkout, no git binary, timeout), which every caller treats as
    failure. Twin of the read-only ``_git`` above, kept separate so the
    read-only helper stays structurally incapable of mutating."""
    try:
        pkg_dir = Path(__file__).resolve().parent.parent
        if not (pkg_dir.parent / ".git").exists():
            return {"returncode": None, "stdout": "",
                    "stderr": "not a git checkout"}
        out = subprocess.run(["git", "-C", str(pkg_dir)] + list(args),
                             capture_output=True, text=True, timeout=timeout)
        return {"returncode": out.returncode, "stdout": out.stdout or "",
                "stderr": out.stderr or ""}
    except Exception as exc:  # noqa: BLE001 -- timeout, missing git, OS errors
        return {"returncode": None, "stdout": "",
                "stderr": "git did not run: %r" % (exc,)}


def perform_apply(*, target_sha: Any, repo: str = UPSTREAM_REPO,
                  branch: str = UPSTREAM_BRANCH,
                  expected_identity: Any = None,
                  operation_id: str,
                  run_dir: Optional[str] = None) -> Dict[str, Any]:
    """Fetch the pinned upstream, verify, journal, and fast-forward to EXACTLY
    ``target_sha``. Blocking (subprocesses); the route runs it off-loop.
    Never raises. The result says what happened::

        {"ok": bool, "stage": str|None, "old_sha": str|None,
         "target_sha": str, "operation_id": str,
         "refusals": [{"reason": str, "message": str}],
         "tree_suspect": bool, "journal_cancelled": bool|None,
         "stderr": str|None}

    The order IS the contract, and each step aborts the rest:

    1. **fetch** ``<explicit https url> <ref>`` -- never ``git pull``, never a
       remote name (see the section comment).
    2. **verify**, read-only: the target commit object is now present; local
       HEAD is an ancestor of the target (a fast-forward is possible); and the
       target is reachable from the fetched ref head (this broker never
       applies a sha the pinned upstream does not have -- possession of a
       40-hex string is not provenance).
    3. **dependency delta** over old..target -- only possible POST-fetch, when
       the target's objects are on disk. A delta, or an UNKNOWN one, refuses.
    4. **begin_deploy** -- the journal the supervisor adjudicates the next
       generation against. A journal that cannot be written refuses, because
       a deploy nobody adjudicates is a deploy whose failure nobody observes.
    5. **merge --ff-only <sha>** with hooks disabled. Nothing runs between the
       merge and the caller arming the restart except journal/audit writes.

    Steps 1-4 leave the worktree untouched, so their failures are plain
    refusals. A merge that fails partway -- Windows file locking is the
    realistic case -- leaves a SUSPECT tree: that is reported honestly as
    ``tree-suspect`` with the git stderr (bounded), the pending journal is
    finalized as cancelled (no restart will follow), and the caller must NOT
    restart onto a tree nobody can vouch for."""
    def _fail(stage: str, code: str, message: str, *,
              stderr: Any = None, tree_suspect: bool = False,
              journal_cancelled: Optional[bool] = None) -> Dict[str, Any]:
        return {"ok": False, "stage": stage, "old_sha": old,
                "target_sha": target_sha, "operation_id": operation_id,
                "refusals": [{"reason": code, "message": message}],
                "tree_suspect": tree_suspect,
                "journal_cancelled": journal_cancelled,
                "stderr": _bounded_stderr(stderr)}

    old: Optional[str] = None
    if not (isinstance(target_sha, str)
            and re.fullmatch(r"[0-9a-f]{40}", target_sha)):
        # The route already 400s this; kept because this function is the last
        # line before argv construction, and full-hex is also what makes
        # option injection structurally impossible (same rule as begin_deploy).
        return _fail("verify", APPLY_BAD_TARGET,
                     "The apply target must be one full 40-hex commit sha.")
    old = local_sha()
    if not old:
        return _fail("verify", APPLY_NOT_A_CHECKOUT,
                     "This install is not a git checkout; there is nothing "
                     "here for git to update.")
    if old == target_sha:
        # A journal whose oldSha == targetSha makes rollback a no-op that
        # "reverts" to the very build being judged. Reachable when a prior
        # apply moved the tree but could not restart (tree_updated), and a
        # retry rides a stale cached check that still says "behind".
        return _fail("verify", APPLY_ALREADY_CURRENT,
                     "HEAD is already the previewed commit; there is "
                     "nothing to apply. Re-run the check -- the preview "
                     "that led here was stale.")

    # ---- 1. fetch: explicit URL + explicit ref, never a remote name --------
    url = upstream_git_url(repo)
    fetched_res = _git_mutation(["fetch", url, branch],
                                timeout=MUTATION_FETCH_TIMEOUT)
    if fetched_res["returncode"] != 0:
        return _fail("fetch", APPLY_FETCH_FAILED,
                     "Fetching %s (%s) failed; the tree was not touched."
                     % (url, branch), stderr=fetched_res["stderr"])

    # ---- 2. verify (read-only) ---------------------------------------------
    if _git("cat-file", "-e", target_sha + "^{commit}") is None:
        return _fail("verify", APPLY_TARGET_MISSING,
                     "The previewed commit is not present even after "
                     "fetching the pinned upstream; it may have been "
                     "garbage-collected or force-pushed away. Re-run the "
                     "preview.")
    if _git("merge-base", "--is-ancestor", "HEAD", target_sha) is None:
        return _fail("verify", APPLY_NOT_FAST_FORWARD,
                     "Local HEAD is not an ancestor of the previewed commit, "
                     "so no fast-forward exists; merging or discarding local "
                     "history is a human decision this route refuses to "
                     "make.")
    fetched_head = _git("rev-parse", "FETCH_HEAD")
    if not (fetched_head and re.fullmatch(r"[0-9a-f]{40}", fetched_head)):
        return _fail("verify", APPLY_TARGET_NOT_UPSTREAM,
                     "The fetched ref head could not be resolved, so the "
                     "previewed commit cannot be proven to belong to the "
                     "pinned upstream.")
    if _git("merge-base", "--is-ancestor", target_sha, fetched_head) is None:
        return _fail("verify", APPLY_TARGET_NOT_UPSTREAM,
                     "The previewed commit is not reachable from the pinned "
                     "upstream's %s; this broker never applies a sha "
                     "upstream does not have." % branch)

    # ---- 3. dependency delta (post-fetch: the objects are now on disk) -----
    delta = collect_dependency_delta(old, target_sha)
    if delta:
        return _fail("delta", APPLY_DEPENDENCY_DELTA,
                     _dependency_delta_message(delta))

    # ---- 4. journal BEFORE the mutation ------------------------------------
    if not begin_deploy(old, target_sha, expected_identity, operation_id,
                        run_dir=run_dir):
        return _fail("journal", APPLY_NO_JOURNAL,
                     "The deploy journal could not be written, so the "
                     "supervisor would adjudicate nothing; applying without "
                     "it would be a deploy whose failure nobody observes. "
                     "The tree was not touched.")

    # ---- 5. the one mutation of the worktree -------------------------------
    merge_res = _git_mutation(
        ["-c", "core.hooksPath=" + os.devnull, "merge", "--ff-only",
         target_sha],
        timeout=MUTATION_MERGE_TIMEOUT)
    if merge_res["returncode"] != 0:
        cancelled = cancel_pending_deploy(
            "tree-suspect: the ff-only merge failed", run_dir=run_dir)
        return _fail("merge", APPLY_TREE_SUSPECT,
                     "The fast-forward merge failed partway; the working "
                     "tree is SUSPECT (on Windows a locked file can leave a "
                     "half-updated checkout) and must be inspected by a "
                     "human before this broker is restarted.",
                     stderr=merge_res["stderr"], tree_suspect=True,
                     journal_cancelled=cancelled)
    now_sha = local_sha()
    if now_sha != target_sha:
        cancelled = cancel_pending_deploy(
            "tree-suspect: post-merge HEAD is %s, not the target"
            % (now_sha or "unreadable"), run_dir=run_dir)
        return _fail("merge", APPLY_TREE_SUSPECT,
                     "The merge reported success but HEAD is not the "
                     "previewed commit; the tree cannot be vouched for and "
                     "must be inspected by a human.",
                     stderr=merge_res["stderr"], tree_suspect=True,
                     journal_cancelled=cancelled)

    return {"ok": True, "stage": None, "old_sha": old,
            "target_sha": target_sha, "operation_id": operation_id,
            "refusals": [], "tree_suspect": False,
            "journal_cancelled": None, "stderr": None}
