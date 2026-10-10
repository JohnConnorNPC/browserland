"""Behavioural tests for the workspaces mod's MCP commands (#244).

``mods/workspaces/workspaces.js`` now registers ``workspaces:list/add/
rename/remove/switch/send-window`` through ``ctx.commands`` (#199, 86h) as
thin wrappers over prompt-free PRIMITIVES (``addWorkspaceNamed`` /
``renameWorkspaceById`` / ``removeWorkspaceById``) that the UI closures call
too. The acceptance facts are behavioural, not textual: ``remove`` refuses a
non-empty workspace with ``{refusal:'needs_confirm'}`` counting LIVE windows
only, ``force`` moves the windows to the ADJACENT workspace without ever
opening a dialog, the last workspace is refused, bad args are refused, and
the UI path and the command path produce IDENTICAL store state through the
same primitive.

So, like ``tests/test_mod_clock.py``, this executes the SHIPPED mod file in
node: the factory injects stubs for every core name the mod reaches for and a
recording ``ctx`` whose ``commands.register`` captures the six specs, then
the scenarios call ``init(ctx)`` per world and drive the captured ``run``
functions (and, for parity, the desktop-menu items whose actions are the UI
closures) against scriptable prompt/confirm spies. PASS/FAIL lines are
parsed by pytest; skipped when node is absent.
"""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from webterm.broker import ui

BROKER_DIR = Path(ui.__file__).resolve().parent
SRC_JS = BROKER_DIR / "mods" / "workspaces" / "workspaces.js"
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="node not installed")

EXPECTED_ASSERTS = 25

_HARNESS = r"""
'use strict';
const fs = require('fs');
const src = fs.readFileSync(__SRC_PATH__, 'utf8');

let pass = 0, fail = 0;
function ok(cond, label) {
    if (cond) { pass++; console.log('PASS: ' + label); }
    else { fail++; console.log('FAIL: ' + label); }
}

// ---- stubs ----------------------------------------------------------------
function mkNode(tag) {
    const node = {
        tagName: tag || 'div', style: {}, dataset: {}, children: [],
        textContent: '', title: '', className: '',
        classList: {
            add: function () {}, remove: function () {},
            toggle: function () {}, contains: function () { return false; },
        },
        appendChild: function (k) { node.children.push(k); return k; },
        insertBefore: function (k) { node.children.push(k); return k; },
        removeChild: function (k) {
            const i = node.children.indexOf(k);
            if (i !== -1) node.children.splice(i, 1);
        },
        remove: function () {},
        addEventListener: function () {},
        removeEventListener: function () {},
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

// ---- one world = one document + layout + maps + spies + one mod init -----
function buildWorld(seedFloatWs) {
    const world = { document: mkDocument() };
    world.L = {
        mode: 'tiling', focusedCol: 0,
        mods: { workspaces: { version: 1, activeId: 'W1', list: [
            { id: 'W1', focusedCol: 0 },
            { id: 'W2', focusedCol: 0 },
            { id: 'W3', focusedCol: 0 },
        ] } },
        columns: [
            { id: 'c1', widthPreset: '1/2', wsId: 'W1', rows: [
                { height: 1, keys: ['local:21'], mode: 'single' } ] },
            { id: 'c2', widthPreset: '1/2', wsId: 'W2', rows: [
                { height: 1, keys: ['local:22'], mode: 'single' } ] },
            { id: 'c3', widthPreset: '1/2', wsId: 'W2', rows: [
                { height: 1, keys: ['local:23'], mode: 'single' } ] },
        ],
    };
    world.windows = new Map();
    world.windows.set('local:21', { id: 'local:21', disposed: false,
        tiled: true, dom: mkNode() });
    world.windows.set('local:22', { id: 'local:22', disposed: false,
        tiled: true, dom: mkNode() });
    // 'local:23' is deliberately ABSENT: a dormant layout key.
    world.sessions = new Map();
    world.prefs = {};
    world.modprefs = seedFloatWs || {};
    world.spies = { prompt: [], confirm: [], notice: [], save: 0 };
    world.nextMint = 100;
    world.frontId = null;

    const w = world;
    function findKeyInLayout(key) {
        for (let ci = 0; ci < w.L.columns.length; ci++) {
            const col = w.L.columns[ci];
            for (let ri = 0; ri < col.rows.length; ri++) {
                const ti = col.rows[ri].keys.indexOf(String(key));
                if (ti !== -1) return { desktop: w.L, col: col, colIndex: ci,
                    row: col.rows[ri], rowIndex: ri, cell: null,
                    cellIndex: -1, tabIndex: ti };
            }
        }
        return null;
    }
    function visibleColumns() { return w.L.columns; }
    function columnKeysFn(col) {
        const out = [];
        for (const r of col.rows) for (const k of r.keys) out.push(k);
        return out;
    }
    function firstLiveKeyInColumn(col) {
        for (const r of col.rows) for (const k of r.keys) return k;
        return null;
    }
    function isLiveKey(k) { return w.windows.has(k); }

    world.commandsCaps = {};
    world.ctx = {
        events: { on: function () {} },
        settings: {
            select: function () {
                return { get: function () { return 'number'; },
                         set: function (v) { w.labelModeSet = v; },
                         onChange: function () {} };
            },
            boolean: function () {
                return { get: function () { return false; },
                         set: function () {}, onChange: function () {} };
            },
        },
        desktop: { columnFilter: function () {}, onColumnCreated: function () {},
                   onPlaced: function () {}, onForgotten: function () {},
                   onReveal: function () {}, onLayoutRender: function () {} },
        taskbar: { onItemsRendered: function () {},
                   interceptActivate: function () {} },
        onUnload: function () {},
        popover: { anchor: function () { return { close: function () {} }; } },
        prefs: {
            get: function (k, d) {
                return Object.prototype.hasOwnProperty.call(w.modprefs, k)
                    ? w.modprefs[k] : d;
            },
            set: function (k, v) { w.modprefs[k] = v; },
        },
        commands: {
            register: function (id, spec) {
                w.commandsCaps[id] = spec; return function () {};
            },
            execute: function () {
                return Promise.resolve({ ok: false, reason: 'absent' });
            },
        },
        registerWindowMenuItems: function () {},
        registerDesktopMenuItems: function (fn) { w.desktopMenu = fn; },
        registerKeyActions: function () {},
    };

    let registered = null;
    function registerMod(def) { registered = def; }
    const factory = new Function('registerMod', 'document', 'prefs',
        'getLayout', 'savePrefs', 'savePrefsLocal', 'windows', 'sessions',
        'frontId', 'relayoutStrip', 'updateTaskbarActive', 'findKeyInLayout',
        'mintLayoutId', 'visibleColumns', 'parkWindow', 'isLiveKey',
        'refitSoon', 'rowKeys', 'colCurrentFrac', 'openConfirmDialog',
        'columnKeys', 'openTextPrompt', 'renderMenu', 'expelToNewColumn',
        'requestRelayout', 'reconcileTiledFocus', 'visibleColIndex',
        'captureTiledFocusContext', 'showNotice', 'bringToFront',
        'firstLiveKeyInColumn', 'reorderTaskbarItems', '_deactivated',
        'console', src);
    factory(registerMod, w.document, w.prefs,
        function () { return w.L; },
        function () { w.spies.save++; },
        function () {},
        w.windows, w.sessions, w.frontId,
        function () {},                       // relayoutStrip
        function () {},                       // updateTaskbarActive
        findKeyInLayout,
        function () { w.nextMint += 1; return 'L' + w.nextMint; },
        visibleColumns,
        function () {},                       // parkWindow
        isLiveKey,
        function () {},                       // refitSoon
        function (row) { return (row && row.keys) ? row.keys.slice() : []; },
        function () { return 0.5; },          // colCurrentFrac
        function (opts) {                     // openConfirmDialog
            w.spies.confirm.push(opts);
            return Promise.resolve(w.confirmAnswer !== undefined
                ? w.confirmAnswer : true);
        },
        columnKeysFn,
        function (opts) {                     // openTextPrompt
            w.spies.prompt.push(opts);
            return Promise.resolve(w.promptAnswer !== undefined
                ? w.promptAnswer : null);
        },
        function () {},                       // renderMenu
        function (win) {                      // expelToNewColumn (stand-in)
            const loc = findKeyInLayout(win.id);
            if (loc) {
                const col = { id: 'L' + (w.nextMint += 1), widthPreset: '1/2',
                              wsId: loc.col.wsId, rows: [
                    { height: 1, keys: [win.id], mode: 'single' } ] };
                w.L.columns.push(col);
            }
        },
        function () {},                       // requestRelayout
        function () {},                       // reconcileTiledFocus
        function (col) { return w.L.columns.indexOf(col); },
        function () { return null; },         // captureTiledFocusContext
        function (m) { w.spies.notice.push(m); },
        function () {},                       // bringToFront
        firstLiveKeyInColumn,
        function () {},                       // reorderTaskbarItems
        false,                               // _deactivated
        console);
    registered.def = registered;              // (shape parity, harmless)
    world.def = registered;
    world.def.init(world.ctx);
    world.commandsCaps = world.commandsCaps;
    world.run = function (id, args) {
        const spec = world.commandsCaps[id];
        return Promise.resolve()
            .then(function () { return spec.run(args); });
    };
    return world;
}

function storeShape(world) {
    const st = world.L.mods.workspaces;
    return JSON.stringify({
        list: st.list.map(function (w) { return { id: w.id, name: w.name || null }; }),
        activeId: st.activeId,
        columnWs: world.L.columns.map(function (c) { return [c.id, c.wsId]; }),
    });
}

(async function () {
    // ---- registration ------------------------------------------------------
    let world = buildWorld({ floatWs: { 'local:9': 'W2' } });
    const expectedIds = ['list', 'add', 'rename', 'remove', 'switch',
                         'send-window'];
    ok(expectedIds.every(function (id) { return !!world.commandsCaps[id]; })
        && Object.keys(world.commandsCaps).length === 6,
        'R1 the six workspaces commands register, and only those');

    // ---- remove: needs_confirm counts LIVE windows only ---------------------
    let r = await world.run('remove', { index: 1 });
    ok(r && r.refusal === 'needs_confirm' && r.windows === 1,
        'RM1 remove non-empty refuses with the LIVE window count');
    ok(world.spies.confirm.length === 0,
        'RM2 the command path never opens a confirm dialog');

    r = await world.run('remove', { index: 1, force: true });
    ok(r && r.removed === true && r.moved === 1,
        'RM3a force removes and reports the moved count');
    const stamps = world.L.columns.map(function (c) { return c.wsId; });
    ok(stamps.indexOf('W2') === -1
        && world.L.columns.every(function (c) { return c.wsId === 'W1'; }),
        'RM3b the victim columns restamp onto the ADJACENT workspace (vi-1)');
    ok(world.L.mods.workspaces.list.length === 2,
        'RM3c the workspace is gone from the list');

    // ---- remove: the last workspace is refused -------------------------------
    world = buildWorld();
    world.L.mods.workspaces.list = [world.L.mods.workspaces.list[0]];
    world.L.columns.forEach(function (c) { c.wsId = 'W1'; });
    r = await world.run('remove', { index: 0, force: true });
    ok(r && r.refusal === 'last_workspace',
        'RM4 the last workspace is refused even with force');
    r = await world.run('remove', { index: 9 });
    ok(r && r.refusal === 'bad_args',
        'RM5 an out-of-range index is bad_args');

    // ---- send-window ----------------------------------------------------------
    world = buildWorld();
    r = await world.run('send-window', { key: 'local:99', target: 0 });
    ok(r && r.refusal === 'unknown_window',
        'SW1 send-window with an unknown key refuses');
    r = await world.run('send-window', { key: 'local:22', target: 9 });
    ok(r && r.refusal === 'bad_args',
        'SW2 send-window with a bad target is bad_args');
    r = await world.run('send-window', { key: 'local:22', target: 2 });
    const movedCol = world.L.columns.filter(function (c) {
        return c.rows.some(function (row) {
            return row.keys.indexOf('local:22') !== -1; });
    })[0];
    ok(r && r.moved === true && movedCol && movedCol.wsId === 'W3',
        'SW3 send-window restamps the column onto the target workspace');

    // ---- add / rename / switch validation -------------------------------------
    world = buildWorld();
    r = await world.run('add', { name: '  extra  ' });
    ok(r && r.index === 3 && typeof r.id === 'string'
        && world.L.mods.workspaces.list[3].name === 'extra'
        && world.L.mods.workspaces.activeId === r.id,
        'AD1 add trims the name, appends and switches');
    r = await world.run('add', { name: '' });
    ok(r && r.refusal === 'bad_args', 'AD2a an empty name is bad_args');
    r = await world.run('add', { name: 'x'.repeat(41) });
    ok(r && r.refusal === 'bad_args', 'AD2b a 41-char name is bad_args');
    r = await world.run('add', { name: 7 });
    ok(r && r.refusal === 'bad_args', 'AD2c a non-string name is bad_args');

    r = await world.run('rename', { index: 0, name: 'main' });
    ok(r && r.renamed === true
        && world.L.mods.workspaces.list[0].name === 'main'
        && world.labelModeSet === 'name',
        'RN1 rename sets the name and the names label mode');
    r = await world.run('rename', { index: 0, name: '   ' });
    ok(r && world.L.mods.workspaces.list[0].name === undefined,
        'RN2 a blank rename clears the name');
    r = await world.run('rename', { index: -1, name: 'x' });
    ok(r && r.refusal === 'bad_args', 'RN3a a bad index is bad_args');
    r = await world.run('rename', { index: 0 });
    ok(r && r.refusal === 'bad_args', 'RN3b a missing name is bad_args');

    r = await world.run('switch', { index: 2 });
    ok(r && world.L.mods.workspaces.activeId === 'W3',
        'ST1 switch changes the active workspace');
    r = await world.run('switch', { index: 5 });
    ok(r && r.refusal === 'bad_args', 'ST2 switch out of range is bad_args');

    // ---- list -------------------------------------------------------------------
    world = buildWorld({ floatWs: { 'local:9': 'W2' } });
    r = await world.run('list');
    ok(r && r.active === 0 && r.items.length === 3
        && r.items[1].id === 'W2' && r.items[1].name === null
        && r.columnWs.c2 === 'W2' && r.columnWs.c1 === 'W1'
        && r.floatWs && r.floatWs['local:9'] === 'W2',
        'LS1 list carries items, columnWs and the float map');

    // ---- UI / command parity through the same primitives -------------------------
    // The desktop menu's Rename/Remove actions target the ACTIVE workspace
    // (index 0 in the seed), so the command side drives index 0 too.
    const wA = buildWorld();
    const rA = await wA.run('rename', { index: 0, name: 'Parity' });
    const wB = buildWorld();
    wB.promptAnswer = 'Parity';
    const items = wB.desktopMenu({ tiling: true });
    const renameItem = items.filter(function (it) {
        return typeof it.label === 'string' && it.label.indexOf('Rename') !== -1;
    })[0];
    await renameItem.action();
    ok(rA && renameItem && storeShape(wA) === storeShape(wB),
        'P1 UI rename and command rename produce identical store state');

    const wC = buildWorld();
    const rC = await wC.run('remove', { index: 0, force: true });
    const wD = buildWorld();
    wD.confirmAnswer = true;
    const itemsD = wD.desktopMenu({ tiling: true });
    const removeItem = itemsD.filter(function (it) {
        return typeof it.label === 'string' && it.label.indexOf('Remove') !== -1;
    })[0];
    await removeItem.action();
    ok(rC && removeItem && wD.spies.confirm.length === 1
        && storeShape(wC) === storeShape(wD),
        'P2 UI remove (confirm) and command remove (force) agree');

    const wE = buildWorld();
    wE.confirmAnswer = false;
    const itemsE = wE.desktopMenu({ tiling: true });
    const removeE = itemsE.filter(function (it) {
        return typeof it.label === 'string' && it.label.indexOf('Remove') !== -1;
    })[0];
    await removeE.action();
    ok(wE.spies.confirm.length === 1
        && wE.L.mods.workspaces.list.length === 3,
        'P3 a refused confirm leaves the workspace in place');

    console.log(pass + ' passed, ' + fail + ' failed');
    process.exitCode = fail ? 1 : 0;
})().catch(function (e) {
    console.error(e && e.stack || e);
    process.exitCode = 1;
});
"""


@pytest.fixture(scope="module")
def scenario_run(tmp_path_factory):
    harness = tmp_path_factory.mktemp("ws-commands") / "harness.js"
    harness.write_text(
        _HARNESS.replace("__SRC_PATH__", json.dumps(str(SRC_JS))),
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
    assert proc.returncode == 0 and tail.group(2) == "0", (
        "harness failed -- no scenario result from this run is trustworthy\n"
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


def test_the_six_commands_register(scenario_run):
    _ok(scenario_run, "R1 the six workspaces commands register, and only those")


def test_remove_refuses_non_empty_with_the_live_count(scenario_run):
    # needs_confirm carries the windows the user is confirming about: live
    # ones. A dormant layout key is not a window.
    _ok(scenario_run, "RM1 remove non-empty refuses with the LIVE window count")
    # No MCP path may ever show a dialog (owner decision on #244).
    _ok(scenario_run, "RM2 the command path never opens a confirm dialog")


def test_force_moves_windows_to_the_adjacent_workspace(scenario_run):
    _ok(scenario_run, "RM3a force removes and reports the moved count")
    _ok(scenario_run, "RM3b the victim columns restamp onto the ADJACENT workspace (vi-1)")
    _ok(scenario_run, "RM3c the workspace is gone from the list")


def test_the_last_workspace_is_refused(scenario_run):
    _ok(scenario_run, "RM4 the last workspace is refused even with force")
    _ok(scenario_run, "RM5 an out-of-range index is bad_args")


def test_send_window_targeting(scenario_run):
    _ok(scenario_run, "SW1 send-window with an unknown key refuses")
    _ok(scenario_run, "SW2 send-window with a bad target is bad_args")
    _ok(scenario_run, "SW3 send-window restamps the column onto the target workspace")


def test_add_and_rename_validation(scenario_run):
    _ok(scenario_run, "AD1 add trims the name, appends and switches")
    _ok(scenario_run, "AD2a an empty name is bad_args")
    _ok(scenario_run, "AD2b a 41-char name is bad_args")
    _ok(scenario_run, "AD2c a non-string name is bad_args")
    _ok(scenario_run, "RN1 rename sets the name and the names label mode")
    _ok(scenario_run, "RN2 a blank rename clears the name")
    _ok(scenario_run, "RN3a a bad index is bad_args")
    _ok(scenario_run, "RN3b a missing name is bad_args")


def test_switch_and_list(scenario_run):
    _ok(scenario_run, "ST1 switch changes the active workspace")
    _ok(scenario_run, "ST2 switch out of range is bad_args")
    _ok(scenario_run, "LS1 list carries items, columnWs and the float map")


def test_ui_and_commands_share_the_primitives(scenario_run):
    # The parity the review demanded: driving the real menu items (whose
    # actions are the UI closures) and driving the commands land on the same
    # store state -- both go through addWorkspaceNamed / renameWorkspaceById /
    # removeWorkspaceById, and a re-duplicated body would drift.
    _ok(scenario_run, "P1 UI rename and command rename produce identical store state")
    _ok(scenario_run, "P2 UI remove (confirm) and command remove (force) agree")
    _ok(scenario_run, "P3 a refused confirm leaves the workspace in place")


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
