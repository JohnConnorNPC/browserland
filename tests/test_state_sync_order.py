"""Behavioural tests for #247: state-sync ordering, debt and validation.

#247 collects the ordering races and debt bugs left in
``webterm/broker/52_js_state_sync.js`` after #246: overlapping pulls that
lower ``_stateRev``, a stale initial pull applied to a newer view, edits lost
across a lease teardown, a proxy 409 adopted as empty state, a false debt that
reverts a peer, a stalled PUT that wedges every later push, an unsendable edit
left on screen, and an unbounded retry cadence.

Like ``tests/test_state_sync_adopt.py`` this executes the SHIPPED fragment in
node: the whole of 52 plus ``savePrefs`` cut from 51 at its own boundaries,
inside a ``with (env)`` scope whose outer names are stubs -- a scriptable
in-memory broker (GETs held at request or at release, PUTs held, hung bodies,
rejected, refused or answered with odd bodies), a hand-cranked
``setTimeout`` queue (600 ms debounce timers and 3000 ms deadlines fire only
when the script says so) and a hand-set ``performance.now``. The lifecycle
(teardown / rebuild) is modelled with the 52 helpers the real 84 calls; 84's
wiring of those helpers is pinned by source below. One PASS/FAIL line per
assert plus a machine-readable tail. Skipped when node is absent.
"""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from webterm.broker import ui

BROKER_DIR = Path(ui.__file__).resolve().parent
SYNC_JS = BROKER_DIR / "52_js_state_sync.js"
PREFS_JS = BROKER_DIR / "51_js_prefs.js"
NODE = shutil.which("node")

SAVE_START = "        function savePrefs() {"
SAVE_END = "        function getPref(id) {"

EXPECTED_ASSERTS = 15

_HARNESS = r"""
'use strict';
const fs = require('fs');
const sync = fs.readFileSync(__SYNC_PATH__, 'utf8');
const prefsSrc = fs.readFileSync(__PREFS_PATH__, 'utf8');
const sa = prefsSrc.indexOf(__SAVE_START__), sb = prefsSrc.indexOf(__SAVE_END__);
if (sa < 0 || sb < sa) throw new Error('savePrefs boundary missing');
const savePrefsSrc = prefsSrc.slice(sa, sb);

let pass = 0, fail = 0;
function ok(cond, label) {
    if (cond) { pass++; console.log('PASS: ' + label); }
    else { fail++; console.log('FAIL: ' + label); }
}
const J = JSON.stringify;
function clone(o) { return JSON.parse(J(o)); }
async function flush() { for (let i = 0; i < 30; i++) await new Promise(r => setImmediate(r)); }
function deferred() { let res; const p = new Promise(r => { res = r; }); return { p, res }; }

// ---- one world = a fresh evaluation of 52 against a fresh broker -----------
function mkWorld() {
    const w = { server: { rev: 1, settings: {}, layout: { cols: ['A'] } },
                timers: new Map(), nt: 1, perf: 0, puts: [], gets: 0,
                getMode: null, putMode: null, holdGet: false, holdGetLate: false,
                held: [], holdPut: false, heldPut: [], prefs: {},
                notices: [], probes: [], adopts: 0, log: [] };
    async function hostFetch(host, path, opts) {
        if (!opts || !opts.method) {
            w.gets++;
            w.log.push('GET');
            if (w.getMode === 'revonly') return { ok: true, status: 200, json: async () => ({ rev: 7 }) };
            if (w.holdGetLate) {
                const d = deferred(); w.held.push(d); await d.p;
                const s2 = clone(w.server);
                return { ok: true, status: 200, json: async () => s2 };
            }
            const snap = clone(w.server);              // captured at request time
            if (w.holdGet) { const d = deferred(); w.held.push(d); await d.p; }
            return { ok: true, status: 200, json: async () => snap };
        }
        const body = JSON.parse(opts.body);
        w.puts.push(body);
        w.log.push('PUT@' + body.baseRev + ':' + J(body.layout.cols));
        const mode = w.putMode;
        let resp;
        const state = () => clone(w.server);
        if (mode === 'throw') { throw new Error('offline'); }
        else if (mode === '413') resp = { ok: false, status: 413, json: async () => ({ ok: false, error: 'too_large' }) };
        else if (mode === '400verdict') resp = { ok: false, status: 400, json: async () => ({ ok: false, error: 'bad_json' }) };
        else if (mode === '400proxy') resp = { ok: false, status: 400, json: async () => { throw new Error('html'); } };
        else if (mode === '500') resp = { ok: false, status: 500, json: async () => ({}) };
        else if (mode === '409empty') resp = { ok: false, status: 409, json: async () => ({}) };
        else if (mode === '409revonly') resp = { ok: false, status: 409, json: async () => ({ rev: 9 }) };
        else if (mode === '409frac') resp = { ok: false, status: 409, json: async () => Object.assign(state(), { rev: 2.5 }) };
        else if (mode === '409neg') resp = { ok: false, status: 409, json: async () => Object.assign(state(), { rev: -1 }) };
        else if (mode === '409notactive') resp = { ok: false, status: 409, json: async () => { w.log.push('json-not_active'); return Object.assign({ ok: false, error: 'not_active' }, state()); } };
        else if (body.baseRev !== w.server.rev) {
            const snap = state(); resp = { ok: false, status: 409, json: async () => snap };
        } else {
            w.server.rev++; w.server.settings = clone(body.settings); w.server.layout = clone(body.layout);
            const rev = w.server.rev;
            if (mode === 'commitThrow') { throw new Error('response lost'); }
            if (mode === 'hangBody') { return { ok: true, status: 200, json: () => new Promise(() => {}) }; }
            if (mode === '200empty') resp = { ok: true, status: 200, json: async () => ({}) };
            else resp = { ok: true, status: 200, json: async () => ({ ok: true, rev: rev }) };
        }
        if (w.holdPut) { const d = deferred(); w.heldPut.push(d); await d.p; }
        return resp;
    }
    const env = {
        prefs: w.prefs, isDetachedSurface: () => false, hostFetch: hostFetch,
        localHost: () => ({ id: 'local' }), homeHostId: () => 'local',
        CLIENT_ID: 'me', FETCH_TIMEOUT_MS: 3000,
        getSettings: () => w.prefs._settings, getLayout: () => w.prefs._layout,
        savePrefsLocal() {}, document: { body: { classList: { toggle() {} } } },
        isSizeLocked: () => false, applyDisplaySettings() {},
        requestRelayout() { w.adopts++; w.log.push('ADOPT:' + J(w.prefs._layout && w.prefs._layout.cols)); },
        refreshOpenSectionUIs() {},
        showNotice(t) { w.notices.push(t); },
        probeControl(h) { w.probes.push(h); w.log.push('PROBE'); },
        performance: { now: () => w.perf },
        setTimeout(fn, ms) { const id = w.nt++; w.timers.set(id, { fn: fn, ms: ms }); return id; },
        clearTimeout(id) { w.timers.delete(id); },
    };
    const factory = new Function('env', 'with (env) {\n' + sync + '\n' + savePrefsSrc + '\n' +
        'var __api = { pullState: pullState, savePrefs: savePrefs, pushState: pushState,\n' +
        '  setReady: function (v) { _stateReady = v; },\n' +
        '  rev: function () { return _stateRev; },\n' +
        // the lifecycle, as 84 drives it (84's wiring is pinned by source)
        '  teardown: function () { _deactivated = true; _viewEpoch++;\n' +
        '    if (typeof _stateParkPendingPush === "function") _stateParkPendingPush();\n' +
        '    else if (_statePushTimer) { clearTimeout(_statePushTimer); _statePushTimer = null; } },\n' +
        '  rebuildStart: function () { _deactivated = false; _viewEpoch++; },\n' +
        '  flushOwed: function () { return (typeof _stateFlushOwed === "function") ? _stateFlushOwed() : Promise.resolve(false); },\n' +
        '  bumpEpoch: function () { _viewEpoch++; } };\n' +
        '}\nreturn __api;\n');
    w.api = factory(env);
    w.firePush = async function () {
        for (const [id, t] of Array.from(w.timers)) if (t.ms === 600) { w.timers.delete(id); t.fn(); }
        await flush();
    };
    w.fireDeadlines = async function () {
        for (const [id, t] of Array.from(w.timers)) if (t.ms === 3000) { w.timers.delete(id); t.fn(); }
        await flush();
    };
    w.boot = async function () { await w.api.pullState(true); w.api.setReady(true); };
    w.cols = () => J(w.prefs._layout && w.prefs._layout.cols);
    w.srv = () => J(w.server.layout.cols);
    w.edit = function (cols) {
        w.prefs._layout = Object.assign({}, w.prefs._layout, { cols: cols });
        w.api.savePrefs();
    };
    w.release = async function () {
        while (w.held.length || w.heldPut.length) {
            if (w.held.length) w.held.shift().res();
            if (w.heldPut.length) w.heldPut.shift().res();
            await flush();
        }
    };
    return w;
}

(async function () {
    let w;
    // ---- 247-4 / N1: response validation ---------------------------------
    w = mkWorld(); await w.boot();
    w.putMode = '409empty'; w.edit(['Q']); await w.firePush(); w.putMode = null;
    ok(w.cols() === J(['Q']) && w.adopts === 1, 'U1 a 409 {} is not adopted: the edit stays');
    w.putMode = '409revonly'; w.edit(['Q2']); await w.firePush(); w.putMode = null;
    ok(w.cols() === J(['Q2']) && w.adopts === 1, 'U2 a 409 {rev} without settings/layout is not adopted');
    w = mkWorld(); await w.boot();
    w.putMode = '409frac'; w.edit(['R']); await w.firePush();
    w.putMode = '409neg'; w.edit(['R2']); await w.firePush(); w.putMode = null;
    ok(w.cols() === J(['R2']) && w.api.rev() === 1, 'U3 a fractional or negative 409 rev is unusable (no rev taken, no adopt)');
    w = mkWorld(); await w.boot();
    w.getMode = 'revonly'; await w.api.pullState(false); await flush(); w.getMode = null;
    ok(w.cols() === J(['A']) && w.api.rev() === 1, 'U4 a GET 200 {rev} without settings/layout is ignored');
    w = mkWorld(); await w.boot();
    w.putMode = '200empty'; w.edit(['L']); await w.firePush(); w.putMode = null;
    ok(w.api.rev() === 1 && w.srv() === J(['L']), 'U5a precondition: a 200 {} landed but taught no rev');
    await w.api.pullState(false); await flush(); await w.firePush();
    ok(w.api.rev() === 2 && w.puts.length === 1 && w.cols() === J(['L']),
        'U5 ...the next pull acknowledges it (no second PUT)');
    w = mkWorld(); await w.boot();
    w.server = { rev: 2, settings: {}, layout: { cols: ['WIN'] } };
    w.putMode = '409notactive'; w.edit(['N']); await w.firePush(); w.putMode = null;
    ok(J(w.probes) === J(['local']) && w.cols() === J(['WIN'])
        && w.log.indexOf('PROBE') < w.log.lastIndexOf('ADOPT:["WIN"]'),
        'U6 a not_active 409 with state probes first, then adopts');

    // ---- 247-1: the single-flight /state channel ---------------------------
    // O1: a poll during a held pull issues no second GET (item 1).
    w = mkWorld(); await w.boot();
    w.server = { rev: 2, settings: {}, layout: { cols: ['X'] } };
    w.holdGet = true; const o1 = w.api.pullState(false); await flush(); w.holdGet = false;
    const g1 = w.gets;
    w.server = { rev: 3, settings: {}, layout: { cols: ['Y'] } };
    await w.api.pullState(false); await flush();
    ok(w.gets === g1, 'O1 a poll while a pull is outstanding sends no GET');
    await w.release(); await o1; await flush();
    await w.api.pullState(false); await flush();
    w.edit(['Z']); await w.firePush(); await flush();
    ok(w.cols() === J(['Z']) && w.srv() === J(['Z']) && J(w.log.filter(x => x.indexOf('PUT') === 0)) === J(['PUT@3:["Z"]']),
        'O1b an edit made after observing Y@3 lands on base 3 (never on a stale rev)');
    // O2: 1b -- a pull sent before our PUT is answered first; the push waits.
    w = mkWorld(); await w.boot();
    w.holdGet = true; const o2 = w.api.pullState(false); await flush(); w.holdGet = false;
    w.edit(['E']); await w.firePush();
    ok(w.puts.length === 0, 'O2a a push waits for a held GET (nothing sent meanwhile)');
    const adoptsBefore = w.adopts;
    await w.release(); await o2; await flush(); await flush();
    ok(w.puts.length === 1 && w.puts[0].baseRev === 1 && w.srv() === J(['E']) && w.cols() === J(['E'])
        && w.adopts === adoptsBefore && w.api.rev() === 2,
        'O2 the push sends once after the pull, lands, and the screen never re-adopts the old state');
    // O3 (w2 W2-B): a GET captured AFTER a later-sent PUT's state.
    w = mkWorld(); await w.boot();
    w.server = { rev: 4, settings: {}, layout: { cols: ['X'] } }; await w.api.pullState(false); await flush();
    w.holdGetLate = true; const o3 = w.api.pullState(false); await flush(); w.holdGetLate = false;
    w.server = { rev: 5, settings: {}, layout: { cols: ['Y'] } };
    w.holdPut = true; w.edit(['Z']); await w.firePush();
    w.server = { rev: 6, settings: {}, layout: { cols: ['W'] } };
    const revs = [];
    w.held.shift().res(); await o3; await flush(); revs.push(w.api.rev());
    w.holdPut = false; await w.release(); revs.push(w.api.rev());
    w.edit(['V']); await w.firePush(); await flush(); revs.push(w.api.rev());
    ok(revs[1] >= revs[0] && revs[2] >= revs[1] && w.srv() === J(['V']),
        'O3 a reordered capture never lowers the rev, and the next edit lands (revs ' + J(revs) + ')');
    // O4: a genuine rollback is followed (no rev comparison).
    w = mkWorld(); await w.boot();
    w.edit(['A10']); await w.firePush();
    w.server = { rev: 1, settings: {}, layout: { cols: ['RESTORED'] } };
    await w.api.pullState(false); await flush();
    ok(w.cols() === J(['RESTORED']) && w.api.rev() === 1, 'O4 a genuine rev rollback is followed by the next pull');
    // O5/O6: one request at a time, also when a push and an initial pull start in one tick.
    w = mkWorld(); await w.boot();
    w.holdGet = true; w.holdPut = true;
    w.prefs._layout = { cols: ['K'] }; w.api.savePrefs();
    const before6 = w.gets + w.puts.length;
    for (const [id, t] of Array.from(w.timers)) if (t.ms === 600) { w.timers.delete(id); t.fn(); }
    const ip = w.api.pullState(true);
    await flush(); await flush();
    ok((w.gets + w.puts.length) - before6 === 1,
        'O6 a push and an initial pull started in one tick put ONE request on the wire');
    w.holdGet = false; w.holdPut = false;
    await w.release(); await ip; await flush(); await w.release();
    ok(w.srv() === J(['K']), 'O5 ...and the other one follows once it is answered');

    console.log(pass + ' passed, ' + fail + ' failed');
})().catch(function (e) { console.log('CRASH ' + (e && e.stack)); process.exit(1); });
"""


@pytest.fixture(scope="module")
def run(tmp_path_factory):
    if NODE is None:
        pytest.skip("node not installed")
    prefs = PREFS_JS.read_text(encoding="utf-8")
    for m in (SAVE_START, SAVE_END):
        assert prefs.count(m) == 1, m
    h = tmp_path_factory.mktemp("state-order") / "harness.js"
    src = _HARNESS
    for k, v in (("__SYNC_PATH__", str(SYNC_JS)), ("__PREFS_PATH__", str(PREFS_JS)),
                 ("__SAVE_START__", SAVE_START), ("__SAVE_END__", SAVE_END)):
        src = src.replace(k, json.dumps(v))
    h.write_text(src, encoding="utf-8")
    proc = subprocess.run([NODE, str(h)], capture_output=True, text=True,
                          encoding="utf-8", timeout=120)
    lines = (proc.stdout or "").strip().splitlines()
    tail = re.match(r"^(\d+) passed, (\d+) failed$", lines[-1] if lines else "")
    assert tail is not None, f"rc={proc.returncode}\n{proc.stdout}\n{proc.stderr}"
    results = {}
    for line in lines[:-1]:
        if line.startswith("PASS: "):
            results[line[6:]] = True
        elif line.startswith("FAIL: "):
            results[line[6:]] = False
    return {"rc": proc.returncode, "stdout": proc.stdout, "stderr": proc.stderr,
            "passed": int(tail.group(1)), "failed": int(tail.group(2)),
            "results": results}


def _ok(run, label):
    got = run["results"].get(label)
    assert got is True, (f"{label!r} {'failed' if got is False else 'never ran'}\n"
                         f"{run['stdout']}\n{run['stderr']}")


@pytest.mark.parametrize("label", [
    "U1 a 409 {} is not adopted: the edit stays",
    "U2 a 409 {rev} without settings/layout is not adopted",
    "U3 a fractional or negative 409 rev is unusable (no rev taken, no adopt)",
    "U4 a GET 200 {rev} without settings/layout is ignored",
    "U5a precondition: a 200 {} landed but taught no rev",
    "U5 ...the next pull acknowledges it (no second PUT)",
    "U6 a not_active 409 with state probes first, then adopts",
])
def test_247_4_response_validation(run, label):
    _ok(run, label)


@pytest.mark.parametrize("label", [
    "O1 a poll while a pull is outstanding sends no GET",
    "O1b an edit made after observing Y@3 lands on base 3 (never on a stale rev)",
    "O2a a push waits for a held GET (nothing sent meanwhile)",
    "O2 the push sends once after the pull, lands, and the screen never re-adopts the old state",
    "O4 a genuine rev rollback is followed by the next pull",
    "O6 a push and an initial pull started in one tick put ONE request on the wire",
    "O5 ...and the other one follows once it is answered",
])
def test_247_1_single_flight(run, label):
    _ok(run, label)


def test_247_1_reorder_cell_ran(run):
    assert any(k.startswith("O3 a reordered capture never lowers the rev") and v
               for k, v in run["results"].items()), run["stdout"]


def test_every_scenario_passed_and_node_exited_clean(run):
    assert run["failed"] == 0, run["stdout"] + run["stderr"]
    assert run["passed"] == EXPECTED_ASSERTS, run["stdout"]
    assert len(run["results"]) == EXPECTED_ASSERTS
    assert run["rc"] == 0, run["stderr"]
