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
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="node not installed")

SAVE_START = "        function savePrefs() {"
SAVE_END = "        function getPref(id) {"

EXPECTED_ASSERTS = 16

_HARNESS = r"""
'use strict';
const fs = require('fs');
const modSrc = fs.readFileSync(__MOD_PATH__, 'utf8');
const sync = fs.readFileSync(__SYNC_PATH__, 'utf8');
const prefsSrc = fs.readFileSync(__PREFS_PATH__, 'utf8');
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
        tagName: tag || 'div', style: {}, dataset: {}, children: [],
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
// Two workspaces; W1 (active) shows c1..c3, W2 holds c4. The entry mirrors
// L.focusedCol, so the seed is already in sync.
const SEED = { mode: 'tiling', focusedCol: 2,
    mods: { workspaces: { version: 1, activeId: 'W1', list: [
        { id: 'W1', focusedCol: 2 }, { id: 'W2', focusedCol: 0 } ] } },
    columns: [
        { id: 'c1', widthPreset: '1/2', wsId: 'W1', rows: [] },
        { id: 'c2', widthPreset: '1/2', wsId: 'W1', rows: [] },
        { id: 'c3', widthPreset: '1/2', wsId: 'W1', rows: [] },
        { id: 'c4', widthPreset: '1/2', wsId: 'W2', rows: [] } ] };
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
                renderQueued: false, localSaves: 0, render: null,
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
        commands: { register: function () { return function () {}; },
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
        windows: new Map(), sessions: new Map(), frontId: null,
        relayoutStrip: function () {}, updateTaskbarActive: function () {},
        findKeyInLayout: function () { return null; },
        mintLayoutId: function () { return 'M' + (t.nextTimer++); },
        visibleColumns: visibleColumns,
        parkWindow: function () {}, isLiveKey: function () { return false; },
        refitSoon: function () {},
        rowKeys: function (row) { return (row && row.keys) ? row.keys.slice() : []; },
        colCurrentFrac: function () { return 0.5; },
        openConfirmDialog: function () { return Promise.resolve(false); },
        columnKeys: function () { return []; },
        openTextPrompt: function () { return Promise.resolve(null); },
        renderMenu: function () {}, expelToNewColumn: function () {},
        requestRelayout: function () { t.renderQueued = true; },
        reconcileTiledFocus: function () {},
        visibleColIndex: function (col) { return visibleColumns().indexOf(col); },
        captureTiledFocusContext: function () { return null; },
        showNotice: function () {}, bringToFront: function () {},
        firstLiveKeyInColumn: function () { return null; },
        reorderTaskbarItems: function () {},
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

    // ---- the #246 shape: focus moved, the entry mirror is written on render --
    A.L().focusedCol = 1;          // core's focus write (its own save elided)
    A.frame();
    ok(A.entry().focusedCol === 1, 'F2 render mirrors focusedCol into the entry');
    ok(!!A.sync.st().timer, 'F2a the mirror write queued a push (savePrefs)');
    await A.firePush();
    ok(puts.length === 1 && puts[0].layout.mods.workspaces.list[0].focusedCol === 1
        && puts[0].layout.focusedCol === 1 && A.clean(),
        'F2b the push carries the mirror and the tab is clean again');

    // ---- no feedback loop: the peer adopts and saves nothing back ------------
    const revAfterA = server.rev;
    await B.tick();
    ok(B.entry().focusedCol === 1 && B.L().focusedCol === 1,
        'F3 the peer adopts the saved focus');
    ok(B.clean() && !B.sync.st().timer, 'F3a the peer render after the adopt does not save');
    for (let i = 0; i < 4; i++) { await A.tick(); await B.tick(); A.frame(); B.frame(); }
    await A.firePush(); await B.firePush();
    ok(server.rev === revAfterA && puts.length === 1,
        'F3b repeated poll/render rounds on both tabs settle: no PUT ping-pong');

    // ---- a mods-off peer's out-of-range index: clamped and saved ONCE -------
    // C runs core without the mod: its focusedCol indexes ALL columns, so
    // focusing c4 (on W2) writes 3 -- out of range for W1's three columns.
    const C = mkTab('C', false);
    await C.boot();
    C.L().focusedCol = 3;
    C.sync.savePrefs();            // core's focus write saves itself
    await C.firePush();
    ok(server.layout.focusedCol === 3, 'F4 precondition: the mods-off peer wrote index 3');
    const putsBefore = puts.length;
    await A.tick();
    ok(A.L().focusedCol === 2 && A.entry().focusedCol === 2,
        'F4 the adopted out-of-range index clamps to the visible columns');
    ok(puts.length === putsBefore + 1 && server.layout.focusedCol === 2,
        'F4a the clamp is saved once');
    const revAfterClamp = server.rev;
    for (let i = 0; i < 3; i++) { await B.tick(); await C.tick(); await A.tick(); }
    ok(server.rev === revAfterClamp && B.L().focusedCol === 2
        && C.L().focusedCol === 2,
        'F4b both peers (mods on and off) adopt the clamp and nobody re-saves');
    // Again, but the entry already says 2: only L.focusedCol is out of range.
    C.L().focusedCol = 3;
    C.sync.savePrefs();
    await C.firePush();
    const revOOR = server.rev;
    await A.tick();
    ok(A.entry().focusedCol === 2 && server.layout.focusedCol === 2
        && server.rev === revOOR + 1,
        'F4c an out-of-range focusedCol alone (entry in sync) is still saved');

    // ---- focus churn inside one debounce window: one PUT, last value --------
    const putsChurn = puts.length;
    for (const v of [0, 1, 0, 2, 1]) { A.L().focusedCol = v; A.frame(); }
    await A.firePush();
    ok(puts.length === putsChurn + 1
        && puts[puts.length - 1].layout.mods.workspaces.list[0].focusedCol === 1,
        'F5 five focus moves in one debounce window cost ONE PUT carrying the last');

    // ---- a torn-down tab never writes shared state ---------------------------
    A.lease({ active: false });
    A.deactivated = true;
    const localBefore = A.localSaves;
    A.L().focusedCol = 9;
    A.frame();
    ok(A.L().focusedCol === 2 && A.localSaves === localBefore && !A.sync.st().timer,
        'F6 lease lost: the clamp still applies, nothing is saved');

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


def test_the_focus_mirror_is_saved(scenario_run):
    # #246: the mirror write used to leave the tab dirty with no push queued.
    _ok(scenario_run, "F2 render mirrors focusedCol into the entry")
    _ok(scenario_run, "F2a the mirror write queued a push (savePrefs)")
    _ok(scenario_run, "F2b the push carries the mirror and the tab is clean again")


def test_no_feedback_loop_between_tabs(scenario_run):
    _ok(scenario_run, "F3 the peer adopts the saved focus")
    _ok(scenario_run, "F3a the peer render after the adopt does not save")
    _ok(scenario_run, "F3b repeated poll/render rounds on both tabs settle: no PUT ping-pong")


def test_out_of_range_index_is_clamped_and_saved_once(scenario_run):
    # A mods-off browser's focusedCol indexes ALL columns; the mod clamps it
    # to the visible ones and saves that ONCE, and no tab writes it back.
    _ok(scenario_run, "F4 precondition: the mods-off peer wrote index 3")
    _ok(scenario_run, "F4 the adopted out-of-range index clamps to the visible columns")
    _ok(scenario_run, "F4a the clamp is saved once")
    _ok(scenario_run, "F4b both peers (mods on and off) adopt the clamp and nobody re-saves")
    _ok(scenario_run, "F4c an out-of-range focusedCol alone (entry in sync) is still saved")


def test_focus_churn_is_one_push(scenario_run):
    _ok(scenario_run, "F5 five focus moves in one debounce window cost ONE PUT carrying the last")


def test_lease_lost_never_saves(scenario_run):
    _ok(scenario_run, "F6 lease lost: the clamp still applies, nothing is saved")


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
