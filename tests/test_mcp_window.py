"""The #243 launch-handle ownership model: the store's ``owner`` field (this
section) and the unified /mcp/launch + POST /mcp/window that ride it (later
atoms). STORE UNIT cells drive ``McpWindowStore`` on its own with an injected
wall clock, the test_mcp_scope template.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from webterm.broker.mcp_windows import McpWindowStore

T = 2_000_000_000.0            # an epoch-shaped wall clock reading
_HANDLE = "h" * 43             # a token_urlsafe-shaped launch handle
_OWNER = hashlib.sha256(_HANDLE.encode()).hexdigest()


def _write_sidecar(path: Path, windows) -> None:
    path.write_text(json.dumps({"windows": {str(k): v
                                            for k, v in windows.items()}}),
                    encoding="utf-8")


def _loaded(tmp_path, windows, now=T):
    """A store loaded from a sidecar holding ``windows`` with the wall clock
    pinned at ``now``."""
    path = tmp_path / "webterm_mcp_windows.json"
    _write_sidecar(path, windows)
    return McpWindowStore.load(path, clock=lambda: now)


def _row(scope=None, mode=None, pid=None, host=None, seen=T, owner=None,
         admin_set=None):
    return {"scope": scope, "mode": mode, "pid": pid, "host": host,
            "owner": owner, "admin_set": admin_set, "seen": seen}


# ---- B1: the owner field -----------------------------------------------------


def test_owner_validated_64_hex_or_none(tmp_path):
    """#243: owner is None or a 64-char lowercase hex sha256 digest — nothing
    else loads from a sidecar and nothing else passes set() (the plaintext
    handle must never be storable by accident)."""
    bad = ["not-hex", "ABC" * 21, "f" * 63, "f" * 65, 7, True]
    raw = {str(i): _row(scope="a", owner=v) for i, v in enumerate(bad)}
    store = _loaded(tmp_path, raw)
    assert len(store) == 0          # every malformed row dropped

    store = _loaded(tmp_path, {"5": _row(scope="a", owner=_OWNER)})
    assert store.get(5)["owner"] == _OWNER

    with pytest.raises(ValueError):
        store.set(5, scope="a", owner="plaintext-handle", now=T)
    # A valid set() through a copy works (set() runs inside the shared
    # writer; here the copy IS the working store).
    work = store.copy()
    assert work.set(5, owner=_OWNER, pid=1, host="h", now=T + 1) is True


def test_set_carries_owner_in_one_call_and_replace_drops_it(tmp_path):
    """#243: owner rides the ONE set() discipline — a merging write keeps an
    owner it is not told about; a REPLACING write (different producer, or
    pid 0) drops it to None unless the call passes it, so a reused id never
    inherits a stale owner."""
    store = _loaded(tmp_path, {"5": _row(scope="a", mode="read", pid=1,
                                         host="h", owner=_OWNER)})
    # merge (same producer): an owner-silent write keeps the hash
    work = store.copy()
    assert work.set(5, mode="readwrite", pid=1, host="h", now=T + 1) is True
    assert work.get(5)["owner"] == _OWNER
    # replace (different producer): the owner is gone
    work = store.copy()
    assert work.set(5, mode="readwrite", pid=2, host="h", now=T + 1) is True
    assert work.get(5)["owner"] is None
    # replace that passes the owner keeps it (one call, all facts)
    work = store.copy()
    assert work.set(5, mode="off", owner=_OWNER, pid=2, host="h",
                    now=T + 1) is True
    assert work.get(5)["owner"] == _OWNER


def test_parse_row_accepts_old_sidecars_without_owner(tmp_path):
    """#243: rows written before this change carry neither owner nor
    admin_set; they load untouched (absent reads as None), so an upgrade
    loses nothing."""
    path = tmp_path / "webterm_mcp_windows.json"
    path.write_text(json.dumps({"windows": {
        "5": {"scope": "a", "mode": "read", "pid": 1, "host": "h",
              "seen": T}}}), encoding="utf-8")
    store = McpWindowStore.load(path, clock=lambda: T)
    assert store.get(5) == _row(scope="a", mode="read", pid=1, host="h")


def test_noop_compare_includes_owner(tmp_path):
    """#243: a write whose ONLY change is clearing (or stamping) the owner is
    a real change — it must return True and reach the disk, never be folded
    into a no-op."""
    store = _loaded(tmp_path, {"5": _row(scope="a", pid=1, host="h",
                                         owner=_OWNER)})
    work = store.copy()
    assert work.set(5, owner=None, pid=1, host="h", now=T + 1) is True
    assert work.get(5)["owner"] is None

    work = store.copy()
    assert work.set(5, scope="a", pid=1, host="h", owner=_OWNER,
                    now=T + 1) is False      # nothing changed
    assert work.get(5)["seen"] == T          # no-op leaves seen alone


def test_owner_keeps_row_alive_when_scope_and_mode_cleared(tmp_path):
    """#243: an owner clearing BOTH overrides (scope null, mode null) keeps
    its row and handle — deleting the row would revoke the handle as an
    unannounced side effect of a "clear override" edit. A row with none of
    the three still dies."""
    store = _loaded(tmp_path, {"5": _row(scope="a", mode="read", pid=1,
                                         host="h", owner=_OWNER)})
    work = store.copy()
    assert work.set(5, scope=None, mode=None, pid=1, host="h",
                    now=T + 1) is True
    assert work.get(5) == _row(pid=1, host="h", owner=_OWNER, seen=T + 1)

    # and with the owner cleared too, nothing is left: the row goes
    work = store.copy()
    assert work.set(5, scope=None, mode=None, owner=None, pid=1, host="h",
                    now=T + 2) is True
    assert work.get(5) is None

    # an owner-only row is valid on LOAD as well (not "malformed")
    store2 = _loaded(tmp_path, {"6": _row(pid=1, host="h", owner=_OWNER)})
    assert store2.get(6)["owner"] == _OWNER


# ---- B2: every /mcp/launch mints a handle ------------------------------------
# The app template and fakes mirror tests/test_mcp_scope.py's; the fake
# launcher mimics the REAL launcher's contract that a raising pre_spawn hook
# surfaces as LaunchError(500, "prespawn_failed").

import asyncio  # noqa: E402

import webterm.broker.app as app_mod  # noqa: E402
from webterm.broker.launcher import LaunchError  # noqa: E402
from webterm.broker.registry import WindowEntry  # noqa: E402

from .auth_helpers import TEST_TOKEN, authed  # noqa: E402

MCP_TOKEN = "scope-store-token"
LAUNCHED = (1 << 52) | 77
_app_seq = 0


def _make_app(tmp_path, monkeypatch, **extra):
    global _app_seq
    _app_seq += 1
    monkeypatch.delenv("WEB_TERMINAL_TOKEN", raising=False)
    monkeypatch.delenv("WEB_TERMINAL_MCP_TOKEN", raising=False)
    cfg = {
        "state_path": str(tmp_path / "webterm_state.json"),
        "mcp_state_path": str(tmp_path / "webterm_mcp.json"),
        "mcp_windows_path": str(tmp_path / "webterm_mcp_windows.json"),
        "auth_token": TEST_TOKEN,
        "mcp_enabled": True,
        "mcp_token": MCP_TOKEN,
        "mcp_default_mode": "off",
        "mcp_allow_launch": True,
    }
    cfg.update(extra)
    app = app_mod.create_app(cfg, name=f"webterm-mcp-window-{_app_seq}")
    app.ctx.mcp_windows.clock = lambda: T
    app.ctx.mcp_windows_uptime = lambda: 0.0
    return app


def _windows_path(tmp_path) -> Path:
    return tmp_path / "webterm_mcp_windows.json"


def _disk_windows(tmp_path):
    return json.loads(_windows_path(tmp_path).read_text(
        encoding="utf-8"))["windows"]


class _WS:
    """Producer WS double: register() only needs send/close."""

    async def send(self, payload):
        pass

    async def close(self, *a, **k):
        pass


def _hello(wid, pid, host="hostA"):
    return {"type": "hello", "window_id": wid, "pid": pid, "title": "t",
            "cols": 80, "rows": 24, "host": host, "kind": "agent"}


class _FakeLauncher:
    """Awaits pre_spawn (the route's own hook), mimicking the real
    launcher's prespawn contract on a raising hook, then registers a hello
    and answers 200. ``hook_first`` lets a cell claim the row inside the
    hook (the mid-write race)."""

    def __init__(self, app, *, pid=4242, hook_claim=None):
        self.app, self.pid = app, pid
        self.hook_claim = hook_claim
        self.calls = []
        self.row_at_spawn = None

    async def launch(self, profile, **kwargs):
        self.calls.append(dict(kwargs, profile=profile))
        pre_spawn = kwargs.get("pre_spawn")
        if pre_spawn is not None:
            try:
                await pre_spawn(LAUNCHED)
            except Exception:
                if self.hook_claim is not None:
                    await self.hook_claim()
                raise LaunchError(500, "prespawn_failed")
        path = self.app.ctx.mcp_windows_path
        if path.exists():
            self.row_at_spawn = json.loads(
                path.read_text(encoding="utf-8"))["windows"].get(
                    str(LAUNCHED))
        await self.app.ctx.registry.register(_WS(), _hello(LAUNCHED,
                                                           self.pid))
        return 200, {"ok": True, "id": LAUNCHED, "registered": True,
                     "agent_pid": self.pid}


def _launch(app, **kw):
    fake = _FakeLauncher(app, **kw)
    app.ctx.launcher = fake
    return fake


def _mcp(app, path, body=None, scope=None):
    headers = [("Authorization", f"Bearer {MCP_TOKEN}")]
    if scope is not None:
        headers.append(("X-Browserland-Scope", scope))
    return app.test_client.post(path, json=body if body is not None else {},
                                headers=headers)


def _live(app, wid, pid, host="hostA"):
    ws = _WS()
    entry = WindowEntry(wid, pid, "t", 80, 24, ws, host=host, kind="agent")
    app.ctx.registry._entries[wid] = entry
    return entry


def test_unscoped_launch_with_mode_registers_in_that_mode(tmp_path,
                                                           monkeypatch):
    """#243: a body mode is honored on an unscoped launch too — the window
    registers in it (the row claims it via the first hello)."""
    app = _make_app(tmp_path, monkeypatch)
    fake = _launch(app)
    _, resp = _mcp(app, "/mcp/launch", {"mode": "read"})
    assert resp.status == 200
    assert resp.json["mode"] == "read"
    entry = app.ctx.registry.get(LAUNCHED)
    assert (entry.mcp_scope, entry.mcp_mode) == (None, "read")
    assert fake.row_at_spawn["mode"] == "read"


def test_unscoped_launch_no_mode_defaults_readwrite(tmp_path, monkeypatch):
    """#243 (owner decision, changelog line): with no body mode an unscoped
    launch comes up READWRITE, matching scoped launches — the old wire
    inherited default_mode (off here), leaving the agent a window it could
    not see."""
    app = _make_app(tmp_path, monkeypatch)
    _launch(app)
    _, resp = _mcp(app, "/mcp/launch", {})
    assert (resp.json["scope"], resp.json["mode"]) == (None, "readwrite")
    entry = app.ctx.registry.get(LAUNCHED)
    assert (entry.mcp_scope, entry.mcp_mode) == (None, "readwrite")


def test_launch_response_carries_handle_sidecar_has_only_sha256(
        tmp_path, monkeypatch):
    """#243: the response carries the plaintext handle; the sidecar row
    carries only its sha256 hexdigest — the file can never leak a credential
    a later reader could replay."""
    app = _make_app(tmp_path, monkeypatch)
    fake = _launch(app)
    _, resp = _mcp(app, "/mcp/launch", {"mode": "read"})
    handle = resp.json["handle"]
    assert isinstance(handle, str) and len(handle) >= 40
    owner = hashlib.sha256(handle.encode()).hexdigest()
    row = fake.row_at_spawn
    assert row["owner"] == owner
    assert handle not in _windows_path(tmp_path).read_text(encoding="utf-8")
    assert _disk_windows(tmp_path)[str(LAUNCHED)]["owner"] == owner


def test_scoped_launch_row_carries_owner_hash(tmp_path, monkeypatch):
    """#243 (owner decision 3): EVERY /mcp/launch, scoped included, mints a
    handle; the scoped pre-spawn row carries the owner hash beside the
    scope."""
    app = _make_app(tmp_path, monkeypatch)
    fake = _launch(app)
    _, resp = _mcp(app, "/mcp/launch", {}, scope="teamA")
    owner = hashlib.sha256(resp.json["handle"].encode()).hexdigest()
    assert fake.row_at_spawn == _row(scope="teamA", mode="readwrite",
                                     owner=owner, seen=T)


def test_launch_rollback_removes_owner_with_row(tmp_path, monkeypatch):
    """#243: a LaunchError after the pre-spawn row landed but before any
    hello rolls the row back (owner included) while it is still
    unclaimed."""
    app = _make_app(tmp_path, monkeypatch)

    class _FailBeforeHello(_FakeLauncher):
        async def launch(self, profile, **kwargs):
            pre_spawn = kwargs.get("pre_spawn")
            if pre_spawn is not None:
                await pre_spawn(LAUNCHED)
            raise LaunchError(500, "agent_exited_early", returncode=3)

    app.ctx.launcher = _FailBeforeHello(app)
    _, resp = _mcp(app, "/mcp/launch", {})
    assert (resp.status, resp.json["error"]) == (500, "agent_exited_early")
    assert str(LAUNCHED) not in _disk_windows(tmp_path)
    assert app.ctx.mcp_windows.get(LAUNCHED) is None


def test_launch_dead_row_collision_deleted_and_retried_once(tmp_path,
                                                             monkeypatch):
    """#243 [c4]: a collided row whose producer is gone (no live entry
    passes same_producer) no longer refuses the launch — ONE writer pass
    deletes that row alone and the launch is retried once. The route-level
    pin (this cell runs on a fake launcher that reuses the id, so the retry
    writes only because the delete landed first): two attempts, and the
    store's row afterwards is the NEW launch's. The fresh-id half lives in
    test_mcp_scope's real-launcher cell."""
    stale = _row(scope="old", mode="read", pid=55, host="hostA",
                 seen=T - 8 * 86400)
    _write_sidecar(_windows_path(tmp_path), {str(LAUNCHED): stale})
    app = _make_app(tmp_path, monkeypatch)
    fake = _launch(app)
    _, resp = _mcp(app, "/mcp/launch", {"mode": "read"})
    assert resp.status == 200
    assert len(fake.calls) == 2
    owner = hashlib.sha256(resp.json["handle"].encode()).hexdigest()
    row = _disk_windows(tmp_path)[str(LAUNCHED)]
    assert row["owner"] == owner and row["scope"] is None
    assert row["mode"] == "read"


def test_launch_claimable_row_still_refuses_prespawn_failed(tmp_path,
                                                             monkeypatch):
    """#243: the collided row IS claimable — its producer is live on the id —
    so the dead-row path declines: prespawn_failed, nothing spawned, the row
    exactly as it was, and only ONE launch attempt."""
    stale = _row(scope="old", mode="read", pid=55, host="hostA",
                 seen=T - 8 * 86400)
    _write_sidecar(_windows_path(tmp_path), {str(LAUNCHED): stale})
    app = _make_app(tmp_path, monkeypatch)
    _live(app, LAUNCHED, 55)          # the old producer is live on the id
    fake = _launch(app)
    _, resp = _mcp(app, "/mcp/launch", {"mode": "read"})
    assert (resp.status, resp.json) == (500, {"ok": False,
                                              "error": "prespawn_failed"})
    assert len(fake.calls) == 1
    # seen is EXCLUDED: the stop-flush's refresh_seen legitimately re-stamps
    # a live producer's row on every request lifecycle (#229). Everything
    # that matters — the row still existing with its original facts — is
    # pinned field by field.
    after = dict(_disk_windows(tmp_path)[str(LAUNCHED)])
    seen = after.pop("seen")
    assert seen >= stale["seen"]
    assert after == {k: v for k, v in stale.items() if k != "seen"}


# ---- B3: POST /mcp/window -----------------------------------------------------

from webterm.protocol import MCP_ADMIN_HEADER  # noqa: E402

ADMIN_TOKEN = "admin-token-0123456789abcdef"


def _make_admin_app(tmp_path, monkeypatch):
    return _make_app(tmp_path, monkeypatch, mcp_admin_token=ADMIN_TOKEN)


def _launch_for_handle(app, *, scope=None):
    """A completed launch returning (handle, owner-hash)."""
    _launch(app)
    headers = [("Authorization", f"Bearer {MCP_TOKEN}")]
    if scope is not None:
        headers.append(("X-Browserland-Scope", scope))
    _, resp = app.test_client.post("/mcp/launch", json={},
                                   headers=headers)
    assert resp.status == 200
    handle = resp.json["handle"]
    return handle, hashlib.sha256(handle.encode()).hexdigest()


def _window(app, body, *, admin=None, token=MCP_TOKEN):
    headers = [("Authorization", f"Bearer {token}")]
    if admin is not None:
        headers.append((MCP_ADMIN_HEADER, admin))
    return app.test_client.post("/mcp/window", json=body, headers=headers)


def test_window_route_sets_scope_and_mode_via_handle(tmp_path, monkeypatch):
    """#243: the owner's edit writes BOTH fields through the one shared
    writer and re-applies onto the live entry."""
    app = _make_app(tmp_path, monkeypatch)
    handle, owner = _launch_for_handle(app)
    _, resp = _window(app, {"id": LAUNCHED, "handle": handle,
                            "scope": "teamB", "mode": "read"})
    assert (resp.status, resp.json) == (200, {
        "ok": True, "id": LAUNCHED, "scope": "teamB", "mode": "read"})
    entry = app.ctx.registry.get(LAUNCHED)
    assert (entry.mcp_scope, entry.mcp_mode) == ("teamB", "read")
    row = app.ctx.mcp_windows.get(LAUNCHED)
    assert (row["scope"], row["mode"], row["owner"]) == ("teamB", "read",
                                                         owner)


def test_owner_sets_off_then_back_and_foreign_scope_then_back(
        tmp_path, monkeypatch):
    """#243: the handle lookup BYPASSES the scope/off filter — an owner that
    sets its window off, or into a scope it does not hold, can undo it in
    the next call."""
    app = _make_app(tmp_path, monkeypatch)
    handle, _owner = _launch_for_handle(app)
    entry = app.ctx.registry.get(LAUNCHED)

    _, resp = _window(app, {"id": LAUNCHED, "handle": handle,
                            "mode": "off"})
    assert resp.status == 200
    assert (entry.mcp_mode, entry.mcp_scope) == ("off", None)

    _, resp = _window(app, {"id": LAUNCHED, "handle": handle,
                            "mode": "readwrite"})
    assert resp.status == 200
    assert entry.mcp_mode == "readwrite"

    _, resp = _window(app, {"id": LAUNCHED, "handle": handle,
                            "scope": "theirs"})
    assert resp.status == 200
    assert entry.mcp_scope == "theirs"

    _, resp = _window(app, {"id": LAUNCHED, "handle": handle,
                            "scope": None})
    assert resp.status == 200
    assert entry.mcp_scope is None


def test_window_wrong_handle_missing_handle_unknown_id_unowned_row_identical_404_bytes(
        tmp_path, monkeypatch):
    """#243: a wrong handle, a missing handle (non-admin), an unknown id and
    an unowned row all answer the ONE 404, byte for byte, so the route
    cannot probe which ids exist."""
    app = _make_app(tmp_path, monkeypatch)
    handle, _owner = _launch_for_handle(app)
    # an unowned row: a plain /session/mcp-written window
    asyncio.run(app.ctx.persist_mcp_windows(
        lambda work: work.set(8, scope="a", mode="read", pid=8,
                              host="hostA", now=work.clock())))
    _, wrong = _window(app, {"id": LAUNCHED, "handle": "not-the-handle",
                             "mode": "off"})
    _, missing = _window(app, {"id": LAUNCHED, "mode": "off"})
    _, unknown = _window(app, {"id": 424242, "handle": handle,
                               "mode": "off"})
    _, unowned = _window(app, {"id": 8, "handle": handle, "mode": "off"})
    assert wrong.status == missing.status == unknown.status \
        == unowned.status == 404
    assert wrong.json == {"error": "unknown_or_off"}
    assert wrong.body == missing.body == unknown.body == unowned.body
    # nothing was written: the launched row still carries its owner
    assert app.ctx.mcp_windows.get(LAUNCHED)["mode"] == "readwrite"


def test_window_bad_mode_bad_scope_400_no_write(tmp_path, monkeypatch):
    """#243: validation runs before any write — a bad mode or a bad scope is
    a 400 (the /session/mcp order: neither key, then mode, then scope) and
    the store is untouched."""
    app = _make_app(tmp_path, monkeypatch)
    handle, _owner = _launch_for_handle(app)
    row_before = app.ctx.mcp_windows.get(LAUNCHED)
    _, neither = _window(app, {"id": LAUNCHED, "handle": handle})
    _, bad_mode = _window(app, {"id": LAUNCHED, "handle": handle,
                                "mode": "sometimes"})
    _, bad_scope = _window(app, {"id": LAUNCHED, "handle": handle,
                                 "scope": "a:b"})
    for resp, code in ((neither, "bad_mode"), (bad_mode, "bad_mode"),
                       (bad_scope, "bad_scope")):
        assert (resp.status, resp.json["error"]) == (400, code)
    assert app.ctx.mcp_windows.get(LAUNCHED) == row_before


def test_window_absent_key_leaves_explicit_null_clears(tmp_path,
                                                        monkeypatch):
    """#243: an absent key leaves the field alone; an explicit null clears it
    (mode null = inherit), exactly as /session/mcp reads its body."""
    app = _make_app(tmp_path, monkeypatch)
    handle, _owner = _launch_for_handle(app, scope="teamA")
    _, resp = _window(app, {"id": LAUNCHED, "handle": handle,
                            "mode": "read"})
    assert resp.json["scope"] == "teamA"      # scope left alone
    _, resp = _window(app, {"id": LAUNCHED, "handle": handle, "mode": None})
    assert resp.json["mode"] is None          # inherit again
    _, resp = _window(app, {"id": LAUNCHED, "handle": handle,
                            "scope": None})
    assert resp.json["scope"] is None         # untagged again


def test_window_change_persists_restart_and_same_producer_reconnect(
        tmp_path, monkeypatch):
    """#243: the change is durable — a fresh broker on the same sidecar
    re-applies it when the same producer reconnects."""
    app = _make_app(tmp_path, monkeypatch)
    handle, _owner = _launch_for_handle(app, scope="teamA")
    _, resp = _window(app, {"id": LAUNCHED, "handle": handle,
                            "scope": "moved", "mode": "read"})
    assert resp.status == 200

    again = _make_app(tmp_path, monkeypatch)
    entry = asyncio.run(again.ctx.registry.register(
        _WS(), _hello(LAUNCHED, 4242)))
    assert (entry.mcp_scope, entry.mcp_mode) == ("moved", "read")


def test_session_mcp_edit_clears_owner_then_handle_404(tmp_path,
                                                        monkeypatch):
    """#243: the browser wins — a /session/mcp edit clears the owner, after
    which the old handle answers the shared 404."""
    app = _make_app(tmp_path, monkeypatch)
    handle, _owner = _launch_for_handle(app)
    _, resp = app.test_client.post(
        "/session/mcp", json={"id": LAUNCHED, "mode": "read"},
        params={"token": app.ctx.auth_token})
    assert resp.status == 200
    assert app.ctx.mcp_windows.get(LAUNCHED)["owner"] is None
    _, resp = _window(app, {"id": LAUNCHED, "handle": handle,
                            "mode": "off"})
    assert (resp.status, resp.json["error"]) == (404, "unknown_or_off")


def test_recycled_id_different_producer_handle_404(tmp_path, monkeypatch):
    """#243: the id was reused by a different producer — the old handle no
    longer names its window, and the answer is the shared 404 (never a
    write to the new producer's row)."""
    app = _make_app(tmp_path, monkeypatch)
    handle, _owner = _launch_for_handle(app)
    row_before = app.ctx.mcp_windows.get(LAUNCHED)
    _live(app, LAUNCHED, 9999)              # a different producer on the id
    _, resp = _window(app, {"id": LAUNCHED, "handle": handle,
                            "mode": "off"})
    assert (resp.status, resp.json["error"]) == (404, "unknown_or_off")
    assert app.ctx.mcp_windows.get(LAUNCHED) == row_before


def test_prespawn_window_editable_before_first_hello(tmp_path, monkeypatch):
    """#243: the row alone suffices when nothing is live — an owner can
    retag its window before the first hello (a 202 mid-flight, or a
    pre-spawn edit), and the row keeps its pid/host wildcards."""
    app = _make_app(tmp_path, monkeypatch)
    handle = "prespawn-handle-0123456789"
    owner = hashlib.sha256(handle.encode()).hexdigest()
    _write_sidecar(_windows_path(tmp_path), {})
    asyncio.run(app.ctx.persist_mcp_windows(
        lambda work: work.set(LAUNCHED, mode="readwrite", owner=owner,
                              now=work.clock())))
    _, resp = _window(app, {"id": LAUNCHED, "handle": handle,
                            "scope": "early"})
    assert resp.status == 200
    row = app.ctx.mcp_windows.get(LAUNCHED)
    assert (row["scope"], row["pid"], row["host"]) == ("early", None, None)


def test_admin_edits_any_window_clears_owner_and_stamps_admin_set(
        tmp_path, monkeypatch):
    """#245: an admin (no handle) edits ANY window — a foreign scope, even
    one with another agent's owner — and the write displaces that owner and
    stamps the admin badge."""
    app = _make_admin_app(tmp_path, monkeypatch)
    handle, _owner = _launch_for_handle(app, scope="theirs")
    _, resp = _window(app, {"id": LAUNCHED, "scope": "mine",
                            "mode": "readwrite"}, admin=ADMIN_TOKEN)
    assert (resp.status, resp.json) == (200, {
        "ok": True, "id": LAUNCHED, "scope": "mine", "mode": "readwrite"})
    row = app.ctx.mcp_windows.get(LAUNCHED)
    assert row["owner"] is None and row["admin_set"] is True
    entry = app.ctx.registry.get(LAUNCHED)
    assert (entry.mcp_scope, entry.mcp_mode, entry.mcp_admin_launch) == (
        "mine", "readwrite", True)
    # the displaced owner's handle no longer works
    _, resp = _window(app, {"id": LAUNCHED, "handle": handle,
                            "mode": "off"})
    assert resp.status == 404


def test_admin_client_handle_edit_keeps_owner_and_admin_set(
        tmp_path, monkeypatch):
    """#245/#243 [c27]: a PRESENT, matching handle takes the owner path for
    everyone, admin included — an admin client's ordinary edit of its own
    launched window keeps the owner and does not re-stamp."""
    app = _make_admin_app(tmp_path, monkeypatch)
    handle, owner = _launch_for_handle(app, scope="teamA")
    _, resp = _window(app, {"id": LAUNCHED, "handle": handle,
                            "mode": "read"}, admin=ADMIN_TOKEN)
    assert resp.status == 200
    row = app.ctx.mcp_windows.get(LAUNCHED)
    assert row["owner"] == owner and row["admin_set"] is None
    assert app.ctx.registry.get(LAUNCHED).mcp_admin_launch is False


def test_owner_clear_concurrent_with_admin_clear_owner_wins(
        tmp_path, monkeypatch):
    """#243/#245 [c1][c2]: the authorization runs INSIDE the writer's
    critical section, so an admin's owner-clear that lands between the
    route's parse and the write turns the owner's edit into the shared 404
    instead of a stale-handle write."""
    app = _make_admin_app(tmp_path, monkeypatch)
    handle, _owner = _launch_for_handle(app)
    real_persist = app.ctx.persist_mcp_windows

    async def interleaved(mutate):
        def admin_clear(work):
            row = work.get(LAUNCHED)
            if row is not None:
                work.set(LAUNCHED, owner=None, pid=row["pid"],
                         host=row["host"], now=work.clock())
        await real_persist(admin_clear)      # the admin write lands first
        return await real_persist(mutate)    # then the owner's

    app.ctx.persist_mcp_windows = interleaved
    _, resp = _window(app, {"id": LAUNCHED, "handle": handle,
                            "mode": "off"})
    assert (resp.status, resp.json["error"]) == (404, "unknown_or_off")
    # and nothing was written by the refused owner call
    row = app.ctx.mcp_windows.get(LAUNCHED)
    assert row["mode"] == "readwrite" and row["owner"] is None


def test_admin_window_write_stamps_live_window_not_old_row(
        tmp_path, monkeypatch):
    """#245 [c7]: a recycled id under a different producer — the admin edit
    stamps the LIVE window's row (the new row carries the live pid), never
    the old row's identity."""
    app = _make_admin_app(tmp_path, monkeypatch)
    _launch_for_handle(app, scope="teamA")
    _live(app, LAUNCHED, 9999)              # a different producer took the id
    _, resp = _window(app, {"id": LAUNCHED, "scope": None, "mode": "read"},
                      admin=ADMIN_TOKEN)
    assert resp.status == 200
    row = app.ctx.mcp_windows.get(LAUNCHED)
    assert (row["pid"], row["host"], row["admin_set"]) == (9999, "hostA",
                                                           True)


def test_info_advertises_window_edit(tmp_path, monkeypatch):
    """#243: /mcp/info carries window_edit: true — the fail-closed flag a
    client checks before sending a handle."""
    app = _make_app(tmp_path, monkeypatch)
    _, resp = app.test_client.get(
        "/mcp/info", headers=[("Authorization", f"Bearer {MCP_TOKEN}")])
    assert resp.status == 200
    assert resp.json["window_edit"] is True
