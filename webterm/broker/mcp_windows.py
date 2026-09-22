"""The durable per-window MCP store (#228): ``webterm_mcp_windows.json``.

One row per window id holding that window's MCP scope and its RAW per-window
mode override, so both survive a broker restart and an agent reconnect.
create_app installs :meth:`McpWindowStore.apply` as the registry's
``on_register`` hook, and every durable write goes through create_app's one
shared writer (``_persist_mcp_windows``), which is also the only place
:meth:`McpWindowStore.prune` runs.

Schema (explicit; nothing else is ever persisted)::

    {"windows": {"<wid>": {"scope": str|null, "mode": str|null,
                           "pid": int|null, "host": str|null,
                           "seen": epoch seconds}}}

* ``mode`` is the RAW override: null means "inherit the broker default", and
  a scope-only change never materialises the default into the row.
* ``pid`` and ``host`` identify the producer the row was written for. Window
  ids are reused (non-launcher producers may use OS window handles, which the
  OS recycles), so a row is re-applied only to a hello from the same producer
  (:func:`registry.same_producer`). A null field is a wildcard filled in by
  the first hello that claims the row: a row written before its window's
  process existed has both null.
* ``seen`` is wall-clock epoch seconds (it spans restarts), bumped whenever a
  hello re-applies or claims the row and whenever a write changes it.
"""

from __future__ import annotations

import json
import logging
import math
import re
import time
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional

from . import registry as _registry

LOGGER = logging.getLogger(__name__)

#: Valid per-window / default MCP access modes: the one PYTHON source (app.py
#: imports it from here). DRIFT: duplicated as ``const MCP_MODES`` in
#: ``webterm/broker/65_js_display_theming.js`` (the title-bar robot menu's
#: value/label pairs); keep both in step.
#: ``tests/test_mcp_scope.py::test_mcp_modes_do_not_drift_from_the_js_menu``
#: enforces it.
MCP_MODES = ("off", "read", "readwrite")

#: A scope name (#237). Always ``fullmatch``: no ``:`` (ids are ``host:int``),
#: no whitespace, 1-64 characters.
SCOPE_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")

#: :meth:`McpWindowStore.prune` is a no-op until the PROCESS has been up this
#: long, so a restart gives every window this long to reconnect before any
#: row can be judged stale.
PRUNE_GRACE_S = 600
#: After the grace, a row whose window is not live and that has not been seen
#: for longer than this is dropped.
PRUNE_AGE_S = 7 * 86400
#: Then, while there are more rows than this, the oldest non-live rows go. A
#: live row is never dropped, so the cap is soft.
MAX_ROWS = 1000

_UNSET = object()
_FIELDS = ("scope", "mode", "pid", "host", "seen")


def _empty_payload() -> Dict[str, Any]:
    return {"windows": {}}


def _valid_scope(value: Any) -> bool:
    return value is None or (isinstance(value, str)
                             and SCOPE_RE.fullmatch(value) is not None)


def _valid_mode(value: Any) -> bool:
    return value is None or (isinstance(value, str) and value in MCP_MODES)


def _valid_pid(value: Any) -> bool:
    # bool is excluded explicitly: isinstance(True, int) is True.
    return value is None or (isinstance(value, int)
                             and not isinstance(value, bool))


def _valid_host(value: Any) -> bool:
    return value is None or isinstance(value, str)


def _valid_seen(value: Any) -> bool:
    # bool is excluded for the same reason as in _valid_pid.
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value))


def _canonical_id(key: Any) -> Optional[int]:
    """The window id a JSON key names, or None unless the key is exactly
    ``str(int)`` of it ("7", "-7"; never "07", " 7" or "+7"), so two spellings
    can never name one window."""
    if not isinstance(key, str):
        return None
    try:
        wid = int(key)
    except ValueError:
        return None
    return wid if str(wid) == key else None


def _parse_row(raw: Any) -> Optional[Dict[str, Any]]:
    """A validated row, or None. A row that carries neither a scope nor a mode
    counts as malformed: set() never writes one (see there)."""
    if not isinstance(raw, dict):
        return None
    row = {field: raw.get(field) for field in _FIELDS}
    if not (_valid_scope(row["scope"]) and _valid_mode(row["mode"])
            and _valid_pid(row["pid"]) and _valid_host(row["host"])
            and _valid_seen(row["seen"])):
        return None
    if row["scope"] is None and row["mode"] is None:
        return None
    return row


class McpWindowStore:
    """In-memory rows plus the payload last written or loaded.

    Not thread-safe and not locked: every method is sync and runs on the event
    loop. The durable writes are serialised by create_app's lock and helper;
    :meth:`apply` runs under the REGISTRY lock only (the on_register
    contract), which is why it mirrors onto :attr:`inflight`."""

    def __init__(self, rows: Optional[Dict[int, Dict[str, Any]]] = None, *,
                 clock: Callable[[], float] = time.time) -> None:
        self._rows: Dict[int, Dict[str, Any]] = rows if rows is not None else {}
        #: Wall clock for ``seen`` and prune's ``now`` (injectable).
        self.clock = clock
        # The payload on disk as far as this store knows: what load() read
        # (normalised) or the last write committed. A missing or unreadable
        # file counts as the empty payload.
        self._persisted: Dict[str, Any] = _empty_payload()
        #: The working copy of a write in progress, set by create_app's
        #: writer for the duration of its critical section (see reapply()).
        self.inflight: Optional["McpWindowStore"] = None

    # -- load ---------------------------------------------------------------

    @classmethod
    def load(cls, path: Path, *,
             clock: Callable[[], float] = time.time) -> "McpWindowStore":
        """The store persisted at ``path``. PROTECTIVE: a missing, unreadable
        or wrong-schema file boots an empty store, and a malformed row is
        dropped while its valid siblings load. Logs exactly one line. Never
        prunes: a row whose window has not reconnected yet must survive boot.

        WARNING, not the update policy's ERROR: that sidecar must fail CLOSED
        because a damaged file could resurrect a revoked permission, while an
        empty store is exactly what every restart gave before #228 (every
        window back on the broker default), so it loses overrides but grants
        nothing a restart did not."""
        path = Path(path)
        store = cls(clock=clock)
        try:
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except FileNotFoundError:
            LOGGER.info("mcp windows: %s (0 rows)", path)
            return store
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            LOGGER.warning("mcp windows %s is unreadable (%s); starting with "
                           "no per-window rows, and the next change replaces "
                           "the file", path, exc)
            return store
        windows = data.get("windows") if isinstance(data, dict) else None
        if not isinstance(windows, dict):
            LOGGER.warning("mcp windows %s is not a {\"windows\": {...}} "
                           "object; starting with no per-window rows, and the "
                           "next change replaces the file", path)
            return store
        dropped = 0
        for key, raw in windows.items():
            wid = _canonical_id(key)
            row = _parse_row(raw) if wid is not None else None
            if row is None:
                dropped += 1
                continue
            store._rows[wid] = row
        store._persisted = store.to_persist()
        kept = len(store._rows)
        if dropped:
            LOGGER.warning("mcp windows %s has %d malformed row%s (%d kept); "
                           "they are dropped from memory and DELETED from "
                           "disk by the next write", path, dropped,
                           "" if dropped == 1 else "s", kept)
        else:
            LOGGER.info("mcp windows: %s (%d row%s)", path, kept,
                        "" if kept == 1 else "s")
        return store

    # -- reads --------------------------------------------------------------

    def __len__(self) -> int:
        return len(self._rows)

    def get(self, wid: int) -> Optional[Dict[str, Any]]:
        """A COPY of the row for ``wid``, or None; editing it changes
        nothing."""
        row = self._rows.get(int(wid))
        return dict(row) if row is not None else None

    def to_persist(self) -> Dict[str, Any]:
        """The explicit on-disk schema (see the module docstring), rows in
        window-id order. Built field by field, so nothing else a row might
        carry in memory can reach the file."""
        return {"windows": {str(wid): {field: row[field] for field in _FIELDS}
                            for wid, row in sorted(self._rows.items())}}

    @property
    def dirty(self) -> bool:
        """Whether memory differs from the payload last written or loaded.
        Derived by comparing the payloads themselves, never a flag, so it
        cannot disagree with what a write would put on disk."""
        return self.to_persist() != self._persisted

    def known_scopes(self, live_entries: Iterable[Any]) -> List[str]:
        """Sorted, deduplicated union of the scopes on the live windows
        (``entry.mcp_scope``) and on every row. Read-only: it never
        prunes."""
        scopes = {entry.mcp_scope for entry in live_entries}
        scopes.update(row["scope"] for row in self._rows.values())
        scopes.discard(None)
        return sorted(scopes)

    # -- the on_register hook ----------------------------------------------

    def _claims(self, row: Dict[str, Any], entry: Any) -> bool:
        """The row gate: fill each null (wildcard) row field from the hello,
        then ask the ONE shared gate, looked up on the registry module at call
        time. So a pid of 0 never matches, not even for a claim."""
        view = {"host": entry.host if row["host"] is None else row["host"],
                "pid": entry.pid if row["pid"] is None else row["pid"]}
        return _registry.same_producer(view, entry)

    def _touch(self, entry: Any, now: float) -> Optional[Dict[str, Any]]:
        """If this store's row for ``entry`` passes the gate: claim its null
        fields, bump ``seen`` and return it; else None."""
        row = self._rows.get(entry.id)
        if row is None or not self._claims(row, entry):
            return None
        if row["pid"] is None:
            row["pid"] = entry.pid
        if row["host"] is None:
            row["host"] = entry.host
        row["seen"] = now
        return row

    def reapply(self, entry: Any) -> bool:
        """Re-apply the row for ``entry`` if it passes the gate: set the
        entry's scope and RAW mode, claim the row's null fields, bump
        ``seen``. Returns whether a row was applied. Memory-only.

        Also called by writers AFTER the shared writer returns, on
        ``registry.get(id)``: an entry that registered while their write was
        in flight got the pre-write row.

        While a write is in flight the touch is mirrored onto
        :attr:`inflight`, re-gated against THAT copy's row, because the writer
        swaps memory to its copy when the write lands. Unmirrored, the swap
        would silently revert a claimed row to pid null (re-opening the
        id-reuse hole the pid rule exists to close) and leave the store
        clean, so no later write would repair it. The copy's payload was
        serialised before the await, so the touch is not on disk yet and the
        store stays dirty until the next write.

        Two consequences of re-gating against the copy, both accepted:

        * If the in-flight mutate REPLACED a claimed row with a fresh
          pre-spawn row (null pid), the OLD process's hello landing during
          the await claims the new row through the wildcard. Replacing a
          claimed row with a pre-spawn one is the launch writer's (#231)
          territory to rule out.
        * If the mutate deleted the row, or prune dropped it, the touch is
          lost with it."""
        now = self.clock()
        row = self._touch(entry, now)
        if row is None:
            return False
        entry.mcp_scope = row["scope"]
        entry.mcp_mode = row["mode"]
        work = self.inflight
        if work is not None:
            work._touch(entry, now)
        return True

    def apply(self, entry: Any, old: Any) -> None:
        """The registry's ``on_register`` hook (its contract: sync,
        memory-only, no lock of its own). A row that passes the gate wins.
        Otherwise the facts carry over from ``old``, the replaced same-id
        entry, under the same gate register() uses when no hook is installed
        (``registry.same_producer``): with this hook installed, register()
        skips its own carry-over, so this is the broker's only one."""
        if self.reapply(entry):
            return
        if old is not None and _registry.same_producer(old, entry):
            entry.mcp_mode = old.mcp_mode
            entry.mcp_scope = old.mcp_scope

    # -- mutation (inside create_app's writer only) ------------------------

    def set(self, wid: int, *, scope: Any = _UNSET, mode: Any = _UNSET,
            pid: Optional[int] = None, host: Optional[str] = None,
            now: float) -> bool:
        """Write ``scope`` and/or the RAW ``mode`` for the producer
        ``(pid, host)`` on ``wid``, in memory. Returns whether the row
        changed. Call it only on the working copy inside create_app's shared
        writer, which makes it durable.

        * The existing row is merged into when its identity is compatible
          (each of its pid/host is null or equal to the one passed);
          otherwise the write REPLACES it and every field not passed starts
          at None, so a reused id never inherits a stale producer's
          override.
        * The passed pid/host are recorded (None = not spawned yet: the first
          hello from the right producer claims it).
        * ``mode=None`` stores null (inherit). A field not passed is left as
          it was, so a scope-only change never materialises the default mode.
        * A row left with neither a scope nor a mode carries nothing and would
          only block apply()'s fallback carry-over, so it is DELETED whatever
          its pid/host. No reservation-only row is needed: the launch writer
          (#231) always writes a scope.
        * A no-op returns False and leaves the row as it was, ``seen``
          included; a change sets ``seen = now``.

        Raises ValueError on an invalid argument (callers validate first)."""
        if isinstance(wid, bool) or not isinstance(wid, int):
            raise ValueError("bad window id")
        if scope is not _UNSET and not _valid_scope(scope):
            raise ValueError("bad scope")
        if mode is not _UNSET and not _valid_mode(mode):
            raise ValueError("bad mode")
        if not _valid_pid(pid) or not _valid_host(host):
            raise ValueError("bad producer identity")
        if not _valid_seen(now):
            raise ValueError("bad now")
        old = self._rows.get(wid)
        compatible = old is not None and (
            (old["pid"] is None or old["pid"] == pid)
            and (old["host"] is None or old["host"] == host))
        base = old if compatible else {"scope": None, "mode": None}
        new = {"scope": base["scope"] if scope is _UNSET else scope,
               "mode": base["mode"] if mode is _UNSET else mode,
               "pid": pid, "host": host}
        if new["scope"] is None and new["mode"] is None:
            if old is None:
                return False
            del self._rows[wid]
            return True
        if old is not None and all(old[field] == new[field]
                                   for field in ("scope", "mode", "pid",
                                                 "host")):
            return False
        new["seen"] = now
        self._rows[wid] = new
        return True

    def prune(self, live_entries: Iterable[Any], now: float, uptime_s: float,
              is_pending: Optional[Callable[[int], bool]] = None) -> int:
        """Drop expired rows; returns how many went. ONLY ever called from
        inside create_app's shared writer, on its working copy: load(),
        apply() and known_scopes() never prune.

        * A no-op while ``uptime_s < PRUNE_GRACE_S``: right after a restart
          nothing has reconnected yet, so nothing may be judged stale.
        * A row is LIVE when a live entry with its id passes
          ``same_producer(row, entry)`` (an unrelated producer on a reused id
          does not keep an orphan row alive) or when its id is a pending
          launch (``is_pending``), the launcher's own rule in
          ``_prune_continuity``: the waiter exists from before the spawn
          until the hello, so a just-spawned window is never collected out
          from under itself.
        * Rows that are not live and whose ``now - seen`` exceeds
          ``PRUNE_AGE_S`` are dropped. A pre-spawn row is protected by
          RECENCY (it was written seconds ago), not by the grace: a broker
          is past the grace for all but its first ten minutes.
        * Then, while more than ``MAX_ROWS`` remain, the oldest non-live rows
          by ``(seen, wid)`` go. Live rows are never dropped."""
        if uptime_s < PRUNE_GRACE_S:
            return 0
        by_id = {entry.id: entry for entry in live_entries}
        live = set()
        for wid, row in self._rows.items():
            entry = by_id.get(wid)
            if entry is not None and _registry.same_producer(row, entry):
                live.add(wid)
            elif is_pending is not None and is_pending(wid):
                live.add(wid)
        doomed = {wid for wid, row in self._rows.items()
                  if wid not in live and now - row["seen"] > PRUNE_AGE_S}
        excess = len(self._rows) - len(doomed) - MAX_ROWS
        if excess > 0:
            survivors = sorted((row["seen"], wid)
                               for wid, row in self._rows.items()
                               if wid not in live and wid not in doomed)
            doomed.update(wid for _seen, wid in survivors[:excess])
        for wid in doomed:
            del self._rows[wid]
        return len(doomed)

    # -- create_app's writer ------------------------------------------------

    def copy(self) -> "McpWindowStore":
        """A working copy for one write: independent row dicts, the same
        clock and the same last-persisted payload."""
        work = McpWindowStore({wid: dict(row) for wid, row in self._rows.items()},
                              clock=self.clock)
        work._persisted = self._persisted
        return work

    def commit(self, work: "McpWindowStore", payload: Dict[str, Any]) -> None:
        """Adopt ``work``'s rows once ``payload`` (``work.to_persist()`` at
        write time) is on disk, or was already there."""
        self._rows = work._rows
        self._persisted = payload
