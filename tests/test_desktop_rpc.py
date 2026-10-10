"""Behavioural tests for the desktop-relay section of
``webterm/broker/64_js_sessions_poll_control.js`` (#244).

The ``// ---- desktop relay (#244)`` section is the browser half of the MCP
desktop relay: it answers ``desktop_rpc`` frames from the broker with exactly
one ``desktop_result`` each, resolves broker-local int ids against the
DELIVERING connection's host id, gates every op on the caller's ``visible``
set (the owner's "a non-admin agent may arrange only the windows it can see",
enforced over the AFFECTED topology only this browser knows), flushes
``pushState`` before acknowledging a mutation, re-checks the lease after each
await, and re-serves cached answers for replayed rids. All of those are
ORDERING/OBSERVABILITY facts a source-slice assert can only quote; running
the SHIPPED section in node against recording mutators proves them.

So, like ``tests/test_visibility_timers.py``, this executes the SHIPPED file:
the section is cut AT RUNTIME at its own marker -- never an embedded copy --
and evaluated in node inside a ``with (env)`` function scope whose every outer
name (the mutators, the maps, ``_deactivated`` via a live getter,
``pushState`` behind a manual gate) is a stub the scenario script drives by
hand. The harness prints one PASS/FAIL line per assert plus a
machine-readable tail; the fixture FAILS if the marker vanishes (a green
result against a stale slice would be a lie). Skipped when node is absent.
"""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from webterm.broker import ui

BROKER_DIR = Path(ui.__file__).resolve().parent
SRC_JS = BROKER_DIR / "64_js_sessions_poll_control.js"
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="node not installed")

# The block under test starts at its own section marker and runs to the end
# of the fragment; the marker IS the boundary (unique, or the fixture fails).
MARKER = "// ---- desktop relay (#244)"

# Every ok() the scenario script makes; pinned so a scenario silently dropped
# from the harness cannot pass by absence.
EXPECTED_ASSERTS = 37

_HARNESS = r"""
'use strict';
const fs = require('fs');
const src = fs.readFileSync(__SRC_PATH__, 'utf8');
const cut = src.indexOf('// ---- desktop relay (#244)');
if (cut < 0) throw new Error('marker missing');
const slice = src.slice(cut);

let pass = 0, fail = 0;
function ok(cond, label) {
    if (cond) { pass++; console.log('PASS: ' + label); }
    else { fail++; console.log('FAIL: ' + label); }
}

// ---- the world ----------------------------------------------------------
// A mutable harness world: L is the layout, windows/sessions the maps, every
// mutator a recorder. state.deactivated flips mid-await through the getter,
// pushState sits behind a manual gate so the flush-before-reply ORDER is
// observable, and _execModCommand answers from a scriptable queue.
const state = { deactivated: false };
let homeId = 'local';
let L = null;
const windows = new Map();
const sessions = new Map();
const prefsObj = {};
const mut = [];
let execCalls = [];
let execNext = null;
let pushCalls = 0;
let pushResolve = null;
let pushImmediate = true;

function findKeyInLayout(key) {
    for (let ci = 0; ci < L.columns.length; ci++) {
        const col = L.columns[ci];
        for (let ri = 0; ri < col.rows.length; ri++) {
            const ti = col.rows[ri].keys.indexOf(String(key));
            if (ti !== -1) return { desktop: L, col: col, colIndex: ci,
                row: col.rows[ri], rowIndex: ri, cell: null, cellIndex: -1,
                tabIndex: ti };
        }
    }
    return null;
}
function visibleColIndex(col) { return L.columns.indexOf(col); }
function columnKeys(col) {
    const out = [];
    for (const r of col.rows) for (const k of r.keys) out.push(k);
    return out;
}
function mk(key, o, sess) {
    windows.set(key, Object.assign({ id: key, disposed: false,
        minimized: false, tiled: true, name: 'win ' + key,
        geom: { left: 0, top: 0, width: 100, height: 100 } }, o || {}));
    if (sess) sessions.set(key, sess);
}
function resetWorld() {
    L = { mode: 'tiling', focusedCol: 0, columns: [
        { id: 'c1', widthPreset: '1/2', rows: [
            { height: 0.5, keys: ['local:7'], mode: 'single' },
            { height: 0.5, keys: ['local:9'], mode: 'single' } ] },
        { id: 'c2', widthPreset: 'max', rows: [
            { height: 1, keys: ['remoteX:5'], mode: 'single' } ] },
    ] };
    windows.clear(); sessions.clear();
    for (const k of Object.keys(prefsObj)) delete prefsObj[k];
    mut.length = 0; execCalls = []; execNext = null;
    pushCalls = 0; pushImmediate = true; state.deactivated = false;
    mk('local:7', null, { title: 'agent 7' });
    mk('local:9', null, { title: 'agent 9' });
    mk('local:12', { tiled: false,
        geom: { left: 40, top: 80, width: 640, height: 480 } },
        { title: 'scratch' });
    mk('remoteX:5', null, { title: 'remote win' });
    mk('app:note:1', { type: 'app', tiled: false, name: 'notes' }, null);
}
resetWorld();

function rec(name, fn) {
    return function () {
        mut.push(name);
        return fn ? fn.apply(null, arguments) : undefined;
    };
}
function pushState() {
    pushCalls++;
    if (pushImmediate) pushResolve();
    return pushPromise;
}
let pushPromise = null;
function rearmPush(immediate) {
    pushImmediate = immediate !== false;
    pushPromise = new Promise(function (res) { pushResolve = res; });
}
rearmPush(true);
function _execModCommand(op, args) {
    execCalls.push({ op: op, args: args });
    return typeof execNext === 'function' ? execNext(op, args) : execNext;
}

const env = {
    sessions: sessions, windows: windows,
    findKeyInLayout: findKeyInLayout,
    visibleColIndex: visibleColIndex, columnKeys: columnKeys,
    getLayout: function () { return L; },
    homeHostId: function () { return homeId; },
    _execModCommand: _execModCommand, pushState: pushState,
    WIDTH_PRESETS: ['1/3', '1/2', '2/3', 'max'],
    revealAndFocusWindow: rec('reveal', function (k) {
        const loc = findKeyInLayout(k);
        if (loc) L.focusedCol = visibleColIndex(loc.col);
    }),
    moveColumn: rec('moveColumn', function (i, d) {
        const j = i + d;
        if (i < 0 || j < 0 || i >= L.columns.length || j >= L.columns.length) return;
        const a = L.columns[i]; L.columns[i] = L.columns[j]; L.columns[j] = a;
        L.focusedCol = j;
    }),
    dragDropNewColumn: rec('dragDropNewColumn', function () {}),
    setColumnPreset: rec('setColumnPreset', function (win, p) {
        const loc = findKeyInLayout(win.id);
        if (loc) loc.col.widthPreset = p;
    }),
    attachToStrip: rec('attachToStrip', function (w) { w.tiled = true; }),
    detachToFloat: rec('detachToFloat', function (w) { w.tiled = false; }),
    clampGeom: function (g) { return g; },
    applyGeomToWindow: rec('applyGeomToWindow', function (w, g) {
        w.geom = { left: g.left, top: g.top, width: g.width, height: g.height };
    }),
    currentFloatGeom: function (w) { return w.geom; },
    getPref: function (id) {
        const k = String(id);
        if (!prefsObj[k]) prefsObj[k] = {};
        return prefsObj[k];
    },
    savePrefs: rec('savePrefs', function () {}),
    refitSoon: rec('refitSoon', function () {}),
    tabWindowInto: rec('tabWindowInto', function () {}),
    splitAtWindow: rec('splitAtWindow', function () {}),
    window: { innerWidth: 1920, innerHeight: 1040 },
};
Object.defineProperty(env, '_deactivated', {
    get: function () { return state.deactivated; },
});

const factory = new Function('env',
    'with (env) {\n' + slice + '\n' +
    'var __exports = { handleDesktopRpc: handleDesktopRpc,\n' +
    '  resolveDesktopWinKey: resolveDesktopWinKey,\n' +
    '  desktopCommandResult: desktopCommandResult };\n' +
    '}\nreturn __exports;\n');
const api = factory(env);

function mkSock() {
    const ws = { sent: [] };
    ws.send = function (t) { ws.sent.push(JSON.parse(t)); };
    return ws;
}
let nextRid = 1;
function freshRid() { return nextRid++; }
async function drive(hid, msg) {
    const ws = mkSock();
    await api.handleDesktopRpc(hid, { ws: ws }, ws, msg);
    return ws.sent[ws.sent.length - 1];
}
function countMut(name) {
    let n = 0;
    for (const m of mut) if (m === name) n++;
    return n;
}

(async function () {
    // ---- G: the dispatch gate -------------------------------------------
    let r = await drive('local', { op: 'get' });
    ok(r && r.ok === false && r.error === 'bad_args',
        'G1 frame without a rid answers bad_args');
    r = await drive('local', { rid: freshRid() });
    ok(r && r.ok === false && r.error === 'bad_args',
        'G2 frame without an op answers bad_args');
    r = await drive('local', { rid: freshRid(), op: 'teleport' });
    ok(r && r.ok === false && r.error === 'bad_op',
        'G3 unknown op answers bad_op');
    r = await drive('local', { rid: freshRid(), op: 'constructor' });
    ok(r && r.ok === false && r.error === 'bad_op',
        'G3b a prototype key (constructor) is not an op');

    const rid5 = freshRid();
    await drive('local', { rid: rid5, op: 'focus',
        args: { id: 7, visible: null } });
    await drive('local', { rid: rid5, op: 'focus',
        args: { id: 7, visible: null } });
    ok(countMut('reveal') === 1,
        'G5b a replayed rid does not re-apply (mutator ran once)');

    resetWorld();
    state.deactivated = true;
    r = await drive('local', { rid: freshRid(), op: 'focus',
        args: { id: 7, visible: null } });
    ok(r && r.ok === false && r.error === 'lost_lease',
        'G6 a deactivated browser answers lost_lease');
    ok(countMut('reveal') === 0,
        'G6b the deactivated op touched no mutator');
    state.deactivated = false;

    r = await drive('remoteX', { rid: freshRid(), op: 'focus', args: { id: 7 } });
    ok(r && r.ok === false && r.error === 'unknown_window',
        'G7 a remote frame never resolves home-namespace ids');

    execNext = { ok: true, value: { active: 0, items: [] } };
    r = await drive('remoteX', { rid: freshRid(), op: 'workspaces:list' });
    ok(r && r.ok === false && r.error === 'not_home',
        'G8a workspaces op on a non-home connection answers not_home');
    ok(execCalls.length === 0,
        'G8b not_home refuses before the command runs');
    execNext = null;

    r = await drive('remoteX', { rid: freshRid(), op: 'focus',
        args: { id: 5, visible: null } });
    ok(r && r.ok === true,
        'G9 a core op on a non-home connection applies (that broker relay)');

    // ---- validate before mutate -----------------------------------------
    resetWorld();
    r = await drive('local', { rid: freshRid(), op: 'tab_into',
        args: { id: 7, target: 7, visible: null } });
    ok(r && r.ok === false && r.error === 'bad_args'
        && mut.indexOf('attachToStrip') === -1,
        'G10 tab_into onto itself is bad_args and touches no mutator');
    r = await drive('local', { rid: freshRid(), op: 'split',
        args: { id: 7, target: 999, axis: 'row', visible: null } });
    ok(r && r.ok === false && r.error === 'unknown_window'
        && mut.length === 0,
        'G10b split with an unknown target touches no mutator');

    // ---- F: flush before ack ---------------------------------------------
    resetWorld();
    rearmPush(false);
    const wsF = mkSock();
    const pF = api.handleDesktopRpc('local', { ws: wsF }, wsF,
        { rid: freshRid(), op: 'float', args: { id: 7, visible: null } });
    await Promise.resolve();
    await Promise.resolve();
    ok(wsF.sent.length === 0,
        'F1a a mutating op replies only after the pushState flush');
    pushResolve();
    await pF;
    ok(wsF.sent.length === 1 && wsF.sent[0].ok === true,
        'F1b the reply lands once the flush resolves');
    rearmPush(true);

    resetWorld();
    execNext = { ok: false, reason: 'absent' };
    await drive('local', { rid: freshRid(), op: 'get', args: {} });
    ok(pushCalls === 0,
        'F2 get never flushes');

    execNext = function () {
        return new Promise(function (res) {
            state.deactivated = true;
            res({ ok: true, value: { removed: true } });
        });
    };
    r = await drive('local', { rid: freshRid(), op: 'workspaces:remove',
        args: { index: 0 } });
    ok(r && r.ok === false && r.error === 'lost_lease',
        'F3 lease lost during the command await answers lost_lease');
    state.deactivated = false;
    execNext = null;

    // ---- V: the visible-set gate ------------------------------------------
    resetWorld();
    r = await drive('local', { rid: freshRid(), op: 'move_column',
        args: { id: 7, to: 'right', visible: [7] } });
    ok(r && r.ok === false && r.error === 'unknown_window',
        'V1a a shared column with an invisible sibling refuses');
    ok(countMut('moveColumn') === 0,
        'V1b the refused op touched no mutator');
    r = await drive('local', { rid: freshRid(), op: 'move_column',
        args: { id: 7, to: 'right', visible: null } });
    ok(r && r.ok === true && countMut('moveColumn') === 1,
        'V2 an admin frame (visible null) applies');
    r = await drive('local', { rid: freshRid(), op: 'set_width',
        args: { id: 9, preset: '1/3', visible: [9] } });
    ok(r && r.ok === false && r.error === 'unknown_window'
        && countMut('setColumnPreset') === 0,
        'V3 set_width gates the whole column, not the named id');
    r = await drive('local', { rid: freshRid(), op: 'tab_into',
        args: { id: 7, target: 12, visible: [7] } });
    ok(r && r.ok === false && r.error === 'unknown_window'
        && countMut('tabWindowInto') === 0,
        'V5 tab_into gates the target window too');

    // ---- C: command-result mapping ----------------------------------------
    resetWorld();
    execNext = { ok: true,
        value: { refusal: 'needs_confirm', windows: 3 } };
    r = await drive('local', { rid: freshRid(), op: 'workspaces:remove',
        args: { index: 1 } });
    ok(r && r.ok === false && r.error === 'needs_confirm'
        && r.detail && r.detail.count === 3,
        'C1 needs_confirm maps to the wire error with the count');
    execNext = { ok: true, value: { refusal: 'last_workspace' } };
    r = await drive('local', { rid: freshRid(), op: 'workspaces:remove',
        args: { index: 0 } });
    ok(r && r.ok === false && r.error === 'last_workspace',
        'C2 last_workspace maps to the wire error');
    execNext = { ok: false, reason: 'absent' };
    r = await drive('local', { rid: freshRid(), op: 'workspaces:list' });
    ok(r && r.ok === false && r.error === 'absent',
        'C3 absent rides the wire as an error code');
    execNext = { ok: false, reason: 'inactive' };
    r = await drive('local', { rid: freshRid(), op: 'workspaces:list' });
    ok(r && r.ok === false && r.error === 'inactive',
        'C4 inactive rides the wire as an error code');
    execNext = { ok: true, value: { moved: true } };
    r = await drive('local', { rid: freshRid(),
        op: 'workspaces:send-window', args: { win: 7, target: 1 } });
    ok(execCalls.length > 0 && execCalls[execCalls.length - 1].args
        && execCalls[execCalls.length - 1].args.key === 'local:7',
        'C5a send-window resolves the wire int to a winKey before dispatch');
    ok(r && r.ok === true && r.value && r.value.moved === true,
        'C5b send-window success maps through');
    execNext = null;

    // ---- S: the snapshot ----------------------------------------------------
    resetWorld();
    execNext = { ok: true, value: {
        active: 0,
        items: [ { index: 0, id: 'W1', name: null },
                 { index: 1, id: 'W2', name: 'two' } ],
        columnWs: { c1: 'W1', c2: 'W2' },
        floatWs: { 'local:12': 'W2', 'app:note:1': null },
    } };
    r = await drive('local', { rid: freshRid(), op: 'get',
        args: { visible: null } });
    const v = r && r.value;
    const byId = {};
    for (const row of (v ? v.windows : [])) byId[row.id + '|' + row.key] = row;
    ok(v && v.mode === 'tiling' && v.focused_column === 0 && v.home === true,
        'S1a snapshot header matches the world');
    const r7 = byId['7|local:7'];
    ok(r7 && r7.column === 0 && r7.row === 0 && r7.tabIndex === 0
        && r7.width_preset === '1/2' && r7.workspace === 0
        && r7.tiled === true,
        'S1b a tiled row carries placement and workspace');
    const r9 = byId['9|local:9'];
    ok(r9 && r9.row === 1 && r9.workspace === 0,
        'S1c the column sibling carries its own row');
    const r12 = byId['12|local:12'];
    ok(r12 && r12.geometry && r12.geometry.x === 40 && r12.geometry.y === 80
        && r12.geometry.w === 640 && r12.geometry.h === 480
        && r12.workspace === 1,
        'S1d a floating row carries live geometry as this browser sees it');
    const ra = byId['null|app:note:1'];
    ok(ra && ra.kind === 'app' && ra.workspace === null,
        'S1e an app window is listed with a null id');
    const rr = byId['null|remoteX:5'];
    ok(rr && rr.workspace === 1,
        'S1f a foreign-broker window is listed with a null id');
    execNext = { ok: false, reason: 'absent' };
    r = await drive('local', { rid: freshRid(), op: 'get',
        args: { visible: null } });
    ok(r && r.value && r.value.workspaces === null
        && r.value.active_workspace === null
        && r.value.windows.length > 0
        && r.value.windows[0].workspace === null,
        'S2 mod off: workspaces fields are null, rows keep flowing');
    execNext = null;

    // ---- get row filtering + geometry read-back -----------------------------
    resetWorld();
    execNext = { ok: false, reason: 'absent' };
    r = await drive('local', { rid: freshRid(), op: 'get',
        args: { visible: [7] } });
    const ids = (r && r.value ? r.value.windows : [])
        .map(function (row) { return row.id; });
    let onlyVisible = ids.length === 1 && ids[0] === 7;
    ok(onlyVisible,
        'V4 a non-admin get keeps only visible rows (null ids drop too)');

    resetWorld();
    env.applyGeomToWindow = rec('applyGeomToWindow', function (w, g) {
        // Simulate a size lock: the height the window keeps is not the one
        // asked for -- the persisted box and the reply must agree with the
        // APPLIED box, not the request.
        w.geom = { left: g.left, top: g.top, width: g.width, height: 222 };
    });
    r = await drive('local', { rid: freshRid(), op: 'set_geometry',
        args: { id: 12, x: 10, y: 20, w: 300, h: 400, visible: null } });
    ok(r && r.ok === true && r.value && r.value.geometry
        && r.value.geometry.h === 222
        && prefsObj['local:12'].geom.height === 222,
        'R1 set_geometry persists the box AS APPLIED, not as asked');

    console.log(pass + ' passed, ' + fail + ' failed');
    process.exitCode = fail ? 1 : 0;
})().catch(function (e) {
    console.error(e && e.stack || e);
    process.exitCode = 1;
});
"""


@pytest.fixture(scope="module")
def scenario_run(tmp_path_factory):
    src = SRC_JS.read_text(encoding="utf-8")
    # FAIL, never skip: without the marker the slice is unmoored from the
    # live source and every green assert below would grade a stale copy.
    assert MARKER in src, (
        f"{MARKER!r} marker missing from {SRC_JS} -- the desktop-relay "
        "slice boundary moved; restore the marker or retarget this test")
    assert src.count(MARKER) == 1, (
        f"{MARKER!r} occurs {src.count(MARKER)}x in {SRC_JS}; the slice "
        "boundary is ambiguous")
    harness = tmp_path_factory.mktemp("desktop-rpc") / "harness.js"
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


# ---- the dispatch gate -----------------------------------------------------


def test_frame_shape_is_gated_first(scenario_run):
    # The broker validates frames too, but the browser is defensive in
    # depth: a frame without a usable rid/op answers bad_args instead of
    # mutating anything, and an unknown op -- including a prototype key
    # sneaking past a sloppy allowlist read -- answers bad_op.
    _ok(scenario_run, "G1 frame without a rid answers bad_args")
    _ok(scenario_run, "G2 frame without an op answers bad_args")
    _ok(scenario_run, "G3 unknown op answers bad_op")
    _ok(scenario_run, "G3b a prototype key (constructor) is not an op")


def test_a_replayed_rid_never_reapplies(scenario_run):
    # The broker sends each rpc to ONE designated socket, but a resend (or a
    # duplicated frame) must not move a window twice; the answered-rid cache
    # re-serves the earlier result instead.
    _ok(scenario_run, "G5b a replayed rid does not re-apply (mutator ran once)")


def test_a_deactivated_browser_answers_lost_lease(scenario_run):
    # While another browser holds the lease this one must not mutate shared
    # state at all; the caller learns why, and nothing half-applies.
    _ok(scenario_run, "G6 a deactivated browser answers lost_lease")
    _ok(scenario_run, "G6b the deactivated op touched no mutator")


def test_ids_resolve_in_the_delivering_connections_namespace(scenario_run):
    # The gh#161 trap: a frame arriving on remoteX's connection must never
    # resolve `7` against the home broker's `local:7` -- the id names a
    # window of the broker that relayed it, and nothing else.
    _ok(scenario_run, "G7 a remote frame never resolves home-namespace ids")


def test_not_home_guards_workspaces_only(scenario_run):
    # Workspace data syncs to the HOME broker's /state blob, so mutating it
    # from any other broker's relay would edit the wrong desktop; window
    # placement is per-browser render and works from any connection.
    _ok(scenario_run, "G8a workspaces op on a non-home connection answers not_home")
    _ok(scenario_run, "G8b not_home refuses before the command runs")
    _ok(scenario_run, "G9 a core op on a non-home connection applies (that broker relay)")


def test_validation_precedes_every_mutator(scenario_run):
    # A rejected op must leave the desktop untouched -- validation, gate and
    # id resolution all run before the first mutator call.
    _ok(scenario_run, "G10 tab_into onto itself is bad_args and touches no mutator")
    _ok(scenario_run, "G10b split with an unknown target touches no mutator")


# ---- the flush-before-ack rule ----------------------------------------------


def test_mutations_ack_only_after_the_state_flush(scenario_run):
    # Success means the /state write was attempted under our lease: the
    # reply waits for pushState(), and `get` (a read) never flushes.
    _ok(scenario_run, "F1a a mutating op replies only after the pushState flush")
    _ok(scenario_run, "F1b the reply lands once the flush resolves")
    _ok(scenario_run, "F2 get never flushes")


def test_lease_lost_mid_await_answers_lost_lease(scenario_run):
    # The stale-capture rule: after every await the lease is re-checked, so
    # an op that started under our lease cannot land on a torn-down browser.
    _ok(scenario_run, "F3 lease lost during the command await answers lost_lease")


# ---- the visible-set gate (the owner's scope rule) ----------------------------


def test_the_gate_covers_the_affected_topology(scenario_run):
    # "A non-admin agent may arrange only the windows it can see": moving or
    # resizing a column reshapes EVERY window in it, so the whole column
    # gates; an admin (visible null) is unrestricted.
    _ok(scenario_run, "V1a a shared column with an invisible sibling refuses")
    _ok(scenario_run, "V1b the refused op touched no mutator")
    _ok(scenario_run, "V2 an admin frame (visible null) applies")
    _ok(scenario_run, "V3 set_width gates the whole column, not the named id")
    _ok(scenario_run, "V5 tab_into gates the target window too")


def test_a_non_admin_get_lists_only_visible_windows(scenario_run):
    # The same set the gate enforces is the set the snapshot admits; rows
    # with no id in this namespace (app windows) drop out for non-admins.
    _ok(scenario_run, "V4 a non-admin get keeps only visible rows (null ids drop too)")


# ---- command-result mapping ---------------------------------------------------


def test_command_results_map_onto_the_wire_errors(scenario_run):
    # execute()'s closed vocabulary and the commands' {refusal} values are
    # not wire shapes; exactly one translation exists, here.
    _ok(scenario_run, "C1 needs_confirm maps to the wire error with the count")
    _ok(scenario_run, "C2 last_workspace maps to the wire error")
    _ok(scenario_run, "C3 absent rides the wire as an error code")
    _ok(scenario_run, "C4 inactive rides the wire as an error code")
    _ok(scenario_run, "C5a send-window resolves the wire int to a winKey before dispatch")
    _ok(scenario_run, "C5b send-window success maps through")


# ---- the snapshot ---------------------------------------------------------------


def test_the_snapshot_matches_the_world(scenario_run):
    _ok(scenario_run, "S1a snapshot header matches the world")
    _ok(scenario_run, "S1b a tiled row carries placement and workspace")
    _ok(scenario_run, "S1c the column sibling carries its own row")
    _ok(scenario_run, "S1d a floating row carries live geometry as this browser sees it")
    _ok(scenario_run, "S1e an app window is listed with a null id")
    _ok(scenario_run, "S1f a foreign-broker window is listed with a null id")
    _ok(scenario_run, "S2 mod off: workspaces fields are null, rows keep flowing")


def test_set_geometry_persists_the_applied_box(scenario_run):
    # applyGeomToWindow may override the requested box (a size-locked
    # window); the persisted pref and the reply must describe the box that
    # was actually applied, never the one that was asked for.
    _ok(scenario_run, "R1 set_geometry persists the box AS APPLIED, not as asked")


# ---- the catch-all ---------------------------------------------------------------


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


# ---- static pins (behaviour not observable from the section slice) ---------------


def test_desktop_branch_wired_into_the_onmessage_chain():
    # The onmessage dispatcher lives OUTSIDE the marked section, so node
    # cannot observe it; pin the wiring by source instead, the way
    # test_ui_assets.py pins served-page symbols.
    src = SRC_JS.read_text(encoding="utf-8")
    assert "msg.type === 'desktop_rpc'" in src
    assert "Promise.resolve(handleDesktopRpc(hid, rec, ws, msg))" in src


def test_the_desktop_slice_never_fetches():
    # The gh#161 class of bug (hostFetch(null, ...) silently hitting the
    # serving origin) is unobservable in node because the harness injects
    # every name; pin that the section simply does not call it.
    src = SRC_JS.read_text(encoding="utf-8")
    section = src[src.index(MARKER):]
    assert "hostFetch(" not in section
    # The allowlists are prototype-free tables read with === true, never {}
    # truthiness (a 'constructor' op must not dispatch).
    assert "const _DESKTOP_OPS = Object.create(null);" in section
    assert "const _DESKTOP_WS_OPS = Object.create(null);" in section
    assert "_DESKTOP_OPS[msg.op] === true" in section
    assert "_DESKTOP_WS_OPS[msg.op] === true" in section
