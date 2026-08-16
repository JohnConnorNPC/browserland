"""/mod-store's sticky per-record no-history flag (#192).

The revision ring (#124) is a feature — scratchpad's History panel is built on
it — right up until the value is a credential. host-registry's opt-in
"passwords" mode publishes broker tokens through this store, so before this
flag every ordinary republish filed the PREVIOUS token-bearing value into a
50-deep, on-disk, readable-whole ring: a second, older copy of the keys to the
fleet that the current value no longer shows (#175's finding — the ring keeps
the plaintext you just encrypted).

``noHistory`` is therefore per-RECORD and STICKY, not per-write like
``purgeRevisions`` (#65): a per-write-only flag re-leaks on the first write
that forgets it (a rebase retry, an older mod build, a different code path),
which is exactly the class of bug being closed. What these pin:

* a flagged PUT pushes nothing and CLEARS the existing ring, observably — as a
  new rev, because ``?rev=<old>`` flipping 200->404 is a mutation and never an
  in-place edit of the current rev;
* the flag survives later writes that never mention it, and a restart;
* un-flagging takes ``noHistory:false`` AND ``purgeRevisions:true`` in ONE
  body, so resuming the archiving of a credential-bearing record can never be
  a side effect;
* the no-op dedupe treats "flag newly set, ring non-empty" as a REAL write —
  get that wrong and the write meant to STOP the archiving is the one that
  leaves the accumulated plaintext readable;
* the PUT response and GET both echo the flag (the backstop behind the /info
  capability gate), and /info advertises ``modstore.noHistory`` so a client can
  feature-detect BEFORE the first password-bearing write rather than probing
  with one;
* unflagged records — scratchpad above all — behave exactly as they did.

The disk assertions read the SIDECAR BYTES, deliberately: "the API no longer
shows it" is not the property under test, "it is not on disk" is.

Route tests use the in-process Sanic test client (each create_app needs a
UNIQUE name) with state_path in a tmp dir, so no sidecar ever lands in the
repo.
"""

from __future__ import annotations

import json

from .auth_helpers import TEST_TOKEN, authed
from webterm.broker.app import _load_modstore, create_app

SIDECAR = "webterm_modstore.json"

#: Distinguishable stand-ins for the thing this feature exists for: a broker
#: token host-registry publishes through /mod-store.
OLD_TOKEN = "tok-OLD-1111111111111111"
NEW_TOKEN = "tok-NEW-2222222222222222"

_app_seq = 0


def _make_app(tmp_path, monkeypatch, **cfg):
    global _app_seq
    _app_seq += 1
    monkeypatch.delenv("WEB_TERMINAL_TOKEN", raising=False)
    conf = {"state_path": str(tmp_path / "webterm_state.json"),
            "auth_token": TEST_TOKEN}
    conf.update(cfg)
    return create_app(conf, name=f"webterm-modstore-nohistory-{_app_seq}")


def _put(client, mod, base_rev, value, **opts):
    body = {"baseRev": base_rev, "value": value}
    body.update(opts)
    _, response = client.put(f"/mod-store/{mod}", json=body)
    return response


def _get(client, mod, rev=None):
    url = f"/mod-store/{mod}" if rev is None else f"/mod-store/{mod}?rev={rev}"
    _, response = client.get(url)
    return response


def _sidecar(tmp_path):
    """The on-disk store, as raw text AND parsed — every history assertion here
    is made against the FILE, never inferred from the API."""
    raw = (tmp_path / SIDECAR).read_text(encoding="utf-8")
    return raw, json.loads(raw)


def _seed_ring(client, mod, values):
    """Write `values` in order, returning the final rev. Each distinct write
    files its predecessor onto the ring."""
    rev = 0
    for val in values:
        response = _put(client, mod, rev, val)
        assert response.status == 200, response.json
        rev = response.json["rev"]
    return rev


# ---- the flagged write: no push, and the existing ring is CLEARED ----------

def test_flagged_put_clears_the_ring_as_a_new_rev(tmp_path, monkeypatch):
    app = _make_app(tmp_path, monkeypatch)
    client = authed(app)
    mod = "host-registry"
    rev = _seed_ring(client, mod, [{"token": OLD_TOKEN, "n": i}
                                   for i in range(3)])
    assert rev == 3
    assert [e["rev"] for e in _get(client, mod).json["revisions"]] == [2, 1]
    # Every ringed rev is readable whole right now — the hazard, stated.
    assert _get(client, mod, rev=1).json["value"]["token"] == OLD_TOKEN

    response = _put(client, mod, rev, {"token": NEW_TOKEN},
                    noHistory=True)
    assert response.status == 200
    # A new rev, NOT an in-place edit: dropping history is observable.
    assert response.json["rev"] == 4
    body = _get(client, mod).json
    assert body["rev"] == 4 and body["value"] == {"token": NEW_TOKEN}
    assert body["revisions"] == []          # no growth, and nothing left
    for old in (1, 2, 3):
        gone = _get(client, mod, rev=old)
        assert gone.status == 404, old
        assert gone.json["error"] == "no_such_rev"
    # The current rev still resolves via ?rev= (the flag kills HISTORY, not the
    # record).
    assert _get(client, mod, rev=4).json["value"] == {"token": NEW_TOKEN}


def test_flagged_write_leaves_no_prior_token_in_the_sidecar(tmp_path,
                                                            monkeypatch):
    """The property, read off the DISK: after a flagged write the sidecar holds
    no prior token-bearing revision. The pre-flag assertion is deliberate — a
    test that only checked the "after" could pass against a store that never
    archived anything."""
    app = _make_app(tmp_path, monkeypatch)
    client = authed(app)
    mod = "host-registry"
    rev = _seed_ring(client, mod, [{"token": OLD_TOKEN},
                                   {"token": OLD_TOKEN, "extra": 1}])
    raw, store = _sidecar(tmp_path)
    assert OLD_TOKEN in raw                       # ...it really is on disk
    assert store[mod]["revisions"], "precondition: a ring exists to clear"

    response = _put(client, mod, rev, {"token": NEW_TOKEN}, noHistory=True)
    assert response.status == 200
    raw, store = _sidecar(tmp_path)
    assert OLD_TOKEN not in raw                   # ...and now it is not
    assert NEW_TOKEN in raw                       # (the current value stays)
    assert store[mod]["revisions"] == []
    assert store[mod]["noHistory"] is True

    # And it STAYS gone across later writes: no ring ever re-forms on disk.
    rev = response.json["rev"]
    for i in range(3):
        response = _put(client, mod, rev, {"token": NEW_TOKEN, "n": i})
        assert response.status == 200
        rev = response.json["rev"]
    raw, store = _sidecar(tmp_path)
    assert store[mod]["revisions"] == []
    assert raw.count(NEW_TOKEN) == 1              # exactly one copy: current


# ---- stickiness ------------------------------------------------------------

def test_flag_is_sticky_across_writes_that_never_mention_it(tmp_path,
                                                            monkeypatch):
    """An ABSENT noHistory means "leave the record as it is" — the third state.
    A later write that simply forgets the option must NOT re-enable archiving;
    that forgetful write is the whole failure mode a per-write flag has."""
    app = _make_app(tmp_path, monkeypatch)
    client = authed(app)
    mod = "host-registry"
    response = _put(client, mod, 0, {"token": OLD_TOKEN}, noHistory=True)
    assert response.status == 200 and response.json["noHistory"] is True
    rev = response.json["rev"]
    for i in range(4):
        response = _put(client, mod, rev, {"token": NEW_TOKEN, "n": i})
        assert response.status == 200
        assert response.json["noHistory"] is True   # still flagged, unasked
        rev = response.json["rev"]
        body = _get(client, mod).json
        assert body["revisions"] == [], f"ring re-formed on write {i}"
        assert body["noHistory"] is True
    _, store = _sidecar(tmp_path)
    assert store[mod]["noHistory"] is True and store[mod]["revisions"] == []


def test_flag_survives_a_broker_restart(tmp_path, monkeypatch):
    app = _make_app(tmp_path, monkeypatch)
    response = _put(authed(app), "host-registry", 0, {"token": OLD_TOKEN},
                    noHistory=True)
    assert response.status == 200 and response.json["rev"] == 1

    # A "restart": a fresh app over the same state dir reads the sidecar.
    again = _make_app(tmp_path, monkeypatch)
    assert again.ctx.modstore["host-registry"]["noHistory"] is True
    client = authed(again)
    assert _get(client, "host-registry").json["noHistory"] is True
    # ...and the flag still GOVERNS after the round trip, which is the point:
    # a restored flag that no longer suppresses the ring would be decoration.
    response = _put(client, "host-registry", 1, {"token": NEW_TOKEN})
    assert response.status == 200 and response.json["noHistory"] is True
    assert _get(client, "host-registry").json["revisions"] == []
    _, store = _sidecar(tmp_path)
    assert store["host-registry"]["revisions"] == []


# ---- un-flagging is one-way and explicit -----------------------------------

def test_unflagging_needs_both_keys_in_the_same_body(tmp_path, monkeypatch):
    app = _make_app(tmp_path, monkeypatch)
    client = authed(app)
    mod = "host-registry"
    assert _put(client, mod, 0, {"token": OLD_TOKEN},
                noHistory=True).status == 200

    # noHistory:false ALONE is refused — 400, not 409 (a 409 means "rebase and
    # retry" to this route's clients, and the retry would be refused again).
    refused = _put(client, mod, 1, {"token": NEW_TOKEN}, noHistory=False)
    assert refused.status == 400
    assert refused.json["error"] == "noHistory_locked"
    assert refused.json["noHistory"] is True     # echoed, so no follow-up GET
    # Nothing moved: not the rev, not the value, not the flag.
    body = _get(client, mod).json
    assert body["rev"] == 1 and body["value"] == {"token": OLD_TOKEN}
    assert body["noHistory"] is True

    # purgeRevisions:true alone does not un-flag either (it is #65's per-write
    # scrub; it says nothing about the record's policy).
    response = _put(client, mod, 1, {"token": NEW_TOKEN}, purgeRevisions=True)
    assert response.status == 200 and response.json["noHistory"] is True

    # Both keys, one body: accepted, and archiving resumes from EMPTY.
    response = _put(client, mod, 2, {"note": "no longer secret"},
                    noHistory=False, purgeRevisions=True)
    assert response.status == 200 and response.json["noHistory"] is False
    body = _get(client, mod).json
    assert body["noHistory"] is False and body["revisions"] == []
    _, store = _sidecar(tmp_path)
    # Un-flagged records return to the pre-#192 on-disk shape exactly — no
    # noHistory:false marker left behind.
    assert "noHistory" not in store[mod]
    # The ring works again from here.
    response = _put(client, mod, body["rev"], {"note": "public"})
    assert response.status == 200 and response.json["noHistory"] is False
    assert [e["rev"] for e in _get(client, mod).json["revisions"]] == \
        [body["rev"]]


def test_redundant_no_history_false_on_an_unflagged_record_is_fine(tmp_path,
                                                                   monkeypatch):
    """Only a real TRANSITION is refused. A client that always passes the
    option through (noHistory:false on every ordinary write) keeps working."""
    app = _make_app(tmp_path, monkeypatch)
    client = authed(app)
    mod = "scratchpad"
    response = _put(client, mod, 0, {"tabs": ["a"]}, noHistory=False)
    assert response.status == 200 and response.json["noHistory"] is False
    response = _put(client, mod, 1, {"tabs": ["a", "b"]}, noHistory=False)
    assert response.status == 200
    assert [e["rev"] for e in _get(client, mod).json["revisions"]] == [1]


# ---- the dedupe trap -------------------------------------------------------

def test_setting_the_flag_on_an_unchanged_value_is_a_real_write(tmp_path,
                                                                monkeypatch):
    """THE bug this issue names: the no-op dedupe must treat "flag newly set,
    ring non-empty" as a real write. Dedupe it and the PUT that was supposed to
    stop the archiving silently leaves 50 revisions of plaintext readable."""
    app = _make_app(tmp_path, monkeypatch)
    client = authed(app)
    mod = "host-registry"
    current = {"token": OLD_TOKEN, "n": 1}
    rev = _seed_ring(client, mod, [{"token": OLD_TOKEN, "n": 0}, current])
    assert rev == 2
    assert _get(client, mod, rev=1).status == 200

    # Same value, only the flag is new.
    response = _put(client, mod, rev, current, noHistory=True)
    assert response.status == 200
    assert response.json["rev"] == 3            # bumped, not mutated in place
    assert response.json["noHistory"] is True
    body = _get(client, mod).json
    assert body["revisions"] == [] and body["value"] == current
    assert _get(client, mod, rev=1).status == 404
    _, store = _sidecar(tmp_path)
    assert store[mod]["revisions"] == []


def test_flag_only_change_with_an_empty_ring_still_bumps_rev(tmp_path,
                                                             monkeypatch):
    """The flag flipping is state a client can SEE (the echo, and every later
    write's behavior), so it lands as a new rev even with nothing to clear —
    otherwise the flag would not be stored at all and stickiness would be a
    lie."""
    app = _make_app(tmp_path, monkeypatch)
    client = authed(app)
    mod = "host-registry"
    value = {"token": OLD_TOKEN}
    assert _put(client, mod, 0, value).json["rev"] == 1   # first write: no ring
    assert _get(client, mod).json["revisions"] == []
    response = _put(client, mod, 1, value, noHistory=True)
    assert response.status == 200 and response.json["rev"] == 2
    assert _get(client, mod).json["noHistory"] is True
    _, store = _sidecar(tmp_path)
    assert store[mod]["noHistory"] is True


def test_a_flagged_resend_is_still_a_true_noop(tmp_path, monkeypatch):
    """The other side of the trap: once the flag is set and the ring is empty,
    an idle autosave resending the current value must NOT churn rev (the
    dedupe's original job survives)."""
    app = _make_app(tmp_path, monkeypatch)
    client = authed(app)
    mod = "host-registry"
    value = {"token": OLD_TOKEN}
    assert _put(client, mod, 0, value, noHistory=True).json["rev"] == 1
    for _ in range(3):
        response = _put(client, mod, 1, value, noHistory=True)
        assert response.status == 200 and response.json["rev"] == 1
        assert response.json["noHistory"] is True
    # ...and the same resend without the option is equally a no-op.
    response = _put(client, mod, 1, value)
    assert response.status == 200 and response.json["rev"] == 1
    assert response.json["noHistory"] is True


# ---- validation ------------------------------------------------------------

def test_no_history_is_a_strict_bool(tmp_path, monkeypatch):
    """bool is an int subclass in Python: a truthy 1 / "true" / {} must not
    sneak a policy change through, mirroring purgeRevisions' guard."""
    app = _make_app(tmp_path, monkeypatch)
    client = authed(app)
    mod = "host-registry"
    assert _put(client, mod, 0, {"token": OLD_TOKEN}).status == 200
    for junk in (1, 0, "true", "false", {}, [], "yes"):
        response = _put(client, mod, 1, {"token": NEW_TOKEN}, noHistory=junk)
        assert response.status == 400, junk
        assert response.json["error"] == "bad_noHistory", junk
    # Validation happens BEFORE the lock/write: nothing moved.
    body = _get(client, mod).json
    assert body["rev"] == 1 and body["value"] == {"token": OLD_TOKEN}
    assert body["noHistory"] is False
    # null is not a bool either — an explicit null must not read as "false" and
    # trip the un-flag path by accident.
    response = _put(client, mod, 1, {"token": NEW_TOKEN}, noHistory=None)
    assert response.status == 200            # absent-equivalent: no-op on flag
    assert response.json["noHistory"] is False


# ---- the echo + the /info capability gate ----------------------------------

def test_put_response_and_get_both_echo_the_flag(tmp_path, monkeypatch):
    """The backstop behind the /info gate: a client reads "this broker is still
    archiving" from the key's ABSENCE, which only works if a build that HAS the
    flag never omits it — including for unflagged records."""
    app = _make_app(tmp_path, monkeypatch)
    client = authed(app)
    plain = _put(client, "scratchpad", 0, {"tabs": []})
    assert plain.json["noHistory"] is False
    assert _get(client, "scratchpad").json["noHistory"] is False
    flagged = _put(client, "host-registry", 0, {"token": OLD_TOKEN},
                   noHistory=True)
    assert flagged.json["noHistory"] is True
    assert _get(client, "host-registry").json["noHistory"] is True
    # A never-written record reads as unflagged rather than omitting the key.
    assert _get(client, "no-such-mod").json["noHistory"] is False


def test_info_advertises_the_modstore_capability(tmp_path, monkeypatch):
    """The PRE-write gate. The flag FAILS OPEN on an older broker (unknown body
    keys are ignored, and it keeps archiving), so the PUT that would discover
    that has already filed the previous plaintext into that broker's ring —
    detection has to happen before the write, off /info."""
    app = _make_app(tmp_path, monkeypatch)
    _, response = authed(app).get("/info")
    assert response.status == 200
    assert response.json["modstore"] == {"noHistory": True}


# ---- unflagged records are untouched (scratchpad's History) ----------------

def test_scratchpad_history_still_lists_and_restores(tmp_path, monkeypatch):
    """scratchpad must NOT set the flag — History is its feature. This is per-
    record policy, and a global purge would be the wrong shape."""
    app = _make_app(tmp_path, monkeypatch)
    client = authed(app)
    mod = "scratchpad"
    notes = [{"v": 1, "tabs": [{"id": "a", "name": "Notes", "text": text}]}
             for text in ("hello", "hello world", "hello world!")]
    rev = _seed_ring(client, mod, notes)
    assert rev == 3
    body = _get(client, mod).json
    assert body["value"] == notes[-1]
    # Metadata only in the list (rev + ts, never a body), newest-first.
    assert [e["rev"] for e in body["revisions"]] == [2, 1]
    assert all("value" not in e and "ts" in e for e in body["revisions"])
    assert body["noHistory"] is False
    # ...and each one still RESTORES a full value.
    for want_rev, expected in ((1, notes[0]), (2, notes[1]), (3, notes[2])):
        got = _get(client, mod, rev=want_rev)
        assert got.status == 200 and got.json["value"] == expected
    # On disk too: an unflagged record keeps its ring and carries no flag key.
    _, store = _sidecar(tmp_path)
    assert [e["rev"] for e in store[mod]["revisions"]] == [2, 1]
    assert "noHistory" not in store[mod]


def test_purge_revisions_alone_is_unchanged_by_the_new_flag(tmp_path,
                                                            monkeypatch):
    """#65's per-write scrub still works and still does NOT make the record
    sticky: history resumes on the next write, which is exactly why it was not
    enough for credentials."""
    app = _make_app(tmp_path, monkeypatch)
    client = authed(app)
    mod = "scratchpad"
    rev = _seed_ring(client, mod, [{"n": 0}, {"n": 1}, {"n": 2}])
    response = _put(client, mod, rev, {"n": "final"}, purgeRevisions=True)
    assert response.status == 200 and response.json["rev"] == rev + 1
    assert response.json["noHistory"] is False
    body = _get(client, mod).json
    assert body["revisions"] == [] and body["noHistory"] is False
    response = _put(client, mod, body["rev"], {"n": "after"})
    assert response.status == 200
    assert [e["rev"] for e in _get(client, mod).json["revisions"]] == \
        [body["rev"]]


# ---- the loader (sidecar round trip + fail-closed self-heal) ---------------

def test_loader_round_trips_the_flag_and_drops_a_paired_ring(tmp_path):
    """_load_modstore fails CLOSED: a hand-edited (or pre-#192) sidecar that
    pairs the flag with a ring loads with the ring DROPPED, not merely ignored
    — a flagged record must never serve history, and an in-memory ring is one
    write away from being re-persisted."""
    path = tmp_path / SIDECAR
    path.write_text(json.dumps({
        "host-registry": {
            "rev": 7, "value": {"token": NEW_TOKEN}, "noHistory": True,
            "revisions": [{"rev": 6, "value": {"token": OLD_TOKEN}, "ts": 1}],
        },
        "scratchpad": {
            "rev": 2, "value": {"tabs": ["b"]},
            "revisions": [{"rev": 1, "value": {"tabs": ["a"]}, "ts": 1}],
        },
    }), encoding="utf-8")
    store = _load_modstore(path)
    assert store["host-registry"]["noHistory"] is True
    assert store["host-registry"]["revisions"] == []
    assert store["host-registry"]["rev"] == 7        # rev is NOT rewound
    # The unflagged record is untouched, key absent rather than false.
    assert "noHistory" not in store["scratchpad"]
    assert [e["rev"] for e in store["scratchpad"]["revisions"]] == [1]


def test_loader_reads_a_junk_flag_as_flagged(tmp_path):
    """Truthy, not strictly `is True`: junk in that field means somebody wrote
    something there, and the conservative reading for a record that may hold
    credentials is "keep not archiving"."""
    path = tmp_path / SIDECAR
    path.write_text(json.dumps({
        "host-registry": {"rev": 1, "value": {"token": NEW_TOKEN},
                          "noHistory": "false", "revisions": []},
        "clock": {"rev": 1, "value": {}, "noHistory": False, "revisions": []},
    }), encoding="utf-8")
    store = _load_modstore(path)
    assert store["host-registry"]["noHistory"] is True   # normalized to a bool
    assert "noHistory" not in store["clock"]             # false == unflagged
