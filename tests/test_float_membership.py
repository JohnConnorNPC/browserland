"""Behavioural tests for #251: float membership is the SHARED layout's
``openTerms`` minus its columns.

A terminal the synced layout lists as open but holds in no column is a float
on some device. Before #251 nothing read it that way: a device that had never
seen the window (a phone taking over) fell through ``decideTiled`` to the
tiling mode, tiled it, and pushed a column every other device then adopted.

The cells execute the SHIPPED code in node, cut from the fragments at named
boundaries and evaluated inside a ``with (env)`` scope whose outer names
(``prefs``, ``windows``, the DOM, ``savePrefs``) are stubs:

* G1 -- 75's layout GC drops a dead key from ``openTerms`` with its column (A0).
* F1-F6, F10, F14 -- 58 ``isKnownFloat``/``decideTiled`` and the 67 capture
  that feeds them (A1): a key open but in no column opens as a float, a brand
  new key still follows the tiling default, and a minimized tiled window keeps
  its column so it is never mistaken for a float.
* F7-F9, F11-F13, F15 -- an adopt through 52's real ``_applyServerState``
  that drops a tiled window's column floats it on this device (62a
  ``reconcileWindowRoles``/``floatLocally``, A2): its own box, no push, the
  placement hook told once with the workspace its column was on (the
  workspaces mod's real ``adoptFloatWorkspace`` stamps it there), and a
  window closed elsewhere, a disposed one, an app window or the detached
  surface untouched; tabbed and split rows, a minimized window and the front
  window (F16, F17).

One PASS/FAIL line per assert plus a machine-readable tail; the fixture FAILS
if a boundary vanishes. Skipped when node is absent.
"""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from webterm.broker import ui

BROKER_DIR = Path(ui.__file__).resolve().parent
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="node not installed")

# name -> (fragment, start marker (unique), end marker (first one after start))
SLICES = {
    "openTerms": ("58_js_layout_mutators.js",
                  "        function getOpenTerms() {",
                  "        // The one desktop (#148)."),
    "findKey": ("58_js_layout_mutators.js",
                "        function findKeyInLayout(key) {",
                "        // ---- _layout mutators"),
    "decide": ("58_js_layout_mutators.js",
               "        // #251: is this key a FLOAT somewhere?",
               "        // ---- consume / expel"),
    "capture": ("67_js_window_lifecycle.js",
                "            // #251: read the SHARED float membership first.",
                "            // #153: the OSC 52"),
    "minimize": ("73_js_window_runtime.js",
                 "        function minimizeWindow(id) {",
                 "        function restoreWindow(id) {"),
    "apply": ("52_js_state_sync.js",
              "        function _applyServerState(srv) {",
              "        // Pull /state. initial=true"),
    "roles": ("62a_js_strip_and_layers.js",
              "        // #251: an ADOPTED layout can drop",
              "        // ---- parking (off-screen custody)"),
    "hooks": ("62a_js_strip_and_layers.js",
              "        const _PLACEMENT_HOOKS = ",
              "        // Its key will never be seen again"),
    "refit": ("73_js_window_runtime.js",
              "        function refitSoon(win) {",
              "        function maybeSendInitialResize(win) {"),
    "resizable": ("73_js_window_runtime.js",
                  "        function isResizable(win) {",
                  "        function sendResize(win, force) {"),
    "wsAdopt": ("mods/workspaces/workspaces.js",
                "            function windowOffActiveWs(win) {",
                "            // Mask floating windows that don't belong"),
    "rowKeys": ("57_js_tiling_model.js",
                "        function rowKeys(row) {",
                "        // Keyed mirror of normalizeRowWidths"),
    "gc": ("75_js_taskbar_hosts.js",
           "            let layoutDropped = 0;",
           "            for (const k of Object.keys(prefs)) {"),
}

EXPECTED_ASSERTS = 19

_HARNESS = r"""
'use strict';
const S = JSON.parse(require('fs').readFileSync(__SLICES_PATH__, 'utf8'));

let pass = 0, fail = 0;
function ok(cond, label) {
    if (cond) { pass++; console.log('PASS: ' + label); }
    else { fail++; console.log('FAIL: ' + label); }
}
const J = JSON.stringify;
function clone(o) { return JSON.parse(J(o)); }

// Evaluate `code` (sloppy, so `with` works) with env's names in scope and
// return the named functions it declares.
function load(code, env, names) {
    const ret = '{' + names.map(n => n + ': typeof ' + n + " === 'function' ? " + n
        + ' : undefined').join(', ') + '}';
    return (new Function('env', 'with (env) { return (function () {\n' + code
        + '\nreturn ' + ret + ';\n})(); }'))(env);
}
function col(key, wsId) {
    const c = { id: 'c-' + key, rows: [{ keys: [key] }] };
    if (wsId !== undefined) c.wsId = wsId;
    return c;
}

// One world: a layout, prefs, and the counters the cells read.
function mkWorld(o) {
    o = o || {};
    const w = { saves: 0, localSaves: 0 };
    w.prefs = { _layout: { mode: o.mode || 'tiling', columns: clone(o.columns || []),
                           openTerms: clone(o.openTerms || []) } };
    w.layout = () => w.prefs._layout;
    w.colKeys = () => [].concat.apply([], w.layout().columns.map(
        c => [].concat.apply([], c.rows.map(r => r.keys))));
    const env = {
        prefs: w.prefs,
        getLayout: () => w.prefs._layout,
        isDetachedSurface: () => !!o.detached,
        savePrefs: () => { w.saves++; },
        savePrefsLocal: () => { w.localSaves++; },
        getPref: (id) => { const k = String(id); if (!w.prefs[k]) w.prefs[k] = {}; return w.prefs[k]; },
        windows: new Map(),
        frontId: null,
        removeKeyFromLayout: (k) => {
            const L = w.prefs._layout;
            for (const c of L.columns) for (const r of c.rows) {
                const i = r.keys.indexOf(k);
                if (i !== -1) {
                    r.keys.splice(i, 1);
                    c.rows = c.rows.filter(x => x.keys.length);
                    L.columns = L.columns.filter(x => x.rows.length);
                    return {};
                }
            }
            return null;
        },
        layoutRemoveKey: (k) => { if (env.removeKeyFromLayout(k)) env.savePrefs(); },
        requestRelayout: () => {},
        requestAnimationFrame: (cb) => cb(),
        updateTaskbarActive: () => {},
        captureTiledFocusContext: () => null,
        reconcileTiledFocus: () => {},
    };
    w.env = env;
    const names = ['getOpenTerms', 'addOpenTerm', 'removeOpenTerm', 'findKeyInLayout',
        'isTilingMode', 'rowKeys', 'isKnownFloat', 'decideTiled', 'minimizeWindow'];
    Object.assign(w, load(S.openTerms + S.findKey + S.rowKeys + S.decide + S.minimize, env, names));
    for (const n of names) env[n] = w[n];
    // 67 openWindow's capture, run exactly as shipped for (win, id).
    w.capture = load('function capture(win, id) {\n' + S.capture + '\nreturn knownFloat;\n}',
        env, ['capture']).capture;
    // A2: the adopt (52), the role reconcile and placement hooks (62a), the
    // refit path (73) and the workspaces mod's float stamp.
    w.resizes = []; w.placed = []; w.z = 0; w.floatWs = {}; w.fronted = [];
    w.desktopKids = [];
    Object.assign(env, {
        _stateApplying: false, _stateLastSerialized: null, _stateRecentSends: [],
        _stateRevSkipped: false,
        getSettings: () => {}, _stateSerialize: () => '', _stateSettleAll: () => {},
        isSizeLocked: () => false, applyDisplaySettings: () => {}, refreshOpenSectionUIs: () => {},
        document: { body: { classList: { toggle: () => {} } },
                    getElementById: (id) => id === 'desktop'
                        ? { appendChild: (dom) => { dom.parent = 'desktop'; w.desktopKids.push(dom); } } : null },
        clampGeom: (g) => Object.assign({}, g),
        defaultGeom: () => ({ left: 5, top: 6, width: 720, height: 480 }),
        applyGeomToWindow: (win, g) => { if (!win.tiled) win.geom = Object.assign({}, g); },
        floatZIndex: () => ++w.z,
        bringToFront: (id) => { w.fronted.push(id); },
        sendResize: (win) => { if (env.isResizable(win)) w.resizes.push(win.id); },
        ModConflictError: (m) => new Error(m),
        // the workspaces mod's world: W1 active, W2 hidden
        wsList: () => [{ id: 'W1' }, { id: 'W2' }],
        activeWorkspaceId: () => 'W1',
        windowWsId: (win) => win ? w.floatWs[win.id] : undefined,
        setWindowWs: (win, wsId) => { w.floatWs[win.id] = wsId; },
        applyTaskbarWorkspace: () => {},
    });
    const a2 = ['_applyServerState', 'reconcileWindowRoles', 'floatLocally', '_columnHolding',
        'registerPlacementHooks', 'notifyWindowPlaced', 'refitSoon', 'isResizable'];
    Object.assign(w, load(S.apply + S.roles + S.hooks + S.refit + S.resizable, env, a2));
    for (const n of a2) env[n] = w[n];
    w.adoptFloatWorkspace = load(S.wsAdopt, env, ['adoptFloatWorkspace']).adoptFloatWorkspace;
    w.adopt = (layout) => w._applyServerState({ settings: {}, layout: clone(layout) });
    return w;
}
function tiledWin(w, id, o) {
    const win = mkWin(id, Object.assign({ tiled: true, cls: ['tiled'],
        body: { getBoundingClientRect: () => ({ width: 400, height: 300 }) } }, o || {}));
    win.dom.parent = 'strip';
    w.env.windows.set(id, win);
    return win;
}
function mkWin(id, o) {
    const cls = new Set((o && o.cls) || []);
    return Object.assign({ id: id, type: 'term', tiled: false, minimized: false, disposed: false,
        dom: { classList: { add: c => cls.add(c), remove: c => cls.delete(c),
                            contains: c => cls.has(c), toggle: (c, on) => on ? cls.add(c) : cls.delete(c) },
               style: {}, cls: cls } }, o || {});
}

// ---- G1 (A0): the layout GC prunes openTerms with the column ---------------
(function () {
    const w = mkWorld({ columns: [col('h:1'), col('h:2'), col('h:3')],
                        openTerms: ['h:1', 'h:2', 'h:3', 'h:9'] });
    const old = Date.now() - 600000;
    Object.assign(w.env, {
        hosts: [{ id: 'h' }],
        pollStateFor: () => ({ ok: true, sessions: [{ id: 2 }] }),
        merged: new Map([['h:2', {}]]),               // h:2 is live
        pendingOpens: new Map([['h:3', {}]]),         // h:3 is opening
        layoutGcMiss: new Map([['h:1', old], ['h:2', old], ['h:3', old]]),
        LAYOUT_GC_GRACE_MS: 120000,
    });
    const gc = load('function runGc() {\n' + S.gc + '\nreturn layoutDropped;\n}', w.env, ['runGc']);
    const dropped = gc.runGc();
    ok(dropped === 1 && J(w.colKeys()) === J(['h:2', 'h:3'])
        && J(w.layout().openTerms) === J(['h:2', 'h:3', 'h:9']) && w.saves >= 1,
        'G1 the layout GC drops a dead key from openTerms with its column; live, pending and float keys stay');
})();

// ---- A1: decideTiled reads the shared float membership --------------------
(function () {
    // F1: open in the shared layout, in no column, no local role -> a float.
    let w = mkWorld({ columns: [col('h:1')], openTerms: ['h:1', 'h:2'] });
    const kf1 = w.isKnownFloat('h:2');
    ok(kf1 === true && w.decideTiled('h:2', kf1) === false && w.saves === 0
        && J(w.colKeys()) === J(['h:1']),
        'F1 a key open in the shared layout but in no column opens as a float, and nothing is written');
    // F2: not in openTerms -> the tiling default.
    w = mkWorld({ columns: [col('h:1')], openTerms: ['h:1'] });
    const kf2 = w.isKnownFloat('h:5');
    ok(kf2 === false && w.decideTiled('h:5', kf2) === true,
        'F2 a key the shared layout does not list as open follows the tiling default');
    // F3: a column wins, whatever the local role or the float hint.
    w = mkWorld({ columns: [col('h:1')], openTerms: ['h:1'] });
    w.env.getPref('h:1').tiled = false;
    ok(w.isKnownFloat('h:1') === false && w.decideTiled('h:1', w.isKnownFloat('h:1')) === true
        && w.decideTiled('h:1', true) === true,
        'F3 a key in a column opens tiled, whatever the local role or openTerms');
    // F4: the shared membership beats a stale local "tiled" role.
    w = mkWorld({ columns: [col('h:1')], openTerms: ['h:1', 'h:2'] });
    w.env.getPref('h:2').tiled = true;
    ok(w.decideTiled('h:2', w.isKnownFloat('h:2')) === false,
        'F4 a stale local tiled role does not re-tile a window floated elsewhere');
    // F5: floating mode, unknown key -> a float (unchanged).
    w = mkWorld({ mode: 'floating', columns: [], openTerms: [] });
    ok(w.decideTiled('h:5', w.isKnownFloat('h:5')) === false,
        'F5 in floating mode an unknown key opens as a float (unchanged)');
    // F6: through 67's own capture: a brand-new key tiles even though the
    // capture's addOpenTerm lists it in openTerms.
    w = mkWorld({ columns: [col('h:1')], openTerms: ['h:1'] });
    const kf6 = w.capture(mkWin('h:7'), 'h:7');
    ok(kf6 === false && w.layout().openTerms.indexOf('h:7') !== -1
        && w.decideTiled('h:7', kf6) === true,
        "F6 through openWindow's capture a brand-new window still tiles, though the capture lists it as open");
    // F6b: ...and a key already open elsewhere, in no column, floats.
    w = mkWorld({ columns: [col('h:1')], openTerms: ['h:1', 'h:8'] });
    const kf6b = w.capture(mkWin('h:8'), 'h:8');
    ok(kf6b === true && w.decideTiled('h:8', kf6b) === false
        && J(w.layout().openTerms) === J(['h:1', 'h:8']),
        "F6b through openWindow's capture a window open elsewhere in no column floats");
    // F10: the detached surface floats, always.
    w = mkWorld({ detached: true, columns: [col('h:1')], openTerms: ['h:1'] });
    ok(w.decideTiled('h:1', w.isKnownFloat('h:1')) === false,
        'F10 the detached surface opens its window as a float, as before');
    // F14: minimizing a tiled window keeps its column, so a restore never
    // mistakes it for a float.
    w = mkWorld({ columns: [col('h:1'), col('h:2')], openTerms: ['h:1', 'h:2'] });
    const win = mkWin('h:1', { tiled: true });
    w.env.windows.set('h:1', win); w.env.frontId = 'h:1';
    w.minimizeWindow('h:1');
    ok(win.minimized === true && !!w.findKeyInLayout('h:1') && w.isKnownFloat('h:1') === false
        && w.decideTiled('h:1', w.isKnownFloat('h:1')) === true && w.saves === 0,
        'F14 a minimized tiled window keeps its column, so it is never a known float');
})();

// ---- A2: an adopt that drops a tiled window's column floats it here --------
(function () {
    const L0 = { mode: 'tiling', columns: [col('h:1', 'W1'), col('h:2', 'W1')], openTerms: ['h:1', 'h:2'] };
    // F7: through 52's real adopt: floated at its own floatGeom, no push,
    // placement announced once, refit at this device's size.
    let w = mkWorld(L0);
    w.registerPlacementHooks({ placed: (win, hint) => w.placed.push([win.id, hint]) });
    const t1 = tiledWin(w, 'h:1', { floatGeom: { left: 1, top: 2, width: 300, height: 200 } });
    const t2 = tiledWin(w, 'h:2');
    w.adopt({ mode: 'tiling', columns: [col('h:2', 'W1')], openTerms: ['h:1', 'h:2'] });
    ok(t1.tiled === false && !t1.dom.cls.has('tiled') && t1.dom.parent === 'desktop'
        && J(t1.geom) === J({ left: 1, top: 2, width: 300, height: 200 })
        && w.env.getPref('h:1').tiled === false && w.saves === 0 && w.localSaves >= 1
        && J(w.placed) === J([['h:1', { wsId: 'W1' }]]) && J(w.resizes) === J(['h:1'])
        && t1.dom.style.zIndex === '1' && w.env.frontId === null && w.fronted.length === 0,
        'F7 an adopt that drops a tiled window\'s column floats it here at its own box, pushes nothing and announces it once');
    // F8: no floatGeom -> the device pref's box, else the default box.
    w = mkWorld({ mode: 'tiling', columns: [col('h:1'), col('h:3'), col('h:2')], openTerms: ['h:1', 'h:2', 'h:3'] });
    const g8 = tiledWin(w, 'h:1'); const d8 = tiledWin(w, 'h:3');
    w.env.getPref('h:1').geom = { left: 40, top: 50, width: 640, height: 400 };
    w.adopt({ mode: 'tiling', columns: [col('h:2')], openTerms: ['h:1', 'h:2', 'h:3'] });
    ok(J(g8.geom) === J({ left: 40, top: 50, width: 640, height: 400 })
        && J(d8.geom) === J({ left: 5, top: 6, width: 720, height: 480 }) && !g8.tiled && !d8.tiled,
        'F8 with no floatGeom it floats at this device\'s saved box, else the default box');
    // F9: still in a column -> untouched; disposed -> untouched.
    w = mkWorld({ mode: 'tiling', columns: [col('h:1', 'W1'), col('h:2', 'W1'), col('app:1', 'W1')],
                  openTerms: ['h:1', 'h:2', 'app:1'] });
    const k9 = tiledWin(w, 'h:2'); const x9 = tiledWin(w, 'h:1', { disposed: true });
    const a9 = tiledWin(w, 'app:1', { type: 'app' });
    w.adopt({ mode: 'tiling', columns: [col('h:2', 'W1')], openTerms: ['h:1', 'h:2', 'app:1'] });
    ok(k9.tiled === true && k9.dom.parent === 'strip' && x9.tiled === true
        && x9.dom.parent === 'strip' && a9.tiled === true && a9.dom.parent === 'strip'
        && w.resizes.length === 0,
        'F9 a tiled window still in a column, a disposed one and an app window are untouched');
    // F11: gone from the columns AND openTerms (closed elsewhere) -> untouched.
    w = mkWorld(L0);
    w.registerPlacementHooks({ placed: (win, hint) => w.placed.push([win.id, hint]) });
    const c11 = tiledWin(w, 'h:1');
    w.adopt({ mode: 'tiling', columns: [col('h:2', 'W1')], openTerms: ['h:2'] });
    ok(c11.tiled === true && c11.dom.parent === 'strip' && w.placed.length === 0,
        'F11 a window closed elsewhere (gone from openTerms too) is not floated');
    // F12: the column was on hidden workspace W2 -> the mod stamps W2 (over a
    // stale W1 stamp) and masks it; it is not the front window.
    w = mkWorld({ mode: 'tiling', columns: [col('h:1', 'W2'), col('h:2', 'W1')], openTerms: ['h:1', 'h:2'] });
    w.registerPlacementHooks({ placed: w.adoptFloatWorkspace });
    const p12 = tiledWin(w, 'h:1'); w.floatWs['h:1'] = 'W1'; w.env.frontId = 'h:1';
    w.adopt({ mode: 'tiling', columns: [col('h:2', 'W1')], openTerms: ['h:1', 'h:2'] });
    ok(p12.tiled === false && w.floatWs['h:1'] === 'W2' && p12.dom.cls.has('ws-hidden')
        && w.env.frontId === null,
        'F12 a window floated by an adopt joins the workspace its column was on, hidden there, not the active one');
    // F13: the detached surface never reconciles (its layout is fixed).
    w = mkWorld({ detached: true, columns: [], openTerms: ['h:1'] });
    const d13 = tiledWin(w, 'h:1');
    w.reconcileWindowRoles({ mode: 'tiling', columns: [col('h:1')], openTerms: ['h:1'] });
    ok(d13.tiled === true && d13.dom.parent === 'strip',
        'F13 the detached surface never floats a window on adopt');
    // F15: a #239 controlled window floats but sends no resize.
    w = mkWorld({ mode: 'tiling', columns: [col('h:1'), col('h:3'), col('h:2')], openTerms: ['h:1', 'h:2', 'h:3'] });
    const c15 = tiledWin(w, 'h:1', { controlled: true }); tiledWin(w, 'h:3');
    w.adopt({ mode: 'tiling', columns: [col('h:2')], openTerms: ['h:1', 'h:2', 'h:3'] });
    ok(c15.tiled === false && J(w.resizes) === J(['h:3']),
        'F15 a controlled window floated by an adopt sends no resize');
    // F16: an inactive tab of a tabbed row and a pane of a split row, both on
    // hidden W2; the pane is minimized.
    w = mkWorld({ mode: 'tiling', openTerms: ['h:1', 'h:4', 'h:5', 'h:6'], columns: [
        { id: 'ct', wsId: 'W2', rows: [{ mode: 'tabbed', keys: ['h:1', 'h:4'], activeTab: 'h:4' }] },
        { id: 'cs', wsId: 'W2', rows: [{ mode: 'split', cells: [{ id: 'a', keys: ['h:5'] },
                                                               { id: 'b', keys: ['h:6'] }] }] }] });
    w.registerPlacementHooks({ placed: w.adoptFloatWorkspace });
    const tab16 = tiledWin(w, 'h:1', { cls: ['tiled', 'tab-hidden'] });
    const pane16 = tiledWin(w, 'h:5', { minimized: true });
    const keep16 = tiledWin(w, 'h:6');
    w.adopt({ mode: 'tiling', openTerms: ['h:1', 'h:4', 'h:5', 'h:6'], columns: [
        { id: 'ct', wsId: 'W2', rows: [{ mode: 'single', keys: ['h:4'] }] },
        { id: 'cs', wsId: 'W2', rows: [{ mode: 'single', keys: ['h:6'] }] }] });
    ok(!tab16.tiled && !tab16.dom.cls.has('tab-hidden') && w.floatWs['h:1'] === 'W2'
        && !pane16.tiled && pane16.minimized === true && w.floatWs['h:5'] === 'W2'
        && keep16.tiled === true && w.resizes.indexOf('h:5') === -1,
        'F16 a tab of a tabbed row and a minimized pane of a split row float into their column\'s workspace');
    // F17: the front window, on the active workspace, stays front.
    w = mkWorld(L0);
    w.registerPlacementHooks({ placed: w.adoptFloatWorkspace });
    const f17 = tiledWin(w, 'h:1'); tiledWin(w, 'h:2'); w.env.frontId = 'h:1';
    w.adopt({ mode: 'tiling', columns: [col('h:2', 'W1')], openTerms: ['h:1', 'h:2'] });
    ok(!f17.tiled && !f17.dom.cls.has('ws-hidden') && J(w.fronted) === J(['h:1']),
        'F17 the front window floated by an adopt stays front (focus restored)');
})();

console.log(pass + ' passed, ' + fail + ' failed');
"""


def _cut(name):
    frag, start, end = SLICES[name]
    src = (BROKER_DIR / frag).read_text(encoding="utf-8")
    # FAIL, never skip: a moved boundary would grade a stale cut.
    assert src.count(start) == 1, (
        f"{start!r} occurs {src.count(start)}x in {frag}; retarget this test")
    a = src.index(start)
    b = src.find(end, a + len(start))
    assert b != -1, f"{end!r} missing after {start!r} in {frag}"
    return src[a:b]


@pytest.fixture(scope="module")
def run(tmp_path_factory):
    d = tmp_path_factory.mktemp("float-membership")
    slices = d / "slices.json"
    slices.write_text(json.dumps({n: _cut(n) for n in SLICES}), encoding="utf-8")
    harness = d / "harness.js"
    harness.write_text(_HARNESS.replace("__SLICES_PATH__", json.dumps(str(slices))),
                       encoding="utf-8")
    proc = subprocess.run([NODE, str(harness)], capture_output=True, text=True,
                          encoding="utf-8", timeout=120)
    lines = (proc.stdout or "").strip().splitlines()
    tail = re.match(r"^(\d+) passed, (\d+) failed$", lines[-1] if lines else "")
    assert tail is not None, (
        "harness produced no machine-readable tail (node crashed?)\n"
        f"rc={proc.returncode}\nstdout={proc.stdout}\nstderr={proc.stderr}")
    results = {}
    for line in lines[:-1]:
        if line.startswith("PASS: "):
            results[line[len("PASS: "):]] = True
        elif line.startswith("FAIL: "):
            results[line[len("FAIL: "):]] = False
    return {"rc": proc.returncode, "stdout": proc.stdout, "stderr": proc.stderr,
            "passed": int(tail.group(1)), "failed": int(tail.group(2)),
            "results": results}


def _ok(run, label):
    got = run["results"].get(label)
    verdict = "failed" if got is False else "never ran"
    assert got is True, (
        f"{label!r} {verdict}\nstdout: {run['stdout']}\nstderr: {run['stderr']}")


@pytest.mark.parametrize("label", [
    "G1 the layout GC drops a dead key from openTerms with its column; live, pending and float keys stay",
])
def test_251_gc_keeps_the_invariant(run, label):
    _ok(run, label)


@pytest.mark.parametrize("label", [
    "F1 a key open in the shared layout but in no column opens as a float, and nothing is written",
    "F2 a key the shared layout does not list as open follows the tiling default",
    "F3 a key in a column opens tiled, whatever the local role or openTerms",
    "F4 a stale local tiled role does not re-tile a window floated elsewhere",
    "F5 in floating mode an unknown key opens as a float (unchanged)",
    "F6 through openWindow's capture a brand-new window still tiles, though the capture lists it as open",
    "F6b through openWindow's capture a window open elsewhere in no column floats",
    "F10 the detached surface opens its window as a float, as before",
    "F14 a minimized tiled window keeps its column, so it is never a known float",
])
def test_251_decide_tiled_reads_shared_membership(run, label):
    _ok(run, label)


@pytest.mark.parametrize("label", [
    "F7 an adopt that drops a tiled window's column floats it here at its own box, pushes nothing and announces it once",
    "F8 with no floatGeom it floats at this device's saved box, else the default box",
    "F9 a tiled window still in a column, a disposed one and an app window are untouched",
    "F11 a window closed elsewhere (gone from openTerms too) is not floated",
    "F12 a window floated by an adopt joins the workspace its column was on, hidden there, not the active one",
    "F13 the detached surface never floats a window on adopt",
    "F15 a controlled window floated by an adopt sends no resize",
    "F16 a tab of a tabbed row and a minimized pane of a split row float into their column's workspace",
    "F17 the front window floated by an adopt stays front (focus restored)",
])
def test_251_adopt_floats_a_dropped_column_locally(run, label):
    _ok(run, label)


def test_every_scenario_passed_and_node_exited_clean(run):
    assert run["failed"] == 0, run["stdout"] + run["stderr"]
    assert run["passed"] == EXPECTED_ASSERTS, run["stdout"]
    assert len(run["results"]) == EXPECTED_ASSERTS
    assert run["rc"] == 0, run["stderr"]
