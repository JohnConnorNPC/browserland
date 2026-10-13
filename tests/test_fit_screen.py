"""Behavioural tests for #248 C3 (owner O4): "Fit to this screen".

The manual escape for a terminal left at another device's size: a window
menu item, a desktop menu "Fit all to this screen" and two core key actions
(unbound by default). A floating window's box is reset first to this device's
default size and persisted -- its box may already be the damaged one, and a
refit derives the grid from the box (audit M7) -- then a forced resize is sent
from it; a tiled window only refits.

Runs the SHIPPED code in node: ``refitSoon`` and the ``isResizable`` ..
``fitAllToThisScreen`` block from 73, the ``CORE_KEY_ACTIONS`` literal and
``buildCtxMenu`` from 78, each cut at its own boundaries. The window-menu
builder has too many collaborators to run alone, so its item is pinned by
source. Skipped when node is absent.
"""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from webterm.broker import ui

BROKER_DIR = Path(ui.__file__).resolve().parent
RT = BROKER_DIR / "73_js_window_runtime.js"
KB = BROKER_DIR / "78_js_keybindings.js"
NODE = shutil.which("node")

CUTS = [
    (str(RT), "        function refitSoon(win) {", "        function maybeSendInitialResize(win) {"),
    (str(RT), "        function isResizable(win) {", "        function scheduleResize(win) {"),
    (str(KB), "        const CORE_KEY_ACTIONS = [", "        // ---- key-action registry (#148)"),
    (str(KB), "        function buildCtxMenu(x, y) {", "        // Per-chip taskbar menu:"),
]

EXPECTED_ASSERTS = 13

_HARNESS = r"""
'use strict';
const fs = require('fs');
let code = '';
for (const [path, a, b] of __CUTS__) {
    const s = fs.readFileSync(path, 'utf8');
    const i = s.indexOf(a), j = s.indexOf(b);
    if (i < 0 || j < i) throw new Error('boundary missing: ' + a);
    code += s.slice(i, j) + '\n';
}
let pass = 0, fail = 0;
function ok(cond, label) {
    if (cond) { pass++; console.log('PASS: ' + label); }
    else { fail++; console.log('FAIL: ' + label); }
}
function mkWorld() {
    const w = { windows: new Map(), saves: 0, geoms: [], menu: null, front: null };
    w.mk = function (id, o) {
        const win = Object.assign({ id: id, hostId: 'local', type: 'term', disposed: false,
            minimized: false, detached: false, controlled: null, tiled: false,
            lastSentDims: { cols: 61, rows: 30 },
            geom: { left: 40, top: 30, width: 622, height: 638 },     // the damaged phone box
            ws: { readyState: 1, sent: [], send: function (m) { this.sent.push(JSON.parse(m)); } },
        }, o || {});
        // body = outer box less borders/title (4 x 30), as the page lays it out
        win.body = { getBoundingClientRect: function () {
            return { width: win.geom.width - 4, height: win.geom.height - 30 };
        } };
        w.windows.set(id, win);
        return win;
    };
    const env = {
        windows: w.windows, WebSocket: { OPEN: 1 },
        readCellDims: function () { return { w: 10, h: 20 }; },
        requestAnimationFrame: function (fn) { fn(); },
        isDetachedSurface: function () { return w.detachedSurface === true; },
        defaultPixelSize: function () { return { width: 1012, height: 498 }; },
        applyGeomToWindow: function (win, g) {
            w.geoms.push(g);
            win.geom = { left: g.left, top: g.top, width: g.width, height: g.height };
        },
        savePrefs: function () { w.saves++; },
        get frontId() { return w.front; },
        // buildCtxMenu (tiling mode) collaborators
        isTilingMode: function () { return true; },
        floatingWindowsOrdered: function () { return []; },
        isSizeLocked: function () { return false; },
        _pushMenuItems: function () {}, _desktopMenuItems: [],
        launchControlPanel: function () {}, lastLayoutSnapshot: null,
        renderMenu: function (items) { w.menu = items; },
        // CORE_KEY_ACTIONS collaborators (referenced only inside arrows)
        focusColumnBy: function () {}, moveColumn: function () {}, activeDesktop: function () { return {}; },
        launchProfile: function () {}, localHost: function () {}, hostDefaultProfile: function () {},
        isTilingMode2: null, enterFloatingMode: function () {}, enterTilingMode: function () {},
        closeFrontWindow: function () {}, minimizeWindow: function () {}, toggleFullscreen: function () {},
        toggleControlPanelWindow: function () {}, detachWindow: function () {},
    };
    w.detachedSurface = false;
    w.api = new Function('env', 'with (env) {\n' + code +
        '\nvar __a = { fit: fitToThisScreen, fitAll: fitAllToThisScreen, sendResize: sendResize,\n' +
        '  actions: CORE_KEY_ACTIONS, buildCtxMenu: buildCtxMenu };\n}\nreturn __a;\n')(env);
    w.api.__setDetached = function (v) { w.detachedSurface = v; };
    return w;
}
const R = function (win) { return win.ws.sent.filter(m => m.type === 'resize'); };

let w = mkWorld();
let f = w.mk('local:1');
w.api.fit(f);
ok(w.geoms.length === 1 && w.geoms[0].width === 1012 && w.geoms[0].height === 498
    && w.geoms[0].left === 40 && w.geoms[0].top === 30 && w.saves === 1,
    'X1 a float box is reset to this device default size, in place, and persisted');
ok(R(f).length === 1 && R(f)[0].cols === 100 && R(f)[0].rows === 23,
    'X2 ...then the grid derived from the RESET box is sent (not the damaged one)');

w = mkWorld();
let t = w.mk('local:2', { tiled: true, lastSentDims: { cols: 100, rows: 23 },
                          geom: { left: 0, top: 0, width: 1012, height: 498 } });
w.api.fit(t);
ok(w.geoms.length === 0 && w.saves === 0 && R(t).length === 1,
    'X3 a tiled window keeps its box and still sends once despite the dedup');

w = mkWorld();
let m = w.mk('local:3', { minimized: true });
w.api.fit(m);
ok(R(m).length === 0 && m.needsReclaim === true, 'X4 a minimized window sends nothing now and keeps the mark');
m.minimized = false;
w.api.sendResize(m, false);
ok(R(m).length === 1, 'X5 ...and sends on its next ordinary refit');

w = mkWorld();
let app = w.mk('app:note:1', { type: 'app' });
let det = w.mk('local:4', { detached: true });
w.api.fit(app); w.api.fit(det);
ok(w.geoms.length === 0 && R(app).length === 0 && R(det).length === 0 && !det.needsReclaim,
    'X6 app and detached windows are untouched');

w = mkWorld();
let a1 = w.mk('local:5'), a2 = w.mk('local:6', { tiled: true });
w.front = 'local:6';
const byId = id => w.api.actions.find(x => x.id === id);
byId('fit-to-screen').run();
ok(R(a2).length === 1 && R(a1).length === 0, 'X7 the fit-to-screen action fits the front window only');
byId('fit-all-to-screen').run();
ok(R(a1).length === 1 && R(a2).length === 2, 'X8 the fit-all-to-screen action fits every terminal');

w = mkWorld();
let b1 = w.mk('local:7', { tiled: true });
w.mk('app:note:2', { type: 'app' });
w.api.buildCtxMenu(0, 0);
let item = (w.menu || []).find(x => x.label === 'Fit all to this screen');
ok(!!item && item.enabled === true, 'X9 the desktop menu offers Fit all to this screen');
if (item) item.action();
ok(R(b1).length === 1, 'X10 ...which fits every terminal');
const ci = w.menu.findIndex(x => x.label === 'Fit all to this screen');
ok(ci >= 0 && /Control panel/.test((w.menu[ci + 1] || {}).label || ''),
    'X11 ...right before Control panel');
w = mkWorld();
w.mk('app:note:3', { type: 'app' });
w.api.buildCtxMenu(0, 0);
item = (w.menu || []).find(x => x.label === 'Fit all to this screen');
ok(!!item && item.enabled === false, 'X12 ...disabled when no terminal is open');

w = mkWorld();
let ds = w.mk('local:9');
w.api.__setDetached(true);
w.api.fit(ds);
ok(w.geoms.length === 0 && R(ds).length === 1,
    'X13 on the detached surface (#226) the window refits from the viewport, no box reset');

console.log(pass + ' passed, ' + fail + ' failed');
"""


@pytest.fixture(scope="module")
def run(tmp_path_factory):
    if NODE is None:
        pytest.skip("node not installed")
    for path, a, b in CUTS:
        src = Path(path).read_text(encoding="utf-8")
        assert src.count(a) == 1 and src.count(b) == 1, (path, a, b)
    h = tmp_path_factory.mktemp("fit-screen") / "harness.js"
    h.write_text(_HARNESS.replace("__CUTS__", json.dumps(CUTS)), encoding="utf-8")
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


@pytest.mark.parametrize("label", [
    "X1 a float box is reset to this device default size, in place, and persisted",
    "X2 ...then the grid derived from the RESET box is sent (not the damaged one)",
    "X3 a tiled window keeps its box and still sends once despite the dedup",
    "X4 a minimized window sends nothing now and keeps the mark",
    "X5 ...and sends on its next ordinary refit",
    "X6 app and detached windows are untouched",
    "X7 the fit-to-screen action fits the front window only",
    "X8 the fit-all-to-screen action fits every terminal",
    "X9 the desktop menu offers Fit all to this screen",
    "X10 ...which fits every terminal",
    "X11 ...right before Control panel",
    "X12 ...disabled when no terminal is open",
    "X13 on the detached surface (#226) the window refits from the viewport, no box reset",
])
def test_fit_to_this_screen(run, label):
    got = run["results"].get(label)
    assert got is True, (f"{label!r} {'failed' if got is False else 'never ran'}\n"
                         f"{run['stdout']}\n{run['stderr']}")


def test_every_scenario_passed_and_node_exited_clean(run):
    assert run["failed"] == 0, run["stdout"] + run["stderr"]
    assert run["passed"] == EXPECTED_ASSERTS, run["stdout"]
    assert run["rc"] == 0, run["stderr"]


def test_the_window_menu_offers_fit_for_terminals_only():
    src = KB.read_text(encoding="utf-8")
    body = src[src.index("        function buildWindowMenu(win, x, y) {"):
               src.index("        function buildCtxMenu(x, y) {")]
    term_only = body[body.index("            if (win.type !== 'app') {\n"
                                "                items.push({ label: 'Open in new window'"):]
    term_only = term_only[:term_only.index("            }\n")]
    assert ("items.push({ label: 'Fit to this screen', enabled: !win.detached,\n"
            "                             action: () => fitToThisScreen(win) });") in term_only


def test_the_new_actions_are_unbound_and_documented():
    store = (BROKER_DIR / "54_js_app_windows_store.js").read_text(encoding="utf-8")
    defaults = store[store.index("const DEFAULT_KEYBINDINGS = {"):]
    defaults = defaults[:defaults.index("};")]
    assert "'fit-to-screen'" not in defaults and "'fit-all-to-screen'" not in defaults
    wiki = BROKER_DIR.parent.parent / "wiki"
    keys = (wiki / "Keyboard-Shortcuts.md").read_text(encoding="utf-8")
    assert "| Fit focused window to this screen | *(unbound by default)* |" in keys
    assert "| Fit all windows to this screen | *(unbound by default)* |" in keys
    menus = (wiki / "Context-Menus.md").read_text(encoding="utf-8")
    assert "- **Fit to this screen** (terminals only)" in menus
    assert "**Fit all to this screen**" in menus
