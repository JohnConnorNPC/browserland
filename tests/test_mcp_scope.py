"""MCP scopes (#237): the durable per-window store, ``webterm_mcp_windows.json``
(#228).

STORE UNIT section: ``McpWindowStore`` on its own, driven with real
``WindowEntry`` objects and an injected wall clock; no app, no loop. It pins
load's protective parsing and its single log line, set()'s raw-override and
identity rules, apply()'s row gate, claim and fallback, prune()'s grace, age,
liveness and cap rules, the explicit schema, known_scopes() and the derived
``dirty``.
"""

from __future__ import annotations

import json
import logging
import math
import re
from pathlib import Path

import pytest

import webterm.broker.mcp_windows as mw
from webterm.broker.mcp_windows import (MAX_ROWS, MCP_MODES, PRUNE_AGE_S,
                                        PRUNE_GRACE_S, McpWindowStore)
from webterm.broker.registry import WindowEntry

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
], ids=["bad-json", "top-level-list", "windows-list", "no-windows",
        "not-utf8"])
def test_load_unreadable_or_wrong_schema_boots_empty(tmp_path, caplog, raw):
    """#228: a corrupt or wrong-schema sidecar boots an EMPTY store with exactly
    one WARNING naming the path and the consequence. It is clean (the empty
    payload), so nothing rewrites the file until a real change does."""
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
    unknown pid (0 on both sides) is not the row's producer: the entry stays
    clean and the row is untouched (seen included)."""
    original = _row(scope="teamA", mode="readwrite", pid=row_pid,
                    host=row_host, seen=T - DAY)
    store = _loaded(tmp_path, {"5": original})
    entry = _entry(5, hello_pid, host=hello_host)
    store.apply(entry, None)
    assert (entry.mcp_scope, entry.mcp_mode) == (None, None)
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
