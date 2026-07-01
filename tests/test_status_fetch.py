"""Outbound AI-provider status proxy: GET /status/fetch (#112).

This is the broker's ONLY outbound HTTP. SSRF defense is STRUCTURAL, not a
filter: the client passes allowlist IDs, never URLs; an unknown ID is dropped, a
request that names providers but validates NONE is a 400, a stray ?url= is
ignored, and the upstream fetch itself is https-only + no-redirect + size-capped.

These cover the token-or-loopback gate + CORS preflight (mirroring
test_broker_info.py), the allowlist/400 SSRF proof, the Statuspage normalization,
the https/no-redirect/200-only/size-cap fetch path, and the TTL cache. NO real
network I/O: the module-level _fetch_status_blocking (for route tests) and the
shared _STATUS_OPENER (for fetch-path tests) are monkeypatched.
"""

from __future__ import annotations

import urllib.error
import urllib.request

import pytest

from webterm.broker import app as broker_app
from webterm.broker.app import (STATUS_ALLOWLIST, STATUS_MAX_BYTES, create_app,
                                _fetch_status_blocking, _normalize_statuspage)


# ---- normalization (pure) ------------------------------------------------

def test_normalize_valid_payload():
    payload = {
        "status": {"indicator": "major", "description": "Partial Outage"},
        "incidents": [{"name": "API errors", "impact": "major",
                       "status": "investigating"}],
        "components": [
            {"name": "API", "status": "partial_outage"},
            {"name": "Healthy", "status": "operational"},      # dropped (ok)
            {"name": "Grp", "status": "degraded_performance",  # dropped (group
             "group": True},                                   #  container)
        ],
    }
    out = _normalize_statuspage("openai", "OpenAI", payload)
    assert out["id"] == "openai" and out["label"] == "OpenAI"
    assert out["indicator"] == "major"
    assert out["description"] == "Partial Outage"
    assert out["incidents"] == [{"name": "API errors", "impact": "major"}]
    assert out["components"] == [{"name": "API", "status": "partial_outage"}]


def test_normalize_unknown_indicator_and_junk_payload():
    # An unrecognized indicator, and non-dict payloads, all normalize to unknown.
    assert _normalize_statuspage(
        "x", "X", {"status": {"indicator": "weird"}})["indicator"] == "unknown"
    assert _normalize_statuspage("x", "X", None)["indicator"] == "unknown"
    assert _normalize_statuspage("x", "X", [])["indicator"] == "unknown"
    # ...and the shape is always complete (never blocks the client renderer).
    j = _normalize_statuspage("x", "X", None)
    assert j["incidents"] == [] and j["components"] == [] and j["description"] == ""


def test_normalize_caps_incidents_and_components():
    payload = {
        "status": {"indicator": "minor"},
        "incidents": [{"name": f"i{i}", "impact": "minor"} for i in range(25)],
        "components": [{"name": f"c{i}", "status": "major_outage"}
                       for i in range(50)],
    }
    out = _normalize_statuspage("x", "X", payload)
    assert len(out["incidents"]) == 10
    assert len(out["components"]) == 20


def test_normalize_skips_resolved_incidents():
    payload = {"status": {"indicator": "none"}, "incidents": [
        {"name": "old", "impact": "none", "status": "resolved"},
        {"name": "live", "impact": "minor", "status": "monitoring"},
    ]}
    out = _normalize_statuspage("x", "X", payload)
    assert [i["name"] for i in out["incidents"]] == ["live"]


# ---- fetch path (real _fetch_status_blocking, faked opener) --------------

class _FakeResp:
    """Minimal urlopen-response stand-in: a context manager exposing .status and
    a size-honoring .read(n)."""

    def __init__(self, status, body):
        self.status = status
        self._body = body

    def read(self, n=-1):
        return self._body[:n] if (n is not None and n >= 0) else self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_fetch_blocking_builds_summary_url_https_only(monkeypatch):
    seen = {}
    body = (b'{"status":{"indicator":"none","description":"All Systems '
            b'Operational"},"incidents":[],"components":[]}')

    def fake_open(req, timeout=None):
        seen["url"] = req.full_url
        seen["timeout"] = timeout
        return _FakeResp(200, body)

    monkeypatch.setattr(broker_app._STATUS_OPENER, "open", fake_open)
    out = _fetch_status_blocking("anthropic")
    # id-only -> the FIXED allowlist base + the summary path; https, no caller URL.
    assert seen["url"] == "https://status.claude.com/api/v2/summary.json"
    assert seen["timeout"] == broker_app.STATUS_FETCH_TIMEOUT
    assert out["indicator"] == "none" and out["label"] == "Anthropic"
    assert "error" not in out


def test_fetch_blocking_non_200_degrades_unknown(monkeypatch):
    monkeypatch.setattr(broker_app._STATUS_OPENER, "open",
                        lambda req, timeout=None: _FakeResp(503, b"nope"))
    out = _fetch_status_blocking("openai")
    assert out["indicator"] == "unknown"
    assert out["error"]                      # exception type name echoed


def test_fetch_blocking_oversize_degrades_unknown(monkeypatch):
    big = b"x" * (STATUS_MAX_BYTES + 10)
    monkeypatch.setattr(broker_app._STATUS_OPENER, "open",
                        lambda req, timeout=None: _FakeResp(200, big))
    out = _fetch_status_blocking("cohere")
    assert out["indicator"] == "unknown"
    assert out["error"] == "ValueError"      # the too_large guard


def test_fetch_blocking_transport_error_degrades_unknown(monkeypatch):
    def boom(req, timeout=None):
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr(broker_app._STATUS_OPENER, "open", boom)
    out = _fetch_status_blocking("copilot")
    assert out["indicator"] == "unknown"
    assert out["error"] == "URLError"


def test_no_redirect_handler_refuses_redirect():
    # The SSRF no-redirect defense: a status host that 30x-es to an internal
    # address is refused BEFORE the fetch can pivot there.
    h = broker_app._NoRedirectHandler()
    req = urllib.request.Request("https://status.claude.com/api/v2/summary.json")
    with pytest.raises(urllib.error.HTTPError):
        h.redirect_request(req, None, 302, "Found", {},
                           "http://169.254.169.254/latest/meta-data/")


# ---- route: gate, allowlist/400 SSRF proof, cache, CORS ------------------

_app_seq = 0


def _make_app(tmp_path, monkeypatch, token=None):
    """A broker app with state_path in tmp_path (identity file lands there, not the
    repo) and a UNIQUE Sanic name. Env token would override config, so clear it and
    set auth_token explicitly when a token is wanted (mirrors test_broker_info)."""
    global _app_seq
    _app_seq += 1
    monkeypatch.delenv("WEB_TERMINAL_TOKEN", raising=False)
    cfg = {"state_path": str(tmp_path / "webterm_state.json")}
    if token:
        cfg["auth_token"] = token
    return create_app(cfg, name=f"webterm-status-test-{_app_seq}")


def _patch_fetch(monkeypatch):
    """Replace the blocking fetch with a no-network fake that records each call.
    _status_one resolves _fetch_status_blocking as a module global at call time,
    so patching the module attribute is what the route actually invokes."""
    calls = []

    def fake(pid):
        calls.append(pid)
        return {"id": pid, "label": STATUS_ALLOWLIST[pid]["label"],
                "indicator": "none", "description": "All Systems Operational",
                "incidents": [], "components": []}

    monkeypatch.setattr(broker_app, "_fetch_status_blocking", fake)
    return calls


def test_status_loopback_ok_no_token_returns_all(tmp_path, monkeypatch):
    _patch_fetch(monkeypatch)
    app = _make_app(tmp_path, monkeypatch, token=None)
    _, r = app.test_client.get("/status/fetch")
    assert r.status == 200
    assert r.json["ok"] is True
    assert isinstance(r.json["fetchedAt"], int)
    assert [p["id"] for p in r.json["providers"]] == list(STATUS_ALLOWLIST.keys())


def test_status_requires_token_when_configured(tmp_path, monkeypatch):
    _patch_fetch(monkeypatch)
    app = _make_app(tmp_path, monkeypatch, token="sekrit")
    _, r = app.test_client.get("/status/fetch")
    assert r.status == 401
    _, r = app.test_client.get("/status/fetch?token=nope")
    assert r.status == 401
    _, r = app.test_client.get("/status/fetch?token=sekrit")
    assert r.status == 200
    assert r.json["ok"] is True


def test_status_empty_provider_returns_all(tmp_path, monkeypatch):
    _patch_fetch(monkeypatch)
    app = _make_app(tmp_path, monkeypatch, token=None)
    _, r = app.test_client.get("/status/fetch?provider=")
    assert r.status == 200
    assert [p["id"] for p in r.json["providers"]] == list(STATUS_ALLOWLIST.keys())


def test_status_unknown_dropped_and_all_unknown_is_400(tmp_path, monkeypatch):
    # The SSRF allowlist proof: unknown ids are DROPPED, an all-unknown request is
    # 400, and a caller-supplied ?url= is IGNORED (never a fetch target).
    _patch_fetch(monkeypatch)
    app = _make_app(tmp_path, monkeypatch, token=None)

    _, r = app.test_client.get("/status/fetch?provider=anthropic,evil,openai")
    assert r.status == 200
    assert [p["id"] for p in r.json["providers"]] == ["anthropic", "openai"]

    _, r = app.test_client.get("/status/fetch?provider=evil")
    assert r.status == 400
    assert r.json["ok"] is False

    # A path-traversal-ish token is just an unknown id -> 400, never a fetch.
    _, r = app.test_client.get("/status/fetch?provider=../secrets")
    assert r.status == 400

    # ?url= alone is ignored -> treated as "no provider" -> all providers.
    _, r = app.test_client.get(
        "/status/fetch?url=http://169.254.169.254/latest/meta-data/")
    assert r.status == 200
    assert [p["id"] for p in r.json["providers"]] == list(STATUS_ALLOWLIST.keys())

    # ...and a url can never rescue an all-unknown provider list.
    _, r = app.test_client.get(
        "/status/fetch?provider=evil&url=http://169.254.169.254/")
    assert r.status == 400


def test_status_caches_within_ttl(tmp_path, monkeypatch):
    # Two calls inside STATUS_CACHE_TTL -> the blocking fetch runs ONCE per id.
    calls = _patch_fetch(monkeypatch)
    app = _make_app(tmp_path, monkeypatch, token=None)
    _, r1 = app.test_client.get("/status/fetch?provider=anthropic")
    assert r1.status == 200
    _, r2 = app.test_client.get("/status/fetch?provider=anthropic")
    assert r2.status == 200
    assert calls == ["anthropic"]            # second served from cache, no refetch


def test_status_options_preflight_cors(tmp_path, monkeypatch):
    # Even with a token configured the preflight is answered ACAO:* (the browser
    # cross-origin fetch depends on it), exactly like /info and /state.
    app = _make_app(tmp_path, monkeypatch, token="sekrit")
    _, r = app.test_client.options("/status/fetch")
    assert r.status == 204
    assert r.headers.get("Access-Control-Allow-Origin") == "*"
    assert r.headers.get("Access-Control-Allow-Methods") == \
        "GET, POST, PUT, OPTIONS"
