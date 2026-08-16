"""The hardened, cached connect-src hosts fragment (#190).

The broker derives, from the registered hosts in the /state settings blob
(``settings._hosts``), the origin list (plus ws/wss twins) that the upcoming
dynamic ``connect-src`` will concatenate into the CSP header. This file pins
the FRAGMENT machinery only — the served header is deliberately untouched
here, and one test pins that too:

* strict parsing: http/https only; malformed / file: / javascript: / bare junk
  is SKIPPED with a log line and never emitted (a bad entry could otherwise
  break parsing for the whole policy, or smuggle a source);
* dedup: two hosts on one origin emit it once (including default-port spellings);
* caps: a bounded host count and a bounded total fragment size — over-cap
  entries are skipped with a log line, never a crash, never an unbounded header;
* cache: computed at boot from the persisted state, recomputed only inside the
  /state locked write when a PUT changes ``_hosts`` — never per request;
* no ``_hosts`` (or empty) -> "" — the header assembler then emits
  ``connect-src 'self'`` plus the serving origin's own ws/wss twins only.

Route tests use the in-process Sanic test client (each create_app needs a
UNIQUE name) with state_path in a tmp dir, mirroring test_mod_policy.py.
"""

from __future__ import annotations

import json
import logging

from .auth_helpers import TEST_TOKEN, authed
from webterm.broker.app import (MAX_CSP_HOSTS, MAX_CSP_HOSTS_FRAGMENT_CHARS,
                                _compute_csp_hosts_fragment, create_app,
                                csp_hosts_fragment)

LOGGER_NAME = "webterm.broker.app"

_app_seq = 0


def _make_app(tmp_path, monkeypatch, **cfg):
    global _app_seq
    _app_seq += 1
    monkeypatch.delenv("WEB_TERMINAL_TOKEN", raising=False)
    conf = {"state_path": str(tmp_path / "webterm_state.json"),
            "auth_token": TEST_TOKEN}
    conf.update(cfg)
    return create_app(conf, name=f"webterm-csphosts-test-{_app_seq}")


def _host(url, **extra):
    entry = {"id": "h1", "label": "box", "url": url, "token": "SECRET-TOKEN"}
    entry.update(extra)
    return entry


# ---- pure compute: parsing, twins, dedup ----------------------------------

def test_no_hosts_or_empty_is_an_empty_fragment():
    """Absent/empty registry -> "" — the header atom then emits
    ``connect-src 'self'`` + the serving origin's self twins only."""
    assert _compute_csp_hosts_fragment(None) == ""
    assert _compute_csp_hosts_fragment([]) == ""


def test_origins_carry_their_ws_twins_and_exact_ports():
    frag = _compute_csp_hosts_fragment([
        _host("http://box-a:4445/"),
        _host("https://ts.example.net/desk?token=x"),
    ])
    # Ports are part of the origin (dev 4445 vs deploy vs throwaway brokers
    # are distinct sources); path/query never leak into the fragment.
    assert frag == ("http://box-a:4445 ws://box-a:4445 "
                    "https://ts.example.net wss://ts.example.net")


def test_duplicate_origins_emit_once():
    frag = _compute_csp_hosts_fragment([
        _host("https://box:8445/one"),
        _host("https://box:8445/two", id="h2", token="OTHER"),
        # Default-port spellings are the SAME origin as the bare form.
        _host("http://plain/"),
        _host("http://plain:80/"),
        _host("https://tls/"),
        _host("https://tls:443/"),
    ])
    assert frag == ("https://box:8445 wss://box:8445 "
                    "http://plain ws://plain https://tls wss://tls")


def test_ipv6_literal_is_rebuilt_bracketed():
    frag = _compute_csp_hosts_fragment([_host("http://[::1]:4445/")])
    assert frag == "http://[::1]:4445 ws://[::1]:4445"


def test_malformed_urls_are_skipped_with_a_log_and_never_emitted(caplog):
    bad = [
        _host("file:///etc/passwd"),            # non-http scheme
        _host("javascript:alert(1)"),           # non-http scheme
        _host("not a url at all"),              # bare junk (no scheme/host)
        _host("http://"),                       # no host
        _host("http://box:notaport/"),          # junk port
        _host("http://box:99999999/"),          # out-of-range port
        _host("http://bad host/"),              # whitespace in host
        _host("http://exa\nmple.com/"),         # control char (urlsplit would
                                                #   silently STRIP it)
        _host("http://u:pw@creds.example/"),    # userinfo is not an origin
        _host("http://evil;connect-src *//"),   # delimiter smuggle attempt
        _host("http://*.wild.example/"),        # wildcard smuggle attempt
        _host("http://under_score.example/"),   # not CSP host-part grammar
        _host("http://.lead.example/"),         # empty leading label
        _host("http://a..b.example/"),          # empty inner label
        _host("http://" + "a" * 2000 + ".example/"),    # over-long url
        _host("http://[::1/"),                  # unclosed IPv6 bracket
        {"id": "h9", "token": "SECRET-TOKEN"},  # entry without a url
        "http://bare-string",                   # entry not even a dict
    ]
    with caplog.at_level(logging.WARNING, logger=LOGGER_NAME):
        frag = _compute_csp_hosts_fragment(
            bad + [_host("https://good.example/")])
    # The one good entry still emits; not one byte of the junk does.
    assert frag == "https://good.example wss://good.example"
    skips = [r for r in caplog.records if "csp hosts fragment" in r.message]
    assert len(skips) == len(bad)
    # An entry carries a token, and a stored URL can embed credentials: the
    # skip lines identify entries by id/position, never by value.
    for r in skips:
        assert "SECRET-TOKEN" not in r.message
        assert "pw@" not in r.message and "passwd" not in r.message


def test_a_non_list_hosts_value_is_ignored_with_a_log(caplog):
    with caplog.at_level(logging.WARNING, logger=LOGGER_NAME):
        assert _compute_csp_hosts_fragment({"url": "http://x/"}) == ""
        assert _compute_csp_hosts_fragment("http://x/") == ""
    assert sum("not a list" in r.message for r in caplog.records) == 2


# ---- pure compute: the two caps -------------------------------------------

def test_host_count_over_cap_is_skipped_with_a_log(caplog):
    hosts = [_host(f"http://box-{i}:1{i:04d}/", id=f"h{i}")
             for i in range(MAX_CSP_HOSTS + 10)]
    with caplog.at_level(logging.WARNING, logger=LOGGER_NAME):
        frag = _compute_csp_hosts_fragment(hosts)
    origins = frag.split(" ")
    assert len(origins) == MAX_CSP_HOSTS * 2          # origin + twin each
    assert f"http://box-{MAX_CSP_HOSTS - 1}:1{MAX_CSP_HOSTS - 1:04d}" in origins
    assert f"http://box-{MAX_CSP_HOSTS}" not in frag  # first over-cap entry
    assert any("10 host entries over" in r.message for r in caplog.records)


def test_fragment_size_over_cap_is_skipped_with_a_log(caplog):
    # A few origins long enough that they cannot all fit under the char cap,
    # but few enough to stay under the count cap: the size cap must trip.
    label = "a" * 200
    hosts = [_host(f"http://{label}{i:02d}.example:4445/", id=f"h{i}")
             for i in range(30)]
    with caplog.at_level(logging.WARNING, logger=LOGGER_NAME):
        frag = _compute_csp_hosts_fragment(hosts)
    assert len(frag) <= MAX_CSP_HOSTS_FRAGMENT_CHARS
    assert frag.startswith(f"http://{label}00.example:4445 "
                           f"ws://{label}00.example:4445")
    assert any("fragment cap" in r.message for r in caplog.records)


# ---- the cache: boot + the /state locked write ----------------------------

def test_boot_computes_the_cache_from_the_persisted_state(tmp_path,
                                                          monkeypatch):
    (tmp_path / "webterm_state.json").write_text(json.dumps({
        "rev": 3,
        "settings": {"_hosts": [_host("https://fleet.example:8445/")]},
        "layout": {},
    }), encoding="utf-8")
    app = _make_app(tmp_path, monkeypatch)
    assert csp_hosts_fragment(app) == \
        "https://fleet.example:8445 wss://fleet.example:8445"
    # The ENFORCED header stays script-src + frame-ancestors only until the
    # Report-Only soak flips it — the full policy (with connect-src) ships
    # ONLY as Content-Security-Policy-Report-Only, pinned in
    # test_csp_policy.py. This pin is the byte-identity half.
    assert "connect-src" not in app.ctx.csp
    assert "fleet.example" not in app.ctx.csp


def test_state_put_changing_hosts_recomputes_the_cache(tmp_path, monkeypatch):
    app = _make_app(tmp_path, monkeypatch)
    assert csp_hosts_fragment(app) == ""              # fresh store: no _hosts
    client = authed(app)

    _, r = client.put("/state", json={
        "baseRev": 0, "clientId": "me", "layout": {},
        "settings": {"_hosts": [_host("http://box-a:4445/")]},
    })
    assert r.status == 200
    assert csp_hosts_fragment(app) == "http://box-a:4445 ws://box-a:4445"

    # A PUT that does NOT touch _hosts leaves the cached object alone — the
    # identity check is the "no per-request recompute" pin (a recompute would
    # build a fresh string even with equal content).
    before = csp_hosts_fragment(app)
    _, r = client.put("/state", json={
        "baseRev": 1, "clientId": "me", "layout": {"cols": 2},
        "settings": {"_hosts": [_host("http://box-a:4445/")]},
    })
    assert r.status == 200
    assert csp_hosts_fragment(app) is before

    # Removing the registry empties the fragment on the next read.
    _, r = client.put("/state", json={
        "baseRev": 2, "clientId": "me", "layout": {}, "settings": {},
    })
    assert r.status == 200
    assert csp_hosts_fragment(app) == ""


def test_a_rejected_put_does_not_move_the_cache(tmp_path, monkeypatch):
    app = _make_app(tmp_path, monkeypatch)
    client = authed(app)
    # Stale baseRev: refused 409, so the registry (and the cache) never moved.
    _, r = client.put("/state", json={
        "baseRev": 7, "clientId": "me", "layout": {},
        "settings": {"_hosts": [_host("http://never-lands:1/")]},
    })
    assert r.status == 409
    assert csp_hosts_fragment(app) == ""
