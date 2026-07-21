"""#142: a token is required on every surface, on every interface, always.

Two halves:

* **Token bootstrap** — ``auth.resolve_or_mint_token`` precedence, the O_EXCL
  mint, sidecar reuse across a restart, and concurrent-mint convergence.
* **Route policy** (added with the gate collapse) — every route outside the
  deliberately-public ``{"/", "/help-corpus.json"}`` refuses an unauthenticated
  request, enumerated off the live router so a route added later cannot quietly
  ship unauthenticated.

Every app factory here passes an EXPLICIT ``auth_token`` and every temp sidecar
lives under ``tmp_path``: a test that minted into the repo root would drop a
real secret next to the source tree.
"""

from __future__ import annotations

import json
import os
import re
from concurrent.futures import ThreadPoolExecutor

import pytest

from webterm.broker import auth
from webterm.broker.app import create_app


@pytest.fixture(autouse=True)
def _no_env_token(monkeypatch):
    """$WEB_TERMINAL_TOKEN wins over everything, so a developer running the
    suite with one exported would silently pass every precedence test."""
    monkeypatch.delenv(auth.TOKEN_ENV, raising=False)


# ---- precedence ---------------------------------------------------------

def test_env_wins_over_config_and_file(tmp_path, monkeypatch):
    path = tmp_path / auth.AUTH_STATE_FILENAME
    path.write_text(json.dumps({"auth_token": "from-file"}), encoding="utf-8")
    monkeypatch.setenv(auth.TOKEN_ENV, "from-env")
    assert auth.resolve_or_mint_token({"auth_token": "from-config"}, path) == (
        "from-env", "env")


def test_config_wins_over_file(tmp_path):
    path = tmp_path / auth.AUTH_STATE_FILENAME
    path.write_text(json.dumps({"auth_token": "from-file"}), encoding="utf-8")
    assert auth.resolve_or_mint_token({"auth_token": "from-config"}, path) == (
        "from-config", "config")


def test_file_used_when_nothing_configured(tmp_path):
    path = tmp_path / auth.AUTH_STATE_FILENAME
    path.write_text(json.dumps({"auth_token": "from-file"}), encoding="utf-8")
    assert auth.resolve_or_mint_token({"auth_token": None}, path) == (
        "from-file", "file")


# ---- minting ------------------------------------------------------------

def test_mints_and_persists_when_nothing_configured(tmp_path):
    path = tmp_path / auth.AUTH_STATE_FILENAME
    token, source = auth.resolve_or_mint_token({}, path)
    assert source == "minted"
    assert token and len(token) >= 32
    assert json.loads(path.read_text(encoding="utf-8"))["auth_token"] == token


@pytest.mark.skipif(os.name == "nt", reason="Windows has no POSIX file mode")
def test_minted_sidecar_is_0600(tmp_path):
    path = tmp_path / auth.AUTH_STATE_FILENAME
    auth.resolve_or_mint_token({}, path)
    assert (path.stat().st_mode & 0o777) == 0o600


def test_restart_reuses_the_minted_token(tmp_path):
    path = tmp_path / auth.AUTH_STATE_FILENAME
    first, first_source = auth.resolve_or_mint_token({}, path)
    second, second_source = auth.resolve_or_mint_token({}, path)
    assert first_source == "minted"
    assert (second, second_source) == (first, "file")


def test_concurrent_mint_converges_on_one_value(tmp_path):
    """Two brokers racing on the same state dir must end up on ONE token —
    the O_EXCL loser re-reads and adopts the winner. A last-writer-wins atomic
    replace would leave the loser running a token that is not the one on disk."""
    path = tmp_path / auth.AUTH_STATE_FILENAME
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(
            lambda _: auth.resolve_or_mint_token({}, path), range(8)))
    tokens = {tok for tok, _ in results}
    assert len(tokens) == 1
    on_disk = json.loads(path.read_text(encoding="utf-8"))["auth_token"]
    assert tokens == {on_disk}
    # Exactly one caller may claim it minted; the rest adopted.
    assert [src for _, src in results].count("minted") <= 1


def test_corrupt_sidecar_is_repaired_not_fatal(tmp_path):
    path = tmp_path / auth.AUTH_STATE_FILENAME
    path.write_text("{not json", encoding="utf-8")
    token, source = auth.resolve_or_mint_token({}, path)
    assert source == "minted"
    assert json.loads(path.read_text(encoding="utf-8"))["auth_token"] == token


def test_unwritable_dir_yields_an_ephemeral_token(tmp_path):
    """A read-only state dir must not stop the broker booting — but the token
    is process-local, so the caller has to warn that it changes on restart and
    that --print-token cannot recover it."""
    path = tmp_path / "no-such-dir" / auth.AUTH_STATE_FILENAME
    token, source = auth.resolve_or_mint_token({}, path)
    assert source == "ephemeral"
    assert token
    assert not path.exists()


# ---- read-only resolution (--print-token) --------------------------------

def test_resolve_existing_never_mints(tmp_path):
    path = tmp_path / auth.AUTH_STATE_FILENAME
    assert auth.resolve_existing_token({}, path) is None
    assert not path.exists()


def test_resolve_existing_reads_each_source(tmp_path, monkeypatch):
    path = tmp_path / auth.AUTH_STATE_FILENAME
    path.write_text(json.dumps({"auth_token": "from-file"}), encoding="utf-8")
    assert auth.resolve_existing_token({}, path) == "from-file"
    assert auth.resolve_existing_token({"auth_token": "cfg"}, path) == "cfg"
    monkeypatch.setenv(auth.TOKEN_ENV, "env")
    assert auth.resolve_existing_token({"auth_token": "cfg"}, path) == "env"


# ---- route policy -------------------------------------------------------
#
# Every request below deliberately uses the RAW test client: the entire point is
# that an UNAUTHENTICATED request is refused, so tests/auth_helpers.authed()
# would append the very token that has to be missing.

TOKEN = "route-policy-token"

#: The ONLY routes that answer unauthenticated, and why:
#:
#: ``/``                  the token is typed INTO this page (the login overlay).
#:                        Auth is query/header-only with no cookies, so gating
#:                        the document deadlocks the bootstrap: every reload,
#:                        bookmark and new tab would 401 forever. Headless, it
#:                        is ``{"ui": false}`` — which is also what health
#:                        probes hit.
#: ``/help-corpus.json``  wiki-derived static help text, served alongside the
#:                        page. Neither response carries host- or
#:                        session-derived data.
#:
#: OPTIONS preflights are also public — they carry no credentials by design and
#: route resolution happens before request middleware, so they must be their own
#: routes. They are filtered out below rather than listed here.
PUBLIC_PATHS = {"/", "/help-corpus.json"}

_seq = 0


def _make_app(tmp_path, name_hint="policy"):
    global _seq
    _seq += 1
    return create_app(
        {"auth_token": TOKEN,
         "state_path": str(tmp_path / "webterm_state.json")},
        name=f"webterm-{name_hint}-{_seq}")


def _concrete(path: str) -> str:
    """A router path with its parameters filled in. ``/mod-store/<modId:str>``
    hits the real handler only as ``/mod-store/something``; left templated it
    would 404 and the assertion would pass for the wrong reason."""
    return re.sub(r"<[^>]+>", "probe", path)


def _http_routes(app):
    """(path, method) for every non-OPTIONS, non-WebSocket route."""
    out = set()
    for route in app.router.routes:
        if getattr(route.extra, "websocket", False):
            continue
        path = "/" + route.path.lstrip("/")
        for method in set(route.methods or ()) - {"OPTIONS"}:
            out.add((path, method))
    return sorted(out)


def test_every_http_route_refuses_an_unauthenticated_request(tmp_path):
    """Enumerated from the LIVE router so a route added later cannot quietly
    ship unauthenticated.

    Browser-realm routes must answer exactly ``401 auth_required`` — not merely
    "not 2xx". A bare not-2xx assertion would pass on a 404 from a mistyped or
    parametric path, i.e. for a route that is actually wide open."""
    app = _make_app(tmp_path)
    routes = _http_routes(app)
    assert len(routes) > 40, f"router looks empty: {routes}"
    checked = 0
    for path, method in routes:
        if path in PUBLIC_PATHS:
            continue
        _, r = app.test_client.request(_concrete(path), http_method=method)
        if path.startswith("/mcp/"):
            # Its own realm: separate mcp_token, Authorization: Bearer, and
            # default-OFF — so it answers 403 mcp_disabled here, not 401.
            assert not (200 <= r.status < 300), \
                f"{method} {path} answered {r.status} unauthenticated"
            continue
        assert r.status == 401, \
            f"{method} {path} answered {r.status}, expected 401"
        assert r.json and r.json.get("error") == "auth_required", \
            f"{method} {path} 401'd with the wrong body: {r.json}"
        checked += 1
    assert checked > 40, f"only {checked} browser-realm routes checked"


def test_the_public_pair_answers_without_a_token(tmp_path):
    """The bootstrap carve-out, asserted rather than assumed: gating these would
    401 every reload, bookmark and new tab forever, since the token is typed
    into the very page being refused."""
    app = _make_app(tmp_path)
    _, page = app.test_client.get("/")
    assert page.status == 200
    assert "<title>Browserland</title>" in page.body.decode("utf-8")
    _, corpus = app.test_client.get("/help-corpus.json")
    assert corpus.status == 200


def test_headless_root_still_answers_for_health_probes(tmp_path):
    """Headless installs are documented as unaffected by #142 — GET / keeps
    answering 200 {"ui": false} with no token, so existing probes keep working."""
    global _seq
    _seq += 1
    app = create_app(
        {"auth_token": TOKEN, "serve_ui": False,
         "state_path": str(tmp_path / "webterm_state.json")},
        name=f"webterm-policy-headless-{_seq}")
    _, r = app.test_client.get("/")
    assert r.status == 200
    assert r.json == {"ui": False}


def test_loopback_is_not_an_exemption(tmp_path):
    """The heart of #142. The test client dials 127.0.0.1, so every request in
    this file already arrives on loopback — these are the endpoints where the
    old policy let that through: host-wide file read/write and shell spawn."""
    app = _make_app(tmp_path)
    for path, body in (("/launch", {"profile": "cmd"}),
                       ("/file/read", {"path": "x"}),
                       ("/file/write", {"path": "x", "content": "y"}),
                       ("/state", None)):
        _, r = app.test_client.post(path, json=body) if body is not None \
            else app.test_client.get(path)
        assert r.status == 401, f"{path} from loopback answered {r.status}"
        assert r.json.get("error") == "auth_required", path


def test_a_valid_token_gets_through(tmp_path):
    """The negative tests above would also pass if every route were broken."""
    app = _make_app(tmp_path)
    _, r = app.test_client.get(f"/sessions?token={TOKEN}")
    assert r.status == 200
    assert isinstance(r.json, list)
    _, r = app.test_client.get("/info", headers={
        "Authorization": f"Bearer {TOKEN}"})      # header form works too
    assert r.status == 200
