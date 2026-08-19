"""OD8: the browser reports its registry origins to the broker (#190).

Two halves, both executed rather than asserted about:

* the CLIENT half -- the shipped 56_js_hosts.js cookie writer, sliced and run
  in node against a fake ``document.cookie`` / ``window.location``: origins
  only (never the entry's token, never userinfo), capped, idempotent, and
  ``Secure`` exactly when the page is https.
* the SERVER half -- the cookie feeds the EXISTING hardened path
  (_csp_origin_pair / _compute_csp_hosts_fragment), is UNIONed with the
  /state-derived fragment, and can widen ``connect-src`` and NOTHING else:
  the hostile-cookie test pins the whole assembled header against a
  ``;``-injected directive, ``'unsafe-inline'``, ``data:``, an over-long value,
  500 origins and an embedded newline.

Client conventions match tests/test_csp_policy.py (in-process Sanic test
client, unique app names, ``serve_ui=False``).
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
from pathlib import Path

import pytest

from .auth_helpers import TEST_TOKEN, authed
from webterm.broker import ui
from webterm.broker.app import (create_app, CSP_HOSTS_COOKIE,
                                _assemble_full_csp)

BROKER_DIR = Path(ui.__file__).resolve().parent
NODE = shutil.which("node")

ENFORCED = "Content-Security-Policy"
REPORT_ONLY = "Content-Security-Policy-Report-Only"
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
    return create_app(conf, name=f"webterm-cspcookie-test-{_app_seq}")


def _get(app, cookie=None, host="dev.example:4445", path="/"):
    headers = {"Host": host}
    if cookie is not None:
        headers["Cookie"] = f"{CSP_HOSTS_COOKIE}={cookie}"
    _, r = authed(app).get(path, headers=headers)
    return r


def _directives(policy):
    out = {}
    for part in policy.split("; "):
        name, _, value = part.partition(" ")
        out[name] = value
    return out


def _connect_src(r):
    return _directives(r.headers[REPORT_ONLY])["connect-src"]


# ---- the server half -------------------------------------------------------

def test_a_reported_origin_widens_connect_src(tmp_path, monkeypatch):
    """The production case OD8 exists for: NOTHING in /state, and the page
    still gets its registered brokers named in connect-src."""
    app = _make_app(tmp_path, monkeypatch)
    assert _connect_src(_get(app)) == \
        "'self' ws://dev.example:4445 wss://dev.example:4445"
    r = _get(app, cookie="http://box-a:4445|https://ts.example.net")
    assert _connect_src(r) == (
        "'self' http://box-a:4445 ws://box-a:4445 "
        "https://ts.example.net wss://ts.example.net "
        "ws://dev.example:4445 wss://dev.example:4445")


def test_the_state_fragment_is_unioned_not_replaced(tmp_path, monkeypatch):
    """The /state path keeps working, the cookie ADDS to it, and an origin in
    both appears once."""
    app = _make_app(tmp_path, monkeypatch, hosts=[
        {"id": "h1", "label": "a", "url": "http://box-a:4445/",
         "token": "SECRET-TOKEN"},
        {"id": "h2", "label": "c", "url": "http://box-c:4445/",
         "token": "SECRET-TOKEN"}])
    # box-a is in BOTH (once in the result), box-c only in /state, box-b only
    # in the cookie -- so neither source can be dropped and still pass.
    r = _get(app, cookie="http://box-a:4445|http://box-b:4445")
    assert _connect_src(r) == (
        "'self' http://box-a:4445 ws://box-a:4445 "
        "http://box-c:4445 ws://box-c:4445 "
        "http://box-b:4445 ws://box-b:4445 "
        "ws://dev.example:4445 wss://dev.example:4445")


def test_two_browsers_get_their_own_policies(tmp_path, monkeypatch):
    app = _make_app(tmp_path, monkeypatch)
    a = _connect_src(_get(app, cookie="http://box-a:4445"))
    b = _connect_src(_get(app, cookie="http://box-b:4445"))
    assert "box-a" in a and "box-b" not in a
    assert "box-b" in b and "box-a" not in b
    # And the cache does not pin the first answer to the second browser.
    assert _connect_src(_get(app, cookie="http://box-a:4445")) == a


def test_the_response_varies_on_cookie(tmp_path, monkeypatch):
    """The header is per-browser now, so an intermediary cache must not serve
    one browser's policy to another. Unconditional -- including on the
    cookie-less response, which is the one that would otherwise be stored and
    replayed."""
    app = _make_app(tmp_path, monkeypatch)
    for cookie in (None, "http://box-a:4445"):
        r = _get(app, cookie=cookie)
        vary = {v.strip().lower()
                for v in (r.headers.get("Vary") or "").split(",")}
        assert "cookie" in vary


def test_an_existing_vary_is_extended_not_clobbered(tmp_path, monkeypatch):
    """A route that already answers with its own Vary keeps it -- the
    middleware adds to the header, it never overwrites it."""
    app = _make_app(tmp_path, monkeypatch)
    r = _get(app, path="/mod-store")
    vary = {v.strip().lower()
            for v in (r.headers.get("Vary") or "").split(",")}
    assert "cookie" in vary


# ---- the hostile cookie ----------------------------------------------------

HOSTILE = [
    ("injected directive", "http://ok:1;script-src 'unsafe-inline'"),
    ("bare directive", "; script-src 'unsafe-inline'"),
    ("keyword source", "'unsafe-inline'"),
    ("data scheme", "data:"),
    ("wildcard", "*"),
    ("scheme source", "https:"),
    ("embedded newline", "http://ok:1\nX-Evil: 1"),
    ("carriage return", "http://a\r\nSet-Cookie: x=1"),
    ("userinfo", "http://user:pw@box-a:4445"),
    ("javascript url", "javascript:alert(1)"),
    ("over-long", "http://" + ("a" * 4000) + ":4445"),
    ("500 origins", "|".join(f"http://h{i}:4445" for i in range(500))),
    ("junk", "not a url at all"),
]


@pytest.mark.parametrize("name,cookie", HOSTILE, ids=[h[0] for h in HOSTILE])
def test_a_hostile_cookie_can_widen_connect_src_and_nothing_else(
        tmp_path, monkeypatch, name, cookie):
    """The whole assembled header is compared against the no-cookie one: every
    directive except connect-src is byte-identical, connect-src differs only
    by well-formed ``origin ws-twin`` pairs, and the enforced header never
    moves at all."""
    app = _make_app(tmp_path, monkeypatch)
    if "\r" in cookie or "\n" in cookie:
        # No HTTP client will put a bare CR/LF on the wire (httpx refuses, and
        # so does every proxy), so this shape is exercised at the assembler
        # instead of over the socket -- which is where the guarantee has to
        # hold anyway, since urlsplit silently STRIPS \r \n \t.
        base_ro = _assemble_full_csp(app, "dev.example:4445", None)
        hostile_ro = _assemble_full_csp(app, "dev.example:4445", cookie)
    else:
        base_r = _get(app)
        hostile_r = _get(app, cookie=cookie)
        assert hostile_r.headers.get(ENFORCED) == HEADLESS_ENFORCED
        assert "X-Evil" not in hostile_r.headers
        assert "Set-Cookie" not in hostile_r.headers
        base_ro = base_r.headers[REPORT_ONLY]
        hostile_ro = hostile_r.headers[REPORT_ONLY]
    assert "\r" not in hostile_ro and "\n" not in hostile_ro
    b = _directives(base_ro)
    h = _directives(hostile_ro)
    assert set(b) == set(h), "a cookie introduced or removed a directive"
    for directive in b:
        if directive == "connect-src":
            continue
        assert b[directive] == h[directive], f"a cookie moved {directive}"
    added = [t for t in h["connect-src"].split(" ")
             if t not in b["connect-src"].split(" ")]
    for tok in added:
        assert tok.startswith(("http://", "https://", "ws://", "wss://")), tok
        assert "'" not in tok and ";" not in tok and "*" not in tok
        assert "@" not in tok
        assert tok.strip() == tok
    assert "unsafe-inline" not in h["connect-src"]
    assert "data:" not in h["connect-src"]


def test_500_origins_are_capped(tmp_path, monkeypatch):
    app = _make_app(tmp_path, monkeypatch)
    cookie = "|".join(f"http://h{i}.example:4445" for i in range(500))
    cs = _connect_src(_get(app, cookie=cookie))
    assert len(cs) < 5000
    assert "h0.example" not in cs, "the over-cap cookie was parsed anyway"


def test_more_origins_than_the_cap_stop_at_the_cap(tmp_path, monkeypatch):
    """150 SHORT origins -- small enough that the whole-cookie length cap does
    not fire, so this pins the COUNT cap on its own."""
    app = _make_app(tmp_path, monkeypatch)
    cookie = "|".join(f"http://h{i}:1" for i in range(150))
    assert len(cookie) < 2048
    cs = _connect_src(_get(app, cookie=cookie))
    assert "http://h0:1" in cs
    assert "http://h64:1" not in cs
    assert "http://h149:1" not in cs


def test_a_giant_cookie_is_ignored_whole(tmp_path, monkeypatch):
    app = _make_app(tmp_path, monkeypatch)
    cookie = "|".join(f"http://h{i}.example:4445" for i in range(200))
    assert len(cookie) > 2048
    assert _connect_src(_get(app, cookie=cookie)) == \
        "'self' ws://dev.example:4445 wss://dev.example:4445"


def test_the_script_src_is_byte_identical_under_a_cookie(tmp_path,
                                                         monkeypatch):
    """#190 says script-src does not move. Not "does not move much"."""
    app = _make_app(tmp_path, monkeypatch)
    base = _directives(_get(app).headers[REPORT_ONLY])["script-src"]
    for cookie in ("http://box-a:4445", "'unsafe-inline'", "data:"):
        r = _get(app, cookie=cookie)
        assert _directives(r.headers[REPORT_ONLY])["script-src"] == base
        assert r.headers.get(ENFORCED) == HEADLESS_ENFORCED


def test_a_skipped_cookie_entry_never_logs_its_value(tmp_path, monkeypatch,
                                                     caplog):
    """The skip lines identify entries positionally -- a cookie value can carry
    credentials just like a stored URL."""
    app = _make_app(tmp_path, monkeypatch)
    with caplog.at_level(logging.WARNING, logger="webterm.broker.app"):
        _get(app, cookie="http://user:hunter2@box-a:4445")
    joined = " ".join(rec.getMessage() for rec in caplog.records)
    assert "hunter2" not in joined
    assert "cookie#0" in joined


# ---- the client half: the shipped cookie writer, run in node ---------------

def _cookie_writer_source():
    """getHosts + the OD8 cookie writer, VERBATIM out of the shipped 56 --
    sliced, not copied, so this stops proving anything the moment the shipped
    writer changes."""
    src = (BROKER_DIR / "56_js_hosts.js").read_text(encoding="utf-8")
    a = src.index("        function getHosts() {")
    b = src.index("        function allHosts()", a)
    c = src.index("        const CSP_HOSTS_COOKIE =")
    d = src.index("        syncHostOriginsCookie();", c)
    body = src[a:b] + src[c:d] + "        syncHostOriginsCookie();\n"
    assert "document.cookie" in body
    return body


def _harness_for(hosts, protocol="http:", extra="") -> str:
    return (
        "'use strict';\n"
        "let prefs = " + json.dumps({"_hosts": hosts}) + ";\n"
        "const writes = [];\n"
        "const document = { set cookie(v) { writes.push(v); } };\n"
        "const window = { location: { protocol: "
        + json.dumps(protocol) + " } };\n"
        + _cookie_writer_source()
        + extra
        + "process.stdout.write(\n"
          "    JSON.stringify({ writes: writes }) + '\\n');\n")


def _run(tmp_path, hosts, protocol="http:", extra=""):
    path = tmp_path / "harness.js"
    path.write_text(_harness_for(hosts, protocol, extra), encoding="utf-8")
    proc = subprocess.run([NODE, str(path)], capture_output=True, text=True,
                          timeout=120)
    assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"
    return json.loads(proc.stdout.strip().splitlines()[-1])


def _value(write):
    return write.split(";")[0].split("=", 1)[1]


REMOTE = [{"id": "r1", "label": "a", "url": "http://box-a:4445/desk",
           "token": "SECRET-TOKEN"},
          {"id": "r2", "label": "b", "url": "https://ts.example.net:443/",
           "token": "OTHER-TOKEN"}]


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_the_page_writes_its_origins_once_at_load(tmp_path):
    r = _run(tmp_path, REMOTE)
    assert len(r["writes"]) == 1
    w = r["writes"][0]
    assert w.startswith("bl_csp_hosts=")
    # Origins ONLY: no path, no default port, no token, no label.
    assert _value(w) == "http://box-a:4445|https://ts.example.net"
    assert "SECRET-TOKEN" not in w and "OTHER-TOKEN" not in w
    assert "/desk" not in w


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_credentials_in_a_stored_url_never_leave_the_browser(tmp_path):
    r = _run(tmp_path, [{"id": "r1", "label": "a", "token": "",
                         "url": "http://user:hunter2@box-a:4445"}])
    assert "hunter2" not in json.dumps(r["writes"])
    assert _value(r["writes"][0]) == ""


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_junk_and_non_http_entries_are_skipped_individually(tmp_path):
    r = _run(tmp_path, [
        {"id": "r1", "label": "a", "token": "", "url": "not a url"},
        {"id": "r2", "label": "b", "token": "", "url": "ftp://box-x:21"},
        {"id": "r3", "label": "c", "token": "", "url": "http://good:4445"},
        {"id": "r4", "label": "d", "token": "", "url": "javascript:alert(1)"},
    ])
    assert _value(r["writes"][0]) == "http://good:4445"


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_secure_is_set_on_https_and_not_on_http(tmp_path):
    """A Secure cookie on the plain-http dev broker is silently never sent --
    which reads exactly like the feature not working."""
    http_w = _run(tmp_path, REMOTE, protocol="http:")["writes"][0]
    https_w = _run(tmp_path, REMOTE, protocol="https:")["writes"][0]
    assert "; Secure" not in http_w
    assert "; Secure" in https_w
    for w in (http_w, https_w):
        assert "; Path=/" in w
        assert "; SameSite=Strict" in w


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_a_repeat_call_writes_nothing_when_the_registry_did_not_move(tmp_path):
    r = _run(tmp_path, REMOTE,
             extra="syncHostOriginsCookie();syncHostOriginsCookie();\n")
    assert len(r["writes"]) == 1


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_a_change_rewrites_the_cookie(tmp_path):
    r = _run(tmp_path, REMOTE, extra=(
        "prefs._hosts.push({id:'r3',label:'c',token:'',"
        "url:'http://box-c:4445'});\nsyncHostOriginsCookie();\n"))
    assert len(r["writes"]) == 2
    assert "box-c" in _value(r["writes"][1])


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_the_cookie_is_capped_in_count_and_length(tmp_path):
    hosts = [{"id": f"r{i}", "label": "x", "token": "",
              "url": f"http://host-number-{i}.example.internal:4445"}
             for i in range(200)]
    w = _run(tmp_path, hosts)["writes"][0]
    value = _value(w)
    assert len(value) <= 1024
    assert len(w) < 4096, "a cookie this big is silently dropped by browsers"
    # Whole origins only -- a truncated origin would be a different host.
    for origin in value.split("|"):
        assert origin.endswith(":4445"), origin


def test_the_registry_repaint_refreshes_the_cookie():
    """The writer is only useful if something calls it after a registry edit.
    renderHostsList (83) is the funnel every add/edit/remove repaint goes
    through -- a source pin, because the alternative is hooking each writer."""
    src = (BROKER_DIR / "83_js_broker_identity.js").read_text(encoding="utf-8")
    a = src.index("        function renderHostsList() {")
    b = src.index("hostsListEl.textContent = '';", a)
    assert "syncHostOriginsCookie();" in src[a:b]


def test_a_state_change_is_not_hidden_by_the_per_cookie_cache(tmp_path,
                                                              monkeypatch):
    """The union is cached per cookie value -- so a /state PUT that moves the
    server-side half must drop that cache, or the browser that asked first
    keeps a policy from before the change forever."""
    app = _make_app(tmp_path, monkeypatch)
    cookie = "http://box-b:4445"
    assert "box-b" in _connect_src(_get(app, cookie=cookie))
    client = authed(app)
    _, r = client.put("/state", json={
        "baseRev": 0, "clientId": "me", "layout": {},
        "settings": {"_hosts": [{"id": "h1", "label": "a",
                                 "url": "http://box-a:4445/", "token": "T"}]},
    })
    assert r.status == 200
    cs = _connect_src(_get(app, cookie=cookie))
    assert "http://box-a:4445" in cs and "http://box-b:4445" in cs
