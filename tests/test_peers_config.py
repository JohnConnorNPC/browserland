"""#252 phase 1, atom 1: the peer list a broker relays to.

* ``peers.parse_peers`` is the one validation: http/https only, a host and an
  optional port, no userinfo / path / query / fragment, not this broker, a
  boolean ``mcp`` that defaults to false, no unknown keys. The origin comes
  back rebuilt (lower-case, default port dropped).
* ``__main__`` refuses a bad list like a bad role: one stderr line, exit 2,
  before the worker binds; the one-shots still run. ``create_app`` raises the
  same line as a ValueError for a direct caller.
* ``GET /info`` publishes ``peers`` as ``{id: origin}`` (never the ``mcp``
  opt-in, never a token) and ``peer_proxy``; the boot log names the ids, plus
  ONE warning line when the ``proxy`` extra is missing.
"""

from __future__ import annotations

import json
import logging
import sys
import types

import pytest

from .auth_helpers import TEST_TOKEN, authed
from .test_broker_roles import _assert_refused, _run_broker
from webterm.broker import peers
from webterm.broker.app import (DESKTOP_RPC_TIMEOUT_S, MAX_MCP_WAIT_MS,
                                create_app)

_seq = 0


def _app(tmp_path, port=4445, **cfg):
    global _seq
    _seq += 1
    base = {"state_path": str(tmp_path / "webterm_state.json"),
            "auth_token": TEST_TOKEN}
    base.update(cfg)
    return create_app(base, port=port, name=f"webterm-peers-cfg-{_seq}")


# ---- normalize / parse ------------------------------------------------------

@pytest.mark.parametrize("raw,origin", [
    ("http://b:4445", "http://b:4445"),
    ("HTTP://B.Example:80/", "http://b.example"),
    ("https://b.example:443", "https://b.example"),
    ("https://b.example:8443", "https://b.example:8443"),
    ("http://100.64.0.2:4445/", "http://100.64.0.2:4445"),
    ("http://[FD7A::1]:4445", "http://[fd7a::1]:4445"),
])
def test_origin_is_rebuilt(raw, origin):
    assert peers.normalize_origin(raw) == (origin, None)


REFUSED = [
    ("scheme", "ftp://b:21", "origin must be http or https"),
    ("ws scheme", "ws://b:4445", "origin must be http or https"),
    ("no scheme", "b:4445", "origin must be http or https"),
    ("path", "http://b:4445/x", "origin must not carry a path"),
    ("peer path", "http://b:4445/peer/c", "origin must not carry a path"),
    ("query", "http://b:4445?q=1", "origin must not carry a query"),
    ("empty query", "http://b:4445/?", "origin must not carry a query"),
    ("fragment", "http://b:4445#f", "origin must not carry a fragment"),
    ("userinfo", "http://u:p@b:4445", "origin must not carry userinfo"),
    ("user only", "http://u@b:4445", "origin must not carry userinfo"),
    ("bad port", "http://b:99999", "origin has an invalid port"),
    ("port zero", "http://b:0", "origin has an invalid port"),
    ("no host", "http://:4445", "origin must name a host"),
    ("whitespace", "http://b :4445", "origin must not contain whitespace"),
    ("backslash", "http://b\\@c", "origin must not contain whitespace"),
    ("not a string", 4445, "origin must be a non-empty string"),
    ("missing", None, "origin must be a non-empty string"),
]


@pytest.mark.parametrize("label,raw,why", REFUSED, ids=[r[0] for r in REFUSED])
def test_origin_refusals(label, raw, why):
    origin, err = peers.normalize_origin(raw)
    assert origin is None
    assert err.startswith(why), err


ENTRY_REFUSED = [
    ("not an object", ["b"], "error: peers must be an object"),
    ("bad id slash", {"a/b": {"origin": "http://b:1"}}, "error: peer id 'a/b'"),
    ("bad id empty", {"": {"origin": "http://b:1"}}, "error: peer id ''"),
    ("entry not object", {"b": "http://b:1"}, "error: peer 'b' must be an "),
    ("unknown key", {"b": {"origin": "http://b:1", "orgin": "x"}},
     "error: peer 'b' has unknown key 'orgin'"),
    ("token key", {"b": {"origin": "http://b:1", "token": "x"}},
     "error: peer 'b' has unknown key 'token'"),
    ("mcp string", {"b": {"origin": "http://b:1", "mcp": "yes"}},
     "error: peer 'b': mcp must be true or false"),
    ("bad origin", {"b": {"origin": "ftp://b:1"}},
     "error: peer 'b': origin must be http or https"),
    ("self loopback", {"b": {"origin": "http://127.0.0.1:4445"}},
     "error: peer 'b': origin http://127.0.0.1:4445 is this broker"),
    ("self localhost", {"b": {"origin": "http://LOCALHOST:4445/"}},
     "error: peer 'b': origin http://localhost:4445 is this broker"),
    ("self bind host", {"b": {"origin": "http://10.0.0.5:4445"}},
     "error: peer 'b': origin http://10.0.0.5:4445 is this broker"),
    ("self ipv6", {"b": {"origin": "http://[::1]:4445"}},
     "error: peer 'b': origin http://[::1]:4445 is this broker"),
]


@pytest.mark.parametrize("label,raw,err", ENTRY_REFUSED,
                         ids=[r[0] for r in ENTRY_REFUSED])
def test_parse_refusals(label, raw, err):
    out, why = peers.parse_peers(raw, 4445, ("10.0.0.5",))
    assert out == {}
    assert why.startswith(err), why


def test_parse_accepts_and_defaults_mcp_off():
    out, why = peers.parse_peers(
        {"b": {"origin": "HTTP://B:4445/"},
         "c-2": {"origin": "https://c.example", "mcp": True},
         # loopback on ANOTHER port is another broker, not this one
         "d_3": {"origin": "http://127.0.0.1:4448"}},
        4445, ("127.0.0.1",))
    assert why is None
    assert out == {"b": {"origin": "http://b:4445", "mcp": False},
                   "c-2": {"origin": "https://c.example", "mcp": True},
                   "d_3": {"origin": "http://127.0.0.1:4448", "mcp": False}}


def test_absent_is_no_peers():
    assert peers.parse_peers(None, 4445) == ({}, None)
    assert peers.parse_peers({}, 4445) == ({}, None)


def test_timeouts_outlast_the_longest_deliberate_hold():
    # R7: a proxied /mcp/read wait (15 s) and a desktop relay call (10 s)
    # must not read as an upstream timeout.
    assert peers.PEER_READ_TIMEOUT_S > MAX_MCP_WAIT_MS / 1000
    assert peers.PEER_READ_TIMEOUT_S > DESKTOP_RPC_TIMEOUT_S
    # S2: a down peer frees its slot before the page's 3 s fetch deadline.
    assert peers.PEER_CONNECT_TIMEOUT_S < 3.0
    assert peers.PEER_MAX_DIALS_PER_PEER < peers.PEER_MAX_DIALS


# ---- create_app / __main__ --------------------------------------------------

def test_create_app_refuses_a_bad_peer_list(tmp_path):
    with pytest.raises(ValueError) as exc:
        _app(tmp_path, peers={"b": {"origin": "http://b:4445/x"}})
    assert str(exc.value) == "error: peer 'b': origin must not carry a path"


def test_create_app_refuses_its_own_origin(tmp_path):
    with pytest.raises(ValueError) as exc:
        _app(tmp_path, port=4471, host="0.0.0.0",
             peers={"me": {"origin": "http://localhost:4471"}})
    assert "is this broker" in str(exc.value)


BAD_PEERS = {"b": {"origin": "http://user:pw@b:4445"}}
BAD_LINE = "error: peer 'b': origin must not carry userinfo"


def test_cli_refuses_a_bad_peer_list_before_bind(tmp_path):
    rc, _, err, port = _run_broker(tmp_path, {"auth_token": TEST_TOKEN,
                                              "peers": BAD_PEERS})
    _assert_refused(rc, err, port, BAD_LINE)
    assert "user:pw" not in err.replace(BAD_LINE, "")


def test_one_shots_still_run_on_a_bad_peer_list(tmp_path):
    rc, out, err, _ = _run_broker(tmp_path, {"auth_token": TEST_TOKEN,
                                             "peers": BAD_PEERS},
                                  "--print-token")
    assert rc == 0, err
    assert out.splitlines()[0] == TEST_TOKEN
    assert BAD_LINE not in err


# ---- /info + boot lines -----------------------------------------------------

def test_info_publishes_origins_only(tmp_path):
    app = _app(tmp_path, peers={
        "b": {"origin": "http://B:4448/", "mcp": True},
        "c": {"origin": "https://c.example"}})
    _, r = authed(app).get("/info")
    assert r.status == 200
    assert r.json["peers"] == {"b": "http://b:4448", "c": "https://c.example"}
    assert r.json["peer_proxy"] is True
    # Nothing about the entry but its origin: no opt-in flag, no credential.
    raw = json.dumps(r.json["peers"])
    assert "mcp" not in raw and "token" not in raw


def test_info_without_peers(tmp_path):
    _, r = authed(_app(tmp_path)).get("/info")
    assert r.json["peers"] == {}
    assert r.json["peer_proxy"] is False


def test_info_still_needs_the_token(tmp_path):
    app = _app(tmp_path, peers={"b": {"origin": "http://b:4448"}})
    _, r = app.test_client.get("/info")
    assert r.status == 401
    assert "peers" not in r.json


def _boot_lines(caplog, tmp_path, **cfg):
    with caplog.at_level(logging.INFO, logger="webterm.broker.app"):
        app = _app(tmp_path, **cfg)
    return app, [r.getMessage() for r in caplog.records
                 if r.name == "webterm.broker.app"]


def test_boot_line_names_the_peer_ids(tmp_path, caplog):
    _, lines = _boot_lines(caplog, tmp_path, peers={
        "c": {"origin": "http://c:1"}, "b": {"origin": "http://b:1"}})
    assert "peer proxy: b, c" in lines
    assert not [ln for ln in lines if "unavailable" in ln]


@pytest.mark.parametrize("missing", ["httpx", "websockets-12"])
def test_missing_extra_is_one_line_and_no_capability(tmp_path, caplog,
                                                     monkeypatch, missing):
    # httpx is installed in the test interpreter (via mcp and sanic-testing),
    # so its absence is simulated -- which only works because peers.py
    # imports it lazily, inside a function. Q11: so is a websockets below
    # 15 (core's floor is 12; the socket half needs 15).
    if missing == "httpx":
        monkeypatch.setitem(sys.modules, "httpx", None)
    else:
        old = types.ModuleType("websockets")
        old.__version__ = "12.1"
        monkeypatch.setitem(sys.modules, "websockets", old)
    app, lines = _boot_lines(caplog, tmp_path, peers={
        "b": {"origin": "http://b:1"}, "c": {"origin": "http://c:1"}})
    warned = [ln for ln in lines if ln.startswith("peer proxy unavailable")]
    assert warned == ["peer proxy unavailable: install webterm[proxy] "
                      "(httpx, websockets>=15); /peer/* answers 503 "
                      "proxy_unavailable"]
    assert app.ctx.peer_proxy is False


def test_no_peers_no_peer_lines(tmp_path, caplog, monkeypatch):
    monkeypatch.setitem(sys.modules, "httpx", None)
    _, lines = _boot_lines(caplog, tmp_path)
    assert not [ln for ln in lines if ln.startswith("peer proxy")]
