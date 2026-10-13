"""#247 item 8: the workspaces mod remembers each workspace's focused column by ID.

The entry's ``focusedCol`` index alone points at whatever column slid into
its slot once the column set changes (a column sent away, a workspace merged
in). The entry now also records ``focusedColId`` with the index it was taken
at (``focusedColIdAt``); the id is trusted only while ``focusedCol`` still
equals that index, so an index-only writer (a browser without this code)
still wins. Runs the SHIPPED mod in node over the real 52 sync fragment (the
#246 focus-sync harness's tab plumbing), one fresh broker per scenario.
Skipped when node is absent.
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

EXPECTED_ASSERTS = 11

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

// ---- a world = one broker seeded per scenario ---------------------------------
// Column cN holds window local:N.
function col(n, ws) {
    return { id: 'c' + n, widthPreset: '1/2', wsId: ws,
             rows: [ { height: 1, keys: ['local:' + n], mode: 'single' } ] };
}
// W1 (active) shows c1..c3, W2 holds c4 + c6, W3 holds c5.
function seed(o) {
    o = o || {};
    return { mode: 'tiling', focusedCol: (o.focusedCol === undefined) ? 1 : o.focusedCol,
        mods: { workspaces: { version: 1, activeId: o.activeId || 'W1', list: o.list || [
            { id: 'W1', focusedCol: 1 }, { id: 'W2', focusedCol: 0 },
            { id: 'W3', focusedCol: 0 } ] } },
        columns: [ col(1, 'W1'), col(2, 'W1'), col(3, 'W1'), col(4, 'W2'),
                   col(6, 'W2'), col(5, 'W3') ] };
}
async function mkWorld(o) {
    const SEED = seed(o);
    const server = { rev: 1, settings: {}, layout: clone(SEED) };
    async function hostFetch(host, path, opts) {
        if (path !== '/state') throw new Error('unexpected path ' + path);
        if (!opts || !opts.method) {
            const snap = clone(server);
            return { ok: true, status: 200, json: async function () { return snap; } };
        }
        const body = JSON.parse(opts.body);
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
    const t = mkTab('A', true, SEED, hostFetch);
    await t.boot();
    t.server = server;
    // Which column focus is on (the visible strip's index -> its id).
    t.focusId = function () {
        const L = t.L();
        const vis = L.columns.filter(function (c) { return !!t.filter(c, L); });
        const c = vis[L.focusedCol];
        return c ? c.id : null;
    };
    t.ws = function (id) {
        return t.L().mods.workspaces.list.find(function (w) { return w.id === id; });
    };
    t.run = function (cmd, args) { const r = t.cmds[cmd].run(args); t.frame(); return r; };
    return t;
}

// ---- one tab = the real sync fragment + the real mod ---------------------------
function mkTab(name, withMod, SEED, hostFetch) {
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
    for (let n = 1; n <= 6; n++) {
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
    let t;
    // ---- W1: a column sent away from LEFT of focus keeps focus on its column
    t = await mkWorld();                                 // W1 [c1,c2,c3], focus c2
    ok(t.focusId() === 'c2', 'W1 precondition: focus is on c2');
    t.run('send-window', { key: 'local:1', target: 2 });   // c1 -> W3
    ok(t.focusId() === 'c2' && t.L().focusedCol === 0,
        'W1 sending away a column left of focus keeps focus on the same column');
    t.run('switch', { index: 1 });
    t.run('switch', { index: 0 });
    ok(t.focusId() === 'c2', 'W1a switching away and back still returns to that column');
    await t.firePush();
    ok(t.server.layout.mods.workspaces.list[0].focusedColId === 'c2'
        && t.server.layout.mods.workspaces.list[0].focusedColIdAt === 0,
        'W1b the id is saved with the index it was taken at');
    // The target remembers the sent column by id too.
    t.run('switch', { index: 2 });
    ok(t.focusId() === 'c1', 'W1c the target workspace focuses the column it received');

    // ---- W2: sending away the FOCUSED column falls back to the clamped index
    t = await mkWorld({ focusedCol: 2, list: [ { id: 'W1', focusedCol: 2 },
        { id: 'W2', focusedCol: 0 }, { id: 'W3', focusedCol: 0 } ] });
    t.run('send-window', { key: 'local:3', target: 1 });   // focused c3 -> W2
    ok(t.focusId() === 'c2' && t.L().focusedCol === 1,
        'W2 sending away the focused column lands on the clamped neighbour');

    // ---- W3: removing the ACTIVE workspace lands on the neighbour's column by id
    t = await mkWorld();
    t.run('switch', { index: 1 });                       // W2 [c4,c6], focus c4
    t.L().focusedCol = 1;                                // focus c6
    t.run('switch', { index: 0 });                       // W2 remembers c6
    t.run('remove', { index: 0, force: true });          // W1 merges into W2
    ok(t.L().mods.workspaces.activeId === 'W2' && t.focusId() === 'c6',
        'W3 removing the active workspace keeps the neighbour\'s remembered column');

    // ---- W4: a non-active workspace merged in keeps the active focus ------
    t = await mkWorld();
    t.run('switch', { index: 1 });                       // W2 [c4,c6]
    t.L().focusedCol = 1;                                // focus c6
    t.run('remove', { index: 0, force: true });          // W1's c1..c3 join W2
    ok(t.L().mods.workspaces.activeId === 'W2' && t.focusId() === 'c6',
        'W4 a non-active workspace merging in keeps focus on the same column');

    // ---- W5: a garbage / unknown id is ignored: the index wins -------------
    t = await mkWorld({ list: [ { id: 'W1', focusedCol: 1 },
        { id: 'W2', focusedCol: 1, focusedColId: 'gone', focusedColIdAt: 1 },
        { id: 'W3', focusedCol: 0, focusedColId: 42, focusedColIdAt: 0 } ] });
    t.run('switch', { index: 1 });
    ok(t.focusId() === 'c6', 'W5 an id naming no column falls back to the index');
    ok(!('focusedColId' in t.ws('W3')) && !('focusedColIdAt' in t.ws('W3')),
        'W5a a non-string id is healed away');

    // ---- W6: an index-only writer moved focus: the index wins --------------
    // An older browser wrote focusedCol 1 and left this code's id (c4 at 0).
    t = await mkWorld({ list: [ { id: 'W1', focusedCol: 1 },
        { id: 'W2', focusedCol: 1, focusedColId: 'c4', focusedColIdAt: 0 },
        { id: 'W3', focusedCol: 0 } ] });
    t.run('switch', { index: 1 });
    ok(t.focusId() === 'c6', 'W6 an index changed by an index-only writer beats the stale id');

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
    harness = tmp_path_factory.mktemp("ws-focus-memory") / "harness.js"
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


@pytest.mark.parametrize("label", [
    "W1 precondition: focus is on c2",
    "W1 sending away a column left of focus keeps focus on the same column",
    "W1a switching away and back still returns to that column",
    "W1b the id is saved with the index it was taken at",
    "W1c the target workspace focuses the column it received",
    "W2 sending away the focused column lands on the clamped neighbour",
    "W3 removing the active workspace keeps the neighbour's remembered column",
    "W4 a non-active workspace merging in keeps focus on the same column",
    "W5 an id naming no column falls back to the index",
    "W5a a non-string id is healed away",
    "W6 an index changed by an index-only writer beats the stale id",
])
def test_247_8_focus_memory(scenario_run, label):
    _ok(scenario_run, label)


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
