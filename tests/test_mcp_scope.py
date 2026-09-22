"""MCP scopes (#237): the durable per-window store, ``webterm_mcp_windows.json``
(#228).

STORE UNIT section: ``McpWindowStore`` on its own, driven with real
``WindowEntry`` objects and an injected wall clock; no app, no loop. It pins
load's protective parsing and its single log line, set()'s raw-override and
identity rules, apply()'s row gate, claim and fallback, prune()'s grace, age,
liveness and cap rules, the explicit schema, known_scopes() and the derived
``dirty``.

CREATE_APP section: the store wired into a real broker app on the
test_mcp_pace template (unique app names, everything under tmp_path) and
driven through the one shared writer (``app.ctx.persist_mcp_windows``) and the
real ``registry.register`` hook path. Writes are observed by wrapping
``app._write_state_atomic`` and filtering on the store's own path; a blocking
variant holds the first store write open so a hello can land mid-write. Only
``app.test_client`` is used here, never ReusableClient: a ReusableClient
app's listeners were measured firing during a later test's request.

WIRE section (#230): the /mcp/* token routes honouring ``X-Browserland-Scope``
on the same app template, with live windows injected straight into the
registry (tagged by setting ``mcp_scope``) and a producer double that answers
the correlated round-trips and records every frame it was sent. It pins the
400 bad_scope refusal on every token route (an invalid name or a repeated
header), the order of the gate's checks, and the empty value's unscoped
meaning.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import math
import os
import re
import threading
import time
from pathlib import Path

import pytest

import webterm.broker.app as app_mod
import webterm.broker.mcp_windows as mw
import webterm.broker.registry as registry_mod
from webterm.broker.mcp_windows import (MAX_ROWS, MCP_MODES, PRUNE_AGE_S,
                                        PRUNE_GRACE_S, SCOPE_HEADER,
                                        McpWindowStore)
from webterm.broker.registry import WindowEntry

from .auth_helpers import TEST_TOKEN

T = 2_000_000_000.0            # an epoch-shaped wall clock reading
DAY = 86400.0
STORE_LOGGER = "webterm.broker.mcp_windows"


def _row(scope=None, mode=None, pid=None, host=None, seen=T):
    return {"scope": scope, "mode": mode, "pid": pid, "host": host,
            "seen": seen}


def _write_sidecar(path: Path, windows) -> None:
    path.write_text(json.dumps({"windows": {str(k): v
                                            for k, v in windows.items()}}),
                    encoding="utf-8")


def _loaded(tmp_path, windows, now=T):
    """A store loaded from a sidecar holding ``windows`` (so its last-persisted
    payload is exactly that file) with the wall clock pinned at ``now``."""
    path = tmp_path / "webterm_mcp_windows.json"
    _write_sidecar(path, windows)
    return McpWindowStore.load(path, clock=lambda: now)


def _entry(wid, pid, host="hostA", scope=None, mode=None):
    entry = WindowEntry(wid, pid, "t", 80, 24, None, host=host)
    entry.mcp_scope = scope
    entry.mcp_mode = mode
    return entry


def _store_records(caplog):
    return [r for r in caplog.records if r.name == STORE_LOGGER]


# -- MCP_MODES / SCOPE_RE ------------------------------------------------------

def test_mcp_modes_do_not_drift_from_the_js_menu():
    """#228: MCP_MODES moved here as the one PYTHON source; its JS twin is the
    title-bar robot menu's value/label list. Both must name the same modes in
    the same order."""
    js = (Path(mw.__file__).parent / "65_js_display_theming.js").read_text(
        encoding="utf-8")
    block = re.search(r"const MCP_MODES = \[(.*?)\];", js, re.S)
    assert block is not None
    assert tuple(re.findall(r"\['(\w+)',", block.group(1))) == MCP_MODES


@pytest.mark.parametrize("scope,ok", [
    ("teamA", True), ("a", True), ("A.b_c-9", True), ("x" * 64, True),
    ("x" * 65, False), ("", False), ("-lead", False), ("a:b", False),
    ("a b", False), ("é", False), ("a\n", False),
])
def test_scope_re_is_the_umbrella_grammar(scope, ok):
    """#228: SCOPE_RE is #237's grammar, always fullmatch'd: 1-64 chars, an
    alphanumeric first char, then [A-Za-z0-9._-]; no ':' or whitespace."""
    assert (mw.SCOPE_RE.fullmatch(scope) is not None) is ok


# -- load ----------------------------------------------------------------------

def test_load_missing_file_is_empty_and_clean(tmp_path, caplog):
    """#228: a missing sidecar is the normal first boot: an empty, clean store
    and exactly one INFO line naming the path."""
    caplog.set_level(logging.INFO, logger=STORE_LOGGER)
    path = tmp_path / "webterm_mcp_windows.json"
    store = McpWindowStore.load(path)
    assert len(store) == 0
    assert store.dirty is False
    recs = _store_records(caplog)
    assert len(recs) == 1
    assert recs[0].levelno == logging.INFO
    assert str(path) in recs[0].getMessage()
    assert "(0 rows)" in recs[0].getMessage()


@pytest.mark.parametrize("raw", [
    b"{not json", b"[]", b'{"windows": []}', b'{"other": {}}', b"\xff\xfe",
    b"[" * 50_000 + b"]" * 50_000,
], ids=["bad-json", "top-level-list", "windows-list", "no-windows",
        "not-utf8", "deep-nesting"])
def test_load_unreadable_or_wrong_schema_boots_empty(tmp_path, caplog, raw):
    """#228: a corrupt or wrong-schema sidecar boots an EMPTY store with exactly
    one WARNING naming the path and the consequence. It is clean (the empty
    payload), so nothing rewrites the file until a real change does. A deeply
    nested value makes json raise RecursionError, which must not escape."""
    caplog.set_level(logging.INFO, logger=STORE_LOGGER)
    path = tmp_path / "webterm_mcp_windows.json"
    path.write_bytes(raw)
    store = McpWindowStore.load(path)
    assert len(store) == 0
    assert store.dirty is False
    recs = _store_records(caplog)
    assert len(recs) == 1
    assert recs[0].levelno == logging.WARNING
    msg = recs[0].getMessage()
    assert str(path) in msg
    assert "starting with no per-window rows" in msg
    assert "next change replaces the file" in msg


def test_load_caps_the_bytes_it_reads(tmp_path, caplog):
    """#228: load() reads at most MAX_SIDECAR_BYTES, capped BEFORE the parse.
    A VALID sidecar padded one byte past the cap boots empty with one WARNING
    naming the path; the same file padded to exactly the cap loads."""
    assert mw.MAX_SIDECAR_BYTES == 8 * 2**20
    caplog.set_level(logging.INFO, logger=STORE_LOGGER)
    path = tmp_path / "webterm_mcp_windows.json"
    body = json.dumps({"windows": {"5": _row(scope="a", pid=1,
                                             host="h")}}).encode("utf-8")
    path.write_bytes(body + b" " * (mw.MAX_SIDECAR_BYTES + 1 - len(body)))
    over = McpWindowStore.load(path)
    recs = _store_records(caplog)
    assert len(over) == 0
    assert [r.levelno for r in recs] == [logging.WARNING]
    assert str(path) in recs[0].getMessage()
    assert "is over" in recs[0].getMessage()
    path.write_bytes(body + b" " * (mw.MAX_SIDECAR_BYTES - len(body)))
    assert McpWindowStore.load(path).get(5) is not None


def test_load_drops_malformed_rows_and_keeps_valid_ones(tmp_path, caplog):
    """#228: each malformed row is dropped on its own; a valid sibling loads.
    One WARNING carries both counts and says the dropped rows are DELETED from
    disk by the next write. bool is rejected for pid and seen explicitly
    (isinstance(True, int) is True); a key must be exactly str(int)."""
    caplog.set_level(logging.INFO, logger=STORE_LOGGER)
    good = _row(scope="teamA", mode="read", pid=77, host="hostA", seen=T)
    store = _loaded(tmp_path, {
        "1": good,
        "2": _row(mode="admin", pid=1, host="h"),          # bad mode
        "3": _row(scope="a:b", pid=1, host="h"),           # scope grammar
        "4": _row(scope="s", pid=True, host="h"),          # bool pid
        "07": _row(scope="s", pid=1, host="h"),            # non-canonical key
        "8": dict(_row(scope="s", pid=1, host="h"), seen="x"),
        "9": dict(_row(scope="s", pid=1, host="h"), seen=True),
        "10": _row(pid=1, host="h"),                       # carries nothing
        "11": "not a row",
        "12": _row(scope="s", pid=1, host=5),              # host not a str
    })
    assert store.get(1) == good
    assert len(store) == 1
    assert store.dirty is False          # memory == the normalised file
    recs = _store_records(caplog)
    assert len(recs) == 1
    assert recs[0].levelno == logging.WARNING
    msg = recs[0].getMessage()
    assert "9 malformed rows (1 kept)" in msg
    assert "DELETED from disk by the next write" in msg


def test_load_logs_the_row_count_like_the_mod_store(tmp_path, caplog):
    """#228: a good file logs one INFO shaped like create_app's
    ``mod store: <path> (<n> mods)`` line."""
    caplog.set_level(logging.INFO, logger=STORE_LOGGER)
    _loaded(tmp_path, {"5": _row(scope="a", pid=1, host="h")})
    recs = _store_records(caplog)
    assert [r.levelno for r in recs] == [logging.INFO]
    assert re.fullmatch(r"mcp windows: .+ \(1 row\)", recs[0].getMessage())


def test_load_never_prunes(tmp_path):
    """#228: an 8-day-old non-live row survives load: its window may simply not
    have reconnected yet."""
    store = _loaded(tmp_path, {"5": _row(scope="a", pid=1, host="h",
                                         seen=T - 8 * DAY)})
    assert store.get(5) is not None


# -- get / to_persist / dirty --------------------------------------------------

def test_get_returns_a_copy(tmp_path):
    """#228: editing get()'s dict changes neither the store nor its payload."""
    store = _loaded(tmp_path, {"5": _row(scope="a", pid=1, host="h")})
    before = store.to_persist()
    got = store.get(5)
    got["scope"] = "hijacked"
    got["pid"] = 999
    assert store.get(5)["scope"] == "a"
    assert store.to_persist() == before


def test_to_persist_is_the_explicit_schema(tmp_path):
    """#228: exactly {"windows": {"<wid>": five fields}}, ids in int order, and
    nothing else a row carries in memory reaches the payload."""
    store = _loaded(tmp_path, {"10": _row(scope="a", pid=1, host="h"),
                               "9": _row(mode="off", pid=2, host="h")})
    store._rows[10]["internal"] = "must not persist"
    payload = store.to_persist()
    assert list(payload) == ["windows"]
    assert list(payload["windows"]) == ["9", "10"]
    for row in payload["windows"].values():
        assert list(row) == ["scope", "mode", "pid", "host", "seen"]


def test_dirty_is_derived_from_the_payload(tmp_path):
    """#228: ``dirty`` compares memory's payload with the last one written or
    loaded; no flag. A hello's seen bump dirties it, committing that payload
    cleans it."""
    store = _loaded(tmp_path, {"5": _row(scope="a", pid=77, host="hostA",
                                         seen=T - DAY)})
    assert store.dirty is False
    store.apply(_entry(5, 77), None)
    assert store.dirty is True
    work = store.copy()
    store.commit(work, work.to_persist())
    assert store.dirty is False


def test_known_scopes_is_a_sorted_deduplicated_union(tmp_path):
    """#228: the scopes of the live windows plus the scopes of every row,
    sorted, each once, None excluded. Seven names, so an unsorted set would
    come out sorted by chance about once in 5040 runs."""
    store = _loaded(tmp_path, {
        "1": _row(scope="mid", pid=1, host="h"),
        "2": _row(scope="alpha", pid=2, host="h"),
        "3": _row(mode="read", pid=3, host="h"),            # no scope
        "4": _row(scope="kappa", pid=4, host="h"),
        "5": _row(scope="beta", pid=5, host="h"),
    })
    live = [_entry(20, 1, scope="zeta"), _entry(21, 2), _entry(22, 3,
                                                               scope="alpha"),
            _entry(23, 4, scope="omega"), _entry(24, 5, scope="delta")]
    assert store.known_scopes(live) == ["alpha", "beta", "delta", "kappa",
                                        "mid", "omega", "zeta"]


def test_apply_and_known_scopes_never_prune(tmp_path):
    """#228: prune runs only inside the shared writer. An 8-day-old non-live
    row survives an apply() (of another window) and a known_scopes()."""
    store = _loaded(tmp_path, {"5": _row(scope="a", pid=1, host="h",
                                         seen=T - 8 * DAY),
                               "6": _row(scope="b", pid=77, host="hostA")})
    store.apply(_entry(6, 77), None)
    assert store.known_scopes([]) == ["a", "b"]
    assert store.get(5) is not None


# -- set -----------------------------------------------------------------------

def test_set_stores_the_raw_override():
    """#228: the row keeps the RAW mode. A scope-only write leaves mode null,
    mode=None stays null, and a later scope change leaves the mode alone."""
    store = McpWindowStore(clock=lambda: T)
    assert store.set(5, scope="teamA", pid=77, host="h", now=T) is True
    assert store.get(5)["mode"] is None
    assert store.set(5, mode=None, pid=77, host="h", now=T + 1) is False
    assert store.get(5)["mode"] is None
    assert store.set(5, mode="read", pid=77, host="h", now=T + 2) is True
    assert store.set(5, scope="teamB", pid=77, host="h", now=T + 3) is True
    assert store.get(5) == _row(scope="teamB", mode="read", pid=77, host="h",
                                seen=T + 3)


def test_set_bumps_seen_only_on_a_real_change():
    """#228: a change sets seen = now; a no-op returns False and leaves the
    row byte-identical, seen included, so it gives a write nothing to do."""
    store = McpWindowStore()
    assert store.set(5, scope="a", mode="off", pid=77, host="h",
                     now=100.0) is True
    before = store.get(5)
    assert before["seen"] == 100.0
    assert store.set(5, scope="a", mode="off", pid=77, host="h",
                     now=200.0) is False
    assert store.get(5) == before


def test_set_replaces_a_row_written_for_another_producer():
    """#228: a write for a different producer on a reused id REPLACES the row;
    the stale producer's mode is not inherited."""
    store = McpWindowStore()
    store.set(5, scope="teamA", mode="readwrite", pid=77, host="h", now=T)
    assert store.set(5, scope="teamB", pid=78, host="h", now=T + 1) is True
    assert store.get(5) == _row(scope="teamB", mode=None, pid=78, host="h",
                                seen=T + 1)


def test_set_never_merges_on_an_unknown_pid_zero():
    """#228: only a NULL pid is a wildcard. 0 is unknown (as in same_producer),
    so a write for a pid-0 producer on a row another pid-0 producer left
    REPLACES it: the recycled id does not inherit the old readwrite."""
    store = McpWindowStore()
    store.set(5, scope="teamA", mode="readwrite", pid=0, host="h", now=T)
    assert store.set(5, scope="teamB", pid=0, host="h", now=T + 1) is True
    assert store.get(5) == _row(scope="teamB", mode=None, pid=0, host="h",
                                seen=T + 1)


def test_set_merges_into_a_pre_spawn_row_and_records_the_identity():
    """#228: a pre-spawn row (null pid/host) is compatible with any producer:
    the next write merges into it and records the identity passed."""
    store = McpWindowStore()
    store.set(5, scope="teamA", now=T)
    assert store.get(5) == _row(scope="teamA", seen=T)
    assert store.set(5, mode="read", pid=77, host="h", now=T + 1) is True
    assert store.get(5) == _row(scope="teamA", mode="read", pid=77, host="h",
                                seen=T + 1)


def test_set_with_no_override_leaves_no_row():
    """#228: a row with neither scope nor mode carries nothing and would only
    block apply()'s fallback, so it is never written and an existing one is
    DELETED, whatever its pid/host."""
    store = McpWindowStore()
    assert store.set(5, pid=77, host="h", now=T) is False
    assert store.get(5) is None
    store.set(5, scope="a", mode="read", pid=77, host="h", now=T)
    assert store.set(5, scope=None, mode=None, pid=77, host="h",
                     now=T + 1) is True
    assert store.get(5) is None
    store.set(6, scope="a", pid=77, host="h", now=T)
    assert store.set(6, scope=None, pid=99, host="other", now=T + 1) is True
    assert store.get(6) is None


@pytest.mark.parametrize("kwargs", [
    {"wid": True, "scope": "a"}, {"wid": "5", "scope": "a"},
    {"wid": 5, "scope": "a b"}, {"wid": 5, "scope": ""},
    {"wid": 5, "mode": "admin"}, {"wid": 5, "scope": "a", "pid": True},
    {"wid": 5, "scope": "a", "host": 5}, {"wid": 5, "scope": "a",
                                          "now": math.nan},
], ids=["bool-id", "str-id", "scope-space", "scope-empty", "bad-mode",
        "bool-pid", "int-host", "nan-now"])
def test_set_rejects_bad_arguments(kwargs):
    """#228: callers validate first, so a bad argument is a programming error:
    ValueError, and the store is untouched."""
    store = McpWindowStore()
    kwargs = dict(kwargs)
    wid = kwargs.pop("wid")
    kwargs.setdefault("now", T)
    with pytest.raises(ValueError):
        store.set(wid, **kwargs)
    assert len(store) == 0


# -- apply / reapply -----------------------------------------------------------

def test_apply_reapplies_a_matching_row(tmp_path):
    """#228: a row whose pid AND host match the hello wins: the entry gets the
    scope and the RAW mode, and the row's seen is bumped to the store clock."""
    store = _loaded(tmp_path, {"5": _row(scope="teamA", mode="readwrite",
                                         pid=77, host="hostA",
                                         seen=T - DAY)}, now=T)
    entry = _entry(5, 77, host="hostA")
    store.apply(entry, None)
    assert (entry.mcp_scope, entry.mcp_mode) == ("teamA", "readwrite")
    assert store.get(5)["seen"] == T
    assert store.dirty is True


@pytest.mark.parametrize("row_pid,row_host,hello_pid,hello_host", [
    (77, "hostA", 78, "hostA"),
    (77, "hostA", 77, "hostB"),
    (0, "hostA", 0, "hostA"),
], ids=["pid-mismatch", "same-pid-other-host", "pid-zero"])
def test_apply_does_not_reapply_to_another_producer(tmp_path, row_pid,
                                                    row_host, hello_pid,
                                                    hello_host):
    """#228: id reuse. A different pid, the same pid from another host, or an
    unknown pid (0 on both sides) is not the row's producer: the entry keeps
    what it had (seeded, so a miss that CLEARED it would show) and the row is
    untouched (seen included)."""
    original = _row(scope="teamA", mode="readwrite", pid=row_pid,
                    host=row_host, seen=T - DAY)
    store = _loaded(tmp_path, {"5": original})
    entry = _entry(5, hello_pid, host=hello_host, scope="keep", mode="off")
    store.apply(entry, None)
    assert (entry.mcp_scope, entry.mcp_mode) == ("keep", "off")
    assert store.get(5) == original
    assert store.dirty is False


def test_apply_claims_a_pre_spawn_row_once(tmp_path):
    """#228: a pre-spawn row (null pid and host) is claimed by the first hello:
    it applies and the row records that producer, so a later hello from a
    different pid on the same id no longer matches."""
    store = _loaded(tmp_path, {"5": _row(scope="teamA", seen=T - 10)})
    first = _entry(5, 55, host="hostA")
    store.apply(first, None)
    assert first.mcp_scope == "teamA"
    assert store.get(5) == _row(scope="teamA", pid=55, host="hostA", seen=T)
    other = _entry(5, 56, host="hostA")
    store.apply(other, None)
    assert other.mcp_scope is None


def test_a_pid_zero_hello_cannot_claim_a_pre_spawn_row(tmp_path):
    """#228: 0 is an unknown pid and never matches, not even a wildcard: the
    gate fills the null pid from the hello and the shared gate rejects 0."""
    store = _loaded(tmp_path, {"5": _row(scope="teamA", seen=T - 10)})
    entry = _entry(5, 0, host="hostA")
    store.apply(entry, None)
    assert entry.mcp_scope is None
    assert store.get(5) == _row(scope="teamA", seen=T - 10)


def test_apply_falls_back_to_the_old_entry_under_the_gate():
    """#228: with no row the facts carry over from the replaced entry, only
    when it is the same producer (with the hook installed this is the broker's
    only carry-over)."""
    store = McpWindowStore(clock=lambda: T)
    old = _entry(5, 77, scope="teamA", mode="readwrite")
    same = _entry(5, 77)
    store.apply(same, old)
    assert (same.mcp_scope, same.mcp_mode) == ("teamA", "readwrite")
    other = _entry(5, 78)
    store.apply(other, old)
    assert (other.mcp_scope, other.mcp_mode) == (None, None)


def test_a_matching_row_beats_the_fallback(tmp_path):
    """#228: the durable row is the authority: with both a matching row and a
    same-producer old entry, the row's facts land, not old's."""
    store = _loaded(tmp_path, {"5": _row(scope="row", mode="off", pid=77,
                                         host="hostA")})
    entry = _entry(5, 77)
    store.apply(entry, _entry(5, 77, scope="old", mode="readwrite"))
    assert (entry.mcp_scope, entry.mcp_mode) == ("row", "off")


def test_reapply_reports_whether_a_row_landed(tmp_path):
    """#228: reapply() is the resync seam writers call on registry.get(id)
    after the shared writer returns: True with the row's facts on the entry,
    False (entry untouched) when no row passes the gate."""
    store = _loaded(tmp_path, {"5": _row(scope="teamA", mode="read", pid=77,
                                         host="hostA")})
    hit = _entry(5, 77)
    assert store.reapply(hit) is True
    assert (hit.mcp_scope, hit.mcp_mode) == ("teamA", "read")
    miss = _entry(5, 78, scope="keep", mode="off")
    assert store.reapply(miss) is False
    assert (miss.mcp_scope, miss.mcp_mode) == ("keep", "off")
    assert store.reapply(_entry(6, 77)) is False


def test_apply_goes_through_reapply(tmp_path, monkeypatch):
    """#228: apply() delegates the row path to reapply() (one copy of that
    logic): a reapply spy reporting a hit suppresses the fallback, and it is
    called exactly once with the entry."""
    store = McpWindowStore(clock=lambda: T)
    calls = []

    def spy(self, entry):
        calls.append(entry)
        return True

    monkeypatch.setattr(McpWindowStore, "reapply", spy)
    entry = _entry(5, 77)
    store.apply(entry, _entry(5, 77, scope="old", mode="readwrite"))
    assert calls == [entry]
    assert (entry.mcp_scope, entry.mcp_mode) == (None, None)


# -- prune ---------------------------------------------------------------------

def test_prune_is_a_noop_inside_the_grace(tmp_path):
    """#228: under PRUNE_GRACE_S of process uptime nothing is judged stale,
    not even an 8-day-old non-live row."""
    assert PRUNE_GRACE_S == 600
    store = _loaded(tmp_path, {"5": _row(scope="a", pid=1, host="h",
                                         seen=T - 8 * DAY)})
    assert store.prune([], T, 599.9) == 0
    assert store.get(5) is not None


def test_prune_after_the_grace_drops_only_stale_non_live_rows(tmp_path):
    """#228: at the grace boundary (600 s) prune runs. Dropped: not live AND
    unseen for more than 7 days. A row is live when a live entry on its id is
    its OWN producer (same_producer) or its id is a pending launch; an
    unrelated producer on a reused id does not keep an orphan alive. Exactly 7
    days old is kept."""
    assert PRUNE_AGE_S == 7 * DAY
    old = T - 8 * DAY
    store = _loaded(tmp_path, {
        "1": _row(scope="a", pid=1, host="h", seen=old),        # stale
        "2": _row(scope="a", pid=2, host="hostA", seen=old),    # live
        "3": _row(scope="a", pid=3, host="h", seen=T - 7 * DAY),
        "4": _row(scope="a", pid=4, host="h", seen=T - 6 * DAY),
        "5": _row(scope="a", pid=5, host="hostA", seen=old),    # id reused
        "6": _row(scope="a", seen=old),                         # pending
    })
    live = [_entry(2, 2), _entry(5, 99)]
    dropped = store.prune(live, T, 600.0, is_pending=lambda wid: wid == 6)
    assert dropped == 2
    assert [store.get(w) is not None for w in (1, 2, 3, 4, 5, 6)] == [
        False, True, True, True, False, True]


def test_prune_cap_drops_the_oldest_non_live_rows_never_a_live_one(tmp_path):
    """#228: after the age rule, while more than MAX_ROWS remain the oldest
    NON-live rows go first. Here the single oldest row is live and survives;
    the two next-oldest (non-live) are the two dropped."""
    assert MAX_ROWS == 1000
    windows = {str(i): _row(scope="a", pid=i, host="h", seen=T - 1000 + i)
               for i in range(3, 1003)}
    windows["1"] = _row(scope="a", pid=1, host="hostA", seen=T - 5000)
    windows["2"] = _row(scope="a", pid=2, host="h", seen=T - 4000)
    windows["3"]["seen"] = T - 3000
    store = _loaded(tmp_path, windows)
    assert len(store) == 1002
    assert store.prune([_entry(1, 1)], T, 10_000.0) == 2
    assert len(store) == MAX_ROWS
    assert store.get(1) is not None
    assert store.get(2) is None and store.get(3) is None
    assert store.get(4) is not None


# =============================================================================
# CREATE_APP section: the store inside a real broker app
# =============================================================================

MCP_TOKEN = "scope-store-token"
_app_seq = 0


def _make_app(tmp_path, monkeypatch, *, uptime=0.0, now=T, **extra):
    """A broker app on the test_mcp_pace template with the store's wall clock
    pinned at ``now`` and its process uptime pinned at ``uptime`` (0 = just
    booted, inside prune's grace)."""
    global _app_seq
    _app_seq += 1
    # Env would override config; clear both so the cfg token/enable are honored.
    monkeypatch.delenv("WEB_TERMINAL_TOKEN", raising=False)
    monkeypatch.delenv("WEB_TERMINAL_MCP_TOKEN", raising=False)
    cfg = {
        "state_path": str(tmp_path / "webterm_state.json"),
        "mcp_state_path": str(tmp_path / "webterm_mcp.json"),
        "auth_token": TEST_TOKEN,
        "mcp_enabled": True,
        "mcp_token": MCP_TOKEN,
        "mcp_default_mode": "off",
    }
    cfg.update(extra)
    app = app_mod.create_app(cfg, name=f"webterm-mcp-scope-{_app_seq}")
    app.ctx.mcp_windows.clock = lambda: now
    app.ctx.mcp_windows_uptime = lambda: uptime
    return app


def _sidecar(tmp_path) -> Path:
    return tmp_path / "webterm_mcp_windows.json"


def _disk(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))["windows"]


def _persist(app, mutate):
    return asyncio.run(app.ctx.persist_mcp_windows(mutate))


def _hello(wid, pid, host="hostA"):
    return {"type": "hello", "window_id": wid, "pid": pid, "title": "t",
            "cols": 80, "rows": 24, "host": host, "kind": "agent"}


class _WS:
    """Producer WS double: register() only needs send/close."""

    async def send(self, payload):
        pass

    async def close(self, *a, **k):
        pass


def _register(app, wid, pid, host="hostA"):
    return asyncio.run(app.ctx.registry.register(_WS(), _hello(wid, pid,
                                                               host)))


def _inject_live(app, wid, pid, host="hostA"):
    """A live entry that did NOT go through register(), so apply() never ran
    and its row's seen is left exactly as the test wrote it."""
    entry = WindowEntry(wid, pid, "t", 80, 24, _WS(), host=host)
    app.ctx.registry._entries[wid] = entry
    return entry


@pytest.fixture
def writes(monkeypatch):
    """Every _write_state_atomic call as ``(path, payload)``, delegating to the
    real one. Callers filter on the store's path (see _store_writes)."""
    calls = []
    real = app_mod._write_state_atomic

    def counting(path, payload):
        calls.append((Path(path), payload))
        return real(path, payload)

    monkeypatch.setattr(app_mod, "_write_state_atomic", counting)
    return calls


def _store_writes(calls, app):
    return [payload for path, payload in calls
            if path == app.ctx.mcp_windows_path]


class _Gate:
    """Holds the FIRST store write open in its executor thread until
    ``release`` is set; later writes (and other paths) pass straight
    through."""

    def __init__(self, monkeypatch, app):
        self.entered = threading.Event()
        self.release = threading.Event()
        self.calls = []
        real = app_mod._write_state_atomic
        store_path = app.ctx.mcp_windows_path

        def blocking(path, payload):
            if Path(path) == store_path:
                self.calls.append(payload)
                if len(self.calls) == 1:
                    self.entered.set()
                    self.release.wait(10)
            return real(path, payload)

        monkeypatch.setattr(app_mod, "_write_state_atomic", blocking)

    async def wait_entered(self):
        assert await asyncio.to_thread(self.entered.wait, 10)


def _mid_write(app, gate, mutate, during):
    """Run ``mutate`` through the shared writer; once its write is parked in
    the executor, run ``during()`` on the loop (awaiting it if it returns a
    coroutine: the hellos), then let the write land. Returns (mutate's result,
    during's result)."""
    async def scenario():
        task = asyncio.ensure_future(app.ctx.persist_mcp_windows(mutate))
        try:
            await gate.wait_entered()
            inner = during()
            got = await inner if asyncio.iscoroutine(inner) else inner
        finally:
            gate.release.set()
        return await task, got
    return asyncio.run(scenario())


# -- round trip / corrupt / load ----------------------------------------------

def test_round_trip_across_two_create_apps(tmp_path, monkeypatch):
    """#228: a row written through the shared writer lands in the default
    sidecar next to the state file and is back, identical, in a second
    create_app on the same tmp_path."""
    app = _make_app(tmp_path, monkeypatch)
    _persist(app, lambda w: w.set(7, scope="teamA", mode="readwrite", pid=77,
                                  host="hostA", now=T))
    assert _sidecar(tmp_path).exists()
    again = _make_app(tmp_path, monkeypatch)
    assert again.ctx.mcp_windows.get(7) == _row(
        scope="teamA", mode="readwrite", pid=77, host="hostA", seen=T)


def test_corrupt_sidecar_boots_empty_logs_and_the_next_write_replaces_it(
        tmp_path, monkeypatch, caplog):
    """#228: a corrupt sidecar never stops the broker: it boots with an empty
    store and one WARNING naming the file, and the next write replaces the
    file with valid JSON holding the new row."""
    caplog.set_level(logging.INFO, logger=STORE_LOGGER)
    _sidecar(tmp_path).write_bytes(b"{not json")
    app = _make_app(tmp_path, monkeypatch)
    warnings = [r for r in _store_records(caplog)
                if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert str(app.ctx.mcp_windows_path) in warnings[0].getMessage()
    assert len(app.ctx.mcp_windows) == 0
    _persist(app, lambda w: w.set(3, scope="s", pid=1, host="h", now=T))
    assert _disk(_sidecar(tmp_path)) == {"3": _row(scope="s", pid=1,
                                                   host="h", seen=T)}


def test_a_deeply_nested_sidecar_does_not_stop_the_broker(tmp_path,
                                                          monkeypatch,
                                                          caplog):
    """#228: deeply nested brackets make json raise RecursionError; the broker
    still boots, with an empty store and one WARNING naming the file. Depth
    50_000, not 1_000: pytest raises the recursion limit to 3000, so 1_000
    parses fine and would land in the wrong-schema branch instead. The
    "is unreadable" wording pins the branch itself."""
    caplog.set_level(logging.INFO, logger=STORE_LOGGER)
    _sidecar(tmp_path).write_bytes(b"[" * 50_000 + b"]" * 50_000)
    app = _make_app(tmp_path, monkeypatch)
    assert len(app.ctx.mcp_windows) == 0
    recs = _store_records(caplog)
    assert [r.levelno for r in recs] == [logging.WARNING]
    msg = recs[0].getMessage()
    assert str(app.ctx.mcp_windows_path) in msg
    assert "is unreadable" in msg


def test_no_prune_at_load(tmp_path, monkeypatch, writes):
    """#228: boot never prunes and never writes: an 8-day-old non-live row
    survives create_app with the file's bytes and file id (st_ino) intact,
    even past the grace. 8 days before the REAL clock (and so before T too),
    because boot runs before any test can pin the store's clock."""
    _write_sidecar(_sidecar(tmp_path), {"5": _row(
        scope="a", pid=1, host="h", seen=time.time() - 8 * DAY)})
    before = (_sidecar(tmp_path).read_bytes(),
              os.stat(_sidecar(tmp_path)).st_ino)
    app = _make_app(tmp_path, monkeypatch, uptime=10_000.0)
    assert app.ctx.mcp_windows.get(5) is not None
    assert (_sidecar(tmp_path).read_bytes(),
            os.stat(_sidecar(tmp_path)).st_ino) == before
    assert _store_writes(writes, app) == []


def test_mcp_windows_path_config_key_is_honoured(tmp_path, monkeypatch):
    """#228: ``mcp_windows_path`` overrides the default location. ABSOLUTE on
    purpose: a relative one resolves against the CWD, which under pytest is
    the repo root."""
    custom = tmp_path / "elsewhere" / "custom_windows.json"
    custom.parent.mkdir()
    app = _make_app(tmp_path, monkeypatch, mcp_windows_path=str(custom))
    assert app.ctx.mcp_windows_path == custom.resolve()
    _persist(app, lambda w: w.set(3, scope="s", pid=1, host="h", now=T))
    assert "3" in _disk(custom)
    assert not _sidecar(tmp_path).exists()


# -- re-apply through the real register() -------------------------------------

def test_restart_reapplies_a_row_with_a_matching_pid(tmp_path, monkeypatch):
    """#228: after a restart, a hello carrying the row's pid and host lands
    with the scope and the RAW mode already on the entry (so /sessions shows
    them), through create_app's on_register hook."""
    app = _make_app(tmp_path, monkeypatch)
    _persist(app, lambda w: w.set(7, scope="teamA", mode="readwrite", pid=77,
                                  host="hostA", now=T))
    again = _make_app(tmp_path, monkeypatch)
    assert again.ctx.registry.on_register == again.ctx.mcp_windows.apply
    entry = _register(again, 7, 77)
    assert (entry.mcp_scope, entry.mcp_mode) == ("teamA", "readwrite")
    summary = entry.summary("off")
    assert (summary["mcp_scope"], summary["mcp"]) == ("teamA", "readwrite")


def test_a_mismatched_pid_does_not_reapply_and_the_next_write_replaces_it(
        tmp_path, monkeypatch):
    """#228: id reuse. A hello on the row's id with a different pid gets a
    clean entry, and the next write for that live producer REPLACES the row
    on disk: the stale producer's mode is gone."""
    app = _make_app(tmp_path, monkeypatch)
    _persist(app, lambda w: w.set(7, scope="teamA", mode="readwrite", pid=77,
                                  host="hostA", now=T))
    again = _make_app(tmp_path, monkeypatch, now=T + 5)
    entry = _register(again, 7, 78)
    assert (entry.mcp_scope, entry.mcp_mode) == (None, None)
    _persist(again, lambda w: w.set(7, scope="teamB", pid=entry.pid,
                                    host=entry.host, now=T + 5))
    assert _disk(_sidecar(tmp_path)) == {"7": _row(
        scope="teamB", mode=None, pid=78, host="hostA", seen=T + 5)}


def test_a_pid_null_row_is_claimed_by_the_first_hello(tmp_path, monkeypatch):
    """#228: a pre-spawn row (no pid yet) is claimed by the first hello on its
    id, which fills in its pid and host; a flush makes that durable, and a
    later hello from another pid no longer matches."""
    app = _make_app(tmp_path, monkeypatch)
    _persist(app, lambda w: w.set(9, scope="teamS", now=T))
    assert _disk(_sidecar(tmp_path))["9"]["pid"] is None
    again = _make_app(tmp_path, monkeypatch, now=T + 5)
    entry = _register(again, 9, 55)
    assert entry.mcp_scope == "teamS"
    assert again.ctx.mcp_windows.dirty is True
    _persist(again, lambda w: None)
    assert _disk(_sidecar(tmp_path))["9"] == _row(scope="teamS", pid=55,
                                                  host="hostA", seen=T + 5)
    asyncio.run(again.ctx.registry.deregister(9))
    assert _register(again, 9, 56).mcp_scope is None


def test_mode_null_round_trips_as_null(tmp_path, monkeypatch):
    """#228: ``mode: null`` is stored as a JSON null and re-applied as None, so
    the window inherits the broker default (readwrite here) instead of the
    default being baked into the row."""
    app = _make_app(tmp_path, monkeypatch)
    _persist(app, lambda w: w.set(7, scope="a", mode=None, pid=77,
                                  host="hostA", now=T))
    assert _disk(_sidecar(tmp_path))["7"]["mode"] is None
    assert '"mode": null' in _sidecar(tmp_path).read_text(encoding="utf-8")
    again = _make_app(tmp_path, monkeypatch, mcp_default_mode="readwrite")
    entry = _register(again, 7, 77)
    assert entry.mcp_scope == "a"
    assert entry.mcp_mode is None
    assert entry.summary(again.ctx.mcp_cfg["default_mode"])["mcp"] == \
        "readwrite"
    assert again.ctx.mcp_windows.get(7)["mode"] is None


def test_the_hook_replaces_the_registry_carry_over_with_the_shared_gate(
        tmp_path, monkeypatch):
    """#228: with create_app's hook installed the registry's own carry-over is
    skipped and the store's fallback carries instead, through the ONE gate:
    a delegating spy on registry.same_producer sees exactly one call (old,
    new) for a same-id, same-pid replacement with no row. A forked copy in
    apply() would make it 0, both carry-overs running would make it 2."""
    calls = []
    real = registry_mod.same_producer

    def spy(a, b):
        calls.append((a, b))
        return real(a, b)

    monkeypatch.setattr(registry_mod, "same_producer", spy)
    app = _make_app(tmp_path, monkeypatch)
    old = _register(app, 5, 77)
    old.mcp_mode = "readwrite"                 # as POST /session/mcp sets it
    new = _register(app, 5, 77)
    assert new is not old
    assert new.mcp_mode == "readwrite"
    assert calls == [(old, new)]


def test_the_row_gate_and_the_fallback_both_ask_the_shared_gate(
        tmp_path, monkeypatch):
    """#228: a same_producer forced to False stops BOTH the row re-apply (a
    matching row stays unapplied) and the fallback carry-over, so neither
    path has a private copy of the predicate."""
    app = _make_app(tmp_path, monkeypatch)
    _persist(app, lambda w: w.set(7, scope="teamA", mode="read", pid=77,
                                  host="hostA", now=T))
    monkeypatch.setattr(registry_mod, "same_producer", lambda a, b: False)
    row_hit = _register(app, 7, 77)
    assert (row_hit.mcp_scope, row_hit.mcp_mode) == (None, None)
    old = _register(app, 8, 77)
    old.mcp_mode = "readwrite"
    new = _register(app, 8, 77)
    assert new.mcp_mode is None


def test_a_raising_apply_still_registers_and_is_logged(tmp_path, monkeypatch,
                                                       caplog):
    """#228: apply() is now the broker's only carry-over path, so its failure
    mode matters: an Exception inside it is logged by the registry's hook
    runner and the window registers anyway."""
    app = _make_app(tmp_path, monkeypatch)

    def boom(self, entry):
        raise KeyError("injected")

    monkeypatch.setattr(McpWindowStore, "reapply", boom)
    caplog.set_level(logging.ERROR, logger="webterm.broker.registry")
    entry = _register(app, 12, 77)
    assert app.ctx.registry.get(12) is entry
    assert any("on_register hook failed for window 12" in r.getMessage()
               for r in caplog.records)


# -- prune inside a write ------------------------------------------------------

def test_prune_inside_a_write_after_the_grace(tmp_path, monkeypatch):
    """#228: past the grace, a write drops an 8-day-old non-live row and keeps
    a LIVE row of the same age, in memory and on disk."""
    _write_sidecar(_sidecar(tmp_path), {
        "1": _row(scope="a", pid=1, host="h", seen=T - 8 * DAY),
        "2": _row(scope="a", pid=2, host="hostA", seen=T - 8 * DAY),
    })
    app = _make_app(tmp_path, monkeypatch, uptime=601.0)
    _inject_live(app, 2, 2)
    _persist(app, lambda w: w.set(3, scope="s", pid=3, host="h", now=T))
    assert app.ctx.mcp_windows.get(1) is None
    assert app.ctx.mcp_windows.get(2) is not None
    assert sorted(_disk(_sidecar(tmp_path))) == ["2", "3"]


def test_no_prune_inside_a_write_before_the_grace(tmp_path, monkeypatch):
    """#228: at 599 s of uptime the same write prunes nothing."""
    _write_sidecar(_sidecar(tmp_path), {
        "1": _row(scope="a", pid=1, host="h", seen=T - 8 * DAY),
        "2": _row(scope="a", pid=2, host="hostA", seen=T - 8 * DAY),
    })
    app = _make_app(tmp_path, monkeypatch, uptime=599.0)
    _inject_live(app, 2, 2)
    _persist(app, lambda w: w.set(3, scope="s", pid=3, host="h", now=T))
    assert sorted(_disk(_sidecar(tmp_path))) == ["1", "2", "3"]


def test_prune_uses_the_store_wall_clock(tmp_path, monkeypatch):
    """#228: ``seen`` is wall-clock epoch, so prune's ``now`` must be the
    store's wall clock too; a monotonic reading (small, per-boot) would make
    every row look fresh and silently disable pruning."""
    seen_now = []
    real = McpWindowStore.prune

    def spy(self, live, now, uptime_s, is_pending=None):
        seen_now.append(now)
        return real(self, live, now, uptime_s, is_pending)

    monkeypatch.setattr(McpWindowStore, "prune", spy)
    _write_sidecar(_sidecar(tmp_path), {
        "1": _row(scope="a", pid=1, host="h", seen=T - 8 * DAY)})
    app = _make_app(tmp_path, monkeypatch, uptime=601.0, now=T)
    _persist(app, lambda w: None)
    assert seen_now == [T]
    assert app.ctx.mcp_windows.get(1) is None


def test_the_cap_holds_through_the_writer(tmp_path, monkeypatch):
    """#228: 1002 fresh rows, one write past the grace: MAX_ROWS (1000)
    remain on disk and the two oldest go."""
    windows = {str(i): _row(scope="a", pid=i, host="h", seen=T - 2000 + i)
               for i in range(1, 1003)}
    _write_sidecar(_sidecar(tmp_path), windows)
    app = _make_app(tmp_path, monkeypatch, uptime=10_000.0)
    _persist(app, lambda w: None)
    disk = _disk(_sidecar(tmp_path))
    assert len(disk) == 1000
    assert "1" not in disk and "2" not in disk and "3" in disk


# -- the writer itself ---------------------------------------------------------

def test_a_no_op_set_does_not_write(tmp_path, monkeypatch, writes):
    """#228: a set() that changes nothing makes no write at all: the counted
    executor writes on the store path, the file's bytes and its file id
    (st_ino: os.replace swaps in a new file) are all unchanged. mtime is NOT
    a usable signal: two back-to-back writes were measured with identical
    st_mtime_ns."""
    app = _make_app(tmp_path, monkeypatch)

    def mutate(w):
        return w.set(7, scope="a", mode="read", pid=77, host="hostA", now=T)

    assert _persist(app, mutate) is True
    path = _sidecar(tmp_path)
    before = (len(_store_writes(writes, app)), path.read_bytes(),
              os.stat(path).st_ino)
    assert before[0] == 1
    assert _persist(app, mutate) is False
    assert (len(_store_writes(writes, app)), path.read_bytes(),
            os.stat(path).st_ino) == before


def test_the_writer_returns_mutate_result_via_app_ctx(tmp_path, monkeypatch):
    """#228: the shared writer is reachable as app.ctx.persist_mcp_windows and
    hands back whatever mutate returned."""
    app = _make_app(tmp_path, monkeypatch)
    sentinel = object()
    assert _persist(app, lambda w: sentinel) is sentinel


def test_a_failed_write_leaves_memory_and_disk_untouched(tmp_path,
                                                         monkeypatch):
    """#228: memory never changes before the write lands. A write raising
    OSError propagates with the row absent from memory and disk, and the
    lock is released (the next write succeeds)."""
    app = _make_app(tmp_path, monkeypatch)
    real = app_mod._write_state_atomic

    def failing(path, payload):
        if Path(path) == app.ctx.mcp_windows_path:
            raise OSError("disk full")
        return real(path, payload)

    monkeypatch.setattr(app_mod, "_write_state_atomic", failing)
    with pytest.raises(OSError):
        _persist(app, lambda w: w.set(7, scope="a", pid=1, host="h", now=T))
    assert app.ctx.mcp_windows.get(7) is None
    assert not _sidecar(tmp_path).exists()
    assert app.ctx.mcp_windows.inflight is None
    monkeypatch.setattr(app_mod, "_write_state_atomic", real)
    _persist(app, lambda w: w.set(7, scope="a", pid=1, host="h", now=T))
    assert app.ctx.mcp_windows.get(7) is not None


def test_a_raising_mutate_leaves_memory_untouched(tmp_path, monkeypatch,
                                                  writes):
    """#228: mutate runs on the working copy, so one that raises part-way
    leaves memory as it was, writes nothing and clears ``inflight``."""
    app = _make_app(tmp_path, monkeypatch)

    def mutate(w):
        w.set(7, scope="a", pid=1, host="h", now=T)
        raise ZeroDivisionError

    with pytest.raises(ZeroDivisionError):
        _persist(app, mutate)
    assert app.ctx.mcp_windows.get(7) is None
    assert app.ctx.mcp_windows.inflight is None
    assert _store_writes(writes, app) == []


def test_an_async_mutate_is_rejected(tmp_path, monkeypatch, writes, caplog):
    """#228: an ``async def`` mutate would change nothing and hand its caller a
    truthy coroutine. The writer logs one ERROR naming itself, CLOSES the
    coroutine and raises TypeError; memory and disk are untouched and
    ``inflight`` is cleared. The coroutine is built here and its state read
    directly, so the close is observed without relying on refcounting to
    trigger a "never awaited" warning."""
    app = _make_app(tmp_path, monkeypatch)

    async def mutate(w):
        w.set(7, scope="a", pid=1, host="h", now=T)

    co = mutate(None)
    caplog.set_level(logging.ERROR, logger=APP_LOGGER)
    with pytest.raises(TypeError):
        _persist(app, lambda w: co)
    assert inspect.getcoroutinestate(co) == "CORO_CLOSED"
    assert [r.levelno for r in caplog.records if r.name == APP_LOGGER
            and "_persist_mcp_windows" in r.getMessage()] == [logging.ERROR]
    assert app.ctx.mcp_windows.get(7) is None
    assert app.ctx.mcp_windows.inflight is None
    assert _store_writes(writes, app) == []


def test_a_mutate_returning_a_future_is_rejected(tmp_path, monkeypatch,
                                                 writes, caplog):
    """#228: a mutate that returns a Task/Future deferred its work past the
    write. The writer logs one ERROR naming itself and raises TypeError; it
    never closes or cancels an awaitable that is not its own, writes nothing
    and clears ``inflight``."""
    app = _make_app(tmp_path, monkeypatch)
    caplog.set_level(logging.ERROR, logger=APP_LOGGER)

    async def scenario():
        future = asyncio.get_running_loop().create_future()
        with pytest.raises(TypeError):
            await app.ctx.persist_mcp_windows(lambda w: future)
        return future

    future = asyncio.run(scenario())
    assert not future.cancelled() and not future.done()
    assert [r.levelno for r in caplog.records if r.name == APP_LOGGER
            and "_persist_mcp_windows" in r.getMessage()] == [logging.ERROR]
    assert app.ctx.mcp_windows.inflight is None
    assert _store_writes(writes, app) == []


def test_two_overlapping_writes_both_land(tmp_path, monkeypatch):
    """#228: the lock serialises the writers. With the first write parked in
    the executor, a second write queues on the lock and copies memory only
    after the first committed, so neither update is lost."""
    app = _make_app(tmp_path, monkeypatch)
    gate = _Gate(monkeypatch, app)

    async def scenario():
        first = asyncio.ensure_future(app.ctx.persist_mcp_windows(
            lambda w: w.set(7, scope="a", pid=1, host="h", now=T)))
        try:
            await gate.wait_entered()
            second = asyncio.ensure_future(app.ctx.persist_mcp_windows(
                lambda w: w.set(8, scope="b", pid=2, host="h", now=T)))
            await asyncio.sleep(0)            # let it reach the lock
        finally:
            gate.release.set()
        await first
        await second

    asyncio.run(scenario())
    assert app.ctx.mcp_windows.get(7) is not None
    assert app.ctx.mcp_windows.get(8) is not None
    assert sorted(_disk(_sidecar(tmp_path))) == ["7", "8"]


# -- F1: hellos landing while a write is in flight -----------------------------

def _pre_spawn_rows(tmp_path, *wids):
    _write_sidecar(_sidecar(tmp_path),
                   {str(w): _row(scope=f"s{w}", seen=T - 10) for w in wids})


def test_a_claim_during_a_write_survives_the_swap(tmp_path, monkeypatch):
    """#228 F1: a hello claims a pre-spawn row while an unrelated write is
    parked in the executor. The swap must keep the claimed pid (memory still
    has it, and it is not on disk yet, so the store is dirty) and the next
    flush persists it. Without the in-flight mirror the swap silently reverts
    the row to pid null AND leaves the store clean, so nothing repairs it."""
    _pre_spawn_rows(tmp_path, 9)
    app = _make_app(tmp_path, monkeypatch, now=T)
    gate = _Gate(monkeypatch, app)
    store = app.ctx.mcp_windows
    _mid_write(app, gate,
               lambda w: w.set(7, scope="x", pid=1, host="h", now=T),
               lambda: app.ctx.registry.register(_WS(), _hello(9, 55)))
    assert (store.get(9)["pid"], store.dirty) == (55, True)
    _persist(app, lambda w: None)
    assert _disk(_sidecar(tmp_path))["9"]["pid"] == 55


def test_two_windows_claimed_during_one_write_both_survive(tmp_path,
                                                          monkeypatch):
    """#228 F1: two different pre-spawn rows claimed during the same write's
    await both keep their claim across the swap."""
    _pre_spawn_rows(tmp_path, 9, 10)
    app = _make_app(tmp_path, monkeypatch)
    gate = _Gate(monkeypatch, app)

    async def hellos():
        await app.ctx.registry.register(_WS(), _hello(9, 55))
        await app.ctx.registry.register(_WS(), _hello(10, 56))

    _mid_write(app, gate,
               lambda w: w.set(7, scope="x", pid=1, host="h", now=T), hellos)
    store = app.ctx.mcp_windows
    assert (store.get(9)["pid"], store.get(10)["pid"]) == (55, 56)
    _persist(app, lambda w: None)
    disk = _disk(_sidecar(tmp_path))
    assert (disk["9"]["pid"], disk["10"]["pid"]) == (55, 56)


def test_one_window_registering_twice_during_one_write(tmp_path, monkeypatch):
    """#228 F1: a claim and then a half-open re-register of the same producer,
    both during one await: both entries get the scope, and the row keeps the
    claim with the LATER seen."""
    _pre_spawn_rows(tmp_path, 9)
    app = _make_app(tmp_path, monkeypatch)
    gate = _Gate(monkeypatch, app)
    clock = [T]
    app.ctx.mcp_windows.clock = lambda: clock[0]

    async def hellos():
        first = await app.ctx.registry.register(_WS(), _hello(9, 55))
        clock[0] = T + 30
        second = await app.ctx.registry.register(_WS(), _hello(9, 55))
        return first, second

    _, (first, second) = _mid_write(
        app, gate, lambda w: w.set(7, scope="x", pid=1, host="h", now=T),
        hellos)
    assert (first.mcp_scope, second.mcp_scope) == ("s9", "s9")
    assert app.ctx.mcp_windows.get(9) == _row(scope="s9", pid=55,
                                              host="hostA", seen=T + 30)


def test_a_fallback_carry_during_a_write_is_memory_only(tmp_path,
                                                        monkeypatch):
    """#228 F1: with no row, a half-open re-register during a write carries the
    in-memory override through apply()'s fallback; the swap neither loses it
    (it lives on the entry) nor persists anything for it."""
    app = _make_app(tmp_path, monkeypatch)
    old = _register(app, 11, 60)
    old.mcp_mode = "readwrite"
    gate = _Gate(monkeypatch, app)
    _, new = _mid_write(
        app, gate, lambda w: w.set(7, scope="x", pid=1, host="h", now=T),
        lambda: app.ctx.registry.register(_WS(), _hello(11, 60)))
    assert new is not old and new.mcp_mode == "readwrite"
    assert app.ctx.mcp_windows.get(11) is None
    assert app.ctx.mcp_windows.dirty is False


def test_the_old_producer_claims_a_row_replaced_mid_write(tmp_path,
                                                          monkeypatch):
    """#228, a STATED hazard (reapply's docstring): a mutate that replaces a
    claimed row with a fresh pre-spawn row, while the OLD process's hello
    lands during the await, lets that old pid claim the NEW row through the
    mirror's wildcard. Pinned so a change to it is deliberate; ruling it out
    is the launch writer's (#231) job."""
    _write_sidecar(_sidecar(tmp_path), {"9": _row(scope="old", pid=55,
                                                  host="hostA", seen=T - 10)})
    app = _make_app(tmp_path, monkeypatch)
    gate = _Gate(monkeypatch, app)
    _, entry = _mid_write(
        app, gate, lambda w: w.set(9, scope="fresh", now=T),
        lambda: app.ctx.registry.register(_WS(), _hello(9, 55)))
    assert entry.mcp_scope == "old"
    assert app.ctx.mcp_windows.get(9) == _row(scope="fresh", pid=55,
                                              host="hostA", seen=T)


def test_a_row_deleted_mid_write_loses_the_touch(tmp_path, monkeypatch):
    """#228, stated and accepted (reapply's docstring): a row the in-flight
    mutate deletes stays deleted even if its producer's hello touched it
    during the await; the entry keeps what it was given."""
    _write_sidecar(_sidecar(tmp_path), {"9": _row(scope="s", pid=55,
                                                  host="hostA", seen=T - 10)})
    app = _make_app(tmp_path, monkeypatch)
    gate = _Gate(monkeypatch, app)
    _, entry = _mid_write(
        app, gate,
        lambda w: w.set(9, scope=None, pid=55, host="hostA", now=T),
        lambda: app.ctx.registry.register(_WS(), _hello(9, 55)))
    assert entry.mcp_scope == "s"
    assert app.ctx.mcp_windows.get(9) is None


# -- the coalesced seen writer -------------------------------------------------

APP_LOGGER = "webterm.broker.app"


def _claimed_rows(tmp_path, **pids):
    """Rows already claimed by producer ``pid`` on host hostA, last seen 100 s
    before T, so a hello at T re-applies them and bumps seen."""
    _write_sidecar(_sidecar(tmp_path), {
        wid.lstrip("w"): _row(scope=f"s{wid}", pid=pid, host="hostA",
                              seen=T - 100)
        for wid, pid in pids.items()})


class _Ticks:
    """A fake ``app.ctx.mcp_windows_sleep``: every call records its interval
    and parks until the test releases it, so ticks are stepped by hand and no
    test sleeps. Build it inside the running loop."""

    def __init__(self):
        self.calls = []                       # [(interval, Event)]
        self.arrived = asyncio.Queue()

    async def sleep(self, seconds):
        event = asyncio.Event()
        self.calls.append((seconds, event))
        self.arrived.put_nowait(len(self.calls))
        await event.wait()

    async def parked(self, n):
        """Wait until the ticker is parked in its ``n``-th sleep, i.e. every
        flush before it has finished."""
        assert await asyncio.wait_for(self.arrived.get(), 10) == n

    def release(self, n):
        self.calls[n - 1][1].set()

    @property
    def intervals(self):
        return [seconds for seconds, _event in self.calls]


def test_the_ticker_coalesces_registers_into_one_write_per_tick(
        tmp_path, monkeypatch, writes):
    """#228: two re-applying hellos between two ticks produce exactly ONE
    counted write, carrying both seen bumps; registering alone writes nothing;
    a tick with nothing new writes nothing. The interval is re-read from
    app.ctx on every iteration."""
    _claimed_rows(tmp_path, w9=55, w10=56)
    app = _make_app(tmp_path, monkeypatch, now=T)
    app.ctx.mcp_windows_flush_s = 7.5

    async def scenario():
        ticks = _Ticks()
        app.ctx.mcp_windows_sleep = ticks.sleep
        ticker = asyncio.ensure_future(app.ctx.mcp_windows_ticker())
        try:
            await ticks.parked(1)
            await app.ctx.registry.register(_WS(), _hello(9, 55))
            await app.ctx.registry.register(_WS(), _hello(10, 56))
            assert _store_writes(writes, app) == []
            ticks.release(1)
            await ticks.parked(2)
            landed = _store_writes(writes, app)
            assert len(landed) == 1
            assert (landed[0]["windows"]["9"]["seen"],
                    landed[0]["windows"]["10"]["seen"]) == (T, T)
            ticks.release(2)
            await ticks.parked(3)
            assert len(_store_writes(writes, app)) == 1
            app.ctx.mcp_windows_flush_s = 9.0
            ticks.release(3)
            await ticks.parked(4)
            assert ticks.intervals == [7.5, 7.5, 7.5, 9.0]
        finally:
            ticker.cancel()
            await asyncio.gather(ticker, return_exceptions=True)

    asyncio.run(scenario())


def test_a_quiet_tick_still_prunes(tmp_path, monkeypatch, writes):
    """#228: prune is not gated behind a dirty store. With nothing registered,
    a tick past the grace still runs the writer, which drops an 8-day-old
    non-live row and writes once."""
    _write_sidecar(_sidecar(tmp_path), {
        "5": _row(scope="a", pid=1, host="h", seen=T - 8 * DAY)})
    app = _make_app(tmp_path, monkeypatch, uptime=601.0, now=T)
    assert app.ctx.mcp_windows.dirty is False

    async def scenario():
        ticks = _Ticks()
        app.ctx.mcp_windows_sleep = ticks.sleep
        ticker = asyncio.ensure_future(app.ctx.mcp_windows_ticker())
        try:
            await ticks.parked(1)
            ticks.release(1)
            await ticks.parked(2)
        finally:
            ticker.cancel()
            await asyncio.gather(ticker, return_exceptions=True)

    asyncio.run(scenario())
    assert len(_store_writes(writes, app)) == 1
    assert _disk(_sidecar(tmp_path)) == {}


def test_the_server_listeners_start_the_ticker_and_flush_at_stop(
        tmp_path, monkeypatch, writes):
    """#228: through the real listeners (app.test_client runs a server per
    request): after_server_start starts the ticker (its first sleep sees the
    injected interval), and before_server_stop cancels it and flushes the
    pending seen bump, exactly one write. A second request with nothing new
    writes nothing."""
    _claimed_rows(tmp_path, w9=55)
    app = _make_app(tmp_path, monkeypatch, now=T)
    app.ctx.mcp_windows_flush_s = 4.25
    intervals = []

    async def forever(seconds):
        intervals.append(seconds)
        await asyncio.Event().wait()

    app.ctx.mcp_windows_sleep = forever
    _register(app, 9, 55)
    assert app.ctx.mcp_windows.dirty is True
    headers = {"Authorization": f"Bearer {TEST_TOKEN}"}
    _, resp = app.test_client.get("/sessions", headers=headers)
    assert resp.status == 200
    assert intervals == [4.25]
    assert len(_store_writes(writes, app)) == 1
    assert _disk(_sidecar(tmp_path))["9"]["seen"] == T
    assert app.ctx.mcp_windows.dirty is False
    assert app.ctx.mcp_windows_task is None
    app.test_client.get("/sessions", headers=headers)
    assert len(_store_writes(writes, app)) == 1


def test_the_stop_flush_survives_a_ticker_that_died(tmp_path, monkeypatch,
                                                    writes, caplog):
    """#228: a ticker that already died of an exception (here a non-number
    interval makes the real asyncio.sleep raise TypeError) is logged at stop,
    and the final flush still runs: the pending seen bump lands, once."""
    _claimed_rows(tmp_path, w9=55)
    app = _make_app(tmp_path, monkeypatch, now=T)
    app.ctx.mcp_windows_flush_s = "not a number"
    _register(app, 9, 55)
    caplog.set_level(logging.ERROR, logger=APP_LOGGER)
    headers = {"Authorization": f"Bearer {TEST_TOKEN}"}
    _, resp = app.test_client.get("/sessions", headers=headers)
    assert resp.status == 200
    assert [(r.levelno, r.exc_info is not None) for r in caplog.records
            if r.name == APP_LOGGER
            and "the flush ticker had died" in r.getMessage()] == [
                (logging.ERROR, True)]
    assert len(_store_writes(writes, app)) == 1
    assert _disk(_sidecar(tmp_path))["9"]["seen"] == T


def test_the_start_listener_starts_the_ctx_ticker(tmp_path, monkeypatch):
    """#228: after_server_start starts ``app.ctx.mcp_windows_ticker`` looked up
    at start time, so replacing that attribute replaces the ticker."""
    app = _make_app(tmp_path, monkeypatch)
    started = []

    async def mine():
        started.append("mine")
        await asyncio.Event().wait()

    app.ctx.mcp_windows_ticker = mine
    headers = {"Authorization": f"Bearer {TEST_TOKEN}"}
    _, resp = app.test_client.get("/sessions", headers=headers)
    assert resp.status == 200
    assert started == ["mine"]


def test_the_stop_listener_flushes_through_app_ctx(tmp_path, monkeypatch):
    """#228: before_server_stop calls ``app.ctx.flush_mcp_windows`` looked up
    at call time, so replacing that attribute before a request replaces the
    final flush."""
    app = _make_app(tmp_path, monkeypatch)
    calls = []

    async def mine():
        calls.append("mine")
        return True

    app.ctx.flush_mcp_windows = mine
    headers = {"Authorization": f"Bearer {TEST_TOKEN}"}
    _, resp = app.test_client.get("/sessions", headers=headers)
    assert resp.status == 200
    assert calls == ["mine"]


def test_the_ticker_flushes_through_app_ctx(tmp_path, monkeypatch):
    """#228: each tick calls ``app.ctx.flush_mcp_windows`` looked up at call
    time."""
    app = _make_app(tmp_path, monkeypatch)
    calls = []

    async def mine():
        calls.append("mine")
        return True

    app.ctx.flush_mcp_windows = mine

    async def scenario():
        ticks = _Ticks()
        app.ctx.mcp_windows_sleep = ticks.sleep
        ticker = asyncio.ensure_future(app.ctx.mcp_windows_ticker())
        try:
            await ticks.parked(1)
            ticks.release(1)
            await ticks.parked(2)
        finally:
            ticker.cancel()
            await asyncio.gather(ticker, return_exceptions=True)

    asyncio.run(scenario())
    assert calls == ["mine"]


def test_the_flush_writes_through_app_ctx(tmp_path, monkeypatch):
    """#228: the flush calls ``app.ctx.persist_mcp_windows`` looked up at call
    time, with a no-op mutate."""
    app = _make_app(tmp_path, monkeypatch)
    mutates = []

    async def mine(mutate):
        mutates.append(mutate)
        return mutate(None)

    app.ctx.persist_mcp_windows = mine
    assert asyncio.run(app.ctx.flush_mcp_windows()) is True
    assert len(mutates) == 1
    assert mutates[0](None) is None


def test_a_failing_flush_logs_once_then_debug_then_recovery(
        tmp_path, monkeypatch, caplog):
    """#228: a flush failure is never swallowed silently and never spams: the
    first failure of a streak is one ERROR with the traceback, repeats are
    DEBUG, the store stays dirty throughout, and the first success after it
    logs one INFO and lands the bump."""
    _claimed_rows(tmp_path, w9=55)
    app = _make_app(tmp_path, monkeypatch, now=T)
    _register(app, 9, 55)
    real = app_mod._write_state_atomic

    def failing(path, payload):
        if Path(path) == app.ctx.mcp_windows_path:
            raise OSError("no such directory")
        return real(path, payload)

    monkeypatch.setattr(app_mod, "_write_state_atomic", failing)
    caplog.set_level(logging.DEBUG, logger=APP_LOGGER)

    def flush_records():
        return [(r.levelno, r.exc_info is not None) for r in caplog.records
                if r.name == APP_LOGGER and "flushing" in r.getMessage()]

    assert asyncio.run(app.ctx.flush_mcp_windows()) is False
    assert asyncio.run(app.ctx.flush_mcp_windows()) is False
    assert flush_records() == [(logging.ERROR, True), (logging.DEBUG, True)]
    assert app.ctx.mcp_windows.dirty is True
    monkeypatch.setattr(app_mod, "_write_state_atomic", real)
    assert asyncio.run(app.ctx.flush_mcp_windows()) is True
    assert flush_records()[2:] == [(logging.INFO, False)]
    assert app.ctx.mcp_windows.dirty is False
    assert _disk(_sidecar(tmp_path))["9"]["seen"] == T


# =============================================================================
# WIRE section: /mcp/* honours X-Browserland-Scope (#230)
# =============================================================================

#: Every /mcp/* route behind the MCP token gate (_mcp_auth_error), with a body
#: that would SUCCEED (or fail for a reason of its own) on window 5 if the
#: scope refusal were missing, so a 400 bad_scope can only come from the gate.
MCP_TOKEN_ROUTES = {
    "/mcp/info": ("GET", None),
    "/mcp/terminals": ("GET", None),
    "/mcp/read": ("POST", {"id": 5}),
    "/mcp/input": ("POST", {"id": 5, "data": "x"}),
    "/mcp/reset": ("POST", {"id": 5}),
    "/mcp/flush": ("POST", {"id": 5}),
    "/mcp/pace": ("POST", {"id": 5, "pace_ms": 40}),
    "/mcp/profiles": ("GET", None),
    "/mcp/launch": ("POST", {}),
}
BAD_SCOPES = ["a:b", "a b", "x" * 65, "-lead"]
_ABSENT = object()


class _Producer:
    """Producer WS double that answers the three correlated round-trips the
    /mcp routes make (screen_text_please, reset_please, flush_input_please)
    and records every frame the broker sent it, so a refused call can be
    shown to have reached nothing."""

    def __init__(self):
        self.entry = None
        self.sent = []

    async def send(self, text):
        self.sent.append(text)
        data = json.loads(text)
        req = data.get("req")
        kind = data.get("type")
        if kind == "screen_text_please":
            self.entry.resolve_rpc(req, "screen_text", {
                "type": "screen_text", "req": req, "cols": 80, "rows": 24,
                "text": "hi", "content_hash": "abc"})
        elif kind == "reset_please":
            self.entry.resolve_rpc(req, "reset_done", {
                "type": "reset_done", "req": req, "ok": True})
        elif kind == "flush_input_please":
            self.entry.resolve_rpc(req, "flush_input_done", {
                "type": "flush_input_done", "req": req, "ok": True})

    async def close(self, *a, **k):
        pass


def _wire_app(tmp_path, monkeypatch, mode="readwrite", **extra):
    return _make_app(tmp_path, monkeypatch, mcp_default_mode=mode, **extra)


def _window(app, wid, scope=None, mcp_mode=None):
    """A live producer entry tagged ``scope`` (None = untagged), injected
    straight into the registry like test_mcp_pace does."""
    ws = _Producer()
    entry = WindowEntry(wid, 111, "t", 80, 24, ws, kind="agent")
    entry.mcp_scope = scope
    entry.mcp_mode = mcp_mode
    ws.entry = entry
    app.ctx.registry._entries[wid] = entry
    return entry


def _mcp(app, path, *, scope=_ABSENT, body=_ABSENT, token=MCP_TOKEN,
         extra_headers=()):
    """One call to an /mcp/* token route, as ``(request, response)``.
    ``scope`` is the header value (``_ABSENT`` sends none); headers go as a
    LIST so a repeated header survives to the wire; ``body`` defaults to the
    route's MCP_TOKEN_ROUTES body."""
    method, default_body = MCP_TOKEN_ROUTES[path]
    headers = [("Authorization", f"Bearer {token}")]
    if scope is not _ABSENT:
        headers.append((SCOPE_HEADER, scope))
    headers.extend(extra_headers)
    payload = default_body if body is _ABSENT else body
    if method == "GET":
        return app.test_client.get(path, headers=headers)
    return app.test_client.post(path, json=payload, headers=headers)


def test_the_route_table_is_every_mcp_token_route(tmp_path, monkeypatch):
    """#230: MCP_TOKEN_ROUTES is exactly the app's non-preflight /mcp/*
    routes minus /mcp/config (browser realm, _gated_auth_error), so a new
    token route cannot ship without its bad_scope cells."""
    app = _wire_app(tmp_path, monkeypatch)
    served = {("/" + r.path, method) for r in app.router.routes
              for method in r.methods
              if r.path.startswith("mcp/") and method != "OPTIONS"}
    served -= {("/mcp/config", "GET"), ("/mcp/config", "POST")}
    assert served == {(path, method) for path, (method, _body)
                      in MCP_TOKEN_ROUTES.items()}


@pytest.mark.parametrize("scope", BAD_SCOPES,
                         ids=["colon", "space", "65chars", "leading-dash"])
@pytest.mark.parametrize("path", sorted(MCP_TOKEN_ROUTES))
def test_a_bad_scope_is_refused_on_every_mcp_route(tmp_path, monkeypatch,
                                                   path, scope):
    """#230: a present-but-invalid scope is 400 bad_scope on every token
    route, from the gate, before the route does anything: the producer is
    sent nothing and the pace is untouched."""
    app = _wire_app(tmp_path, monkeypatch)
    entry = _window(app, 5, scope="a")
    _, resp = _mcp(app, path, scope=scope)
    assert (resp.status, resp.json) == (400, {"error": "bad_scope"})
    assert entry.ws.sent == [] and entry.pace_ms == 0


def test_two_scope_headers_reach_the_app_as_two_values(tmp_path, monkeypatch):
    """#230: the premise the duplicate cells lean on. The test client sends a
    repeated header as two header lines (a dict literal would collapse them
    first), and the app sees both."""
    app = _wire_app(tmp_path, monkeypatch)
    request, _resp = _mcp(app, "/mcp/info", scope="a",
                          extra_headers=[(SCOPE_HEADER, "b")])
    assert request.headers.getall(SCOPE_HEADER) == ["a", "b"]


@pytest.mark.parametrize("path", sorted(MCP_TOKEN_ROUTES))
def test_a_repeated_scope_header_is_refused(tmp_path, monkeypatch, path):
    """#230, a decision beyond the issue's text: two scope headers, even two
    valid ones, are 400 bad_scope rather than a silent pick of the first."""
    app = _wire_app(tmp_path, monkeypatch)
    entry = _window(app, 5, scope="a")
    _, resp = _mcp(app, path, scope="a", extra_headers=[(SCOPE_HEADER, "b")])
    assert (resp.status, resp.json) == (400, {"error": "bad_scope"})
    assert entry.ws.sent == [] and entry.pace_ms == 0


@pytest.mark.parametrize("path", sorted(MCP_TOKEN_ROUTES))
def test_the_token_is_checked_before_the_scope(tmp_path, monkeypatch, path):
    """#230: without the token a bad scope is 401 auth_required, so a caller
    that cannot authenticate cannot probe scope validity."""
    app = _wire_app(tmp_path, monkeypatch)
    _window(app, 5, scope="a")
    _, resp = _mcp(app, path, scope="a:b", token="wrong-token")
    assert (resp.status, resp.json) == (401, {"error": "auth_required"})


@pytest.mark.parametrize("path", sorted(MCP_TOKEN_ROUTES))
def test_mcp_disabled_is_checked_before_the_scope(tmp_path, monkeypatch,
                                                  path):
    """#230: with the surface disabled a bad scope is 403 mcp_disabled."""
    app = _wire_app(tmp_path, monkeypatch, mcp_enabled=False)
    _window(app, 5, scope="a")
    _, resp = _mcp(app, path, scope="a:b")
    assert (resp.status, resp.json) == (403, {"error": "mcp_disabled"})


def test_an_empty_scope_header_is_unscoped(tmp_path, monkeypatch):
    """#230: a header sent with an EMPTY value is unscoped, like no header
    (the fail-open direction _mcp_scope's docstring names): it is not
    refused and the listing shows every window, tagged or not."""
    app = _wire_app(tmp_path, monkeypatch)
    _window(app, 5, scope="a")
    _window(app, 6)
    request, resp = _mcp(app, "/mcp/terminals", scope="")
    assert request.headers.getall(SCOPE_HEADER) == [""]
    assert resp.status == 200
    assert sorted(row["id"] for row in resp.json) == [5, 6]
