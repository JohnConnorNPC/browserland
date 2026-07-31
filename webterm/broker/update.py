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
import random
import re
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable, Dict, Optional

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
