"""#192, client half: the publish path is EXECUTED, not string-matched.

The verify round for checkpoint 5 graded E19/E20 partly on `assert "<literal>"
in code`, which cannot see whether a refused broker actually receives zero
PUTs or whether the 409 rebase really carries the flag -- the two properties
the issue is about. `host-registry.js`'s publish functions live inside the
same sliced range `test_host_registry_crypto.py` already runs in node, so
this file stubs what they reach for and CALLS them.

What is proven here, per the criteria:
  * a broker that does not advertise `modstore.noHistory` gets NO PUT at all
    (E20) -- including the shape the pre-write gate cannot see, where the
    OUTGOING value is token-free but the value being replaced is not;
  * every PUT that does go out carries `noHistory: true`, and the one-shot
    409 rebase re-sends it (E19/E20);
  * a broker that answers without echoing the flag is reported as still
    archiving, by name, and never folded into the success count (E20).
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from webterm.broker import ui

BROKER_DIR = Path(ui.__file__).resolve().parent
MOD_JS = BROKER_DIR / "mods" / "host-registry" / "host-registry.js"
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="node not installed")

_SLICE_START = "// ---- pure model + crypto ---"
_SLICE_END = "// ---- dialogs ---"


def _model_source() -> str:
    src = MOD_JS.read_text(encoding="utf-8")
    body = src[src.index(_SLICE_START):src.index(_SLICE_END)]
    for needed in ("async function publishTo", "async function checkNoHistory",
                   "function infoAdvertisesNoHistory",
                   "function valueHasPlainTokens"):
        assert needed in body, f"{needed} left the executed range"
    return body


_HARNESS = r"""
'use strict';
function def(name, value) {
    Object.defineProperty(globalThis, name,
        { value, writable: true, configurable: true });
}
def('window', { isSecureContext: true, crypto: globalThis.crypto,
                location: { origin: 'http://me.example:4445' } });

// ---- the fleet ----------------------------------------------------------
let HOSTS = [];
globalThis.getHosts = () => HOSTS;
globalThis.localHost = () => HOSTS[0];
globalThis.hostById = (id) => HOSTS.find(h => h.id === id) || null;
globalThis.strictHex = (v) => v;
globalThis.normalizeHostUrl = (u) => String(u);
globalThis.mintHostId = () => 'minted';
globalThis.showNotice = () => {};
globalThis.srcLabel = (h) => (h && (h.label || h.id)) || 'unknown';
globalThis.PROBE_MS = 8000;
globalThis.withDeadline = async (p, _ms, fallback) => {
    try { return await p; } catch (_) { return fallback; }
};

// ---- what each broker's /info says, and every request made --------------
let INFO = {};              // hostId -> 'modern' | 'old' | 'down' | 'junk'
const infoCalls = [];
globalThis.hostFetch = async (host, path) => {
    if (!host) throw new Error('hostFetch got a null host');
    if (path !== '/info') throw new Error('unexpected path ' + path);
    infoCalls.push(host.id);
    const kind = INFO[host.id] || 'old';
    if (kind === 'down') throw new TypeError('Failed to fetch');
    if (kind === 'junk') {
        return { ok: true, status: 200, json: async () => { throw new Error('x'); } };
    }
    const body = (kind === 'modern')
        ? { ok: true, modstore: { noHistory: true } }
        : { ok: true };
    return { ok: true, status: 200, json: async () => body };
};

// ---- the store: records every PUT, per host -----------------------------
let STORE = {};             // hostId -> {rev, value, echo}
const puts = [];            // {host, baseRev, noHistory, purge}
let conflictOnce = null;    // hostId that answers 409 the first time
globalThis.ctx = {
    storage: { set() {}, get() { return null; } },
    serverStore: {
        get: async (opts) => {
            const hid = (opts && opts.host) || 'local';
            const rec = STORE[hid];
            return rec
                ? { status: 200, ok: true, rev: rec.rev, value: rec.value }
                : { status: 200, ok: true, rev: 0, value: null };
        },
        set: async (value, baseRev, opts) => {
            const hid = (opts && opts.host) || 'local';
            puts.push({ host: hid, baseRev: baseRev,
                        noHistory: opts ? opts.noHistory : undefined,
                        purge: opts ? opts.purgeRevisions : undefined });
            if (conflictOnce === hid) {
                conflictOnce = null;
                const live = (STORE[hid] ? STORE[hid].rev : 0) + 5;
                return { status: 409, ok: false, error: 'conflict', rev: live };
            }
            const rec = STORE[hid] || { rev: 0, echo: true };
            rec.rev += 1;
            rec.value = value;
            STORE[hid] = rec;
            const out = { status: 200, ok: true, rev: rec.rev };
            // An OLD broker does not echo the flag back.
            if (rec.echo !== false) out.noHistory = true;
            return out;
        },
    },
};

__MODEL__

// ---- driver -------------------------------------------------------------
const TOKENY = { hosts: [{ id: 'h1', label: 'one',
                           url: 'https://one.example:4445', token: 'SECRET-1' }] };
const PLAIN = { hosts: [{ id: 'h1', label: 'one',
                          url: 'https://one.example:4445', token: '' }] };
function reset(hosts) {
    HOSTS = hosts;
    INFO = {}; STORE = {}; puts.length = 0; infoCalls.length = 0;
    conflictOnce = null;
}
const CASES = {};

// A capable broker takes the write, flag and all.
CASES.capable_write = async () => {
    reset([{ id: 'local', label: 'this broker' }]);
    INFO.local = 'modern';
    STORE.local = { rev: 3, value: PLAIN, echo: true };
    const r = await publishTo('local', TOKENY, false);
    return { r: r, puts: puts.slice(), infoCalls: infoCalls.slice() };
};

// THE LEAK THE VERIFY ROUND FOUND: the outgoing value is token-FREE, so the
// pre-write gate never looks -- but the value being REPLACED holds a
// password, and this broker would file it. Nothing may be sent.
CASES.token_free_over_stored_password = async () => {
    reset([{ id: 'local', label: 'this broker' }]);
    INFO.local = 'old';
    STORE.local = { rev: 2, value: TOKENY, echo: false };
    const r = await publishTo('local', PLAIN, false, false);
    return { r: r, puts: puts.slice(), infoCalls: infoCalls.slice() };
};

// Same shape, capable broker: the write proceeds.
CASES.token_free_over_stored_password_modern = async () => {
    reset([{ id: 'local', label: 'this broker' }]);
    INFO.local = 'modern';
    STORE.local = { rev: 2, value: TOKENY, echo: true };
    const r = await publishTo('local', PLAIN, false, false);
    return { r: r, puts: puts.slice() };
};

// An ordinary list update over a list that never held a password does not
// probe at all -- no friction added to the common case.
CASES.token_free_over_token_free = async () => {
    reset([{ id: 'local', label: 'this broker' }]);
    INFO.local = 'old';
    STORE.local = { rev: 1, value: PLAIN, echo: false };
    const r = await publishTo('local', PLAIN, false, false);
    return { r: r, puts: puts.slice(), infoCalls: infoCalls.slice() };
};

// The 409 rebase re-sends the SAME opts -- the flag included.
CASES.rebase_carries_the_flag = async () => {
    reset([{ id: 'local', label: 'this broker' }]);
    INFO.local = 'modern';
    STORE.local = { rev: 4, value: PLAIN, echo: true };
    conflictOnce = 'local';
    const r = await publishTo('local', TOKENY, true);
    return { r: r, puts: puts.slice() };
};

// A broker that takes the write but never echoes reads as still archiving.
CASES.missing_echo_is_still_archiving = async () => {
    reset([{ id: 'local', label: 'this broker' }]);
    INFO.local = 'modern';                     // claimed capable at probe time
    STORE.local = { rev: 1, value: PLAIN, echo: false };   // ...but does not echo
    const r = await publishTo('local', TOKENY, false, true);
    return { r: r, puts: puts.slice() };
};

// checkNoHistory itself: every not-capable shape is a NO with its own reason,
// and a host that vanished is a refusal rather than a null-host fetch.
CASES.capability_shapes = async () => {
    reset([{ id: 'local', label: 'this broker' },
           { id: 'old', label: 'old box' },
           { id: 'down', label: 'sleepy' },
           { id: 'junk', label: 'weird' }]);
    INFO.local = 'modern'; INFO.old = 'old';
    INFO.down = 'down'; INFO.junk = 'junk';
    const out = {};
    for (const id of ['local', 'old', 'down', 'junk', 'ghost']) {
        const c = await checkNoHistory(id);
        out[id] = { capable: c.capable, why: c.why, name: c.name };
    }
    return { out: out, puts: puts.slice() };
};

(async () => {
    const name = process.argv[2];
    const fn = CASES[name];
    if (!fn) { console.error('no case ' + name); process.exit(2); }
    const res = await fn();
    process.stdout.write(JSON.stringify(res));
})().catch((e) => { console.error(e && e.stack || String(e)); process.exit(1); });
"""


@pytest.fixture(scope="module")
def harness(tmp_path_factory) -> Path:
    path = tmp_path_factory.mktemp("hrnh") / "harness.mjs"
    path.write_text(_HARNESS.replace("__MODEL__", _model_source()),
                    encoding="utf-8")
    return path


def run(harness: Path, case: str):
    proc = subprocess.run([NODE, str(harness), case], capture_output=True,
                          text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def test_a_capable_broker_takes_the_write_with_the_flag(harness):
    r = run(harness, "capable_write")
    assert r["r"]["ok"] is True
    assert len(r["puts"]) == 1
    assert r["puts"][0]["noHistory"] is True
    assert r["r"]["archiving"] is False


def test_a_token_free_publish_over_a_stored_password_sends_nothing(harness):
    """The leak the pre-write gate cannot see: it reads the OUTGOING list, but
    the ring archives the value being REPLACED. An old broker holding a
    password must not receive an ordinary list update, because that write is
    what files the password into its history."""
    r = run(harness, "token_free_over_stored_password")
    assert r["puts"] == [], "a PUT went out that would have archived a password"
    assert r["r"]["ok"] is False
    assert r["r"]["skipped"] is True
    assert r["r"]["error"] == "would_archive"
    assert r["infoCalls"] == ["local"], "the capability was actually asked"


def test_the_same_publish_proceeds_against_a_capable_broker(harness):
    r = run(harness, "token_free_over_stored_password_modern")
    assert r["r"]["ok"] is True
    assert len(r["puts"]) == 1 and r["puts"][0]["noHistory"] is True


def test_an_ordinary_list_update_never_probes(harness):
    """No credential in either direction: no probe, no friction, old brokers
    keep working exactly as before."""
    r = run(harness, "token_free_over_token_free")
    assert r["infoCalls"] == []
    assert len(r["puts"]) == 1
    assert r["r"]["ok"] is True


def test_the_409_rebase_resends_the_flag(harness):
    """The one code path most likely to drop it: a retry that rebuilt its opts
    would land the rebased value on an unflagged record and quietly resume
    archiving."""
    r = run(harness, "rebase_carries_the_flag")
    assert len(r["puts"]) == 2, "expected the one-shot rebase"
    assert [p["noHistory"] for p in r["puts"]] == [True, True]
    assert [p["purge"] for p in r["puts"]] == [True, True]
    assert r["puts"][1]["baseRev"] != r["puts"][0]["baseRev"], "rebased"
    assert r["r"]["ok"] is True


def test_a_broker_that_does_not_echo_reads_as_still_archiving(harness):
    r = run(harness, "missing_echo_is_still_archiving")
    assert r["r"]["ok"] is True
    assert r["r"]["archiving"] is True, (
        "a write that did not confirm the flag must say so, per host")


def test_every_uncertain_capability_answer_is_a_no(harness):
    """Fail closed: unreachable, unparseable and vanished all count as 'we did
    not learn that it honours the flag', each with its own honest reason."""
    r = run(harness, "capability_shapes")["out"]
    assert r["local"]["capable"] is True and r["local"]["why"] == ""
    for hid in ("old", "down", "junk", "ghost"):
        assert r[hid]["capable"] is False, hid
        assert r[hid]["why"], f"{hid} must say why"
    assert "host not found" in r["ghost"]["why"]
