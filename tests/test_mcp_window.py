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
