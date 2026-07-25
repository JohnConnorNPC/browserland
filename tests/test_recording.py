"""Tests for the #140 terminal-session recording endpoints (/recording/*).

Same driving style as test_file_api.py: single-request cases use the light
app.test_client (full server lifecycle per request); the stateful save flow
(begin/chunk/commit spans requests and lives in-memory on app.ctx) uses
ReusableClient. Every app configures a token explicitly (#142).
"""

import asyncio
import base64
import gzip
import json
import os
import random
import threading
import time

from sanic_testing.reusable import ReusableClient

import webterm.broker.app as app_mod
from .auth_helpers import TEST_TOKEN, authed, authed_reusable, with_token
from webterm.broker.app import (_reap_rec_sessions, _rec_load_notes,
                                _rec_sanitize_meta, _unlink_quiet, create_app)

_app_seq = 0


def _make_rec_app(tmp_path, monkeypatch):
    global _app_seq
    _app_seq += 1
    monkeypatch.delenv("WEB_TERMINAL_TOKEN", raising=False)
    cfg = {"auth_token": TEST_TOKEN,
           "editor_root": str(tmp_path),
           "state_path": str(tmp_path / "webterm_state.json"),
           "recordings_dir": str(tmp_path / "recs")}
    return create_app(cfg, name=f"webterm-rec-test-{_app_seq}")


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _save_one(client, payload: bytes, meta=None, step=1 << 20) -> str:
    """Drive a full begin/chunk*N/commit save; returns the recording id."""
    _, r = client.post("/recording/begin", json={})
    assert r.status == 200 and r.json["ok"], r.json
    rec_id = r.json["recording_id"]
    off = 0
    while off < len(payload):
        part = payload[off:off + step]
        _, r = client.post("/recording/chunk",
                           json={"recording_id": rec_id, "offset": off,
                                 "content_b64": _b64(part)})
        assert r.status == 200 and r.json["ok"], r.json
        off += len(part)
    _, r = client.post("/recording/commit",
                       json={"recording_id": rec_id, "meta": meta or {}})
    assert r.status == 200 and r.json["ok"], r.json
    return rec_id


def test_recording_routes_registered(tmp_path, monkeypatch):
    app = _make_rec_app(tmp_path, monkeypatch)
    paths = {"/" + r.path.lstrip("/") for r in app.router.routes}
    for p in ("/recording/begin", "/recording/chunk", "/recording/commit",
              "/recording/abort", "/recordings", "/recording",
              "/recording/delete", "/recording/notes"):
        assert p in paths, f"missing recording route {p}"


# ---- meta sanitizer (pure) -------------------------------------------------

def test_meta_sanitizer_whitelists_and_clamps():
    meta = _rec_sanitize_meta({
        "title": "x" * 500, "cols": 120, "rows": 30.0,
        "startedAt": 1700000000000, "durationMs": 12345,
        "fontFamily": "F" * 500, "fontSize": 14,
        "events": 10, "bytes": 999,
        "junk": "dropped", "nested": {"a": 1},
        "negative": -5,
    })
    assert meta["title"] == "x" * 300
    assert meta["cols"] == 120 and meta["rows"] == 30
    assert meta["fontFamily"] == "F" * 200
    assert "junk" not in meta and "nested" not in meta
    # non-dict / bool-typed numerics degrade instead of raising
    assert _rec_sanitize_meta(None) == {}
    assert "cols" not in _rec_sanitize_meta({"cols": True})
    # NaN/Infinity pass json.loads but must never reach int() (500)
    assert "cols" not in _rec_sanitize_meta({"cols": float("nan")})
    assert "rows" not in _rec_sanitize_meta({"rows": float("inf")})


def test_meta_sanitizer_segment_chain():
    # #151: the segment chain a rolling recording is split into. `series` is an
    # IDENTITY the library groups by, so unlike `title` a malformed one is
    # REJECTED rather than truncated -- clamping a 200-char id to 64 could fold
    # two distinct chains into one. The segment still saves, just unlinked.
    ok = _rec_sanitize_meta({"series": "s1abcd-0011223344ff", "seg": 3})
    assert ok["series"] == "s1abcd-0011223344ff" and ok["seg"] == 3
    assert "series" not in _rec_sanitize_meta({"series": "x" * 65})
    assert "series" not in _rec_sanitize_meta({"series": "bad id"})
    assert "series" not in _rec_sanitize_meta({"series": "../../etc/passwd"})
    assert "series" not in _rec_sanitize_meta({"series": ""})
    assert "series" not in _rec_sanitize_meta({"series": 7})
    # seg is 1-based: 0 is not a position in a chain.
    assert "seg" not in _rec_sanitize_meta({"seg": 0})
    assert "seg" not in _rec_sanitize_meta({"seg": -1})
    assert "seg" not in _rec_sanitize_meta({"seg": True})
    assert "seg" not in _rec_sanitize_meta({"seg": float("nan")})
    assert _rec_sanitize_meta({"seg": 2.0})["seg"] == 2
    # ...and unlike the other numerics, a non-integral seg is REJECTED rather
    # than truncated: int(1.9) == 1 would file a second segment as the first.
    assert "seg" not in _rec_sanitize_meta({"seg": 1.9})


# ---- save flow (stateful; ReusableClient) ----------------------------------

def test_save_list_get_roundtrip(tmp_path, monkeypatch):
    app = _make_rec_app(tmp_path, monkeypatch)
    payload = (json.dumps({"v": 1, "cols": 80}) + "\n").encode() \
        + os.urandom(2 * 2 ** 20)          # >1 chunk at 1 MiB steps
    with authed_reusable(app) as client:
        rec_id = _save_one(client, payload,
                           meta={"title": "demo", "cols": 80, "rows": 24,
                                 "durationMs": 5000, "startedAt": 123})
        # temp .part gone, .blrec.gz + meta sidecar present. #159: the bare
        # .blrec suffix is NEVER written any more -- it only ever means an
        # older, uncompressed recording.
        rec_dir = tmp_path / "recs"
        assert not list(rec_dir.glob(".webterm-rec-*.part"))
        assert not (rec_dir / f"{rec_id}.blrec").exists()
        stored = (rec_dir / f"{rec_id}.blrec.gz").read_bytes()
        assert gzip.decompress(stored) == payload
        meta = json.loads((rec_dir / f"{rec_id}.meta.json").read_text())
        assert meta["title"] == "demo"
        # `size` keeps meaning UNCOMPRESSED bytes across the #159 switch, so a
        # library holding both encodings compares like with like; the on-disk
        # footprint gets its own separately named field.
        assert meta["size"] == len(payload)
        assert meta["enc"] == "gzip"
        # diskSize is the on-disk footprint. This particular payload is
        # os.urandom, i.e. INCOMPRESSIBLE, so it is very slightly LARGER than
        # the source -- deflate's stored-block + per-member header overhead.
        # The compression win is asserted on realistic content instead, in
        # test_realistic_recording_compresses.
        assert meta["diskSize"] == len(stored)
        # list inlines the sidecar meta + both sizes
        _, r = client.get("/recordings")
        assert r.status == 200 and r.json["ok"]
        entry = [e for e in r.json["recordings"] if e["id"] == rec_id][0]
        assert entry["title"] == "demo"
        assert entry["size"] == len(payload)
        assert entry["diskSize"] == len(stored)
        assert entry["enc"] == "gzip"
        assert entry["notesCount"] == 0
        # fetch returns the exact bytes, decoded: the wire format is unchanged
        # by #159, so the recorder mod needed no change at all.
        _, r = client.get(f"/recording?id={rec_id}")
        assert r.status == 200 and r.body == payload
        assert "content-encoding" not in r.headers
        # ...with the download headers pinned. Since the recorder mod switched
        # to a Blob download (it fetches, then anchors off URL.createObjectURL
        # so the auth token never lands in the browser's Downloads list), the
        # CLIENT names the file via `a.download` and no longer reads
        # Content-Disposition -- so nothing in the app would notice if this
        # header rotted. The content type still matters: Response.blob()
        # inherits it, and a wrong one can make the browser open the recording
        # in-tab instead of saving it.
        assert r.headers["content-type"] == "application/octet-stream"
        assert (r.headers["content-disposition"]
                == f'attachment; filename="{rec_id}.blrec"')


def test_list_inlines_segment_chain(tmp_path, monkeypatch):
    # #151: the library groups a rolled recording by (series, seg) and labels
    # "part N/M" from the LIST response, so both keys have to survive the commit
    # sidecar AND the list handler's own key whitelist -- the sanitizer passing
    # them is only half the path.
    app = _make_rec_app(tmp_path, monkeypatch)
    head = (json.dumps({"v": 1, "cols": 80}) + "\n").encode()
    with authed_reusable(app) as client:
        ids = [_save_one(client, head,
                         meta={"title": "long run", "series": "sABC-1234",
                               "seg": seg, "startedAt": 1000 + seg})
               for seg in (1, 2)]
        _, r = client.get("/recordings")
        assert r.status == 200 and r.json["ok"]
        by_id = {e["id"]: e for e in r.json["recordings"]}
        for seg, rec_id in enumerate(ids, start=1):
            assert by_id[rec_id]["series"] == "sABC-1234"
            assert by_id[rec_id]["seg"] == seg
            # the part number is DERIVED from seg, never baked into the title
            assert by_id[rec_id]["title"] == "long run"


def test_chunk_offset_and_session_guards(tmp_path, monkeypatch):
    app = _make_rec_app(tmp_path, monkeypatch)
    with authed_reusable(app) as client:
        _, r = client.post("/recording/begin", json={})
        rec_id = r.json["recording_id"]
        _, r = client.post("/recording/chunk",
                           json={"recording_id": rec_id, "offset": 0,
                                 "content_b64": _b64(b"abc")})
        assert r.json["received"] == 3
        # gap/dup offsets are rejected without appending
        _, r = client.post("/recording/chunk",
                           json={"recording_id": rec_id, "offset": 99,
                                 "content_b64": _b64(b"zzz")})
        assert r.status == 409 and r.json["error"] == "bad_offset"
        assert r.json["received"] == 3
        # unknown session -> 404
        _, r = client.post("/recording/chunk",
                           json={"recording_id": "rec-nope", "offset": 0,
                                 "content_b64": _b64(b"a")})
        assert r.status == 404
        # abort is idempotent cleanup
        _, r = client.post("/recording/abort", json={"recording_id": rec_id})
        assert r.json["ok"] is True
        _, r = client.post("/recording/abort", json={"recording_id": rec_id})
        assert r.json["ok"] is True
        assert not list((tmp_path / "recs").glob(".webterm-rec-*.part"))


def test_recording_size_cap_drops_session(tmp_path, monkeypatch):
    app = _make_rec_app(tmp_path, monkeypatch)
    monkeypatch.setattr(app_mod, "MAX_RECORDING_BYTES", 100)
    with authed_reusable(app) as client:
        _, r = client.post("/recording/begin", json={})
        rec_id = r.json["recording_id"]
        _, r = client.post("/recording/chunk",
                           json={"recording_id": rec_id, "offset": 0,
                                 "content_b64": _b64(b"x" * 101)})
        assert r.status == 400 and r.json["error"] == "too_large"
        # session dropped -> a follow-up chunk finds nothing
        _, r = client.post("/recording/chunk",
                           json={"recording_id": rec_id, "offset": 0,
                                 "content_b64": _b64(b"y")})
        assert r.status == 404


# ---- id validation is the traversal defense --------------------------------

def test_bad_ids_rejected_everywhere(tmp_path, monkeypatch):
    app = _make_rec_app(tmp_path, monkeypatch)
    evil = ("..%2F..%2Fetc%2Fpasswd", "rec-1", "rec-20250101-000000-zzzzzzzz")
    for bad in evil:
        _, r = authed(app).get(f"/recording?id={bad}")
        assert r.status == 400, (bad, r.status)
    _, r = authed(app).post("/recording/delete",
                                json={"id": "../../x"})
    assert r.status == 400
    _, r = authed(app).get("/recording/notes?id=..")
    assert r.status == 400
    # commit refuses an id that isn't a live session (and thus any forged one)
    _, r = authed(app).post("/recording/commit",
                                json={"recording_id": "../../x"})
    assert r.status == 404


def test_get_delete_missing_recording_404(tmp_path, monkeypatch):
    app = _make_rec_app(tmp_path, monkeypatch)
    good_shape = "rec-20250101-000000-0011aabb"
    _, r = authed(app).get(f"/recording?id={good_shape}")
    assert r.status == 404
    _, r = authed(app).post("/recording/delete", json={"id": good_shape})
    assert r.status == 404
    # notes GET mirrors the PUT existence check — an orphan sidecar must not
    # make a deleted recording look note-valid
    _, r = authed(app).get(f"/recording/notes?id={good_shape}")
    assert r.status == 404


def test_delete_removes_file_and_sidecars(tmp_path, monkeypatch):
    app = _make_rec_app(tmp_path, monkeypatch)
    with authed_reusable(app) as client:
        rec_id = _save_one(client, b"data\n", meta={"durationMs": 1000})
        _, r = client.post("/recording/notes",
                           json={"id": rec_id, "baseRev": 0,
                                 "notes": [{"t": 5, "text": "n"}]})
        assert r.json["ok"], r.json
        rec_dir = tmp_path / "recs"
        assert (rec_dir / f"{rec_id}.notes.json").exists()
        _, r = client.post("/recording/delete", json={"id": rec_id})
        assert r.json["ok"] is True
        assert not any(rec_dir.glob(f"{rec_id}*"))


# ---- notes -----------------------------------------------------------------

def test_notes_roundtrip_clamp_and_conflict(tmp_path, monkeypatch):
    app = _make_rec_app(tmp_path, monkeypatch)
    with authed_reusable(app) as client:
        rec_id = _save_one(client, b"x\n", meta={"durationMs": 10_000})
        _, r = client.get(f"/recording/notes?id={rec_id}")
        assert r.json == {"ok": True, "rev": 0, "notes": []}
        # out-of-range times clamp to [0, durationMs]; list comes back sorted
        _, r = client.post("/recording/notes",
                           json={"id": rec_id, "baseRev": 0,
                                 "notes": [{"t": 99_999, "text": "end"},
                                           {"t": -5, "text": "start"}]})
        assert r.json["ok"] and r.json["rev"] == 1
        _, r = client.get(f"/recording/notes?id={rec_id}")
        assert r.json["notes"] == [{"t": 0, "text": "start"},
                                   {"t": 10_000, "text": "end"}]
        # stale baseRev -> 409 with the live value inlined
        _, r = client.post("/recording/notes",
                           json={"id": rec_id, "baseRev": 0,
                                 "notes": []})
        assert r.status == 409 and r.json["error"] == "conflict"
        assert r.json["rev"] == 1 and len(r.json["notes"]) == 2
        # malformed notes -> 400, nothing written
        # (a NaN t is guarded in the handler too, but httpx refuses to encode
        # NaN — the loader-level case below covers the parse path instead)
        for bad in ([{"t": "x", "text": "a"}], [{"t": 1}], ["nope"],
                    [{"t": 1, "text": "y" * 5000}]):
            _, r = client.post("/recording/notes",
                               json={"id": rec_id, "baseRev": 1,
                                     "notes": bad})
            assert r.status == 400, bad
        # notes on a recording that doesn't exist -> 404
        _, r = client.post("/recording/notes",
                           json={"id": "rec-20990101-000000-00000000",
                                 "baseRev": 0, "notes": []})
        assert r.status == 404


def test_notes_corrupt_sidecar_degrades(tmp_path):
    p = tmp_path / "x.notes.json"
    p.write_text("{not json", encoding="utf-8")
    assert _rec_load_notes(p) == {"rev": 0, "notes": []}
    p.write_text(json.dumps({"rev": -3, "notes": "nope"}), encoding="utf-8")
    assert _rec_load_notes(p) == {"rev": 0, "notes": []}
    # NaN survives python json round-trips but must never reach int()
    p.write_text('{"rev": 1, "notes": [{"t": NaN, "text": "x"},'
                 ' {"t": 5, "text": "ok"}]}', encoding="utf-8")
    assert _rec_load_notes(p) == {"rev": 1, "notes": [{"t": 5, "text": "ok"}]}


# ---- sweeps (pure) ---------------------------------------------------------

def test_stale_session_sweep_unlinks_temp(tmp_path):
    # The sweep is split in two: _reap_rec_sessions mutates the (loop-owned)
    # session table and hands back the temps, _unlink_quiet does the blocking
    # unlink in a worker. Driven here exactly as _recording_begin drives it.
    tmp = tmp_path / ".webterm-rec-x.part"
    tmp.write_bytes(b"partial")
    sessions = {"rec-a": {"tmp": str(tmp), "received": 7,
                          "created": time.time() - 7200}}
    _unlink_quiet(_reap_rec_sessions(sessions, time.time()))
    assert sessions == {} and not tmp.exists()
    # a young session survives
    tmp.write_bytes(b"partial")
    sessions = {"rec-b": {"tmp": str(tmp), "received": 7,
                          "created": time.time()}}
    assert _reap_rec_sessions(sessions, time.time()) == []
    assert "rec-b" in sessions and tmp.exists()


def test_recording_chunk_concurrent_same_offset_exactly_one_wins(tmp_path,
                                                                 monkeypatch):
    # /recording/chunk carries the identical exposure /file/upload_chunk does:
    # the append now rides a worker, so without the per-session asyncio.Lock two
    # POSTs at the same offset would both clear the guard and both append. See
    # test_file_api.py's twin for the full reasoning.
    app = _make_rec_app(tmp_path, monkeypatch)
    payload = b"{}\n" * 256
    # Patch _append_chunk_GZ, not _append_chunk: #159 switched this handler to
    # the gzip variant, and for a while this widening patched a function
    # /recording/chunk no longer calls — an inert race-widener that still passed.
    real_append = app_mod._append_chunk_gz

    def slow_append(tmp, data):
        time.sleep(0.25)                       # in the executor, not on the loop
        real_append(tmp, data)

    monkeypatch.setattr(app_mod, "_append_chunk_gz", slow_append)
    with authed_reusable(app) as client:
        _, r = client.post("/recording/begin", json={})
        assert r.status == 200, r.json
        rec_id = r.json["recording_id"]
        body = {"recording_id": rec_id, "offset": 0,
                "content_b64": _b64(payload)}
        url = with_token(
            f"http://{client.host}:{client.port}/recording/chunk",
            app.ctx.auth_token)
        session = client._session

        async def _both():
            return await asyncio.gather(session.post(url, json=body),
                                        session.post(url, json=body))

        first, second = client._loop.run_until_complete(_both())
        codes = sorted((first.status_code, second.status_code))
        assert codes == [200, 409], \
            f"expected exactly one 200 and one 409, got {codes}"
        loser = first if first.status_code == 409 else second
        assert loser.json()["error"] == "bad_offset"
        assert loser.json()["received"] == len(payload)
        _, r = client.post("/recording/commit",
                           json={"recording_id": rec_id, "meta": {}})
        assert r.status == 200 and r.json["size"] == len(payload), r.json
    stored = (tmp_path / "recs" / f"{rec_id}.blrec.gz").read_bytes()
    assert gzip.decompress(stored) == payload


# ---- #160: a cancelled request must not release the lock mid-write ----------
# Sanic cancels the handler task on connection_lost, and a RUNNING executor
# future cannot be cancelled — so an unshielded `async with` unwinds and frees
# the lock while the worker keeps writing. The upload twin of the first test
# lives at test_file_api.py::test_cancelled_chunk_leaves_accounting_and_disk_in_step.


def _cancel_once_entered(client, url, body, entered, settled):
    """POST, cancel the request the moment ``entered`` fires, then wait for the
    shielded task's side effects instead of guessing with a fixed sleep.

    ``settled`` is polled on the loop; the bounded 4 s ceiling only ever runs to
    the end when the behaviour under test is BROKEN, so a correct run is fast.
    """
    async def _drive():
        task = asyncio.ensure_future(client._session.post(url, json=body))
        while not entered.is_set():            # cancel INSIDE the worker hop
            await asyncio.sleep(0.01)
        task.cancel()
        try:
            await task
        except BaseException:      # noqa: BLE001 - cancellation is the point
            pass
        for _ in range(400):
            if settled():
                break
            await asyncio.sleep(0.01)

    client._loop.run_until_complete(_drive())


def test_cancelled_recording_chunk_leaves_accounting_and_disk_in_step(
        tmp_path, monkeypatch):
    """A tab close mid-append must not strand unaccounted bytes on disk.

    Unshielded, the cancel releases the session lock while the worker is still
    inside the gzip append: the member lands but ``received`` stays at the old
    offset. The 409 body hands that stale ``received`` back specifically so a
    client can resume — and a resumed POST at that offset appends the SAME
    member twice. Post-#159 the file is a gzip stream, so the duplicate is
    invisible to every reader, and a genuinely CONCURRENT pair (which the stale
    offset makes possible) interleaves 3-4 raw writes into an invalid deflate
    stream that costs the WHOLE archive, not just the bad chunk.

    The invariant is decompressed length, not ``getsize``: the temp holds
    compressed members while ``received`` counts decoded input bytes.
    """
    app = _make_rec_app(tmp_path, monkeypatch)
    payload = b'{"t":1,"d":"x"}\n' * 32
    real_append = app_mod._append_chunk_gz
    entered = threading.Event()

    def slow_append(tmp, data):
        entered.set()          # the worker is now inside the hop
        time.sleep(0.40)       # ...and stays there long enough to be cancelled
        real_append(tmp, data)

    monkeypatch.setattr(app_mod, "_append_chunk_gz", slow_append)
    with authed_reusable(app) as client:
        _, r = client.post("/recording/begin", json={})
        assert r.status == 200, r.json
        rec_id = r.json["recording_id"]
        tmp = app.ctx.rec_uploads[rec_id]["tmp"]
        url = with_token(
            f"http://{client.host}:{client.port}/recording/chunk",
            app.ctx.auth_token)
        body = {"recording_id": rec_id, "offset": 0,
                "content_b64": _b64(payload)}
        _cancel_once_entered(
            client, url, body, entered,
            lambda: app.ctx.rec_uploads.get(rec_id, {}).get("received"))

        with open(tmp, "rb") as fh:
            on_disk = len(gzip.decompress(fh.read())) if os.path.getsize(tmp) \
                else 0
        session = app.ctx.rec_uploads.get(rec_id)
        accounted = session["received"] if session else None
        assert accounted == on_disk, (
            f"accounting ({accounted}) diverged from disk ({on_disk}) after a "
            "cancelled append: the 409 body would hand a client that stale "
            "offset, and re-POSTing it duplicates the gzip member")


def test_cancelled_recording_commit_publishes_and_pops_together(tmp_path,
                                                                monkeypatch):
    """A recording that reached disk must never leave its session behind.

    The commit body is tiny, so this one is deterministically cancellable — no
    ``pause_reading`` coin flip — which makes it the path a stock tab close
    actually hits. Unshielded, the cancel lands inside the publishing
    ``os.replace``: the worker finishes it, the recording is on disk and listed,
    and the ``rec_uploads.pop`` after the await never runs. The session then
    holds one of only MAX_RECORDING_SESSIONS slots for RECORDING_SESSION_TTL,
    and it is the precondition for the sidecar deletion covered by
    ``test_second_commit_never_strips_a_published_recordings_sidecar``.
    """
    app = _make_rec_app(tmp_path, monkeypatch)
    payload = b'{"t":1}\n' * 16
    real_replace = os.replace
    entered = threading.Event()

    def slow_replace(src, dst, *a, **kw):
        # Gate on the PUBLISH hop only. _write_state_atomic writes the meta
        # sidecar through its own os.replace first, and widening THAT reproduces
        # a different (benign, by-design) case instead.
        if not str(dst).endswith(".blrec.gz"):
            return real_replace(src, dst, *a, **kw)
        entered.set()
        time.sleep(0.40)
        return real_replace(src, dst, *a, **kw)

    monkeypatch.setattr(os, "replace", slow_replace)
    with authed_reusable(app) as client:
        _, r = client.post("/recording/begin", json={})
        assert r.status == 200, r.json
        rec_id = r.json["recording_id"]
        _, r = client.post("/recording/chunk",
                           json={"recording_id": rec_id, "offset": 0,
                                 "content_b64": _b64(payload)})
        assert r.status == 200, r.json
        url = with_token(
            f"http://{client.host}:{client.port}/recording/commit",
            app.ctx.auth_token)
        _cancel_once_entered(
            client, url, {"recording_id": rec_id, "meta": {"title": "t"}},
            entered, lambda: rec_id not in app.ctx.rec_uploads)

        blrec = tmp_path / "recs" / f"{rec_id}.blrec.gz"
        # Both assertions matter, and they must be read INSIDE the client
        # context: _drain_rec_sessions clears rec_uploads at server stop, which
        # would forge a pass.
        assert blrec.exists(), \
            "the shielded replace should still have published the recording"
        assert rec_id not in app.ctx.rec_uploads, (
            "the recording is published and listed but its session leaked: it "
            "holds a save slot for RECORDING_SESSION_TTL and arms the "
            "destructive second-commit cleanup")
    assert gzip.decompress(blrec.read_bytes()) == payload


def test_second_commit_never_strips_a_published_recordings_sidecar(tmp_path,
                                                                   monkeypatch):
    """Commit cleanup must never delete metadata for a recording on disk.

    Defence in depth behind the shield, so the precondition is FABRICATED here:
    a live session whose temp is already gone, which is exactly what a commit
    that published and then leaked leaves behind. ``os.path.getsize(tmp)`` then
    raises FileNotFoundError and the cleanup used to unlink ``[tmp, meta_path]``
    — stripping size/title/savedAt off a recording that stays listed and
    downloadable. ``/recording/delete`` is the only path allowed to remove a
    published recording's files.
    """
    app = _make_rec_app(tmp_path, monkeypatch)
    with authed_reusable(app) as client:
        rec_id = _save_one(client, b'{"t":1}\n' * 8, meta={"title": "keepme"})
        meta_path = tmp_path / "recs" / f"{rec_id}.meta.json"
        blrec = tmp_path / "recs" / f"{rec_id}.blrec.gz"
        assert meta_path.exists() and blrec.exists()
        # The leaked session a cancelled-mid-replace commit would leave.
        app.ctx.rec_uploads[rec_id] = {
            "tmp": str(tmp_path / "recs" / ".webterm-rec-gone.part"),
            "received": 0, "created": time.time(), "lock": asyncio.Lock(),
        }
        _, r = client.post("/recording/commit",
                           json={"recording_id": rec_id, "meta": {}})
        assert r.status == 400, r.json          # the temp really is gone
        assert blrec.exists(), "cleanup must not touch the published events file"
        assert meta_path.exists(), (
            "cleanup deleted the sidecar of a published, listed recording — it "
            "stays downloadable but loses its size/title/savedAt")
        assert json.loads(meta_path.read_text())["title"] == "keepme"
        _, r = client.get("/recordings")
        entry = next(e for e in r.json["recordings"] if e["id"] == rec_id)
        assert entry["title"] == "keepme"


def test_committed_recordings_never_swept(tmp_path, monkeypatch):
    # The durability contract: only /recording/delete removes a committed
    # recording — a later begin's sweeps must leave existing .blrec files
    # alone (unlike paste images, which are TTL+count swept).
    app = _make_rec_app(tmp_path, monkeypatch)
    with authed_reusable(app) as client:
        rec_id = _save_one(client, b"keep me\n")
        blrec = tmp_path / "recs" / f"{rec_id}.blrec.gz"
        old = time.time() - 30 * 24 * 3600
        os.utime(blrec, (old, old))            # a month old
        _, r = client.post("/recording/begin", json={})   # runs both sweeps
        assert r.json["ok"]
        assert blrec.exists()


# ---- #159: gzip on disk, both encodings readable ---------------------------

def _realistic_blrec(target_bytes=3 << 20) -> bytes:
    """A recording that looks like a terminal session rather than noise.

    The compression claim in #159 is about REAL captures -- spinner redraws,
    coloured log lines, full-screen repaints -- so asserting a ratio against
    os.urandom would prove nothing, and asserting it against a single repeated
    byte would prove too much."""
    rng = random.Random(159)
    words = "building compiling linking test passed ok error warning".split()
    out = [json.dumps({"v": 1, "cols": 120, "rows": 40, "title": "demo"})]
    t = 0
    total = 0
    i = 0
    while total < target_bytes:
        r = rng.random()
        if r < 0.45:                                    # spinner redraw
            payload = ("\r\x1b[K" + "|/-\\"[i % 4] + " working "
                       + str(i % 100) + "%").encode()
        elif r < 0.8:                                   # coloured log line
            payload = ("\x1b[32m[ok]\x1b[0m "
                       + " ".join(rng.choice(words) for _ in range(8))
                       + "\r\n").encode()
        else:                                           # full repaint
            payload = b"".join(
                ("\x1b[%d;1H\x1b[K" % row
                 + " ".join(rng.choice(words) for _ in range(10))).encode()
                for row in range(1, 41))
        total += len(payload)
        t += rng.randint(5, 90)
        out.append(json.dumps({"t": t, "k": "o", "d": _b64(payload)}))
        i += 1
    return ("\n".join(out) + "\n").encode()


def _plant_raw_recording(rec_dir, rec_id, payload, meta=None):
    """Write a pre-#159 UNCOMPRESSED recording the way the old broker did.

    Deliberately not driven through the API: the API cannot produce one any
    more, and the point of these cases is that files already sitting on a
    user's disk keep working."""
    rec_dir.mkdir(parents=True, exist_ok=True)
    (rec_dir / (rec_id + ".blrec")).write_bytes(payload)
    side = {"title": "old one", "cols": 80, "rows": 24, "size": len(payload),
            "startedAt": 1000, "durationMs": 4000}
    side.update(meta or {})
    (rec_dir / (rec_id + ".meta.json")).write_text(json.dumps(side),
                                                  encoding="utf-8")
    return rec_dir / (rec_id + ".blrec")


OLD_ID = "rec-20250101-010101-deadbeef"


def test_realistic_recording_compresses(tmp_path, monkeypatch):
    # The acceptance bar from #159: a realistic recording is >=5x smaller on
    # disk. Anything less and the feature is not worth its complexity.
    app = _make_rec_app(tmp_path, monkeypatch)
    payload = _realistic_blrec()
    with authed_reusable(app) as client:
        rec_id = _save_one(client, payload, step=2 << 20)
        stored = (tmp_path / "recs" / (rec_id + ".blrec.gz")).read_bytes()
        ratio = len(payload) / len(stored)
        assert ratio >= 5, "only %.1fx smaller on disk" % ratio
        # ...and it is still the same recording, byte for byte.
        assert gzip.decompress(stored) == payload
        _, r = client.get("/recording?id=" + rec_id)
        assert r.status == 200 and r.body == payload


def test_chunk_boundaries_do_not_break_the_stream(tmp_path, monkeypatch):
    # Each chunk lands as its own gzip MEMBER, so a multi-chunk save produces a
    # multi-member file. That is a valid gzip stream (RFC 1952 2.2) and every
    # reader must see the WHOLE thing, not just the first member.
    app = _make_rec_app(tmp_path, monkeypatch)
    payload = _realistic_blrec(target_bytes=1 << 20)
    with authed_reusable(app) as client:
        rec_id = _save_one(client, payload, step=64 << 10)   # many members
        stored = (tmp_path / "recs" / (rec_id + ".blrec.gz")).read_bytes()
        assert stored.count(b"\x1f\x8b\x08") > 5, "expected several members"
        assert gzip.decompress(stored) == payload
        _, r = client.get("/recording?id=" + rec_id)
        assert r.body == payload
        # The member headers must not carry the session temp's path (GzipFile
        # takes FNAME from fileobj.name unless told otherwise) -- that would
        # leak the recordings dir into every downloaded file.
        assert b".webterm-rec-" not in stored


def test_empty_chunk_adds_no_member(tmp_path, monkeypatch):
    # A zero-length chunk is a legal no-op on the raw path; it must not append
    # an empty gzip member either.
    app = _make_rec_app(tmp_path, monkeypatch)
    with authed_reusable(app) as client:
        _, r = client.post("/recording/begin", json={})
        rec_id = r.json["recording_id"]
        _, r = client.post("/recording/chunk",
                           json={"recording_id": rec_id, "offset": 0,
                                 "content_b64": ""})
        assert r.status == 200 and r.json["received"] == 0
        _, r = client.post("/recording/chunk",
                           json={"recording_id": rec_id, "offset": 0,
                                 "content_b64": _b64(b"hi\n")})
        assert r.status == 200
        _, r = client.post("/recording/commit",
                           json={"recording_id": rec_id, "meta": {}})
        assert r.status == 200
        stored = (tmp_path / "recs" / (rec_id + ".blrec.gz")).read_bytes()
        assert gzip.decompress(stored) == b"hi\n"
        assert stored.count(b"\x1f\x8b\x08") == 1


def test_old_uncompressed_recording_still_works(tmp_path, monkeypatch):
    # Existing .blrec files are durable user data that nothing rewrites. They
    # must still list, play (get), annotate and delete.
    app = _make_rec_app(tmp_path, monkeypatch)
    payload = (json.dumps({"v": 1, "cols": 80}) + "\n").encode() \
        + b'{"t":1,"k":"o","d":"aGVsbG8="}\n'
    blrec = _plant_raw_recording(tmp_path / "recs", OLD_ID, payload)
    client = authed(app)
    _, r = client.get("/recordings")
    entry = [e for e in r.json["recordings"] if e["id"] == OLD_ID][0]
    assert entry["title"] == "old one"
    assert entry["size"] == len(payload)      # stat IS the true size here
    assert entry["diskSize"] == len(payload)
    assert "enc" not in entry                 # raw: no encoding claimed
    # get returns it verbatim, with no Content-Encoding to inflate
    _, r = client.get("/recording?id=" + OLD_ID)
    assert r.status == 200 and r.body == payload
    assert "content-encoding" not in r.headers
    assert (r.headers["content-disposition"]
            == 'attachment; filename="%s.blrec"' % OLD_ID)
    # annotate
    _, r = client.post("/recording/notes",
                       json={"id": OLD_ID, "baseRev": 0,
                             "notes": [{"t": 1, "text": "still here"}]})
    assert r.status == 200 and r.json["rev"] == 1
    _, r = client.get("/recording/notes?id=" + OLD_ID)
    assert r.json["notes"] == [{"t": 1, "text": "still here"}]
    # delete
    _, r = client.post("/recording/delete", json={"id": OLD_ID})
    assert r.status == 200 and r.json["ok"]
    assert not blrec.exists()
    assert not (tmp_path / "recs" / (OLD_ID + ".meta.json")).exists()


def test_get_never_serves_gzip_on_the_wire(tmp_path, monkeypatch):
    """The multi-member trap, pinned so nobody optimizes it back in.

    Chunks are stored as separate gzip MEMBERS. That is valid gzip, and Python
    reads it transparently -- but HTTP client decoders commonly wrap a bare
    `zlib.decompressobj(wbits=31)`, which stops after the FIRST member and
    reports success. httpx does exactly that (asserted below against the real
    decoder). Passing the stored bytes through under Content-Encoding: gzip
    would therefore hand such a client a silently truncated recording, so the
    endpoint always decodes, whatever the client says it accepts."""
    from httpx._decoders import GZipDecoder
    two_members = gzip.compress(b"first-", 6) + gzip.compress(b"second", 6)
    dec = GZipDecoder()
    assert dec.decode(two_members) + dec.flush() == b"first-",         "if this ever passes the whole file, revisit the pass-through"
    assert gzip.decompress(two_members) == b"first-second"   # Python is fine

    app = _make_rec_app(tmp_path, monkeypatch)
    payload = _realistic_blrec(target_bytes=256 << 10)
    with authed_reusable(app) as client:
        rec_id = _save_one(client, payload, step=32 << 10)    # many members
        for header in ("gzip, deflate", "identity", "*", "gzip;q=0", ""):
            _, r = client.get("/recording?id=" + rec_id,
                              headers={"Accept-Encoding": header})
            assert r.status == 200, header
            assert "content-encoding" not in r.headers, header
            assert r.body == payload, header


def test_get_reports_a_corrupt_recording(tmp_path, monkeypatch):
    # A truncated .blrec.gz must be a clean 400, not a 500: a damaged archive
    # is a thing that happens, and the player surfaces the error.
    app = _make_rec_app(tmp_path, monkeypatch)
    rec_dir = tmp_path / "recs"
    rec_dir.mkdir(parents=True, exist_ok=True)
    good = gzip.compress(b'{"v":1}\n' + b"payload " * 200, 6)
    (rec_dir / (OLD_ID + ".blrec.gz")).write_bytes(good[:len(good) // 2])
    client = authed(app)
    # `identity` forces the server-side inflate, which is where a damaged
    # file actually surfaces.
    _, r = client.get("/recording?id=" + OLD_ID,
                      headers={"Accept-Encoding": "identity"})
    assert r.status == 400 and not r.json["ok"]


def test_gzip_wins_when_both_encodings_exist(tmp_path, monkeypatch):
    # Ids carry 4 random bytes so this essentially cannot happen on its own,
    # but a user can copy files into the recordings dir. The tie must break the
    # SAME way in list, get and delete, or the three disagree about what the
    # recording is.
    app = _make_rec_app(tmp_path, monkeypatch)
    rec_dir = tmp_path / "recs"
    _plant_raw_recording(rec_dir, OLD_ID, b'{"v":1}\nOLD\n')
    (rec_dir / (OLD_ID + ".blrec.gz")).write_bytes(
        gzip.compress(b'{"v":1}\nNEW\n'))
    client = authed(app)
    _, r = client.get("/recordings")
    hits = [e for e in r.json["recordings"] if e["id"] == OLD_ID]
    assert len(hits) == 1, "one id must list once"
    assert hits[0]["enc"] == "gzip"
    _, r = client.get("/recording?id=" + OLD_ID,
                      headers={"Accept-Encoding": "identity"})
    assert r.body == b'{"v":1}\nNEW\n'
    # ...and delete takes BOTH, so the loser cannot reappear next listing.
    _, r = client.post("/recording/delete", json={"id": OLD_ID})
    assert r.status == 200
    assert not (rec_dir / (OLD_ID + ".blrec")).exists()
    assert not (rec_dir / (OLD_ID + ".blrec.gz")).exists()


def test_mixed_encoding_series_lists_together(tmp_path, monkeypatch):
    # #151 segment chains can cross a broker restart, so one series can hold
    # both encodings. Nothing may assume a series is uniform.
    app = _make_rec_app(tmp_path, monkeypatch)
    rec_dir = tmp_path / "recs"
    _plant_raw_recording(rec_dir, OLD_ID, b'{"v":1}\npart one\n',
                         meta={"series": "s-abc", "seg": 1})
    payload = (json.dumps({"v": 1}) + "\n").encode() + b"part two\n"
    with authed_reusable(app) as client:
        new_id = _save_one(client, payload,
                           meta={"series": "s-abc", "seg": 2})
        _, r = client.get("/recordings")
        chain = {e["id"]: e for e in r.json["recordings"]
                 if e.get("series") == "s-abc"}
        assert set(chain) == {OLD_ID, new_id}
        assert [chain[OLD_ID]["seg"], chain[new_id]["seg"]] == [1, 2]
        assert "enc" not in chain[OLD_ID] and chain[new_id]["enc"] == "gzip"


def test_size_absent_rather_than_wrong_without_a_sidecar(tmp_path, monkeypatch):
    # stat() on a .blrec.gz is the COMPRESSED length. Reporting it under
    # `size` -- which means uncompressed bytes -- would understate every
    # recording whose sidecar was lost. Absent beats confidently wrong.
    app = _make_rec_app(tmp_path, monkeypatch)
    rec_dir = tmp_path / "recs"
    rec_dir.mkdir(parents=True, exist_ok=True)
    blob = gzip.compress(b'{"v":1}\n' + b"x" * 5000, 6)
    (rec_dir / (OLD_ID + ".blrec.gz")).write_bytes(blob)
    client = authed(app)
    _, r = client.get("/recordings")
    entry = [e for e in r.json["recordings"] if e["id"] == OLD_ID][0]
    assert "size" not in entry
    assert entry["diskSize"] == len(blob)


def test_byte_cap_still_counts_input_bytes(tmp_path, monkeypatch):
    # MAX_RECORDING_BYTES is checked against DECODED INPUT bytes, before
    # compression, so the ceiling did not move: a payload that compresses 100x
    # is still rejected at the threshold it always was.
    app = _make_rec_app(tmp_path, monkeypatch)
    monkeypatch.setattr(app_mod, "MAX_RECORDING_BYTES", 4096)
    with authed_reusable(app) as client:
        _, r = client.post("/recording/begin", json={})
        rec_id = r.json["recording_id"]
        blob = b"a" * 5000                       # ~30 bytes once compressed
        _, r = client.post("/recording/chunk",
                           json={"recording_id": rec_id, "offset": 0,
                                 "content_b64": _b64(blob)})
        assert r.status == 400 and r.json["error"] == "too_large"
