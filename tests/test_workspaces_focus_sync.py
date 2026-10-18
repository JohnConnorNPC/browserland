"""Behavioural tests for the workspaces mod's ``syncFocusedCol`` save (#246).

``syncFocusedCol`` (run on every layout render) clamps ``L.focusedCol`` to the
visible columns and mirrors it into the active workspace entry. Both live in
the SYNCED layout; it used to write them without ``savePrefs()``, leaving the
tab dirty with no push queued, which froze ``/state`` adoption (#246). It now
writes only on a real change and then saves.

A save on render invites two failure modes: a feedback loop (save -> peer
adopts -> peer renders -> peer saves -> ...) and save storms on focus churn.
Both are cross-tab timing facts, so this runs the SHIPPED code end to end in
node: per tab, the real ``mods/workspaces/workspaces.js`` (``init(ctx)``, its
``onLayoutRender`` handler captured) over the real ``52_js_state_sync.js``
plus ``savePrefs`` cut from 51, two tabs sharing one in-memory broker, a
render queued by each adopt's ``requestRelayout`` and the debounce timer
cranked by hand. Core's ``visibleColumns`` is the one stand-in (the mod's
registered column filter applied to ``L.columns``). Skipped when node is
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
MOD_JS = BROKER_DIR / "mods" / "workspaces" / "workspaces.js"
SYNC_JS = BROKER_DIR / "52_js_state_sync.js"
PREFS_JS = BROKER_DIR / "51_js_prefs.js"
CONST_JS = BROKER_DIR / "50_js_constants.js"
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="node not installed")

SAVE_START = "        function savePrefs() {"
SAVE_END = "        function getPref(id) {"

EXPECTED_ASSERTS = 21

_HARNESS = r"""
'use strict';
const fs = require('fs');
const modSrc = fs.readFileSync(__MOD_PATH__, 'utf8');
const sync = fs.readFileSync(__SYNC_PATH__, 'utf8');
const prefsSrc = fs.readFileSync(__PREFS_PATH__, 'utf8');
// #258: the REAL palette, sliced from 50 (colour commands accept only it).
const constSrc = fs.readFileSync(__CONST_PATH__, 'utf8');
const PALETTE = new Function(constSrc.slice(constSrc.indexOf('const PALETTE = ['),
    constSrc.indexOf('];', constSrc.indexOf('const PALETTE = [')) + 2) + ' return PALETTE;')();
const a = prefsSrc.indexOf(__SAVE_START__), b = prefsSrc.indexOf(__SAVE_END__);
if (a < 0 || b < a) throw new Error('savePrefs boundary missing');
const savePrefsSrc = prefsSrc.slice(a, b);

let pass = 0, fail = 0;
function ok(cond, label) {
    if (cond) { pass++; console.log('PASS: ' + label); }
    else { fail++; console.log('FAIL: ' + label); }
}
const J = JSON.stringify;
function clone(o) { return JSON.parse(J(o)); }
async function flush() {
    for (let i = 0; i < 10; i++) await new Promise(function (r) { setImmediate(r); });
}

// ---- DOM stubs (the mod's pager/taskbar chrome) -----------------------------
function mkNode(tag) {
    const node = {
        tagName: tag || 'div', dataset: {}, children: [],
        // #258: a coloured dot sets --ws-color, as in a browser.
        style: { setProperty: function () {}, removeProperty: function () {} },
        setAttribute: function () {},
        textContent: '', title: '', className: '',
        classList: { add: function () {}, remove: function () {},
                     toggle: function () {}, contains: function () { return false; } },
        appendChild: function (k) { node.children.push(k); return k; },
        insertBefore: function (k) { node.children.push(k); return k; },
        removeChild: function () {}, remove: function () {},
        addEventListener: function () {}, removeEventListener: function () {},
        querySelectorAll: function () { return []; },
        querySelector: function () { return null; },
        getBoundingClientRect: function () {
            return { left: 0, top: 0, bottom: 0, right: 0, width: 0, height: 0 };
        },
        offsetWidth: 0, offsetHeight: 0, offsetLeft: 0, offsetTop: 0,
    };
    return node;
}
function mkDocument() {
    const byId = { taskbar: mkNode(), 'host-status': mkNode(),
                   'ws-pager': mkNode(), park: mkNode(), desktop: mkNode() };
    return {
        getElementById: function (id) { return byId[id] || null; },
        createElement: function (t) { return mkNode(t); },
        querySelectorAll: function () { return []; },
        body: mkNode('body'),
    };
}

// ---- the shared broker ---------------------------------------------------------
// Three workspaces; W1 (active) shows c1..c3, W2 holds c4, W3 holds c5.
// Column cN holds window local:N.
function col(n, ws) {
    return { id: 'c' + n, widthPreset: '1/2', wsId: ws,
             rows: [ { height: 1, keys: ['local:' + n], mode: 'single' } ] };
}
const SEED = { mode: 'tiling', focusedCol: 2,
    mods: { workspaces: { version: 1, activeId: 'W1', list: [
        { id: 'W1', focusedCol: 2 }, { id: 'W2', focusedCol: 0 },
        { id: 'W3', focusedCol: 0 } ] } },
    columns: [ col(1, 'W1'), col(2, 'W1'), col(3, 'W1'), col(4, 'W2'),
               col(5, 'W3') ] };
const server = { rev: 1, settings: {}, layout: clone(SEED) };
const puts = [];
async function hostFetch(host, path, opts) {
    if (path !== '/state') throw new Error('unexpected path ' + path);
    if (!opts || !opts.method) {
        const snap = clone(server);
        return { ok: true, status: 200, json: async function () { return snap; } };
    }
    const body = JSON.parse(opts.body);
    puts.push(body);
    if (body.baseRev !== server.rev) {
        const snap = clone(server);
        return { ok: false, status: 409, json: async function () { return snap; } };
    }
    server.rev++;
    server.settings = clone(body.settings);
    server.layout = clone(body.layout);
    const rev = server.rev;
    return { ok: true, status: 200, json: async function () { return { rev: rev }; } };
}

// ---- one tab = the real sync fragment + the real mod ---------------------------
function mkTab(name, withMod) {
    // prefs as loadPrefs() leaves them: the localStorage cache of the seed.
    const t = { name: name, prefs: { _settings: {}, _layout: clone(SEED) }, timers: new Map(), nextTimer: 1,
                renderQueued: false, localSaves: 0, render: null, cmds: {},
                filter: null, lease: null, deactivated: false };
    const syncEnv = {
        prefs: t.prefs,
        isDetachedSurface: function () { return false; },
        hostFetch: hostFetch,
        localHost: function () { return 'local'; },
        CLIENT_ID: 'cid-shared',
        FETCH_TIMEOUT_MS: 5000,
        getSettings: function () { return t.prefs._settings; },
        getLayout: function () { return t.prefs._layout; },
        savePrefsLocal: function () { t.localSaves++; },
        document: { body: { classList: { toggle: function () {} } } },
        isSizeLocked: function () { return false; },
        applyDisplaySettings: function () {},
        requestRelayout: function () { t.renderQueued = true; },
        refreshOpenSectionUIs: function () {},
        setTimeout: function (fn, ms) {
            const id = t.nextTimer++;
            t.timers.set(id, { fn: fn, ms: ms });
            return id;
        },
        clearTimeout: function (id) { t.timers.delete(id); },
    };
    Object.defineProperty(syncEnv, '_deactivated',
        { get: function () { return t.deactivated; } });
    t.sync = new Function('env',
        'with (env) {\n' + sync + '\n' + savePrefsSrc + '\n' +
        'var __api = { pullState: pullState, savePrefs: savePrefs,\n' +
        '  setReady: function (v) { _stateReady = v; },\n' +
        '  st: function () { return { timer: _statePushTimer,\n' +
        '    last: _stateLastSerialized }; } };\n' +
        '}\nreturn __api;\n')(syncEnv);

    function getLayout() { return t.prefs._layout; }
    t.windows = new Map();
    for (let n = 1; n <= 5; n++) {
        t.windows.set('local:' + n, { id: 'local:' + n, disposed: false,
            tiled: true, dom: mkNode() });
    }
    function columnKeys(c) {
        const out = [];
        for (const r of (c.rows || [])) for (const k of r.keys) out.push(k);
        return out;
    }
    function findKeyInLayout(key) {
        const L = getLayout();
        for (let ci = 0; ci < L.columns.length; ci++) {
            const c = L.columns[ci];
            for (let ri = 0; ri < c.rows.length; ri++) {
                const ti = c.rows[ri].keys.indexOf(String(key));
                if (ti !== -1) return { desktop: L, col: c, colIndex: ci,
                    row: c.rows[ri], rowIndex: ri, cell: null, cellIndex: -1,
                    tabIndex: ti };
            }
        }
        return null;
    }
    function visibleColumns() {
        const L = getLayout();
        return t.filter ? L.columns.filter(function (c) { return !!t.filter(c, L); })
                        : L.columns;
    }
    const ctx = {
        events: { on: function (ev, fn) {
            if (ev === 'lease:changed') { t.lease = fn; fn({ active: true }); }
        } },
        settings: {
            select: function () {
                return { get: function () { return 'number'; },
                         set: function () {}, onChange: function () {} };
            },
            boolean: function () {
                return { get: function () { return false; },
                         set: function () {}, onChange: function () {} };
            },
        },
        desktop: { columnFilter: function (fn) { t.filter = fn; },
                   onColumnCreated: function () {}, onPlaced: function () {},
                   onForgotten: function () {}, onReveal: function () {},
                   onLayoutRender: function (fn) { t.render = fn; } },
        taskbar: { onItemsRendered: function () {}, interceptActivate: function () {} },
        onUnload: function () {},
        popover: { anchor: function () { return { close: function () {} }; } },
        prefs: { get: function (k, d) { return d; }, set: function () {} },
        commands: { register: function (id, spec) {
                        t.cmds[id] = spec; return function () {}; },
                    execute: function () { return Promise.resolve({ ok: false }); } },
        registerWindowMenuItems: function () {},
        registerDesktopMenuItems: function () {},
        registerKeyActions: function () {},
    };
    let def = null;
    const modEnv = {
        registerMod: function (d) { def = d; },
        document: mkDocument(), prefs: t.prefs,
        getLayout: getLayout, savePrefs: t.sync.savePrefs,
        savePrefsLocal: function () { t.localSaves++; },
        windows: t.windows, sessions: new Map(), frontId: null,
        relayoutStrip: function () {}, updateTaskbarActive: function () {},
        findKeyInLayout: findKeyInLayout,
        mintLayoutId: function () { return 'M' + (t.nextTimer++); },
        visibleColumns: visibleColumns,
        parkWindow: function () {}, isLiveKey: function () { return false; },
        refitSoon: function () {},
        rowKeys: function (row) { return (row && row.keys) ? row.keys.slice() : []; },
        colCurrentFrac: function () { return 0.5; },
        openConfirmDialog: function () { return Promise.resolve(false); },
        columnKeys: columnKeys,
        openTextPrompt: function () { return Promise.resolve(null); },
        renderMenu: function () {}, expelToNewColumn: function () {},
        requestRelayout: function () { t.renderQueued = true; },
        reconcileTiledFocus: function () {},
        visibleColIndex: function (col) { return visibleColumns().indexOf(col); },
        captureTiledFocusContext: function () { return null; },
        showNotice: function () {}, bringToFront: function () {},
        firstLiveKeyInColumn: function (c) {
            const k = columnKeys(c); return k.length ? k[0] : null; },
        reorderTaskbarItems: function () {},
        PALETTE: PALETTE,
    };
    Object.defineProperty(modEnv, '_deactivated',
        { get: function () { return t.deactivated; } });
    // A mods-off browser: core alone, so nothing hooks the render.
    if (withMod !== false) {
        new Function('env', 'with (env) {\n' + modSrc + '\n}')(modEnv);
        def.init(ctx);
    } else {
        t.render = function () {};
    }

    // The next animation frame: core's relayout lands on the render seam.
    t.frame = function () { t.renderQueued = false; t.render(); };
    t.firePush = async function () {
        for (const [id, x] of Array.from(t.timers)) {
            if (x.ms === 600) { t.timers.delete(id); x.fn(); }
        }
        await flush();
    };
    t.boot = async function () {
        await t.sync.pullState(true);
        t.sync.setReady(true);
        if (t.renderQueued) t.frame();
        await t.firePush();
    };
    // One poll tick: pull, then render whatever the adopt queued, then let
    // any save that render made reach the broker.
    t.tick = async function () {
        await t.sync.pullState(false);
        if (t.renderQueued) t.frame();
        await t.firePush();
    };
    t.L = function () { return t.prefs._layout; };
    t.entry = function () { return t.L().mods.workspaces.list[0]; };
    t.clean = function () {
        return J({ settings: t.prefs._settings, layout: t.prefs._layout })
            === t.sync.st().last;
    };
    return t;
}

(async function () {
    const A = mkTab('A'), B = mkTab('B');
    await A.boot();
    await B.boot();
    ok(puts.length === 0 && server.rev === 1,
        'F1 booting two in-sync tabs saves nothing');
    const savesA = A.localSaves, savesB = B.localSaves;
    A.frame(); B.frame();
    ok(!A.sync.st().timer && !B.sync.st().timer, 'F1a an unchanged render queues no push');
    await A.firePush(); await B.firePush();
    ok(puts.length === 0 && A.localSaves === savesA && B.localSaves === savesB
        && A.clean() && B.clean(),
        'F1b an unchanged render never saves');

    // ---- A6: a peer that adopts a focus change saves nothing back ------------
    // Core's bringToFront writes L.focusedCol and saves WITHOUT a relayout,
    // so its push reaches peers with the workspace entry untouched. The peer
    // must not "repair" anything there: its save would win the race and the
    // source tab's next focus change would 409 and be reverted (#246 A6).
    function coreFocus(tab, v) { tab.L().focusedCol = v; tab.sync.savePrefs(); }
    coreFocus(A, 0);
    await A.firePush();
    ok(puts.length === 1 && puts[0].layout.focusedCol === 0
        && puts[0].layout.mods.workspaces.list[0].focusedCol === 2,
        'F2 precondition: the focus push carries focusedCol 0, entry untouched');
    await B.tick();
    ok(B.L().focusedCol === 0 && puts.length === 1 && B.clean() && !B.sync.st().timer,
        'F2a the peer adopts the focus change and saves nothing back');
    coreFocus(A, 1);                               // the next focus change
    await A.firePush();
    const last = puts[puts.length - 1];
    ok(puts.length === 2 && last.baseRev === server.rev - 1
        && server.layout.focusedCol === 1 && A.L().focusedCol === 1,
        'F2b the source tab\'s next focus change lands (no 409) and sticks');
    await B.tick();
    ok(B.L().focusedCol === 1 && puts.length === 2, 'F2c the peer follows, still silent');

    // ---- no feedback loop: renders on either tab save nothing ---------------
    const revAfterA = server.rev;
    A.frame();
    ok(A.clean() && !A.sync.st().timer, 'F3a the source tab\'s own render does not save');
    for (let i = 0; i < 4; i++) { await A.tick(); await B.tick(); A.frame(); B.frame(); }
    await A.firePush(); await B.firePush();
    ok(server.rev === revAfterA && puts.length === 2,
        'F3b repeated poll/render rounds on both tabs settle: no PUT ping-pong');

    // ---- a mods-off peer's out-of-range index: clamped and saved ONCE -------
    // C runs core without the mod: its focusedCol indexes ALL columns, so
    // focusing c4 (on W2) writes 3 -- out of range for W1's three columns.
    const C = mkTab('C', false);
    await C.boot();
    coreFocus(C, 3);
    await C.firePush();
    ok(server.layout.focusedCol === 3, 'F4 precondition: the mods-off peer wrote index 3');
    const putsBefore = puts.length;
    await A.tick();
    ok(A.L().focusedCol === 2, 'F4 the adopted out-of-range index clamps to the visible columns');
    ok(puts.length === putsBefore + 1 && server.layout.focusedCol === 2,
        'F4a the clamp is saved once');
    const revAfterClamp = server.rev;
    for (let i = 0; i < 3; i++) { await B.tick(); await C.tick(); await A.tick(); }
    ok(server.rev === revAfterClamp && B.L().focusedCol === 2
        && C.L().focusedCol === 2,
        'F4b both peers (mods on and off) adopt the clamp and nobody re-saves');

    // ---- focus churn: only core's own saves, one debounced PUT --------------
    const putsChurn = puts.length, savesChurn = A.localSaves;
    for (const v of [0, 1, 0, 2, 1]) { coreFocus(A, v); A.frame(); }
    await A.firePush();
    ok(puts.length === putsChurn + 1 && puts[puts.length - 1].layout.focusedCol === 1
        && A.localSaves === savesChurn + 5,
        'F5 five focus moves cost ONE PUT and the renders add no saves');
    await B.tick(); await C.tick();

    // ---- the entry is recorded where it is read ------------------------------
    // Switching away and back still returns to the column you left.
    A.cmds['switch'].run({ index: 1 });            // W1 (focus 1) -> W2
    A.frame();
    A.cmds['switch'].run({ index: 0 });            // and back
    A.frame();
    ok(A.L().focusedCol === 1, 'F7 switching away and back returns to the focused column');
    await A.firePush();
    await B.tick(); await C.tick();
    // A send-window between two OTHER workspaces keeps the current focus: the
    // active entry (recorded at the last switch, 1) must not rewind it.
    coreFocus(A, 2);
    await A.firePush();
    A.cmds['send-window'].run({ key: 'local:5', target: 1 });   // W3 -> W2
    ok(A.L().focusedCol === 2
        && A.L().columns.find(function (c) { return c.id === 'c5'; }).wsId === 'W2',
        'F8 a send-window elsewhere keeps the active focus (no rewind to the entry)');
    await A.firePush();

    // ---- a torn-down tab never writes shared state ---------------------------
    A.lease({ active: false });
    A.deactivated = true;
    const localBefore = A.localSaves;
    A.L().focusedCol = 9;
    A.frame();
    ok(A.L().focusedCol === 2 && A.localSaves === localBefore && !A.sync.st().timer,
        'F6 lease lost: the clamp still applies, nothing is saved');

    // ---- #258 SY: order, colours and stacks sync across tabs ----------------
    server.rev = 1; server.settings = {}; server.layout = clone(SEED);
    puts.length = 0;
    const C2 = mkTab('C2'), D2 = mkTab('D2');
    await C2.boot();
    await D2.boot();
    const run = function (t, id, args) { const c = t.cmds[id]; return c ? c.run(args) : null; };
    const wsOf = function (L) { return J(L && L.mods && L.mods.workspaces); };
    run(C2, 'move', { index: 0, direction: 'right' });           // W2,W1,W3
    run(C2, 'color', { index: 0, color: PALETTE[2] });           // W2
    const sk = run(C2, 'stack', { index: 1, target: 2 });        // W1 -> W3: W2,W3,W1
    const sid = sk && sk.stack;
    run(C2, 'rename', { stack: sid, name: 'dev' });
    run(C2, 'color', { stack: sid, color: PALETTE[3] });
    run(C2, 'switch', { index: 1 });                             // W3, a member: lastId
    await C2.firePush();
    const sent = puts.length ? puts[puts.length - 1].layout.mods.workspaces : null;
    ok(puts.length === 1 && !!sent && typeof sid === 'string'
        && sent.list.map(function (w) { return w.id + (w.stack ? '@' : '') + (w.color || ''); }).join(',')
            === 'W2' + PALETTE[2] + ',W3@,W1@'
        && J(sent.stacks) === J([{ id: sid, name: 'dev', color: PALETTE[3], lastId: 'W3' }]),
        'SY1 a reorder, colours, a stack and its last-used member reach the broker in ONE push');
    const putsC = puts.length;
    await D2.tick();
    ok(wsOf(D2.L()) === wsOf(server.layout) && puts.length === putsC && D2.clean()
        && !D2.sync.st().timer,
        'SY2 the second tab adopts them and saves nothing back');

    // A browser with the mod OFF writes the layout: the workspaces store rides along.
    const E2 = mkTab('E2', false);
    await E2.boot();
    E2.L().columns[0].widthPreset = '1/3';                    // a real, workspace-blind edit
    E2.sync.savePrefs();
    await E2.firePush();
    const keptByOff = puts.length === putsC + 1 && server.layout.columns[0].widthPreset === '1/3'
        && wsOf(server.layout) === wsOf(C2.L());
    await C2.tick();
    await D2.tick();
    ok(keptByOff && puts.length === putsC + 1 && wsOf(C2.L()) === wsOf(server.layout)
        && wsOf(D2.L()) === wsOf(server.layout) && C2.clean() && D2.clean(),
        'SY3 a mods-off browser\'s save keeps the order, colours and stacks, and both tabs adopt it silently');

    // SY6: D's own edit loses a 409 to C's; D yields to the stacked winner.
    run(D2, 'switch', { index: 0 });                             // D's edit, not yet sent
    run(C2, 'unstack', { index: 2 });                            // C wins the race
    await C2.firePush();
    const putsWin = puts.length;
    await D2.firePush();                                         // 409 -> adopt C's state
    const inOneTrip = wsOf(D2.L()) === wsOf(server.layout);     // converged in the 409 itself
    if (D2.renderQueued) D2.frame();
    await D2.firePush();
    await D2.tick();
    ok(inOneTrip && puts.length === putsWin + 1 && puts[puts.length - 1].baseRev < server.rev
        && wsOf(D2.L()) === wsOf(server.layout) && D2.clean(),
        'SY6 a tab whose edit lost a 409 adopts the stacked winner and sends nothing more');

    console.log(pass + ' passed, ' + fail + ' failed');
    process.exit(fail ? 1 : 0);
})().catch(function (e) {
    console.log('HARNESS ERROR: ' + (e && e.stack || e));
    console.log(pass + ' passed, ' + (fail + 1) + ' failed');
    process.exit(1);
});
"""


@pytest.fixture(scope="module")
def scenario_run(tmp_path_factory):
    psrc = PREFS_JS.read_text(encoding="utf-8")
    for marker in (SAVE_START, SAVE_END):
        # FAIL, never skip: a moved boundary would grade a stale cut.
        assert psrc.count(marker) == 1, (
            f"{marker!r} occurs {psrc.count(marker)}x in {PREFS_JS}; the "
            "savePrefs cut is unmoored -- retarget this test")
    assert psrc.index(SAVE_START) < psrc.index(SAVE_END)
    harness = tmp_path_factory.mktemp("ws-focus-sync") / "harness.js"
    harness.write_text(
        _HARNESS
        .replace("__MOD_PATH__", json.dumps(str(MOD_JS)))
        .replace("__SYNC_PATH__", json.dumps(str(SYNC_JS)))
        .replace("__PREFS_PATH__", json.dumps(str(PREFS_JS)))
        .replace("__CONST_PATH__", json.dumps(str(CONST_JS)))
        .replace("__SAVE_START__", json.dumps(SAVE_START))
        .replace("__SAVE_END__", json.dumps(SAVE_END)),
        encoding="utf-8")
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
        f"{label!r} {verdict}\nstdout: {run['stdout']}\n"
        f"stderr: {run['stderr']}")


def test_in_sync_renders_never_save(scenario_run):
    _ok(scenario_run, "F1 booting two in-sync tabs saves nothing")
    _ok(scenario_run, "F1a an unchanged render queues no push")
    _ok(scenario_run, "F1b an unchanged render never saves")


def test_a_peer_adopting_a_focus_change_saves_nothing_back(scenario_run):
    # #246 review A6: core's bringToFront saves focusedCol without a
    # relayout; a peer that "repaired" the workspace entry on adopt saved, won,
    # and reverted the source tab's next focus change through a 409.
    _ok(scenario_run, "F2 precondition: the focus push carries focusedCol 0, entry untouched")
    _ok(scenario_run, "F2a the peer adopts the focus change and saves nothing back")
    _ok(scenario_run, "F2b the source tab's next focus change lands (no 409) and sticks")
    _ok(scenario_run, "F2c the peer follows, still silent")


def test_no_feedback_loop_between_tabs(scenario_run):
    _ok(scenario_run, "F3a the source tab's own render does not save")
    _ok(scenario_run, "F3b repeated poll/render rounds on both tabs settle: no PUT ping-pong")


def test_out_of_range_index_is_clamped_and_saved_once(scenario_run):
    # A mods-off browser's focusedCol indexes ALL columns; the mod clamps it
    # to the visible ones and saves that ONCE (state genuinely invalid
    # here), and no tab writes it back.
    _ok(scenario_run, "F4 precondition: the mods-off peer wrote index 3")
    _ok(scenario_run, "F4 the adopted out-of-range index clamps to the visible columns")
    _ok(scenario_run, "F4a the clamp is saved once")
    _ok(scenario_run, "F4b both peers (mods on and off) adopt the clamp and nobody re-saves")


def test_focus_churn_is_one_push(scenario_run):
    _ok(scenario_run, "F5 five focus moves cost ONE PUT and the renders add no saves")


def test_the_workspace_entry_is_recorded_where_it_is_read(scenario_run):
    _ok(scenario_run, "F7 switching away and back returns to the focused column")
    _ok(scenario_run, "F8 a send-window elsewhere keeps the active focus (no rewind to the entry)")


def test_lease_lost_never_saves(scenario_run):
    _ok(scenario_run, "F6 lease lost: the clamp still applies, nothing is saved")


def test_258_order_colours_and_stacks_sync_without_ping_pong(scenario_run):
    # #258: the new fields ride the same synced store under the #246/#247
    # rules -- one push, an adopting tab never writes back, a mods-off peer
    # keeps them, and a 409 loser yields to the stacked winner.
    _ok(scenario_run, "SY1 a reorder, colours, a stack and its last-used member reach the broker in ONE push")
    _ok(scenario_run, "SY2 the second tab adopts them and saves nothing back")
    _ok(scenario_run, "SY3 a mods-off browser's save keeps the order, colours and stacks, and both tabs adopt it silently")
    _ok(scenario_run, "SY6 a tab whose edit lost a 409 adopts the stacked winner and sends nothing more")


def test_every_scenario_passed_and_node_exited_clean(scenario_run):
    run = scenario_run
    assert run["failed"] == 0, (
        f"harness reported failures\nstdout: {run['stdout']}\n"
        f"stderr: {run['stderr']}")
    assert run["passed"] == EXPECTED_ASSERTS, (
        f"expected {EXPECTED_ASSERTS} asserts, harness ran {run['passed']} "
        f"-- scenario drift\nstdout: {run['stdout']}")
    assert len(run["results"]) == EXPECTED_ASSERTS
    assert run["rc"] == 0, (
        f"node exited {run['rc']}\nstdout: {run['stdout']}\n"
        f"stderr: {run['stderr']}")
