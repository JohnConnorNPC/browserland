"""Update check against GitHub (#182 Part 1).

The broker's SECOND outbound HTTP route, and it carries the same structural
posture as /status/fetch: a constant upstream (never a client-supplied URL and
never `git remote get-url`), https-only, no-redirect, empty ProxyHandler,
size-capped, and cached.

The honesty rules are what most of this file is about. Two of them contradict
the issue that specified the feature, and both contradictions were confirmed by
adversarial review before implementation:

* A compare 404 is NOT an "ahead/diverged" signal -- it has many other causes
  and is unstable around a force-push. Every non-200 compare is `unknown`.
* An unauthenticated 304 is NOT free of rate-limit quota. ETags are a bandwidth
  optimisation here, never a quota strategy.

No real network I/O anywhere: the pure paths take an injected `fetcher`, and the
fetch-path tests monkeypatch the module-level `_OPENER`.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from .auth_helpers import TEST_TOKEN, authed
from webterm.broker import app as broker_app
from webterm.broker import update
from webterm.broker.app import create_app


# ---- helpers ---------------------------------------------------------------

def _ok(data, status=200, etag=None):
    return {"status": status, "data": data, "etag": etag,
            "reset_at": None, "error": None}


def _err(status=None, error=update.REASON_OFFLINE, reset_at=None):
    return {"status": status, "data": None, "etag": None,
            "reset_at": reset_at, "error": error}


def _compare(ahead, behind, base_sha="upstreamsha"):
    return _ok({"ahead_by": ahead, "behind_by": behind,
                "base_commit": {"sha": base_sha},
                "html_url": "https://github.com/x/y/compare/a...b"})


SHA = "a" * 40


class _FakeResp:
    """Minimal context-manager stand-in for urlopen's return."""

    def __init__(self, body: bytes, status: int = 200, etag=None):
        self._body = body
        self.status = status
        self.headers = {"ETag": etag} if etag else {}

    def read(self, n=-1):
        return self._body[:n] if n and n > 0 else self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


# ---- E1: the hardened fetch primitive --------------------------------------

def test_a_redirect_is_refused_not_followed(monkeypatch):
    """A redirecting upstream must not be able to bounce us at an internal
    address. The handler raises; fetch_json degrades, it does not follow."""
    def boom(req, timeout=None):
        raise urllib.error.HTTPError(req.full_url, 302, "redirect refused",
                                     {}, None)
    monkeypatch.setattr(update._OPENER, "open", boom)
    out = update.fetch_json("/repos/x/y/releases/latest",
                            max_bytes=update.RELEASE_MAX_BYTES)
    assert out["status"] == 302
    assert out["data"] is None


def test_the_no_redirect_handler_raises_rather_than_redirecting():
    """Directly: the handler subclasses HTTPRedirectHandler so build_opener drops
    the permissive default, and it refuses instead of rewriting the request."""
    h = update._NoRedirectHandler()
    req = urllib.request.Request("https://api.github.com/x")
    with pytest.raises(urllib.error.HTTPError):
        h.redirect_request(req, None, 302, "Found", {},
                           "http://169.254.169.254/")


def test_a_broker_process_proxy_env_cannot_reroute_this_egress(monkeypatch):
    """Passing an EMPTY ProxyHandler makes build_opener skip the env-honouring
    default, so HTTPS_PROXY in the broker's environment cannot pivot the fetch.
    Asserted by contrast against what the default opener does with the same env."""
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:9/")
    monkeypatch.setenv("ALL_PROXY", "http://127.0.0.1:9/")
    default = urllib.request.build_opener()
    default_proxied = [h for h in default.handlers
                       if isinstance(h, urllib.request.ProxyHandler) and h.proxies]
    assert default_proxied, "sanity: the stdlib default does honour the env"
    ours = [h for h in update._OPENER.handlers
            if isinstance(h, urllib.request.ProxyHandler) and h.proxies]
    assert not ours, "our opener must dial api.github.com direct"


def test_the_no_redirect_handler_is_installed():
    assert any(isinstance(h, update._NoRedirectHandler)
               for h in update._OPENER.handlers)


def test_a_body_over_the_cap_is_rejected(monkeypatch):
    monkeypatch.setattr(update._OPENER, "open",
                        lambda req, timeout=None: _FakeResp(b"x" * 5000))
    out = update.fetch_json("/x", max_bytes=100)
    assert out["error"] == update.REASON_TOO_LARGE
    assert out["data"] is None


def test_the_compare_cap_is_larger_than_the_releases_cap():
    """/compare carries commit data, file metadata and patches for up to 300
    changed files. The 512 KB that suits a small JSON object rejects ordinary
    comparisons -- this is a real bug the review caught, so assert the caps
    cannot silently be collapsed back together."""
    assert update.COMPARE_MAX_BYTES > update.RELEASE_MAX_BYTES


def test_a_rate_limit_is_not_retried_and_records_its_reset(monkeypatch):
    calls = []

    def limited(req, timeout=None):
        calls.append(req.full_url)
        raise urllib.error.HTTPError(
            req.full_url, 403, "rate limited",
            {"X-RateLimit-Reset": "1900000000"}, None)
    monkeypatch.setattr(update._OPENER, "open", limited)
    out = update.fetch_json("/x", max_bytes=1000)
    assert out["status"] == 403
    assert out["reset_at"] == 1900000000.0
    assert len(calls) == 1, "a 403 must never be retried on a timer"


def test_retry_after_seconds_is_honoured(monkeypatch):
    monkeypatch.setattr(update._OPENER, "open", lambda req, timeout=None: (_ for _ in ()).throw(
        urllib.error.HTTPError(req.full_url, 429, "slow down",
                               {"Retry-After": "120"}, None)))
    out = update.fetch_json("/x", max_bytes=1000)
    assert out["reset_at"] is not None and out["reset_at"] > 0


def test_a_network_failure_degrades_to_offline(monkeypatch):
    def down(req, timeout=None):
        raise urllib.error.URLError("no route to host")
    monkeypatch.setattr(update._OPENER, "open", down)
    out = update.fetch_json("/x", max_bytes=1000)
    assert out["status"] is None and out["error"] == update.REASON_OFFLINE


def test_malformed_json_is_a_bad_response_not_a_crash(monkeypatch):
    monkeypatch.setattr(update._OPENER, "open",
                        lambda req, timeout=None: _FakeResp(b"<html>nope"))
    out = update.fetch_json("/x", max_bytes=1000)
    assert out["error"] == update.REASON_BAD_RESPONSE


def test_an_etag_is_sent_when_supplied(monkeypatch):
    seen = {}

    def capture(req, timeout=None):
        seen["inm"] = req.get_header("If-none-match")
        return _FakeResp(json.dumps({"ok": 1}).encode())
    monkeypatch.setattr(update._OPENER, "open", capture)
    update.fetch_json("/x", max_bytes=1000, etag='W/"abc"')
    assert seen["inm"] == 'W/"abc"'


def test_the_upstream_repo_is_a_constant_not_derived_from_git():
    """A fork that derived its upstream from `git remote get-url` would check
    ITSELF and report current forever."""
    with open(update.__file__, "r", encoding="utf-8") as fh:
        body = fh.read()
    # The string literal, not the prose: the docstring explains WHY we don't do
    # this, and a substring scan would flag its own warning.
    assert '"remote"' not in body and "'remote'" not in body
    assert update.UPSTREAM_REPO == "JohnConnorNPC/browserland"


def test_the_api_base_is_https():
    assert update.GITHUB_API.startswith("https://")


# ---- E2: mode selection is data-driven -------------------------------------

def test_a_published_release_selects_release_mode():
    assert update.select_mode(_ok({"tag_name": "v1.0.0"})) == "release"


@pytest.mark.parametrize("result", [
    _err(status=404, error="http_404"),      # no releases published yet
    _err(status=403, error="http_403"),      # rate limited
    _err(status=None),                       # offline
    _ok(None, status=200),                   # 200 but useless body
])
def test_anything_that_is_not_a_clean_200_falls_back_to_commit_mode(result):
    """Keyed on 200, not on 404: a 403 or a timeout must not be mistaken for
    'releases exist'."""
    if result.get("status") == 200 and result.get("data") is None:
        # a 200 with a null body still selects release mode at this layer;
        # compute_state is what rejects it. Assert that split explicitly.
        assert update.select_mode(result) == "release"
        st = update.compute_state(mode="release", local_version="0.8.0",
                                  sha=SHA, release_result=result)
        assert st["state"] == update.STATE_UNKNOWN
        return
    assert update.select_mode(result) == "commit"


def test_publishing_a_release_flips_behaviour_with_no_code_change():
    """The pre-1.0 -> post-1.0 switch must not be a hand-flipped flag: the same
    unmodified function must change mode purely because the data changed."""
    before = update.select_mode(_err(status=404, error="http_404"))
    after = update.select_mode(_ok({"tag_name": "v1.0.0"}))
    assert (before, after) == ("commit", "release")


# ---- E3: state derivation, the honesty rules -------------------------------

def test_behind_reports_the_commit_count():
    st = update.compute_state(mode="commit", local_version="0.8.0", sha=SHA,
                              compare_result=_compare(ahead=0, behind=7))
    assert st["state"] == update.STATE_BEHIND
    assert st["behindBy"] == 7


def test_local_commits_report_ahead_not_behind():
    """4445 is exactly this machine. A dev checkout must never be told it is
    stale."""
    st = update.compute_state(mode="commit", local_version="0.8.0", sha=SHA,
                              compare_result=_compare(ahead=3, behind=0))
    assert st["state"] == update.STATE_AHEAD
    assert st["aheadBy"] == 3


def test_both_nonzero_is_diverged_and_still_reports_ahead():
    st = update.compute_state(mode="commit", local_version="0.8.0", sha=SHA,
                              compare_result=_compare(ahead=2, behind=9))
    assert st["state"] == update.STATE_AHEAD
    assert st["aheadBy"] == 2 and st["behindBy"] == 9


def test_identical_history_is_current():
    st = update.compute_state(mode="commit", local_version="0.8.0", sha=SHA,
                              compare_result=_compare(ahead=0, behind=0))
    assert st["state"] == update.STATE_CURRENT


def test_a_compare_404_is_unknown_NOT_ahead():
    """THE correction. The issue specified 404 as the ahead/diverged signal.
    It is not one: the same endpoint 404s for a garbage-collected sha, a repo
    that moved or went private, a malformed ref, and unstably around a
    force-push -- so the same checkout can read diverged now and 404 later.
    Guessing 'ahead' here would report a fabricated state as fact."""
    st = update.compute_state(mode="commit", local_version="0.8.0", sha=SHA,
                              compare_result=_err(status=404,
                                                  error="http_404"))
    assert st["state"] == update.STATE_UNKNOWN
    assert st["reason"] == update.REASON_COMPARE_UNAVAILABLE
    assert st["state"] != update.STATE_AHEAD


def test_every_failure_path_reports_unknown_with_a_distinct_reason():
    """A checker that says 'up to date' when it means 'I could not check' is
    worse than none, because it is trusted."""
    cases = {
        update.REASON_OFFLINE: _err(status=None),
        update.REASON_RATE_LIMITED: _err(status=403, error="http_403"),
        update.REASON_COMPARE_UNAVAILABLE: _err(status=404, error="http_404"),
        update.REASON_TOO_LARGE: _err(status=200,
                                      error=update.REASON_TOO_LARGE),
    }
    seen = set()
    for expected, result in cases.items():
        st = update.compute_state(mode="commit", local_version="0.8.0",
                                  sha=SHA, compare_result=result)
        assert st["state"] == update.STATE_UNKNOWN
        assert st["reason"] == expected
        seen.add(st["reason"])
    assert len(seen) == len(cases), "reasons must be distinguishable"


def test_a_rate_limited_state_carries_its_reset_time():
    st = update.compute_state(
        mode="commit", local_version="0.8.0", sha=SHA,
        compare_result=_err(status=429, error="http_429", reset_at=1900000000.0))
    assert st["reason"] == update.REASON_RATE_LIMITED
    assert st["resetAt"] == 1900000000.0


def test_no_git_hash_is_unknown_never_a_bare_version_match():
    """build_version() degrades to a bare '0.8.0' without git, and its own
    docstring says equality then cannot distinguish same-version-different-code.
    So no sha means no answer."""
    st = update.compute_state(mode="commit", local_version="0.8.0", sha=None,
                              compare_result=_compare(0, 0))
    assert st["state"] == update.STATE_UNKNOWN
    assert st["reason"] == update.REASON_NO_GIT


def test_a_missing_sha_short_circuits_before_spending_a_request():
    """There is no question to ask, so do not burn one of 60 requests/hour
    learning that."""
    calls = []

    def fetcher(path, **kw):
        calls.append(path)
        return _err(status=404, error="http_404")
    out = update.run_check(local_version="0.8.0", sha=None, fetcher=fetcher)
    assert out["state"] == update.STATE_UNKNOWN
    assert out["reason"] == update.REASON_NO_GIT
    assert not any("compare" in c for c in calls)


def test_a_garbage_compare_body_is_bad_response():
    st = update.compute_state(mode="commit", local_version="0.8.0", sha=SHA,
                              compare_result=_ok({"ahead_by": "lots"}))
    assert st["reason"] == update.REASON_BAD_RESPONSE


def test_the_local_sha_is_full_length_and_not_parsed_from_build_version():
    """build_version() carries a SHORT sha and is a display string, not an API.
    Short shas are ambiguity-prone; resolve the real one."""
    sha = update.local_sha()
    if sha is not None:                       # None on a non-checkout install
        assert len(sha) == 40
    with open(update.__file__, "r", encoding="utf-8") as fh:
        body = fh.read()
    assert '"rev-parse"' in body and '"HEAD"' in body, (
        "the sha must be resolved by rev-parse, not sliced out of a display "
        "string that carries only the short form")
    assert '"--short"' not in body


def test_the_tree_is_never_claimed_verified():
    """Build ids carry no dirty marker on purpose, so the payload must not imply
    the working tree was checked."""
    out = update.run_check(local_version="0.8.0", sha=SHA,
                           fetcher=lambda p, **kw: _compare(0, 0)
                           if "compare" in p else _err(404, "http_404"))
    assert out["treeVerified"] is False


# ---- E4: semver, not lexical ------------------------------------------------

def test_semver_is_numeric_not_lexical():
    assert update.parse_version("0.10.0") > update.parse_version("0.9.0")
    assert "0.10.0" < "0.9.0", "the string comparison this replaces is wrong"


@pytest.mark.parametrize("tag,expected", [
    ("v1.2.3", (1, 2, 3)), ("1.2.3", (1, 2, 3)),
    ("v0.10.0", (0, 10, 0)), ("1.2.3-rc1", (1, 2, 3)),
    ("nonsense", None), (None, None), (123, None), ("", None),
])
def test_version_parsing(tag, expected):
    assert update.parse_version(tag) == expected


def test_release_mode_reports_behind_on_a_newer_release():
    st = update.compute_state(
        mode="release", local_version="0.9.0", sha=SHA,
        release_result=_ok({"tag_name": "v0.10.0",
                            "html_url": "https://github.com/x/y/releases/v0.10.0"}))
    assert st["state"] == update.STATE_BEHIND
    assert st["upstream"]["tag"] == "v0.10.0"


def test_release_mode_reports_current_when_not_older():
    st = update.compute_state(mode="release", local_version="1.0.0", sha=SHA,
                              release_result=_ok({"tag_name": "v1.0.0"}))
    assert st["state"] == update.STATE_CURRENT


def test_release_mode_with_an_unparseable_tag_is_unknown():
    st = update.compute_state(mode="release", local_version="1.0.0", sha=SHA,
                              release_result=_ok({"tag_name": "latest"}))
    assert st["state"] == update.STATE_UNKNOWN


def test_the_check_uses_releases_latest_not_the_releases_list():
    """/releases/latest already excludes drafts and prereleases; /releases[0]
    does not."""
    paths = []

    def fetcher(path, **kw):
        paths.append(path)
        return _ok({"tag_name": "v1.0.0"})
    update.run_check(local_version="0.8.0", sha=SHA, fetcher=fetcher)
    assert any(p.endswith("/releases/latest") for p in paths)
    assert not any(p.endswith("/releases") for p in paths)


# ---- run_check wiring -------------------------------------------------------

def _commit_mode_fetcher(paths, upstream_sha="b" * 40, compare=None):
    def fetcher(path, **kw):
        paths.append(path)
        if "releases/latest" in path:
            return _err(status=404, error="http_404")
        if "/commits/" in path:
            return _ok({"sha": upstream_sha})
        return compare if compare is not None else _compare(0, 5)
    return fetcher


def test_a_cold_commit_mode_check_costs_three_requests_at_most():
    """releases probe, upstream head, and -- only when git could not answer
    locally -- one compare."""
    paths = []
    out = update.run_check(local_version="0.8.0", sha=SHA,
                           fetcher=_commit_mode_fetcher(paths),
                           ancestor_fn=lambda s: None)
    assert out["state"] == update.STATE_BEHIND and out["behindBy"] == 5
    assert len(paths) == 3


def test_when_git_can_answer_locally_no_compare_is_requested():
    """The interesting case -- a checkout with unpushed commits -- is exactly
    the one GitHub's compare cannot answer. Asking git instead turns 'could not
    tell' into a correct answer AND saves a request."""
    paths = []
    out = update.run_check(
        local_version="0.8.0", sha=SHA,
        fetcher=_commit_mode_fetcher(paths),
        ancestor_fn=lambda s: {"aheadBy": 3, "behindBy": 0})
    assert out["state"] == update.STATE_AHEAD
    assert out["aheadBy"] == 3
    assert out["ancestrySource"] == "local-git"
    assert not any("compare" in p for p in paths)
    assert len(paths) == 2


def test_the_local_and_api_paths_agree_about_what_the_numbers_mean():
    """Both routes go through _from_counts, so 'ahead wins over behind' cannot
    drift between them."""
    for ahead, behind, expected in [(0, 0, update.STATE_CURRENT),
                                    (0, 4, update.STATE_BEHIND),
                                    (2, 0, update.STATE_AHEAD),
                                    (2, 4, update.STATE_AHEAD)]:
        local = update.compute_state(
            mode="commit", local_version="0.8.0", sha=SHA,
            ancestry={"aheadBy": ahead, "behindBy": behind},
            upstream_sha="b" * 40)
        api = update.compute_state(
            mode="commit", local_version="0.8.0", sha=SHA,
            compare_result=_compare(ahead, behind))
        assert local["state"] == api["state"] == expected
        assert local["aheadBy"] == api["aheadBy"] == ahead
        assert local["behindBy"] == api["behindBy"] == behind


def test_local_ancestry_refuses_a_sha_it_does_not_have():
    """Never a guess: an object that is not in the local store returns None so
    the caller falls back, rather than reporting a measured-looking zero."""
    assert update.local_ancestry("f" * 40) is None
    assert update.local_ancestry("not-a-sha") is None
    assert update.local_ancestry("") is None


def test_local_ancestry_measures_this_repo():
    """Against a commit this checkout definitely has: HEAD itself is zero/zero."""
    here = update.local_sha()
    if here is None:
        pytest.skip("not a git checkout")
    assert update.local_ancestry(here) == {"aheadBy": 0, "behindBy": 0}


def test_a_dead_upstream_head_still_falls_back_to_compare():
    paths = []

    def fetcher(path, **kw):
        paths.append(path)
        if "releases/latest" in path:
            return _err(status=404, error="http_404")
        if "/commits/" in path:
            return _err(status=None)          # offline for this one call
        return _compare(0, 2)
    out = update.run_check(local_version="0.8.0", sha=SHA, fetcher=fetcher,
                           ancestor_fn=lambda s: None)
    assert out["state"] == update.STATE_BEHIND


def test_release_mode_costs_one_request():
    paths = []

    def fetcher(path, **kw):
        paths.append(path)
        return _ok({"tag_name": "v9.0.0"})
    update.run_check(local_version="0.8.0", sha=SHA, fetcher=fetcher)
    assert len(paths) == 1


def test_the_ttl_is_daily_with_jitter():
    """60 requests/hour is shared across every process on the source IP. Hourly
    checking across a handful of brokers behind one NAT burns it."""
    assert update.CHECK_TTL >= 24 * 60 * 60
    assert update.next_ttl(rand=lambda: 0.0) == update.CHECK_TTL
    assert update.next_ttl(rand=lambda: 1.0) > update.CHECK_TTL


def test_run_check_always_returns_a_renderable_shape():
    out = update.run_check(local_version="0.8.0", sha=SHA,
                           fetcher=lambda p, **kw: _err(status=None))
    for key in ("state", "reason", "mode", "local", "repo", "checkedAt",
                "upstream", "treeVerified"):
        assert key in out


# ---- E5 / E20: the route, its two gates, and its cache ---------------------

_app_seq = 0


def _make_app(tmp_path, monkeypatch, token=None, enabled=True):
    """A broker app with state_path in tmp_path and a UNIQUE Sanic name (mirrors
    test_status_fetch)."""
    global _app_seq
    _app_seq += 1
    monkeypatch.delenv("WEB_TERMINAL_TOKEN", raising=False)
    cfg = {"state_path": str(tmp_path / "webterm_state.json"),
           "auth_token": token or TEST_TOKEN,
           "update_check_enabled": enabled}
    return create_app(cfg, name=f"webterm-update-test-{_app_seq}")


def _patch_run_check(monkeypatch, result=None, explode=False):
    """Replace the whole check with a no-network fake that counts calls.
    The route resolves it through the module object, so patching the attribute
    is what the route actually invokes."""
    calls = []

    def fake(**kwargs):
        calls.append(kwargs)
        if explode:
            raise AssertionError(
                "the route made an upstream call it was not permitted to make")
        return result or {"state": update.STATE_CURRENT, "reason": None,
                          "mode": "commit", "local": {"version": "0.8.0",
                                                      "sha": SHA},
                          "repo": update.UPSTREAM_REPO, "checkedAt": 1,
                          "upstream": {"sha": SHA}, "treeVerified": False}

    monkeypatch.setattr(broker_app.update_check, "run_check", fake)
    return calls


def test_the_route_requires_a_token(tmp_path, monkeypatch):
    _patch_run_check(monkeypatch)
    app = _make_app(tmp_path, monkeypatch, token="sekrit")
    # RAW client on purpose: authed() would append the token this test needs
    # to be missing.
    _, r = app.test_client.get("/update/check")
    assert r.status == 401
    _, r = app.test_client.get("/update/check?token=nope")
    assert r.status == 401


def test_a_broker_that_never_opted_in_makes_no_request(tmp_path, monkeypatch):
    """E20, and the one that makes G2 true. The mod shipping default-off does
    NOT gate this route -- without the server-side switch, 'no outbound request
    until you opt in' would be a claim the code does not keep. The fake raises
    if called, so a regression fails loudly rather than silently egressing."""
    _patch_run_check(monkeypatch, explode=True)
    app = _make_app(tmp_path, monkeypatch, enabled=False)
    _, r = authed(app).get("/update/check")
    assert r.status == 503
    assert r.json["ok"] is False
    assert r.json["error"] == "update_check_disabled"


def test_the_gate_defaults_to_off(tmp_path, monkeypatch):
    """Absent config must mean disabled, not enabled."""
    global _app_seq
    _app_seq += 1
    monkeypatch.delenv("WEB_TERMINAL_TOKEN", raising=False)
    app = create_app({"state_path": str(tmp_path / "s.json"),
                      "auth_token": TEST_TOKEN},
                     name=f"webterm-update-default-{_app_seq}")
    assert app.ctx.update_check_enabled is False


def test_an_enabled_broker_returns_the_check(tmp_path, monkeypatch):
    _patch_run_check(monkeypatch)
    app = _make_app(tmp_path, monkeypatch)
    _, r = authed(app).get("/update/check")
    assert r.status == 200
    assert r.json["ok"] is True
    assert r.json["check"]["state"] == update.STATE_CURRENT


def test_a_second_call_inside_the_ttl_does_not_refetch(tmp_path, monkeypatch):
    calls = _patch_run_check(monkeypatch)
    app = _make_app(tmp_path, monkeypatch)
    client = authed(app)
    client.get("/update/check")
    client.get("/update/check")
    client.get("/update/check")
    assert len(calls) == 1, "the daily TTL must serve the cached result"


def test_the_client_cannot_supply_a_repo_or_url(tmp_path, monkeypatch):
    """Structural, not filtered: there is no parameter to smuggle a URL
    through, so an attacker-supplied host cannot become the fetch target."""
    calls = _patch_run_check(monkeypatch)
    app = _make_app(tmp_path, monkeypatch)
    authed(app).get("/update/check?repo=evil/repo"
                    "&url=http://169.254.169.254/latest/meta-data/")
    assert len(calls) == 1
    assert calls[0]["repo"] == update.UPSTREAM_REPO
    for value in calls[0].values():
        assert "169.254" not in str(value)
        assert "evil" not in str(value)


def test_a_rate_limited_result_waits_for_the_reset(tmp_path, monkeypatch):
    """Not our own retry timer: wait exactly as long as GitHub said."""
    import time as _time
    reset_at = _time.time() + 3600
    _patch_run_check(monkeypatch, result={
        "state": update.STATE_UNKNOWN,
        "reason": update.REASON_RATE_LIMITED, "resetAt": reset_at,
        "mode": "commit", "local": {}, "repo": update.UPSTREAM_REPO,
        "checkedAt": 1, "upstream": None, "treeVerified": False})
    app = _make_app(tmp_path, monkeypatch)
    authed(app).get("/update/check")
    assert app.ctx.update_cache["until"] == pytest.approx(reset_at, abs=2)


def test_a_transient_failure_is_cached_briefly_not_for_a_day(tmp_path,
                                                             monkeypatch):
    """An offline blip must not pin `unknown` until tomorrow."""
    import time as _time
    _patch_run_check(monkeypatch, result={
        "state": update.STATE_UNKNOWN, "reason": update.REASON_OFFLINE,
        "mode": "commit", "local": {}, "repo": update.UPSTREAM_REPO,
        "checkedAt": 1, "upstream": None, "treeVerified": False})
    app = _make_app(tmp_path, monkeypatch)
    authed(app).get("/update/check")
    ahead = app.ctx.update_cache["until"] - _time.time()
    assert 0 < ahead <= broker_app.UPDATE_RETRY_TTL + 2
    assert ahead < update.CHECK_TTL


def test_a_success_is_cached_for_a_day(tmp_path, monkeypatch):
    import time as _time
    _patch_run_check(monkeypatch)
    app = _make_app(tmp_path, monkeypatch)
    authed(app).get("/update/check")
    ahead = app.ctx.update_cache["until"] - _time.time()
    assert ahead >= update.CHECK_TTL - 2


def test_the_egress_comment_names_both_routes():
    """app.py's egress comment is load-bearing for reviewing this codebase's
    threat model. Adding a second outbound route made the old wording false."""
    src = Path(broker_app.__file__).read_text(encoding="utf-8")
    assert "GET /status/fetch is its ONLY" not in src
    head = src[:src.index("STATUS_ALLOWLIST")]
    assert "/update/check" in head and "/status/fetch" in head
