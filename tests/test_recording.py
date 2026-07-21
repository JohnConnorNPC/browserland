"""Tests for the #140 terminal-session recording endpoints (/recording/*).

Same driving style as test_file_api.py: single-request cases use the light
app.test_client (full server lifecycle per request); the stateful save flow
(begin/chunk/commit spans requests and lives in-memory on app.ctx) uses
ReusableClient. Every app configures a token explicitly (#142).
"""

import base64
import json
import os
import time

from sanic_testing.reusable import ReusableClient

import webterm.broker.app as app_mod
from .auth_helpers import TEST_TOKEN, authed, authed_reusable
from webterm.broker.app import (_rec_load_notes, _rec_sanitize_meta,
                                _sweep_rec_sessions, create_app)

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


# ---- save flow (stateful; ReusableClient) ----------------------------------

def test_save_list_get_roundtrip(tmp_path, monkeypatch):
    app = _make_rec_app(tmp_path, monkeypatch)
    payload = (json.dumps({"v": 1, "cols": 80}) + "\n").encode() \
        + os.urandom(2 * 2 ** 20)          # >1 chunk at 1 MiB steps
    with authed_reusable(app) as client:
        rec_id = _save_one(client, payload,
                           meta={"title": "demo", "cols": 80, "rows": 24,
                                 "durationMs": 5000, "startedAt": 123})
        # temp .part gone, .blrec + meta sidecar present
        rec_dir = tmp_path / "recs"
        assert not list(rec_dir.glob(".webterm-rec-*.part"))
        assert (rec_dir / f"{rec_id}.blrec").read_bytes() == payload
        meta = json.loads((rec_dir / f"{rec_id}.meta.json").read_text())
        assert meta["title"] == "demo" and meta["size"] == len(payload)
        # list inlines the sidecar meta + size
        _, r = client.get("/recordings")
        assert r.status == 200 and r.json["ok"]
        entry = [e for e in r.json["recordings"] if e["id"] == rec_id][0]
        assert entry["title"] == "demo"
        assert entry["size"] == len(payload)
        assert entry["notesCount"] == 0
        # fetch returns the exact bytes
        _, r = client.get(f"/recording?id={rec_id}")
        assert r.status == 200 and r.body == payload
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
    tmp = tmp_path / ".webterm-rec-x.part"
    tmp.write_bytes(b"partial")
    sessions = {"rec-a": {"tmp": str(tmp), "received": 7,
                          "created": time.time() - 7200}}
    _sweep_rec_sessions(sessions, time.time())
    assert sessions == {} and not tmp.exists()
    # a young session survives
    tmp.write_bytes(b"partial")
    sessions = {"rec-b": {"tmp": str(tmp), "received": 7,
                          "created": time.time()}}
    _sweep_rec_sessions(sessions, time.time())
    assert "rec-b" in sessions and tmp.exists()


def test_committed_recordings_never_swept(tmp_path, monkeypatch):
    # The durability contract: only /recording/delete removes a committed
    # recording — a later begin's sweeps must leave existing .blrec files
    # alone (unlike paste images, which are TTL+count swept).
    app = _make_rec_app(tmp_path, monkeypatch)
    with authed_reusable(app) as client:
        rec_id = _save_one(client, b"keep me\n")
        blrec = tmp_path / "recs" / f"{rec_id}.blrec"
        old = time.time() - 30 * 24 * 3600
        os.utime(blrec, (old, old))            # a month old
        _, r = client.post("/recording/begin", json={})   # runs both sweeps
        assert r.json["ok"]
        assert blrec.exists()
