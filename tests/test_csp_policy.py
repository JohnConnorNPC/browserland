"""The full #190 CSP policy, served Report-Only while the soak runs.

Two headers, two pins:

* ``Content-Security-Policy`` (ENFORCED) stays byte-identical to what it was
  before #190's assembly landed: ``script-src`` + ``frame-ancestors`` only.
  Not a substring check — the exact string, so nothing can leak into it.
* ``Content-Security-Policy-Report-Only`` carries the FULL policy:
  ``default-src 'self'``, the enumerated carve-outs, a per-response
  ``connect-src`` = 'self' + the cached registered-hosts fragment
  (test_csp_hosts_fragment.py pins that machinery) + the serving origin's
  ws/wss twins derived from the request Host header, ``frame-src 'self'``,
  ``form-action 'self'``, ``base-uri 'self'``, today's ``frame-ancestors``.

The flip (make the full policy the enforced one, drop Report-Only) is a
separate change gated on the soak; when it lands, the Report-Only pins here
become the enforced pins.

Same client conventions as test_csp_hosts_fragment.py: in-process Sanic test
client, unique app names, state_path in a tmp dir. Header tests default to
``serve_ui=False`` — the middleware decorates every response identically and
a headless app skips the UI assembly; one test runs the real page to pin the
hash-bearing variant.
"""

from __future__ import annotations

import json
import logging

from .auth_helpers import TEST_TOKEN, authed
from webterm.broker.app import create_app

LOGGER_NAME = "webterm.broker.app"
ENFORCED = "Content-Security-Policy"
REPORT_ONLY = "Content-Security-Policy-Report-Only"
#: The enforced header of a headless broker, byte-for-byte. If this ever has
#: to change, the #190 soak-then-flip contract changed with it.
HEADLESS_ENFORCED = "script-src 'self'; frame-ancestors 'none'"

_app_seq = 0


def _make_app(tmp_path, monkeypatch, hosts=None, **cfg):
    global _app_seq
    _app_seq += 1
    monkeypatch.delenv("WEB_TERMINAL_TOKEN", raising=False)
    if hosts is not None:
        (tmp_path / "webterm_state.json").write_text(json.dumps({
            "rev": 1,
            "settings": {"_hosts": hosts},
            "layout": {},
        }), encoding="utf-8")
    conf = {"state_path": str(tmp_path / "webterm_state.json"),
            "auth_token": TEST_TOKEN,
            "serve_ui": False}
    conf.update(cfg)
    return create_app(conf, name=f"webterm-csppolicy-test-{_app_seq}")


def _host(url, **extra):
    entry = {"id": "h1", "label": "box", "url": url, "token": "SECRET-TOKEN"}
    entry.update(extra)
    return entry


def _get(app, path="/", host=None):
    headers = {"Host": host} if host else None
    _, r = authed(app).get(path, headers=headers)
    return r


def _directives(policy):
    """``{directive: value}`` for a policy joined with '; '."""
    out = {}
    for part in policy.split("; "):
        name, _, value = part.partition(" ")
        out[name] = value
    return out


# ---- the enforced header: byte-identical -----------------------------------

def test_enforced_header_is_byte_identical_headless(tmp_path, monkeypatch):
    """A registered host changes the Report-Only policy and NOTHING else: the
    enforced header is the exact pre-#190 string, on / and on error paths
    (the middleware decorates those too — pinned in test_auth_mandatory)."""
    app = _make_app(tmp_path, monkeypatch,
                    hosts=[_host("http://box-a:4445/")])
    r = _get(app)
    assert r.headers.get(ENFORCED) == HEADLESS_ENFORCED
    assert REPORT_ONLY in r.headers
    r404 = _get(app, path="/definitely-not-a-route")
    assert r404.status == 404
    assert r404.headers.get(ENFORCED) == HEADLESS_ENFORCED
    assert REPORT_ONLY in r404.headers


def test_enforced_header_is_byte_identical_with_ui(tmp_path, monkeypatch):
    """The serve_ui variant: the enforced header is exactly the inline-hash
    form, and the Report-Only script-src is byte-identical to the enforced
    one (same hash, same origins, same order — shared helper)."""
    from webterm.broker.ui import INDEX_HTML, inline_script_hash
    app = _make_app(tmp_path, monkeypatch, serve_ui=True)
    r = _get(app)
    expected = (f"script-src '{inline_script_hash(INDEX_HTML)}' 'self'; "
                "frame-ancestors 'none'")
    assert r.headers.get(ENFORCED) == expected
    enforced_script = _directives(r.headers[ENFORCED])["script-src"]
    ro_script = _directives(r.headers[REPORT_ONLY])["script-src"]
    assert ro_script == enforced_script


# ---- the Report-Only policy: full shape ------------------------------------

def test_report_only_carries_the_full_policy(tmp_path, monkeypatch):
    """Whole-string pin: registered hosts' origins + ws twins ride connect-src
    in registry order, then the serving origin's BOTH twins (the broker
    terminates no TLS, so it cannot know the browser-facing scheme); the
    fixed directives sit around them exactly as specified."""
    app = _make_app(tmp_path, monkeypatch, hosts=[
        _host("http://box-a:4445/"),
        _host("https://ts.example.net/desk", id="h2", token="OTHER"),
    ])
    r = _get(app, host="dev.example:4445")
    assert r.headers.get(REPORT_ONLY) == (
        "default-src 'self'; "
        "script-src 'self'; "
        "style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data:; "
        "connect-src 'self' "
        "http://box-a:4445 ws://box-a:4445 "
        "https://ts.example.net wss://ts.example.net "
        "ws://dev.example:4445 wss://dev.example:4445; "
        "frame-src 'self'; "
        "form-action 'self'; "
        "base-uri 'self'; "
        "frame-ancestors 'none'")


def test_no_hosts_connect_src_is_self_plus_self_twins(tmp_path, monkeypatch):
    app = _make_app(tmp_path, monkeypatch)
    r = _get(app, host="dev.example:4445")
    assert _directives(r.headers[REPORT_ONLY])["connect-src"] == \
        "'self' ws://dev.example:4445 wss://dev.example:4445"


def test_host_header_is_normalized_like_the_fragment(tmp_path, monkeypatch):
    """urlsplit lowercases the host and unbrackets IPv6 — the twins come out
    in the same shape _csp_origin_pair emits for a registered URL."""
    app = _make_app(tmp_path, monkeypatch)
    r = _get(app, host="BOX.Example:4445")
    assert _directives(r.headers[REPORT_ONLY])["connect-src"] == \
        "'self' ws://box.example:4445 wss://box.example:4445"
    r = _get(app, host="[::1]:4445")
    assert _directives(r.headers[REPORT_ONLY])["connect-src"] == \
        "'self' ws://[::1]:4445 wss://[::1]:4445"


def test_hostile_host_header_omits_twins_and_logs_once(tmp_path, monkeypatch,
                                                       caplog):
    """A Host that fails the grammar never reaches the header — the twins are
    OMITTED (connect-src stays 'self' + the hosts fragment), the value is
    never echoed, and the warning fires once per process, not per request."""
    app = _make_app(tmp_path, monkeypatch)
    with caplog.at_level(logging.WARNING, logger=LOGGER_NAME):
        r = _get(app, host="evil;connect-src *")
        assert _directives(r.headers[REPORT_ONLY])["connect-src"] == "'self'"
        assert "evil" not in r.headers[REPORT_ONLY]
        # Byte-identity holds on this response too.
        assert r.headers.get(ENFORCED) == HEADLESS_ENFORCED
        r = _get(app, host="also bad")
        assert _directives(r.headers[REPORT_ONLY])["connect-src"] == "'self'"
    warnings = [rec for rec in caplog.records
                if "Host header failed validation" in rec.message]
    assert len(warnings) == 1
    assert "evil" not in warnings[0].message


# ---- the dynamic half: two brokers, and a registry change ------------------

def test_two_brokers_with_different_registries_differ(tmp_path, monkeypatch):
    """E9's two-broker check: each broker enumerates ITS registry for the
    pages it serves, so the same request Host yields different connect-src."""
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    app_a = _make_app(tmp_path / "a", monkeypatch,
                      hosts=[_host("http://box-a:4445/")])
    app_b = _make_app(tmp_path / "b", monkeypatch,
                      hosts=[_host("https://box-b:8446/")])
    ro_a = _get(app_a, host="dev.example:4445").headers[REPORT_ONLY]
    ro_b = _get(app_b, host="dev.example:4445").headers[REPORT_ONLY]
    assert ro_a != ro_b
    assert "http://box-a:4445 ws://box-a:4445" in ro_a
    assert "box-b" not in ro_a
    assert "https://box-b:8446 wss://box-b:8446" in ro_b
    assert "box-a" not in ro_b


def test_state_put_changing_hosts_shows_in_the_next_response(tmp_path,
                                                             monkeypatch):
    """The registry funnels through the /state locked write; the NEXT response
    reflects it (an already-loaded document keeps its boot-time policy — the
    header is per-document, same semantics as installed mods)."""
    app = _make_app(tmp_path, monkeypatch)
    assert "box-a" not in _get(app, host="dev:1").headers[REPORT_ONLY]
    client = authed(app)
    _, r = client.put("/state", json={
        "baseRev": 0, "clientId": "me", "layout": {},
        "settings": {"_hosts": [_host("http://box-a:4445/")]},
    })
    assert r.status == 200
    assert _directives(_get(app, host="dev:1").headers[REPORT_ONLY])[
        "connect-src"] == \
        "'self' http://box-a:4445 ws://box-a:4445 ws://dev:1 wss://dev:1"
