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

EXPECTED_ASSERTS = 75

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
            if (w.getMode === 'hangBody') return { ok: true, status: 200, json: () => new Promise(() => {}) };
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
        '  bumpEpoch: function () { _viewEpoch++; },\n' +
        '  deactivate: function () { _deactivated = true; } };\n' +
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
    // 84 rebuildView's order: flush the parked edit, then the initial pull.
    w.rebuild = async function () {
        w.api.rebuildStart();
        await w.api.flushOwed(); await w.api.pullState(true); await flush();
    };
    w.putsLog = () => w.log.filter(x => x.indexOf('PUT@') === 0);
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

    // ---- 247-6: our 200 is recorded; one deadline; epoch gates -------------
    // E1 (item 6): a PUT that landed before a teardown, answered after it.
    w = mkWorld(); await w.boot();
    w.holdPut = true; w.edit(['A8']); await w.firePush(); w.holdPut = false;
    w.api.teardown(); w.api.rebuildStart();
    w.server.layout = { cols: ['S'] }; w.server.rev = 3;            // the new holder wrote
    const e1 = w.api.pullState(true); await flush();               // the rebuild's pull waits
    await w.release(); await e1; await flush();
    w.edit(['D']); await w.firePush(); await flush();
    ok(w.srv() === J(['D']) && w.cols() === J(['D']),
        'E1 a stale 200 never sets the rebuilt view back: a fresh edit lands');
    // E2: a 409 that arrives while torn down adopts nothing.
    w = mkWorld(); await w.boot();
    w.holdPut = true; w.server.rev = 2; w.server.layout = { cols: ['PEER'] };
    w.edit(['MINE']); await w.firePush(); w.holdPut = false;
    w.api.teardown();
    const a2 = w.adopts;
    await w.release(); await flush();
    ok(w.adopts === a2 && w.cols() === J(['MINE']), 'E2 a 409 after a teardown adopts nothing');
    // E3: our 200 is recorded even while torn down.
    w = mkWorld(); await w.boot();
    w.holdPut = true; w.edit(['L']); await w.firePush(); w.holdPut = false;
    w.api.teardown();
    await w.release(); await flush();
    ok(w.api.rev() === 2, 'E3 our 200 is recorded across a teardown (rev 2)');
    // E4: a push asked for during the flight never re-arms into a newer view.
    w = mkWorld(); await w.boot();
    w.holdPut = true; w.edit(['P1']); await w.firePush();
    w.edit(['P2']); await w.firePush();                              // _statePushAgain
    w.holdPut = false;
    w.api.teardown(); w.api.rebuildStart();
    await w.release(); await flush();
    ok(!Array.from(w.timers.values()).some(t => t.ms === 600), 'E4 no requeue across an epoch');
    // E5 (S3): a PUT whose body never arrives does not wedge later pushes.
    w = mkWorld(); await w.boot();
    w.putMode = 'hangBody'; w.edit(['H']); await w.firePush(); w.putMode = null;
    await w.fireDeadlines();
    await w.api.pullState(false); await flush();                      // the next poll
    w.edit(['H2']); await w.firePush(); await flush();
    ok(w.puts.length === 2 && w.srv() === J(['H2']), 'E5 a hung PUT body hits the deadline; the next edit is sent');
    // E6 (S3): a GET whose body never arrives frees the channel at the deadline.
    w = mkWorld(); await w.boot();
    w.getMode = 'hangBody'; const e6 = w.api.pullState(false); await flush(); w.getMode = null;
    w.edit(['G']); await w.firePush();
    const sentBefore = w.puts.length;
    await w.fireDeadlines(); await e6; await flush();
    ok(sentBefore === 0 && w.puts.length === 1 && w.srv() === J(['G']),
        'E6 a hung GET body holds the channel only until its deadline');

    // ---- 247-2: a pull that outlives its view applies nothing --------------
    // P1 (the issue): a superseded rebuild pull never overwrites the newer view.
    w = mkWorld(); await w.boot();
    w.server = { rev: 2, settings: {}, layout: { cols: ['A2'] } };
    w.api.teardown(); w.api.rebuildStart();
    w.holdGet = true; const r1 = w.api.pullState(true); await flush(); w.holdGet = false;
    w.api.teardown(); w.api.rebuildStart();
    w.server = { rev: 3, settings: {}, layout: { cols: ['B'] } };
    const r2 = w.api.pullState(true); await flush();
    await w.release(); await r1; await r2; await flush();
    w.edit(['C']); await w.firePush(); await flush();
    ok(w.log.indexOf('ADOPT:["A2"]') === -1 && w.srv() === J(['C']) && w.cols() === J(['C']),
        'P1 a superseded rebuild pull is dropped; the newer view and the next edit stand');
    // P2: a background pull answered after a teardown applies nothing.
    w = mkWorld(); await w.boot();
    w.server = { rev: 2, settings: {}, layout: { cols: ['LATE'] } };
    w.holdGet = true; const p2 = w.api.pullState(false); await flush(); w.holdGet = false;
    w.api.teardown();
    const a2b = w.adopts;
    await w.release(); await p2; await flush();
    ok(w.adopts === a2b && w.api.rev() === 1, 'P2 a background pull answered after a teardown applies nothing');
    // P3: the load pull, superseded by the boot's re-pull, is dropped.
    w = mkWorld();
    w.server = { rev: 2, settings: {}, layout: { cols: ['OLD'] } };
    w.holdGet = true; const lp = w.api.pullState(true); await flush(); w.holdGet = false;   // 85's load pull
    w.api.rebuildStart();                                              // the first boot bumps the epoch
    w.server = { rev: 3, settings: {}, layout: { cols: ['NEW'] } };
    const bp = w.api.pullState(true); await flush();                   // B3's boot re-pull (waits)
    await w.release(); await lp; await bp; await flush();
    ok(w.log.indexOf('ADOPT:["OLD"]') === -1 && w.cols() === J(['NEW']) && w.api.rev() === 3,
        'P3 a load pull superseded by a lifecycle pull is dropped; the newer one adopts');
    // P3b: an epoch bump alone (no lifecycle pull sent yet) does not drop the load pull.
    w = mkWorld();
    w.holdGet = true; const lp2 = w.api.pullState(true); await flush(); w.holdGet = false;
    w.api.rebuildStart();
    await w.release(); await lp2; await flush();
    ok(w.cols() === J(['A']) && w.api.rev() === 1, 'P3b the load pull still applies across the first epoch bump');
    // P4: an inactive (never booted) page's load pull applies.
    w = mkWorld();
    w.api.deactivate();
    await w.api.pullState(true); await flush();
    ok(w.cols() === J(['A']) && w.api.rev() === 1, 'P4 an inactive page still adopts its load pull');

    // ---- 247-3: park, flush, M3, R1 ---------------------------------------
    // D1: resetLocalView (teardown + rebuild in one tick) keeps a debounced edit.
    w = mkWorld(); await w.boot();
    w.edit(['D']); w.api.teardown(); await w.rebuild(); await w.firePush();
    ok(w.srv() === J(['D']) && w.cols() === J(['D']) && J(w.putsLog()) === J(['PUT@1:["D"]']),
        'D1 an edit in its debounce at a reset lands (one PUT, its old base)');
    // D2: a lease loss, nobody writes, the lease comes back.
    w = mkWorld(); await w.boot();
    w.edit(['D2']); w.api.teardown();
    await w.api.pullState(false); await flush();                       // polls are stopped / skipped
    await w.rebuild();
    ok(w.srv() === J(['D2']), 'D2 a parked edit is saved when this page takes the lease back (Q1)');
    // D3: someone wrote meanwhile -> the flush yields, nothing is clobbered.
    w = mkWorld(); await w.boot();
    w.edit(['MINE']); w.api.teardown();
    w.server = { rev: 2, settings: {}, layout: { cols: ['PEER'] } };
    await w.rebuild(); await w.firePush();
    ok(w.srv() === J(['PEER']) && w.cols() === J(['PEER']) && J(w.putsLog()) === J(['PUT@1:["MINE"]']),
        'D3 a peer wrote meanwhile: the parked edit 409s on its old base and yields');
    // D3b (codex r2 #5): the flush fails (5xx), the M3 pull takes the peer's rev,
    // and the retry must still use the teardown-time base -> it yields, never clobbers.
    w = mkWorld(); await w.boot();
    w.edit(['MINE']); w.api.teardown();
    w.server = { rev: 2, settings: {}, layout: { cols: ['PEER'] } };
    w.api.rebuildStart();
    w.putMode = '500'; await w.api.flushOwed(); w.putMode = null;
    await w.api.pullState(true); await flush();
    for (let i = 0; i < 3; i++) { await w.api.pullState(false); await flush(); await w.firePush(); }
    ok(w.srv() === J(['PEER']) && w.cols() === J(['PEER']),
        'D3b a parked edit retried after a failed flush keeps its teardown-time base and yields');
    // D4: a PUT in flight at the teardown + a newer edit in its debounce.
    w = mkWorld(); await w.boot();
    w.holdPut = true; w.edit(['P1']); await w.firePush(); w.holdPut = false;
    w.edit(['P2']); w.api.teardown();
    const d4 = w.rebuild(); await flush(); await w.release(); await d4; await w.release();
    ok(w.srv() === J(['P2']) && J(w.putsLog()) === J(['PUT@1:["P1"]', 'PUT@2:["P2"]']),
        'D4 an in-flight PUT and a newer debounced edit both land, in order');
    // D4b (M1): the newer edit QUEUED behind the in-flight PUT (its debounce fired).
    w = mkWorld(); await w.boot();
    w.holdPut = true; w.edit(['P1']); await w.firePush();
    w.edit(['P2']); await w.firePush(); w.holdPut = false;             // _statePushAgain
    w.api.teardown();
    const d4b = w.rebuild(); await flush(); await w.release(); await d4b; await w.release();
    ok(w.srv() === J(['P2']) && J(w.putsLog()) === J(['PUT@1:["P1"]', 'PUT@2:["P2"]']),
        'D4b an edit queued behind an in-flight PUT is parked and lands after it (M1)');
    // D5: nothing pending -> the rebuild sends nothing.
    w = mkWorld(); await w.boot();
    w.api.teardown(); await w.rebuild();
    ok(w.puts.length === 0, 'D5 a rebuild with nothing owed sends no PUT');
    // D6 (M3): the flush's own PUT stalls past the deadline -> the pull keeps the edit.
    w = mkWorld(); await w.boot();
    w.putMode = 'hangBody'; w.edit(['P1']); await w.firePush(); w.putMode = null;
    w.edit(['P2']); w.api.teardown();
    w.api.rebuildStart();
    w.holdPut = true;
    const fo6 = w.api.flushOwed(); await flush();
    await w.fireDeadlines(); await flush(); await w.fireDeadlines();
    await fo6; await w.api.pullState(true); await flush();
    ok(w.cols() === J(['P2']) && w.log.indexOf('ADOPT:["P1"]') === -1,
        'D6 a flush that did not get out: the initial pull keeps the parked edit (M3)');
    w.holdPut = false; await w.release();
    // D7 (R1a): the in-flight PUT is refused not_active AT the lease loss.
    w = mkWorld(); await w.boot();
    w.holdPut = true; w.putMode = '409notactive'; w.edit(['P1']); await w.firePush();
    w.holdPut = false; w.putMode = null;
    w.api.teardown();
    await w.release();                                                 // its 409 arrives torn down
    await w.rebuild();
    ok(w.srv() === J(['P1']) && w.cols() === J(['P1']), 'D7 an in-flight PUT refused not_active at the lease loss is pushed on regain (R1)');
    // D7b: the same, but the new holder wrote meanwhile -> yield.
    w = mkWorld(); await w.boot();
    w.holdPut = true; w.putMode = '409notactive'; w.edit(['P1']); await w.firePush();
    w.holdPut = false; w.putMode = null;
    w.api.teardown(); await w.release();
    w.server = { rev: 2, settings: {}, layout: { cols: ['PEER'] } };
    await w.rebuild();
    ok(w.srv() === J(['PEER']) && w.cols() === J(['PEER']), 'D7b ...and yields when someone wrote meanwhile');
    // D8 (R1b): the in-flight PUT gets a 500 after the teardown.
    w = mkWorld(); await w.boot();
    w.holdPut = true; w.putMode = '500'; w.edit(['P1']); await w.firePush();
    w.holdPut = false;
    w.api.teardown(); await w.release(); w.putMode = null;
    await w.rebuild();
    ok(w.srv() === J(['P1']), 'D8 an in-flight PUT that fails with a 500 after the teardown stays owed and lands on regain (R1)');

    // ---- 247-5: thrown failures, editGen (S4), own-write recognition (R2) ---
    // F1 (the issue): the PUT landed, its reply was lost, a peer built on it.
    w = mkWorld(); await w.boot();
    w.putMode = 'commitThrow'; w.edit(['E']); await w.firePush(); w.putMode = null;
    w.server = { rev: 3, settings: {}, layout: { cols: ['E', 'peer'] } };
    await w.api.pullState(false); await flush(); await w.firePush(); await flush();
    ok(w.srv() === J(['E', 'peer']) && w.cols() === J(['E', 'peer']),
        'F1 a false debt never reverts the peer write built on it (original base -> 409 -> yield)');
    // F2: it landed and nobody wrote -> the acknowledgement, no PUT.
    w = mkWorld(); await w.boot();
    w.putMode = 'commitThrow'; w.edit(['E']); await w.firePush(); w.putMode = null;
    await w.api.pullState(false); await flush(); await w.firePush();
    ok(w.puts.length === 1 && w.api.rev() === 2, 'F2 a lost reply whose PUT landed is acknowledged by the next pull (no retry)');
    // F3: it never landed and nobody wrote -> it lands on the original base.
    w = mkWorld(); await w.boot();
    w.putMode = 'throw'; w.edit(['E']); await w.firePush(); w.putMode = null;
    await w.api.pullState(false); await flush(); await w.firePush(); await flush();
    ok(w.srv() === J(['E']) && J(w.putsLog()) === J(['PUT@1:["E"]', 'PUT@1:["E"]']),
        'F3 a thrown PUT that never landed retries on its original base and lands');
    // F4: a NEW edit after the throw is fresh intent: it rebases and wins.
    w = mkWorld(); await w.boot();
    w.putMode = 'throw'; w.edit(['E']); await w.firePush(); w.putMode = null;
    w.server = { rev: 2, settings: {}, layout: { cols: ['P'] } };
    await w.api.pullState(false); await flush();
    w.edit(['E2']); await w.firePush(); await flush();
    ok(w.srv() === J(['E2']), 'F4 a new edit (a byte change) after a throw rebases and lands (policy a)');
    // F5: ABA -- edit away and back to the same bytes is still new intent.
    w = mkWorld(); await w.boot();
    w.putMode = 'throw'; w.edit(['E']); await w.firePush(); w.putMode = null;
    w.server = { rev: 2, settings: {}, layout: { cols: ['P'] } };
    await w.api.pullState(false); await flush();
    w.edit(['X']); w.edit(['E']); await w.firePush(); await flush();
    ok(w.srv() === J(['E']), 'F5 edit-away-and-back (ABA) is new intent: it rebases and lands');
    // F7 (S4): a byte-identical save during the debt is NOT new intent.
    w = mkWorld(); await w.boot();
    w.putMode = 'commitThrow'; w.edit(['E']); await w.firePush(); w.putMode = null;
    w.server = { rev: 3, settings: {}, layout: { cols: ['E', 'peer'] } };
    w.api.savePrefs();                                                 // nothing changed
    await w.api.pullState(false); await flush(); await w.firePush(); await flush();
    ok(w.srv() === J(['E', 'peer']), 'F7 a byte-identical save during the debt keeps the original base (S4)');
    // F8 (S4): a byte-identical save during a PUT -> the 409 still yields.
    w = mkWorld(); await w.boot();
    w.server = { rev: 2, settings: {}, layout: { cols: ['PEER'] } };
    w.holdPut = true; w.edit(['MINE']); await w.firePush();
    w.api.savePrefs(); await w.firePush();                             // byte-identical, mid-flight
    w.holdPut = false; await w.release(); await w.firePush(); await flush();
    ok(w.srv() === J(['PEER']) && w.cols() === J(['PEER']) && w.puts.length === 1,
        'F8 a byte-identical save during a PUT does not re-push over the 409 winner (S4)');
    // F9 (own write): a lost-reply PUT, then a new edit on the stale base.
    w = mkWorld(); await w.boot();
    w.putMode = 'commitThrow'; w.edit(['E1']); await w.firePush(); w.putMode = null;
    w.edit(['E2']); await w.firePush(); await flush(); await w.firePush(); await flush();
    ok(w.srv() === J(['E2']) && J(w.putsLog()) === J(['PUT@1:["E1"]', 'PUT@1:["E2"]', 'PUT@2:["E2"]']),
        'F9 a 409 whose winner is our own previous write rebases on it and lands');
    // F10 (R2): a peer reverts to an OLDER send of ours -> that is a peer write.
    w = mkWorld(); await w.boot();
    w.edit(['A1']); await w.firePush();                                // lands at 2
    w.putMode = 'commitThrow'; w.edit(['B']); await w.firePush(); w.putMode = null;   // lands at 3, reply lost
    w.server = { rev: 4, settings: {}, layout: { cols: ['A1'] } };     // a peer reverts to A1
    w.edit(['C']); await w.firePush(); await flush(); await w.firePush(); await flush();
    ok(w.srv() === J(['A1']) && w.putsLog().indexOf('PUT@4:["C"]') === -1,
        'F10 a peer revert to an older send of ours is not taken for our write: the edit yields (R2)');
    // F11 (codex B1): an adopt in between -- the next edit is built on the
    // ADOPTED state, so a peer that restores our last send is a peer write.
    w = mkWorld(); await w.boot();
    w.edit(['B']); await w.firePush();                                 // lands at 2
    w.server = { rev: 3, settings: {}, layout: { cols: ['P'] } };
    await w.api.pullState(false); await flush();                       // adopts P
    w.server = { rev: 4, settings: {}, layout: { cols: ['B'] } };      // a peer restores B
    w.edit(['C']); await w.firePush(); await flush(); await w.firePush(); await flush();
    ok(w.srv() === J(['B']) && w.cols() === J(['B']) && w.putsLog().indexOf('PUT@4:["C"]') === -1,
        'F11 after an adopt, a peer restoring our last send is a peer write: the edit yields');
    // F12 (codex B2): retries of one edit never evict its parent.
    w = mkWorld(); await w.boot();
    w.putMode = 'commitThrow'; w.edit(['E1']); await w.firePush();    // lands at 2, reply lost
    w.putMode = '500'; w.edit(['E2']); await w.firePush();             // E2 fails ...
    for (let i = 0; i < 3; i++) { w.api.savePrefs(); await w.firePush(); }   // ... 3 more times (no pull)
    w.putMode = null; w.perf += 60000;
    w.api.savePrefs(); await w.firePush(); await flush(); await w.firePush(); await flush();
    ok(w.srv() === J(['E2']) && w.cols() === J(['E2']),
        'F12 four failed sends of one edit keep its parent: the late 409 on our own write rebases and lands');
    // F13 (codex B3): our parent shown at a LOWER rev (a rollback) is the base.
    w = mkWorld(); await w.boot();
    w.server.rev = 9; await w.api.pullState(false); await flush();     // same bytes, rev 9
    w.putMode = 'commitThrow'; w.edit(['E1']); await w.firePush();    // lands at 10, reply lost
    w.putMode = 'throw'; w.edit(['E2']); await w.firePush();          // thrown, base 9
    w.putMode = null;
    w.server = { rev: 1, settings: {}, layout: { cols: ['E1'] } };     // the store rolled back
    const f13 = w.puts.length;
    w.api.savePrefs(); await w.firePush(); await flush();
    for (let i = 0; i < 4; i++) { await w.firePush(); await flush(); }
    ok(w.srv() === J(['E2']) && w.puts.length - f13 <= 2,
        'F13 a rollback showing our parent at a lower rev rebases onto it: no 409 loop (' + (w.puts.length - f13) + ' PUTs)');
    // D6b (M3, completed): the parked edit of D6 lands once the channel recovers.
    w = mkWorld(); await w.boot();
    w.putMode = 'hangBody'; w.edit(['P1']); await w.firePush(); w.putMode = null;
    w.edit(['P2']); w.api.teardown();
    w.api.rebuildStart();
    w.holdPut = true;
    const fo6b = w.api.flushOwed(); await flush();
    await w.fireDeadlines(); await flush(); await w.fireDeadlines();
    await fo6b; await w.api.pullState(true); await flush();
    w.holdPut = false; await w.release();
    for (let i = 0; i < 3; i++) { await w.api.pullState(false); await flush(); await w.firePush(); await flush(); }
    ok(w.srv() === J(['P2']), 'D6b ...and lands once the channel recovers (it never yields to our own P1)');
    // E5b: a hung PUT, then a new edit with no poll in between.
    w = mkWorld(); await w.boot();
    w.putMode = 'hangBody'; w.edit(['H']); await w.firePush(); w.putMode = null;
    await w.fireDeadlines();
    w.edit(['H2']); await w.firePush(); await flush(); await w.firePush(); await flush();
    ok(w.srv() === J(['H2']), 'E5b after a hung PUT the next edit lands without waiting for a poll');

    // ---- 247-7 (Q4): a rejected save goes back to the saved layout --------
    const REJ = "couldn't save this change — back to the saved layout";
    // V1: a 413 converges at once, with the notice, and nothing is re-sent.
    w = mkWorld(); await w.boot();
    w.putMode = '413'; w.edit(['TOO-BIG']); await w.firePush(); w.putMode = null; await flush();
    await w.firePush(); await w.api.pullState(false); await flush(); await w.firePush();
    ok(w.cols() === J(['A']) && J(w.notices) === J([REJ]) && w.puts.length === 1,
        'V1 a 413 goes back to the saved layout at once, says so, and re-sends nothing');
    // V2: a 400 that is the broker's verdict on the payload: the same.
    w = mkWorld(); await w.boot();
    w.putMode = '400verdict'; w.edit(['BAD']); await w.firePush(); w.putMode = null; await flush();
    ok(w.cols() === J(['A']) && J(w.notices) === J([REJ]), 'V2 a broker-verdict 400 (bad_json) converges the same way');
    // V2b: a 400 that is not the broker's (a proxy page): no converge.
    w = mkWorld(); await w.boot();
    w.putMode = '400proxy'; w.edit(['KEEP']); await w.firePush(); w.putMode = null; await flush();
    ok(w.cols() === J(['KEEP']) && w.notices.length === 0, 'V2b a proxy 400 does not discard the edit');
    // V3 (S5a): a 413 for an OLDER payload never clears a newer queued edit.
    w = mkWorld(); await w.boot();
    w.holdPut = true; w.putMode = '413'; w.edit(['TOO-BIG']); await w.firePush();
    w.edit(['SMALL']); await w.firePush();                             // newer, queued
    w.holdPut = false; w.heldPut.shift().res(); await flush(); w.putMode = null;
    await w.firePush(); await flush();
    ok(w.srv() === J(['SMALL']) && w.cols() === J(['SMALL']) && w.notices.length === 0,
        'V3 a 413 for an older payload lets the newer edit land (no converge, no notice)');
    // V4: a 413 answered after a teardown converges nothing.
    w = mkWorld(); await w.boot();
    w.holdPut = true; w.putMode = '413'; w.edit(['TOO-BIG']); await w.firePush();
    w.holdPut = false;
    w.api.teardown();
    const a4 = w.adopts;
    await w.release(); w.putMode = null; await flush();
    ok(w.adopts === a4 && w.notices.length === 0, 'V4 a 413 answered after a teardown converges nothing');
    // V4b (codex S4): ...and the parked edit is not dropped silently: the next
    // view re-sends it once and, refused again, converges WITH the notice.
    w = mkWorld(); await w.boot();
    w.holdPut = true; w.putMode = '413'; w.edit(['TOO-BIG']); await w.firePush();
    w.holdPut = false;
    w.api.teardown();
    await w.release(); await flush();
    await w.rebuild(); await flush(); await w.firePush(); await flush();
    ok(w.cols() === J(['A']) && J(w.notices) === J([REJ]) && w.puts.length === 2,
        'V4b the parked edit refused across the teardown is re-sent once by the next view and converges with the notice');

    // ---- 247-9 (item 9 b + S6): the retry ladder ---------------------------
    // One "tick" = a poll + the debounce firing, at the current performance.now.
    const tick = async function (w) { await w.api.pullState(false); await flush(); await w.firePush(); await flush(); };
    // B1: three retries at poll cadence.
    w = mkWorld(); await w.boot();
    w.putMode = '500'; w.edit(['F']); await w.firePush();
    for (let i = 0; i < 3; i++) await tick(w);
    ok(w.puts.length === 4, 'B1 the first three retries ride the poll cadence (4 PUTs)');
    // B2: then the 4th waits 4 s.
    await tick(w);
    const b2a = w.puts.length;
    w.perf += 3999; await tick(w);
    const b2b = w.puts.length;
    w.perf += 1; await tick(w);
    ok(b2a === 4 && b2b === 4 && w.puts.length === 5, 'B2 after the fast phase the next retry waits 4 s');
    // B3: 8, 16, 32 s, then the 60 s cap.
    const waits = [8000, 16000, 32000, 60000, 60000];
    let capOk = true;
    for (const ms of waits) {
        const n = w.puts.length;
        w.perf += ms - 1; await tick(w);
        if (w.puts.length !== n) capOk = false;
        w.perf += 1; await tick(w);
        if (w.puts.length !== n + 1) capOk = false;
    }
    ok(capOk, 'B3 the back-off doubles to 8, 16, 32 s and caps at 60 s');
    // B4: a landed retry resets the ladder.
    w.putMode = null; w.perf += 60000; await tick(w);
    ok(w.srv() === J(['F']), 'B4a precondition: the broker recovered and the owed edit landed');
    w.putMode = '500'; w.edit(['G']); await w.firePush();
    const b4 = w.puts.length;
    await tick(w);
    ok(w.puts.length === b4 + 1, 'B4 after a success a new failure retries at poll cadence again');
    // B5: a NEW edit restarts the ladder (fresh intent).
    w = mkWorld(); await w.boot();
    w.putMode = '500'; w.edit(['F']); await w.firePush();
    for (let i = 0; i < 4; i++) await tick(w);                       // ladder engaged
    w.edit(['F2']); await w.firePush();                               // a user save: sent
    const b5 = w.puts.length;
    await tick(w);
    ok(w.puts.length === b5 + 1, 'B5 a new edit restarts the ladder (its retry rides the poll again)');
    // B6: a user save inside the back-off is never delayed.
    w = mkWorld(); await w.boot();
    w.putMode = '500'; w.edit(['F']); await w.firePush();
    for (let i = 0; i < 4; i++) await tick(w);
    const b6 = w.puts.length;
    w.edit(['NOW']); await w.firePush();
    ok(w.puts.length === b6 + 1, 'B6 a user save inside the back-off is sent at its normal 600 ms');
    // B7 (S6): after the ladder engages, a retry uses the original base -> yields to a peer.
    w = mkWorld(); await w.boot();
    w.putMode = '500'; w.edit(['MINE']); await w.firePush();
    for (let i = 0; i < 3; i++) await tick(w);                       // 4 failures: the ladder is on
    w.server = { rev: 2, settings: {}, layout: { cols: ['PEER'] } };
    w.putMode = null; w.perf += 4000; await tick(w); await tick(w);
    ok(w.srv() === J(['PEER']) && w.cols() === J(['PEER']), 'B7 a late retry (ladder engaged) yields to a peer write (S6)');
    // B7b: in the fast phase the retry still rebases and wins (#246 policy a).
    w = mkWorld(); await w.boot();
    w.putMode = '500'; w.edit(['MINE']); await w.firePush(); w.putMode = null;
    w.server = { rev: 2, settings: {}, layout: { cols: ['PEER'] } };
    await tick(w);
    ok(w.srv() === J(['MINE']), 'B7b a fast-phase retry rebases and wins (policy a)');

    // ---- Z1 (w2 code audit): a teardown keeps the debt's ORIGINAL base ----
    const PEERZ = { rev: 2, settings: {}, layout: { cols: ['PEER'] } };
    // Z1: Q2 -- a thrown failure, a peer writes, the lease is lost and regained.
    w = mkWorld(); await w.boot();
    w.putMode = 'throw'; w.edit(['MINE']); await w.firePush(); w.putMode = null;
    w.server = clone(PEERZ);
    await w.api.pullState(false); await flush();                       // takes rev 2, keeps the owed edit
    let zn = w.putsLog().length;
    w.api.teardown(); await w.rebuild(); await w.firePush(); await flush();
    ok(w.srv() === J(['PEER']) && w.cols() === J(['PEER']) && J(w.putsLog().slice(zn)) === J(['PUT@1:["MINE"]']),
        'Z1 a thrown failure parked across a lease loss still retries on its original base and yields (Q2)');
    // Z2: S6 -- the ladder engaged, a peer writes, the lease is lost and regained.
    w = mkWorld(); await w.boot();
    w.putMode = '500'; w.edit(['MINE']); await w.firePush();
    for (let i = 0; i < 3; i++) await tick(w);                         // 4 failures: the ladder is on
    w.putMode = null;
    w.server = clone(PEERZ);
    await w.api.pullState(false); await flush();
    zn = w.putsLog().length;
    w.api.teardown(); await w.rebuild();
    w.perf += 60000; await tick(w); await tick(w);
    ok(w.srv() === J(['PEER']) && w.cols() === J(['PEER']) && J(w.putsLog().slice(zn)) === J(['PUT@1:["MINE"]']),
        'Z2 a late retry parked across a lease loss still uses its original base and yields (S6)');
    // Z3: a Q2 retry IN FLIGHT at the teardown is parked with that PUT's base.
    w = mkWorld(); await w.boot();
    w.putMode = 'throw'; w.edit(['MINE']); await w.firePush(); w.putMode = null;
    w.server = clone(PEERZ);
    await w.api.pullState(false); await flush();
    w.putMode = '500'; w.holdPut = true; w.api.savePrefs(); await w.firePush();   // the retry, held
    w.holdPut = false;
    w.api.teardown();
    await w.release(); await flush(); w.putMode = null;
    zn = w.putsLog().length;
    await w.rebuild(); await w.firePush(); await flush();
    ok(w.srv() === J(['PEER']) && w.cols() === J(['PEER']) && J(w.putsLog().slice(zn)) === J(['PUT@1:["MINE"]']),
        'Z3 a Q2 retry in flight at the teardown is parked with its base 1 and yields on regain');
    // Z3b: a parked edit whose flush failed, parked AGAIN by a second
    // teardown, keeps its first teardown-time base (not the rev of the moment).
    w = mkWorld(); await w.boot();
    w.edit(['MINE']); w.api.teardown();                                // parked on base 1
    w.server = clone(PEERZ);                                           // a peer writes meanwhile
    w.putMode = '500'; await w.rebuild(); w.putMode = null;            // flush fails; the pull takes rev 2
    zn = w.putsLog().length;
    w.api.teardown(); await w.rebuild(); await w.firePush(); await flush();
    ok(w.srv() === J(['PEER']) && w.cols() === J(['PEER']) && J(w.putsLog().slice(zn)) === J(['PUT@1:["MINE"]']),
        'Z3b a parked edit parked again keeps its first base and yields to the peer');

    // ---- Z4 (w2 code audit): a superseded initial pull never claims ------
    w = mkWorld(); await w.boot();
    w.holdPut = true; w.edit(['H']); await w.firePush();               // a PUT holds the channel
    const z4g = w.gets;
    const z4 = w.api.pullState(true); await flush();                   // a rebuild's pull waits
    w.api.teardown(); w.api.rebuildStart();                            // ... and is superseded
    w.holdPut = false; await w.release(); await z4; await flush();
    ok(w.gets === z4g, 'Z4 an initial pull superseded while it waited for the channel sends no GET');
    w.server = { rev: 9, settings: {}, layout: { cols: ['NEXT'] } };
    await w.rebuild();
    ok(w.gets === z4g + 1 && w.cols() === J(['NEXT']) && w.api.rev() === 9,
        'Z4a ...and the view that superseded it still pulls and adopts');
    // Z4b: the load pull keeps its exemption while it waits (P3b's rule).
    w = mkWorld();
    w.holdPut = true; w.api.setReady(true); w.edit(['H']); await w.firePush();
    const z4l = w.api.pullState(true); await flush();                  // the LOAD pull waits
    w.api.bumpEpoch();                                                 // the first boot's bump
    w.holdPut = false; await w.release(); await z4l; await flush();
    ok(w.gets === 1, 'Z4b the load pull still sends after the first epoch bump while it waited');

    // ---- Z5 (manager ruling): a proxy 400 is a definitive failure --------
    w = mkWorld(); await w.boot();
    w.putMode = '400proxy'; w.edit(['KEEP']); await w.firePush(); w.putMode = null; await flush();
    const z5cols = w.cols();
    await tick(w);
    ok(z5cols === J(['KEEP']) && w.notices.length === 0 && w.srv() === J(['KEEP']) && w.cols() === J(['KEEP']),
        'Z5 a proxy 400 keeps the edit on screen and the next poll retries it until it lands');
    // Z5b: ...on the retry ladder, like any failure.
    w = mkWorld(); await w.boot();
    w.putMode = '400proxy'; w.edit(['KEEP']); await w.firePush();
    for (let i = 0; i < 3; i++) await tick(w);
    const z5b = w.puts.length;
    await tick(w);
    ok(z5b === 4 && w.puts.length === 4 && w.cols() === J(['KEEP']) && w.notices.length === 0,
        'Z5b repeated proxy 400s ride the retry ladder (3 fast retries, then back-off)');

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


@pytest.mark.parametrize("label", [
    "E1 a stale 200 never sets the rebuilt view back: a fresh edit lands",
    "E2 a 409 after a teardown adopts nothing",
    "E3 our 200 is recorded across a teardown (rev 2)",
    "E4 no requeue across an epoch",
    "E5 a hung PUT body hits the deadline; the next edit is sent",
    "E6 a hung GET body holds the channel only until its deadline",
])
def test_247_6_deadline_and_epoch_gates(run, label):
    _ok(run, label)


@pytest.mark.parametrize("label", [
    "P1 a superseded rebuild pull is dropped; the newer view and the next edit stand",
    "P2 a background pull answered after a teardown applies nothing",
    "P3 a load pull superseded by a lifecycle pull is dropped; the newer one adopts",
    "P3b the load pull still applies across the first epoch bump",
    "P4 an inactive page still adopts its load pull",
])
def test_247_2_pull_epoch_gate(run, label):
    _ok(run, label)


@pytest.mark.parametrize("label", [
    "D1 an edit in its debounce at a reset lands (one PUT, its old base)",
    "D2 a parked edit is saved when this page takes the lease back (Q1)",
    "D3 a peer wrote meanwhile: the parked edit 409s on its old base and yields",
    "D3b a parked edit retried after a failed flush keeps its teardown-time base and yields",
    "D4 an in-flight PUT and a newer debounced edit both land, in order",
    "D4b an edit queued behind an in-flight PUT is parked and lands after it (M1)",
    "D5 a rebuild with nothing owed sends no PUT",
    "D6 a flush that did not get out: the initial pull keeps the parked edit (M3)",
    "D7 an in-flight PUT refused not_active at the lease loss is pushed on regain (R1)",
    "D7b ...and yields when someone wrote meanwhile",
    "D8 an in-flight PUT that fails with a 500 after the teardown stays owed and lands on regain (R1)",
])
def test_247_3_park_flush(run, label):
    _ok(run, label)


def test_247_3_84_wiring():
    """84 drives the 52 helpers the harness models: teardownView parks instead
    of clearing the debounce timer, and rebuildView flushes the parked edit
    BEFORE its initial pull, with the epoch re-checked across the flush."""
    src = (BROKER_DIR / "84_js_active_view_lifecycle.js").read_text(encoding="utf-8")
    td = src[src.index("        function teardownView() {"):src.index("        async function rebuildView() {")]
    assert "_stateParkPendingPush();" in td
    assert "clearTimeout(_statePushTimer)" not in td
    rb = src[src.index("        async function rebuildView() {"):src.index("        function onRemoteControlStatus(")]
    flush = rb.index("try { await _stateFlushOwed(); } catch (_) {}")
    regate = rb.index("if (_deactivated || epoch !== _viewEpoch) return;", flush)
    pull = rb.index("try { await pullState(true); } catch (_) {}")
    assert flush < regate < pull


@pytest.mark.parametrize("label", [
    "F1 a false debt never reverts the peer write built on it (original base -> 409 -> yield)",
    "F2 a lost reply whose PUT landed is acknowledged by the next pull (no retry)",
    "F3 a thrown PUT that never landed retries on its original base and lands",
    "F4 a new edit (a byte change) after a throw rebases and lands (policy a)",
    "F5 edit-away-and-back (ABA) is new intent: it rebases and lands",
    "F7 a byte-identical save during the debt keeps the original base (S4)",
    "F8 a byte-identical save during a PUT does not re-push over the 409 winner (S4)",
    "F9 a 409 whose winner is our own previous write rebases on it and lands",
    "F10 a peer revert to an older send of ours is not taken for our write: the edit yields (R2)",
    "F11 after an adopt, a peer restoring our last send is a peer write: the edit yields",
    "F12 four failed sends of one edit keep its parent: the late 409 on our own write rebases and lands",
    "D6b ...and lands once the channel recovers (it never yields to our own P1)",
    "E5b after a hung PUT the next edit lands without waiting for a poll",
])
def test_247_5_thrown_failures_and_own_writes(run, label):
    _ok(run, label)


@pytest.mark.parametrize("label", [
    "V1 a 413 goes back to the saved layout at once, says so, and re-sends nothing",
    "V2 a broker-verdict 400 (bad_json) converges the same way",
    "V2b a proxy 400 does not discard the edit",
    "V3 a 413 for an older payload lets the newer edit land (no converge, no notice)",
    "V4 a 413 answered after a teardown converges nothing",
    "V4b the parked edit refused across the teardown is re-sent once by the next view and converges with the notice",
])
def test_247_7_rejected_save_converges(run, label):
    _ok(run, label)


@pytest.mark.parametrize("label", [
    "B1 the first three retries ride the poll cadence (4 PUTs)",
    "B2 after the fast phase the next retry waits 4 s",
    "B3 the back-off doubles to 8, 16, 32 s and caps at 60 s",
    "B4a precondition: the broker recovered and the owed edit landed",
    "B4 after a success a new failure retries at poll cadence again",
    "B5 a new edit restarts the ladder (its retry rides the poll again)",
    "B6 a user save inside the back-off is sent at its normal 600 ms",
    "B7 a late retry (ladder engaged) yields to a peer write (S6)",
    "B7b a fast-phase retry rebases and wins (policy a)",
])
def test_247_9_retry_ladder(run, label):
    _ok(run, label)


def test_247_codex_rollback_rebases_without_a_loop(run):
    hits = [k for k, v in run["results"].items()
            if k.startswith("F13 a rollback showing our parent at a lower rev rebases onto it")]
    assert len(hits) == 1 and run["results"][hits[0]] is True, run["stdout"]


@pytest.mark.parametrize("label", [
    "Z1 a thrown failure parked across a lease loss still retries on its original base and yields (Q2)",
    "Z2 a late retry parked across a lease loss still uses its original base and yields (S6)",
    "Z3 a Q2 retry in flight at the teardown is parked with its base 1 and yields on regain",
    "Z3b a parked edit parked again keeps its first base and yields to the peer",
])
def test_247_z1_park_keeps_the_original_base(run, label):
    _ok(run, label)


@pytest.mark.parametrize("label", [
    "Z4 an initial pull superseded while it waited for the channel sends no GET",
    "Z4a ...and the view that superseded it still pulls and adopts",
    "Z4b the load pull still sends after the first epoch bump while it waited",
])
def test_247_z4_superseded_initial_pull(run, label):
    _ok(run, label)


@pytest.mark.parametrize("label", [
    "Z5 a proxy 400 keeps the edit on screen and the next poll retries it until it lands",
    "Z5b repeated proxy 400s ride the retry ladder (3 fast retries, then back-off)",
])
def test_247_z5_proxy_400_is_a_failure(run, label):
    _ok(run, label)


def test_every_scenario_passed_and_node_exited_clean(run):
    assert run["failed"] == 0, run["stdout"] + run["stderr"]
    assert run["passed"] == EXPECTED_ASSERTS, run["stdout"]
    assert len(run["results"]) == EXPECTED_ASSERTS
    assert run["rc"] == 0, run["stderr"]
