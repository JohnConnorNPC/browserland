"""Behavioural tests for #262's browser halves outside the desktop relay.

* ``webterm/broker/75_js_taskbar_hosts.js``: the one bounded catch-up a desktop
  op waits for when it names a window the page has not polled yet
  (``refreshSessionsFor``), together with the REAL ``refreshTaskbar`` whose
  mutex and run promise it waits on.
* open on launch, end to end in one page: the shipped ``session_launched``
  handler of ``64_js_sessions_poll_control.js`` (sliced with the relay
  section above it, so the REAL ``desktopLeaseLost`` and ``_desktopRpcBoot``
  judge the frame), the shipped id-directed auto-open pass of
  ``refreshTaskbarInner`` (75) and the shipped ``seedRestoreQueue`` (84), so
  frame -> park -> open-once -> close -> never again -> reload is observed
  as the windows the page ends up with.

Like ``tests/test_desktop_rpc.py`` this executes the SHIPPED source: each
block is cut AT RUNTIME between its own markers -- never an embedded copy --
and evaluated in node inside a ``with (env)`` scope whose every outer name
(polls, the inner refresh, timers, the lease flag) is a stub the scenario
drives by hand. The fixture FAILS if a marker vanishes. Skipped when node is
absent.
"""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from webterm.broker import ui

BROKER_DIR = Path(ui.__file__).resolve().parent
SRC_64 = BROKER_DIR / "64_js_sessions_poll_control.js"
SRC_75 = BROKER_DIR / "75_js_taskbar_hosts.js"
SRC_84 = BROKER_DIR / "84_js_active_view_lifecycle.js"
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="node not installed")

# The catch-up block: from refreshTaskbar's watchdog comment to its end marker.
CATCHUP_START = "// Hard ceiling on how long ONE refresh run may hold refreshInFlight."
CATCHUP_END = "// ---- end #262 session catch-up"
# The launch handler, sliced from the desktop relay's marker so the relay's
# lease helpers come with it.
LAUNCH_START = "// ---- desktop relay (#244)"
LAUNCH_END = "// ---- end #262 open on launch"
# refreshTaskbarInner's id-directed auto-open pass.
PASS_START = "// ---- id-directed auto-open ----"
PASS_END = "if (!pendingOpens.size) stopFastPoll();"
# Restore-on-refresh's seed (a page reload).
RESTORE_START = "function seedRestoreQueue() {"
RESTORE_END = "if (seeded) startFastPoll();\n        }"
# 84's boot, teardown and rebuild, which own the _viewReady flag.
LIFE_START = "async function bootActiveView() {"
LIFE_END = "// Remote-host lease status."
# 66's z-order block: withoutFocus, focusWin and the real bringToFront.
SRC_66 = BROKER_DIR / "66_js_notices_zorder.js"
Z_START = "// ---- z-order"
Z_END = "function updateTaskbarActive() {"
# 58's layoutAddColumn (it moves focusedCol unless the open is quiet).
SRC_58 = BROKER_DIR / "58_js_layout_mutators.js"
LA_START = "function layoutAddColumn(key, preset, atIndex) {"
LA_END = "function layoutRemoveKey(key) {"

# Every ok() the scenario script makes; pinned so a scenario silently dropped
# from the harness cannot pass by absence.
EXPECTED_ASSERTS = 38

_HARNESS = r"""
'use strict';
const fs = require('fs');
function cut(src, a, z) {
    const i = src.indexOf(a);
    const j = i < 0 ? -1 : src.indexOf(z, i);
    if (i < 0 || j < 0) throw new Error('marker missing: ' + (i < 0 ? a : z));
    return src.slice(i, j + z.length);
}
const slice75 = cut(fs.readFileSync(__SRC75__, 'utf8'), __CU_A__, __CU_Z__);

let pass = 0, fail = 0;
function ok(cond, label) {
    if (cond) { pass++; console.log('PASS: ' + label); }
    else { fail++; console.log('FAIL: ' + label); }
}
let unhandled = 0;
process.on('unhandledRejection', function () { unhandled++; });
function sleep(ms) { return new Promise(function (r) { setTimeout(r, ms); }); }
async function ticks(n) { for (let i = 0; i < (n || 20); i++) await Promise.resolve(); }
function gate() {
    let open = null;
    const p = new Promise(function (r) { open = r; });
    return { p: p, open: open };
}

// ---- the catch-up world -------------------------------------------------
// Timers are counted so a leaked deadline timer is observable; polls and the
// inner refresh are scriptable per scenario.
const pending = new Set();
function fakeSetTimeout(fn, ms) {
    const h = setTimeout(function () { pending.delete(h); fn(); }, ms);
    pending.add(h);
    return h;
}
function fakeClearTimeout(h) { pending.delete(h); clearTimeout(h); }
const W = {};
const pollSt = {};
function resetCU() {
    W.deactivated = false; W.polls = 0; W.inners = 0;
    W.pollImpl = null; W.innerImpl = null;
    for (const k of Object.keys(pollSt)) delete pollSt[k];
}
resetCU();
function pollStateFor(h) {
    if (!pollSt[h]) pollSt[h] = { pollPromise: null };
    return pollSt[h];
}
function pollHostOnce(host) {
    W.polls++;
    const s = pollStateFor(host.id);
    if (s.pollPromise) return null;
    const p = Promise.resolve(W.pollImpl ? W.pollImpl(host) : undefined);
    s.pollPromise = p;
    p.then(function () { s.pollPromise = null; },
           function () { s.pollPromise = null; });
    return p;
}
const envCU = {
    POLL_MS: 20,
    refreshInFlight: false, refreshSeq: 0,
    refreshTaskbarInner: function (seq) {
        W.inners++;
        return W.innerImpl ? W.innerImpl(seq) : Promise.resolve();
    },
    pollStateFor: pollStateFor, pollHostOnce: pollHostOnce,
    hostById: function (id) { return id === 'local' ? { id: 'local' } : null; },
    setTimeout: fakeSetTimeout, clearTimeout: fakeClearTimeout,
};
Object.defineProperty(envCU, '_deactivated', {
    get: function () { return W.deactivated; },
});
const cu = new Function('env',
    'with (env) {\n' + slice75 + '\n' +
    'var __exports = { refreshTaskbar: refreshTaskbar,\n' +
    '  refreshSessionsFor: refreshSessionsFor,\n' +
    '  getRun: function () { return refreshRunPromise; } };\n' +
    '}\nreturn __exports;\n')(envCU);

// ---- the open-on-launch world ------------------------------------------
// One page: its pendingOpens/windows/openTerms, an openWindow that records,
// the lease view (rec + the home flags) and the remote hosts' poll state.
const sliceL = cut(fs.readFileSync(__SRC64__, 'utf8'), __LA_A__, __LA_Z__);
const passSrc = cut(fs.readFileSync(__SRC75__, 'utf8'), __PS_A__, __PS_Z__);
const restoreSrc = cut(fs.readFileSync(__SRC84__, 'utf8'), __RS_A__, __RS_Z__);
const P = {};
function resetPage() {
    P.pendingOpens = new Map(); P.restoreKeys = new Set();
    P.windows = new Map(); P.openTerms = [];
    P.opened = []; P.notices = []; P.fast = 0; P.refreshes = 0;
    P.viewReady = true; P.deactivated = false; P.homeActive = true;
    P.detached = false; P.remote = {}; P.failFast = false;
    P.quietOpens = new Set(); P.quiet = 0; P.quietOpened = [];
}
resetPage();
const envL = {
    homeHostId: function () { return 'local'; },
    pollStateFor: function (h) {
        if (!P.remote[h]) P.remote[h] = { everOk: true, ok: true };
        return P.remote[h];
    },
    isDetachedSurface: function () { return P.detached; },
    AUTO_OPEN_TIMEOUT_MS: 15000,
    startFastPoll: function () {
        if (P.failFast) throw new Error('fast poll exploded');
        P.fast++;
    },
    stopFastPoll: function () {},
    refreshTaskbar: function () { P.refreshes++; return Promise.resolve(); },
    withoutFocus: function (keys, fn) {
        P.quiet++;
        try { return fn(); } finally { P.quiet--; }
    },
    openWindow: function (key, sess) {
        P.opened.push(key);
        if (P.quiet > 0) P.quietOpened.push(key);
        P.windows.set(key, { id: key, disposed: false });
        P.openTerms.push(key);                // addOpenTerm, as openWindow does
    },
    removeOpenTerm: function (key) {
        const i = P.openTerms.indexOf(key);
        if (i !== -1) P.openTerms.splice(i, 1);
    },
    addOpenTerm: function (key) {
        if (P.openTerms.indexOf(key) === -1) P.openTerms.push(key);
    },
    getOpenTerms: function () { return P.openTerms; },
    getSettings: function () { return { restoreOnRefresh: true }; },
    showNotice: function (t) { P.notices.push(t); },
    profilesCache: new Map(), profilesWarmAt: new Map(),
    PROFILES_WARM_RETRY_MS: 1e9,
    fetchProfiles: function () {}, applyWarmProfileColors: function () {},
};
['pendingOpens', 'restoreKeys', 'windows', 'quietOpens'].forEach(function (k) {
    Object.defineProperty(envL, k, { get: function () { return P[k]; } });
});
[['_viewReady', 'viewReady'], ['_deactivated', 'deactivated'],
 ['_homeActive', 'homeActive']].forEach(function (p) {
    Object.defineProperty(envL, p[0], { get: function () { return P[p[1]]; } });
});
const pageL = new Function('env',
    'with (env) {\n' + sliceL + '\n' +
    'function autoOpenPass(merged, hosts) {\n' + passSrc + '\n}\n' +
    restoreSrc + '\n' +
    'var __exports = { noteSessionLaunched: noteSessionLaunched,\n' +
    '  autoOpenPass: autoOpenPass, seedRestoreQueue: seedRestoreQueue };\n' +
    '}\nreturn __exports;\n')(envL);
const REC = { leaseBoot: 'b1', leaseGen: 3 };
function frame(id, o) {
    return Object.assign({ type: 'session_launched', id: id, gen: 3,
                           bootId: 'b1' }, o || {});
}
function poll(keys) {
    const merged = new Map();
    for (const k of keys) merged.set(k, { key: k });
    pageL.autoOpenPass(merged, []);
}
function openedCount(key) {
    return P.opened.filter(function (k) { return k === key; }).length;
}

// ---- the view lifecycle (84) that owns _viewReady -----------------------
const lifeSrc = cut(fs.readFileSync(__SRC84__, 'utf8'), __LF_A__, __LF_Z__);
const V = {};
const envV = {
    _booted: false, _deactivated: true, _viewEpoch: 0, _viewReady: false,
    _stateReady: false, _slowPollTimer: null, frontId: null,
    _stateReadyPromise: null,
    pendingOpens: new Map(), restoreKeys: new Set(),
    windows: new Map(), sessions: new Map(),
    params: { get: function () { return null; } },
    document: { getElementById: function () { return null; } },
    AUTO_OPEN_TIMEOUT_MS: 15000,
    pullState: function () { return V.pull ? V.pull.p : Promise.resolve(); },
    _stateFlushOwed: function () { return Promise.resolve(); },
    isDetachedSurface: function () { return false; },
};
['hideBecomeActiveOverlay', 'showBecomeActiveOverlay', 'restoreAppWindows',
 'seedRestoreQueue', 'refreshTaskbar', 'startSlowPoll', 'startFastPoll',
 'stopFastPoll', 'pruneDetachedSet', '_stateParkPendingPush',
 'updateStripScrollbar', 'applyDisplaySettings', '_emitModEvent',
 'bootDetachedView', 'rebuildDetachedView', 'saveAppWindow'].forEach(
    function (k) { envV[k] = function () {}; });
const life = new Function('env',
    'with (env) {\n' + lifeSrc + '\n' +
    'var __exports = { bootActiveView: bootActiveView,\n' +
    '  teardownView: teardownView, rebuildView: rebuildView };\n' +
    '}\nreturn __exports;\n')(envV);

// ---- the real z-order block (66): withoutFocus -> bringToFront -> focusWin
// focusWin runs for real, so the observable is the window's own
// term.focus() being called (the seam openWindow's attach path ends in).
const zSrc = cut(fs.readFileSync(__SRC66__, 'utf8'), __Z_A__, __Z_Z__)
    .slice(0, -'function updateTaskbarActive() {'.length);
const Z = { rafs: [], focused: [], scrolls: 0, L: null };
function zWin(key, tiled) {
    return { id: key, type: 'terminal', tiled: tiled, minimized: false,
             controlled: false, hostId: 'local',
             term: { focus: function () { Z.focused.push(key); } },
             dom: { classList: { contains: function () { return false; } },
                    style: {} } };
}
const envZ = {
    windows: new Map(), frontId: null, lastTermId: null,
    requestAnimationFrame: function (fn) { Z.rafs.push(fn); },
    hostHidden: function () { return false; },
    findKeyInLayout: function (k) {
        for (const col of Z.L.columns) {
            if (col.rows[0].keys.indexOf(k) !== -1) {
                return { desktop: Z.L, col: col, row: col.rows[0], cell: null };
            }
        }
        return null;
    },
    visibleColIndex: function (col) { return Z.L.columns.indexOf(col); },
    savePrefs: function () {}, requestRelayout: function () {},
    scrollColumnIntoView: function () { Z.scrolls++; },
    updateTaskbarActive: function () {},
    floatZIndex: function () { return 777; },
};
const zApi = new Function('env',
    'with (env) {\n' + zSrc + '\n' +
    'var __exports = { withoutFocus: withoutFocus, bringToFront: bringToFront,\n' +
    '  focusIsQuiet: focusIsQuiet };\n' +
    '}\nreturn __exports;\n')(envZ);
function zReset() {
    // Run (never drop) earlier scopes' pending clears, so no key stays quiet.
    for (let i = 0; i < 4 && Z.rafs.length; i++) zFlush();
    Z.focused = []; Z.scrolls = 0;
    Z.L = { focusedCol: 0, columns: [
        { id: 'c0', rows: [{ keys: ['local:1'], mode: 'single' }] },
        { id: 'c1', rows: [{ keys: ['local:2'], mode: 'single' }] } ] };
    envZ.windows.clear();
    envZ.windows.set('local:1', zWin('local:1', true));
    envZ.windows.set('local:2', zWin('local:2', true));
    envZ.windows.set('local:3', zWin('local:3', false));
    envZ.frontId = 'local:1'; envZ.lastTermId = 'local:1';
}
function zFlush() { const q = Z.rafs.splice(0); for (const f of q) f(); }

// ---- 58's layoutAddColumn ----------------------------------------------
const laSrc = cut(fs.readFileSync(__SRC58__, 'utf8'), __AC_A__, __AC_Z__)
    .slice(0, -'function layoutRemoveKey(key) {'.length);
const LA = { L: null, quiet: false };
const envLA = {
    WIDTH_PRESETS: ['1/3', '1/2', '2/3', 'max'], DEFAULT_NEW_PRESET: '1/2',
    activeDesktop: function () { return LA.L; },
    newColumn: function () { return { id: 'n' + LA.L.columns.length, rows: [] }; },
    newRow: function (keys) { return { keys: keys }; },
    visibleColumns: function () { return LA.L.columns; },
    storageColIndex: function (vi) { return vi; },
    notifyColumnCreated: function () {},
    visibleColIndex: function (col) { return LA.L.columns.indexOf(col); },
    savePrefs: function () {},
    focusIsQuiet: function () { return LA.quiet; },
};
const laApi = new Function('env',
    'with (env) {\n' + laSrc + '\n' +
    'var __exports = { layoutAddColumn: layoutAddColumn };\n' +
    '}\nreturn __exports;\n')(envLA);
function laReset(quiet) {
    LA.quiet = quiet;
    LA.L = { focusedCol: 1, columns: [{ id: 'a' }, { id: 'b' }, { id: 'c' }] };
}

(async function () {
    // ---- H: the catch-up ---------------------------------------------------
    resetCU();
    W.pollImpl = function () { return new Promise(function () {}); };
    let t0 = Date.now();
    await cu.refreshSessionsFor('local', 60);
    const dt = Date.now() - t0;
    ok(dt >= 50 && dt < 1000 && W.inners === 1,
        'H1 a poll that never settles costs at most the budget, then the refresh still runs');
    ok(pending.size === 0, 'H2 no timer is left behind on the slow path');

    resetCU();
    await cu.refreshSessionsFor('local', 5000);
    ok(W.polls === 1 && W.inners === 1 && pending.size === 0,
        'H3 the fast path polls once, refreshes once and leaves no timer behind');

    // A run already in flight may have merged before the poll landed: the
    // catch-up waits for it, THEN runs a fresh refresh of its own.
    resetCU();
    const g1 = gate();
    let firstDone = false, secondAfterFirst = false;
    W.innerImpl = function () {
        if (W.inners === 1) return g1.p.then(function () { firstDone = true; });
        secondAfterFirst = firstDone;
        return Promise.resolve();
    };
    const runX = cu.refreshTaskbar();
    const h3 = cu.refreshSessionsFor('local', 2000);
    await ticks();
    const innersWhileHeld = W.inners;
    g1.open();
    await h3; await runX;
    ok(innersWhileHeld === 1 && W.inners === 2 && secondAfterFirst,
        'H4 an in-flight run is waited for, then a fresh refresh runs after it');

    // A run that takes the mutex just before the catch-up's own call turns
    // that call into the coalesced no-op: the catch-up waits for THAT run.
    resetCU();
    const gX = gate(), gY = gate();
    let yDone = false;
    W.innerImpl = function () {
        if (W.inners === 1) return gX.p;
        return gY.p.then(function () { yDone = true; });
    };
    const runX2 = cu.refreshTaskbar();
    cu.getRun().then(function () { cu.refreshTaskbar(); });   // the foreign run
    let h4done = false;
    const h4 = cu.refreshSessionsFor('local', 2000).then(function () {
        h4done = true;
        return yDone;
    });
    await ticks();
    gX.open();
    await ticks(40);
    const earlyReturn = h4done;
    gY.open();
    const sawY = await h4;
    await runX2;
    ok(!earlyReturn && sawY === true && W.inners === 2,
        'H5 a run that took the mutex first is waited for, not skipped');

    // The watchdog retires a wedged run: its promise goes with it, and the
    // wedged run finishing late never clears the NEXT run's promise.
    resetCU();
    const gW = gate(), gN = gate();
    W.innerImpl = function () { return W.inners === 1 ? gW.p : gN.p; };
    const wedged = cu.refreshTaskbar();
    await sleep(160);                 // > REFRESH_WATCHDOG_MS (POLL_MS * 5)
    const retired = cu.getRun() === null;
    const next = cu.refreshTaskbar();
    gW.open();
    await wedged;
    const kept = cu.getRun() !== null;
    gN.open();
    await next;
    ok(retired, 'H6 the watchdog retires the wedged run\'s promise with its mutex');
    ok(kept && cu.getRun() === null,
        'H7 a retired run finishing late never clears the current run\'s promise');

    // Two ops for one host in the same tick share ONE catch-up.
    resetCU();
    const pA = cu.refreshSessionsFor('local', 2000);
    const pB = cu.refreshSessionsFor('local', 2000);
    await pA; await pB;
    ok(pA === pB && W.polls === 1 && W.inners === 1,
        'H8 concurrent catch-ups for one host share one poll and one refresh');

    resetCU();
    W.pollImpl = function () { return Promise.reject(new Error('poll down')); };
    W.innerImpl = function () { return Promise.reject(new Error('refresh down')); };
    let threw = false;
    try { await cu.refreshSessionsFor('local', 2000); } catch (_) { threw = true; }
    ok(!threw && W.inners === 1,
        'H9 a rejecting poll and refresh never throw, and the refresh still runs');

    resetCU();
    await cu.refreshSessionsFor('elsewhere', 2000);
    ok(W.polls === 0 && W.inners === 0 && pending.size === 0,
        'H10 an unknown host polls nothing and leaves no timer');
    resetCU();
    W.deactivated = true;
    await cu.refreshSessionsFor('local', 2000);
    ok(W.polls === 0 && W.inners === 0,
        'H11 a torn-down view polls nothing');

    // ---- L: open on launch, one page ----------------------------------------
    resetPage();
    let t1 = Date.now();
    pageL.noteSessionLaunched('local', REC, {}, frame(77));
    const dl = P.pendingOpens.get('local:77');
    ok(typeof dl === 'number' && dl >= t1 + 14000 && P.fast === 1
        && P.refreshes === 1 && P.opened.length === 0,
        'L1 a launch frame parks the id with a deadline and the fast poll, opening nothing yet');
    poll([]);
    const stillParked = P.pendingOpens.has('local:77') && P.opened.length === 0;
    poll(['local:77']);
    ok(stillParked && openedCount('local:77') === 1
        && !P.pendingOpens.has('local:77'),
        'L2 it opens exactly once when /sessions lists it, and is unparked');
    P.windows.delete('local:77');                 // the user closes it ...
    envL.removeOpenTerm('local:77');              // ... (closeWindow's step)
    poll(['local:77']); poll(['local:77']); poll(['local:77']);
    ok(openedCount('local:77') === 1,
        'L3 a window the user closed is not reopened by later polls');
    pageL.noteSessionLaunched('local', REC, {}, frame(77));
    poll(['local:77']);
    ok(openedCount('local:77') === 1 && P.pendingOpens.size === 0,
        'L4 a repeated frame for the same launch is ignored');
    P.windows.clear(); P.pendingOpens.clear();    // a reload of this page ...
    pageL.seedRestoreQueue();                     // ... restores openTerms only
    poll(['local:77']);
    ok(openedCount('local:77') === 1,
        'L5 a reload does not reopen the closed window');
    pageL.noteSessionLaunched('local', { leaseBoot: 'b2', leaseGen: 1 }, {},
                              frame(77, { bootId: 'b2', gen: 1 }));
    ok(P.pendingOpens.has('local:77'),
        'L6 the same id from a new broker run is a new launch');

    // A frame this page must not act on parks nothing.
    resetPage();
    const drops = [
        function () { P.homeActive = false; },
        function () { P.deactivated = true; },
        function () { P.viewReady = false; },
        function () { P.detached = true; },
    ];
    let parked = 0;
    for (const d of drops) {
        resetPage(); d();
        pageL.noteSessionLaunched('local', REC, {}, frame(80));
        parked += P.pendingOpens.size;
    }
    resetPage();
    P.remote.remoteX = { everOk: true, ok: true, leaseInactive: true };
    pageL.noteSessionLaunched('remoteX', REC, {}, frame(81));
    parked += P.pendingOpens.size;
    resetPage();
    pageL.noteSessionLaunched('local', REC, {}, frame(82, { gen: 2,
        bootId: 'b1' }));
    pageL.noteSessionLaunched('local', { leaseBoot: 'b1', leaseGen: 4 }, {},
                              frame(83));
    pageL.noteSessionLaunched('local', REC, {}, frame(84, { bootId: 'bX' }));
    parked += P.pendingOpens.size;
    ok(parked === 0 && P.fast === 0,
        'L7 no lease, a torn-down or booting view, the detached surface, a masked remote, an older generation or another run parks nothing');
    resetPage();
    for (const bad of ['85', true, 1.5, -1, undefined, null, 2 ** 60]) {
        pageL.noteSessionLaunched('local', REC, {}, frame(bad));
    }
    pageL.noteSessionLaunched('local', REC, {}, null);
    ok(P.pendingOpens.size === 0,
        'L8 a frame without a usable id parks nothing');
    resetPage();
    P.viewReady = false;
    pageL.noteSessionLaunched('local', REC, {}, frame(90));
    P.viewReady = true;
    pageL.noteSessionLaunched('local', REC, {}, frame(90));
    ok(P.pendingOpens.has('local:90'),
        'L9 a frame dropped during boot is not remembered, so a later one parks');
    resetPage();
    P.windows.set('local:91', { id: 'local:91', disposed: false });
    P.pendingOpens.set('local:92', Date.now() + 1000);
    pageL.noteSessionLaunched('local', REC, {}, frame(91));
    pageL.noteSessionLaunched('local', REC, {}, frame(92));
    ok(P.fast === 0 && P.pendingOpens.size === 1,
        'L10 an id already open or already parked is left alone');
    resetPage();
    pageL.noteSessionLaunched('local', REC, {}, frame(93));
    P.pendingOpens.set('local:93', Date.now() - 1);
    poll([]);
    ok(!P.pendingOpens.has('local:93') && P.notices.length === 1
        && P.notices[0] === 'session 93 not found' && P.opened.length === 0,
        'L11 an unopened seed expires with the UI launch\'s notice');
    resetPage();
    pageL.noteSessionLaunched('local', REC, {}, frame(94));
    P.pendingOpens.clear();                       // teardownView
    poll(['local:94']);
    pageL.noteSessionLaunched('local', REC, {}, frame(94));
    ok(P.opened.length === 0 && P.pendingOpens.size === 0,
        'L12 a seed a teardown cleared is not reopened, nor re-parked by the same launch');
    resetPage();
    P.failFast = true;
    let threwL = false;
    try { pageL.noteSessionLaunched('local', REC, {}, frame(95)); }
    catch (_) { threwL = true; }
    ok(!threwL, 'L13 a failing seed never throws out of the frame handler');

    // ---- V: _viewReady follows the real boot / teardown / rebuild ---------
    const gS = gate();
    envV._stateReadyPromise = gS.p;
    const boot = life.bootActiveView();
    await ticks();
    const duringBoot = envV._viewReady;
    gS.open();
    await boot;
    ok(duringBoot === false && envV._viewReady === true,
        'V1 the view is ready only once the boot has restored it');
    life.teardownView();
    ok(envV._viewReady === false, 'V2 a teardown clears it at once');
    V.pull = gate();
    const rebuild = life.rebuildView();
    await ticks();
    const duringRebuild = envV._viewReady;
    V.pull.open();
    await rebuild;
    V.pull = null;
    ok(duringRebuild === false && envV._viewReady === true,
        'V3 a rebuild sets it only once it has restored the view');
    life.teardownView();
    V.pull = gate();
    const superseded = life.rebuildView();
    await ticks();
    life.teardownView();                  // lost again mid-rebuild
    V.pull.open();
    await superseded;
    V.pull = null;
    ok(envV._viewReady === false,
        'V4 a rebuild a teardown superseded never marks the view ready');

    // ---- F: #262 a launch frame's window opens without focus -------------
    resetPage();
    pageL.noteSessionLaunched('local', REC, {}, frame(96));
    P.pendingOpens.set('local:97', Date.now() + 15000);   // the UI's own launch
    poll(['local:96', 'local:97']);
    ok(P.quietOpened.length === 1 && P.quietOpened[0] === 'local:96'
        && openedCount('local:97') === 1 && P.quietOpens.size === 0,
        'F1 a launched window opens without focus; the UI\'s own launch still focuses');
    resetPage();
    pageL.noteSessionLaunched('local', REC, {}, frame(98));
    P.pendingOpens.clear();                               // teardownView
    poll([]);
    ok(P.quietOpens.size === 0,
        'F2 a quiet mark whose park a teardown cleared is dropped by the next pass');

    // ---- Z: the real bringToFront under withoutFocus ----------------------
    zReset();
    zApi.bringToFront('local:2');
    ok(Z.focused.join() === 'local:2' && envZ.lastTermId === 'local:2'
        && envZ.frontId === 'local:2' && Z.L.focusedCol === 1 && Z.scrolls === 1,
        'Z1 an ordinary bringToFront focuses, moves lastTermId and scrolls');
    zReset();
    zApi.withoutFocus(['local:2'], function () { zApi.bringToFront('local:2'); });
    ok(Z.focused.length === 0 && envZ.lastTermId === 'local:1'
        && envZ.frontId === 'local:1' && Z.L.focusedCol === 0 && Z.scrolls === 0,
        'Z2 inside withoutFocus nothing moves: focus, lastTermId, frontId, the focused column');
    zReset();
    zApi.withoutFocus(['local:3'], function () { zApi.bringToFront('local:3'); });
    const w3 = envZ.windows.get('local:3');
    ok(Z.focused.length === 0 && w3.dom.style.zIndex === '777'
        && envZ.frontId === 'local:1',
        'Z3 a quiet float still stacks, without focus');
    zReset();
    zApi.withoutFocus(['local:2'], function () {
        envZ.requestAnimationFrame(function () { zApi.bringToFront('local:2'); });
    });
    zFlush();                          // the deferred re-entry, one frame on
    const deferredQuiet = Z.focused.length === 0;
    zFlush();                          // the quiet window closes
    zApi.bringToFront('local:2');      // a later click
    ok(deferredQuiet && Z.focused.join() === 'local:2',
        'Z4 a bringToFront deferred to the next frame stays quiet; a later one focuses');
    zReset();
    try {
        zApi.withoutFocus(['local:2'], function () { throw new Error('mutator threw'); });
    } catch (_) {}
    zApi.bringToFront('local:1');
    ok(Z.focused.join() === 'local:1',
        'Z5 a throwing scope still ends: other windows focus again at once');
    zReset();
    zApi.withoutFocus(['local:2'], function () {});
    zApi.withoutFocus(['local:2'], function () {});
    zFlush(); zFlush();                // the first scope's clear runs ...
    const stillQuiet = zApi.focusIsQuiet('local:2');
    ok(stillQuiet === false,
        'Z6 overlapping scopes on one key all clear');

    // ---- LA: layoutAddColumn keeps the focused column when quiet ----------
    laReset(false);
    laApi.layoutAddColumn('local:9');
    const loud = LA.L.focusedCol;
    laReset(true);
    laApi.layoutAddColumn('local:9');
    const quietAppend = LA.L.focusedCol;
    laReset(true);
    const focusedBefore = LA.L.columns[1];
    laApi.layoutAddColumn('local:9', '1/2', 0);
    ok(loud === 3 && quietAppend === 1
        && LA.L.columns[LA.L.focusedCol] === focusedBefore,
        'LA1 a quiet new column keeps the focused column (an insert before it shifts the index)');

    await sleep(20);
    ok(unhandled === 0, 'H12 no unhandled rejection escaped any scenario');

    console.log(pass + ' passed, ' + fail + ' failed');
    process.exitCode = fail ? 1 : 0;
})().catch(function (e) {
    console.error(e && e.stack || e);
    process.exitCode = 1;
});
"""


@pytest.fixture(scope="module")
def scenario_run(tmp_path_factory):
    # FAIL, never skip: without the markers the slice is unmoored from the
    # live source and every green assert below would grade a stale copy.
    for path, markers in ((SRC_75, (CATCHUP_START, CATCHUP_END,
                                    PASS_START, PASS_END)),
                          (SRC_64, (LAUNCH_START, LAUNCH_END)),
                          (SRC_84, (RESTORE_START, RESTORE_END,
                                    LIFE_START, LIFE_END)),
                          (SRC_66, (Z_START, Z_END)),
                          (SRC_58, (LA_START, LA_END))):
        src = path.read_text(encoding="utf-8")
        for marker in markers:
            assert src.count(marker) == 1, (
                f"{marker!r} occurs {src.count(marker)}x in {path}; the "
                "slice boundary is missing or ambiguous")
    harness = tmp_path_factory.mktemp("mcp-launch-open") / "harness.js"
    subst = {"__SRC64__": SRC_64, "__SRC75__": SRC_75, "__SRC84__": SRC_84,
             "__CU_A__": CATCHUP_START, "__CU_Z__": CATCHUP_END,
             "__LA_A__": LAUNCH_START, "__LA_Z__": LAUNCH_END,
             "__PS_A__": PASS_START, "__PS_Z__": PASS_END,
             "__RS_A__": RESTORE_START, "__RS_Z__": RESTORE_END,
             "__LF_A__": LIFE_START, "__LF_Z__": LIFE_END,
             "__SRC66__": SRC_66, "__Z_A__": Z_START, "__Z_Z__": Z_END,
             "__SRC58__": SRC_58, "__AC_A__": LA_START, "__AC_Z__": LA_END}
    text = _HARNESS
    for k, v in subst.items():
        text = text.replace(k, json.dumps(str(v)))
    harness.write_text(text, encoding="utf-8")
    proc = subprocess.run([NODE, str(harness)],
                          capture_output=True, text=True, encoding="utf-8",
                          timeout=120)
    lines = (proc.stdout or "").strip().splitlines()
    tail = re.match(r"^(\d+) passed, (\d+) failed$",
                    lines[-1] if lines else "")
    assert tail is not None, (
        "harness produced no machine-readable tail (node crashed?)\n"
        f"rc={proc.returncode}\nstdout={proc.stdout}\nstderr={proc.stderr}")
    results = {}
    for line in lines[:-1]:
        if line.startswith("PASS: "):
            results[line[len("PASS: "):]] = True
        elif line.startswith("FAIL: "):
            results[line[len("FAIL: "):]] = False
    return {"rc": proc.returncode, "stdout": proc.stdout,
            "stderr": proc.stderr, "passed": int(tail.group(1)),
            "failed": int(tail.group(2)), "results": results}


def _ok(run, label):
    got = run["results"].get(label)
    verdict = "failed" if got is False else "never ran"
    assert got is True, (
        f"scenario {label!r} {verdict}\n"
        f"stdout: {run['stdout']}\nstderr: {run['stderr']}")


# ---- the catch-up (refreshSessionsFor) --------------------------------------


def test_the_catch_up_is_bounded_and_leaves_no_timer(scenario_run):
    _ok(scenario_run, "H1 a poll that never settles costs at most the budget, then the refresh still runs")
    _ok(scenario_run, "H2 no timer is left behind on the slow path")
    _ok(scenario_run, "H3 the fast path polls once, refreshes once and leaves no timer behind")


def test_the_catch_up_waits_for_the_run_that_holds_the_mutex(scenario_run):
    # refreshTaskbar coalesces (`if (refreshInFlight) return;`), so a bare
    # call can be a no-op; the catch-up waits on the owning run instead.
    _ok(scenario_run, "H4 an in-flight run is waited for, then a fresh refresh runs after it")
    _ok(scenario_run, "H5 a run that took the mutex first is waited for, not skipped")


def test_the_run_promise_is_owned_like_the_mutex(scenario_run):
    _ok(scenario_run, "H6 the watchdog retires the wedged run's promise with its mutex")
    _ok(scenario_run, "H7 a retired run finishing late never clears the current run's promise")


def test_concurrent_catch_ups_coalesce_per_host(scenario_run):
    _ok(scenario_run, "H8 concurrent catch-ups for one host share one poll and one refresh")


def test_the_catch_up_never_throws_and_skips_what_it_cannot_reach(scenario_run):
    _ok(scenario_run, "H9 a rejecting poll and refresh never throw, and the refresh still runs")
    _ok(scenario_run, "H10 an unknown host polls nothing and leaves no timer")
    _ok(scenario_run, "H11 a torn-down view polls nothing")
    _ok(scenario_run, "H12 no unhandled rejection escaped any scenario")


# ---- open on launch (session_launched) ----------------------------------------


def test_a_launch_frame_opens_the_window_once(scenario_run):
    # The frame only parks the id; the existing auto-open pass opens it the
    # moment /sessions lists it -- once.
    _ok(scenario_run, "L1 a launch frame parks the id with a deadline and the fast poll, opening nothing yet")
    _ok(scenario_run, "L2 it opens exactly once when /sessions lists it, and is unparked")
    _ok(scenario_run, "L11 an unopened seed expires with the UI launch's notice")


def test_a_closed_window_stays_closed(scenario_run):
    # The owner's constraint: a terminal the user closed stays a chip --
    # no later poll, repeated frame or reload brings it back.
    _ok(scenario_run, "L3 a window the user closed is not reopened by later polls")
    _ok(scenario_run, "L4 a repeated frame for the same launch is ignored")
    _ok(scenario_run, "L5 a reload does not reopen the closed window")
    _ok(scenario_run, "L6 the same id from a new broker run is a new launch")
    _ok(scenario_run, "L12 a seed a teardown cleared is not reopened, nor re-parked by the same launch")


def test_only_the_lease_holder_with_a_live_view_parks(scenario_run):
    _ok(scenario_run, "L7 no lease, a torn-down or booting view, the detached surface, a masked remote, an older generation or another run parks nothing")
    _ok(scenario_run, "L8 a frame without a usable id parks nothing")
    _ok(scenario_run, "L9 a frame dropped during boot is not remembered, so a later one parks")
    _ok(scenario_run, "L10 an id already open or already parked is left alone")
    _ok(scenario_run, "L13 a failing seed never throws out of the frame handler")


def test_the_view_ready_flag_follows_the_real_lifecycle(scenario_run):
    # The handler drops a frame unless the view is fully up; _booted and
    # !_deactivated turn true before a boot's first await, so 84 keeps its
    # own flag, set at the end of a boot/rebuild and cleared by a teardown.
    _ok(scenario_run, "V1 the view is ready only once the boot has restored it")
    _ok(scenario_run, "V2 a teardown clears it at once")
    _ok(scenario_run, "V3 a rebuild sets it only once it has restored the view")
    _ok(scenario_run, "V4 a rebuild a teardown superseded never marks the view ready")


def test_an_open_the_user_did_not_ask_for_takes_no_focus(scenario_run):
    # Adversary M1: a launched window must not pull keyboard focus into an
    # agent's terminal. The pass opens a frame-parked key withoutFocus; the
    # REAL bringToFront (66) and layoutAddColumn (58) then move nothing.
    _ok(scenario_run, "F1 a launched window opens without focus; the UI's own launch still focuses")
    _ok(scenario_run, "F2 a quiet mark whose park a teardown cleared is dropped by the next pass")
    _ok(scenario_run, "Z1 an ordinary bringToFront focuses, moves lastTermId and scrolls")
    _ok(scenario_run, "Z2 inside withoutFocus nothing moves: focus, lastTermId, frontId, the focused column")
    _ok(scenario_run, "Z3 a quiet float still stacks, without focus")
    _ok(scenario_run, "Z4 a bringToFront deferred to the next frame stays quiet; a later one focuses")
    _ok(scenario_run, "Z5 a throwing scope still ends: other windows focus again at once")
    _ok(scenario_run, "Z6 overlapping scopes on one key all clear")
    _ok(scenario_run, "LA1 a quiet new column keeps the focused column (an insert before it shifts the index)")


def test_the_launch_frame_is_wired_into_the_control_dispatcher():
    # The onmessage dispatcher is outside every slice, so pin the wiring by
    # source: the new type routes to the handler BEFORE the status
    # fall-through (which is what makes an older page ignore it).
    src = SRC_64.read_text(encoding="utf-8")
    route = "if (msg.type === 'session_launched') {"
    assert src.count(route) == 1
    assert "noteSessionLaunched(hid, rec, ws, msg);" in src
    assert src.index(route) < src.index(
        "if (!msg || msg.type !== 'status') return;")


def test_every_scenario_passed_and_node_exited_clean(scenario_run):
    run = scenario_run
    assert run["failed"] == 0, (
        f"harness reported failures\nstdout: {run['stdout']}\n"
        f"stderr: {run['stderr']}")
    assert run["passed"] == EXPECTED_ASSERTS, (
        f"expected {EXPECTED_ASSERTS} asserts, harness ran {run['passed']} "
        f"-- scenario drift\nstdout: {run['stdout']}")
    assert len(run["results"]) == EXPECTED_ASSERTS, (
        f"expected {EXPECTED_ASSERTS} labelled results, "
        f"parsed {len(run['results'])}\nstdout: {run['stdout']}")
    assert run["rc"] == 0, (
        f"node exited {run['rc']}\nstdout: {run['stdout']}\n"
        f"stderr: {run['stderr']}")
