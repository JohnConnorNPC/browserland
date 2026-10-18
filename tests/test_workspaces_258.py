"""Behavioural tests for #258: moving, colouring and stacking workspaces.

Like ``tests/test_workspaces_commands.py`` this executes the SHIPPED
``mods/workspaces/workspaces.js`` in node, but over a fake DOM rich enough to
render the pager and drive it: real class lists, listeners that bubble,
``isConnected``, class/id selectors, ``style.setProperty``, focus. The world
factory's stub list is the one ``test_workspaces_commands.py`` passes, with
three changes, each named where it happens: ``visibleColumns`` applies the
mod's OWN registered column filter, ``mintLayoutId`` is the REAL 57 mint
seeded from the seeded columns exactly as 57 seeds it, and ``PALETTE`` is the
real array sliced from 50.

The first block (BL*) is the baseline: it passes on the mod as it was before
#258, and pins that a workspace that is not in a stack behaves exactly as
before. PASS/FAIL lines are parsed by pytest; skipped when node is absent.
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
MODEL_JS = BROKER_DIR / "57_js_tiling_model.js"
CONST_JS = BROKER_DIR / "50_js_constants.js"
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="node not installed")

EXPECTED_ASSERTS = 16

_PRELUDE = r"""
'use strict';
const fs = require('fs');
const src = fs.readFileSync(__SRC_PATH__, 'utf8');
const modelSrc = fs.readFileSync(__MODEL_PATH__, 'utf8');
const constSrc = fs.readFileSync(__CONST_PATH__, 'utf8');
function cut(text, from, to) {
    const a = text.indexOf(from), b = text.indexOf(to, a + 1);
    if (a < 0 || b < 0) throw new Error('slice boundary missing: ' + from);
    return text.slice(a, b);
}
// The REAL mint + seed from 57 and the REAL PALETTE from 50.
const MINT_SRC = cut(modelSrc, '        let _layoutIdSeq = 0;',
                     '        function newColumn() {');
const PALETTE = new Function(cut(constSrc, 'const PALETTE = [', '];') + '];'
    + ' return PALETTE;')();

let pass = 0, fail = 0;
function ok(cond, label) {
    if (cond) { pass++; console.log('PASS: ' + label); }
    else { fail++; console.log('FAIL: ' + label); }
}
const J = JSON.stringify;

// ---- fake DOM -------------------------------------------------------------
let DOC = null;
class Node {
    constructor(tag) {
        this.tagName = (tag || 'div').toUpperCase();
        this.children = [];
        this.parentNode = null;
        this.dataset = {};
        this.id = '';
        this.title = '';
        this.draggable = false;
        this.type = '';
        this._text = '';
        this._cls = [];
        this._ls = {};
        this.rect = { left: 0, top: 0, right: 0, bottom: 0, width: 0, height: 0 };
        const styleProps = {};
        this.style = {
            setProperty: function (k, v) { styleProps[k] = String(v); },
            getPropertyValue: function (k) { return styleProps[k] || ''; },
            removeProperty: function (k) { delete styleProps[k]; },
        };
        const self = this;
        this.classList = {
            add: function () { for (const c of arguments) if (self._cls.indexOf(c) < 0) self._cls.push(c); },
            remove: function () { for (const c of arguments) { const i = self._cls.indexOf(c); if (i >= 0) self._cls.splice(i, 1); } },
            toggle: function (c, on) {
                const has = self._cls.indexOf(c) >= 0;
                const want = (on === undefined) ? !has : !!on;
                if (want && !has) self._cls.push(c);
                if (!want && has) self._cls.splice(self._cls.indexOf(c), 1);
                return want;
            },
            contains: function (c) { return self._cls.indexOf(c) >= 0; },
        };
    }
    get className() { return this._cls.join(' '); }
    set className(v) { this._cls = String(v).split(/\s+/).filter(Boolean); }
    get textContent() {
        return this._text + this.children.map(function (c) { return c.textContent; }).join('');
    }
    set textContent(v) { this.children.forEach(function (c) { c.parentNode = null; }); this.children = []; this._text = String(v); }
    set innerHTML(v) { this.textContent = ''; }
    get innerHTML() { return ''; }
    get isConnected() {
        let n = this;
        while (n) { if (n === DOC.documentElement) return true; n = n.parentNode; }
        return false;
    }
    get firstChild() { return this.children[0] || null; }
    appendChild(k) {
        if (k.parentNode) k.parentNode.removeChild(k);
        k.parentNode = this; this.children.push(k); return k;
    }
    insertBefore(k, ref) {
        if (k.parentNode) k.parentNode.removeChild(k);
        const i = ref ? this.children.indexOf(ref) : -1;
        k.parentNode = this;
        if (i < 0) this.children.push(k); else this.children.splice(i, 0, k);
        return k;
    }
    removeChild(k) {
        const i = this.children.indexOf(k);
        if (i !== -1) { this.children.splice(i, 1); k.parentNode = null; }
        return k;
    }
    remove() { if (this.parentNode) this.parentNode.removeChild(this); }
    contains(n) { while (n) { if (n === this) return true; n = n.parentNode; } return false; }
    addEventListener(t, fn) { (this._ls[t] = this._ls[t] || []).push(fn); }
    removeEventListener(t, fn) {
        const l = this._ls[t] || []; const i = l.indexOf(fn); if (i >= 0) l.splice(i, 1);
    }
    focus() { DOC.activeElement = this; }
    blur() { if (DOC.activeElement === this) DOC.activeElement = DOC.body; }
    click() { fire(this, 'click'); }
    getBoundingClientRect() { return Object.assign({}, this.rect); }
    get offsetWidth() { return this.rect.width; }
    get offsetHeight() { return this.rect.height; }
    setAttribute(k, v) { this['attr:' + k] = String(v); }
    getAttribute(k) { const v = this['attr:' + k]; return v === undefined ? null : v; }
    querySelectorAll(sel) { return queryAll(this, sel); }
    querySelector(sel) { return queryAll(this, sel)[0] || null; }
}
// One compound selector: tag? #id? .cls* :not(.cls)*
function matchCompound(n, comp) {
    const m = comp.match(/^([a-z]*)((?:#[\w-]+)?)((?:\.[\w-]+)*)((?::not\(\.[\w-]+\))*)$/i);
    if (!m) throw new Error('unsupported selector ' + comp);
    if (m[1] && n.tagName !== m[1].toUpperCase()) return false;
    if (m[2] && n.id !== m[2].slice(1)) return false;
    for (const c of m[3].split('.').filter(Boolean)) if (!n.classList.contains(c)) return false;
    for (const nc of (m[4].match(/\.[\w-]+/g) || [])) if (n.classList.contains(nc.slice(1))) return false;
    return true;
}
function descendants(root) {
    const out = [];
    (function walk(n) { for (const k of n.children) { out.push(k); walk(k); } })(root);
    return out;
}
function queryAll(root, sel) {
    const parts = sel.trim().split(/\s+/);
    return descendants(root).filter(function (n) {
        if (!matchCompound(n, parts[parts.length - 1])) return false;
        let p = n.parentNode, i = parts.length - 2;
        while (i >= 0 && p && p !== root.parentNode) {
            if (matchCompound(p, parts[i])) i--;
            p = p.parentNode;
        }
        return i < 0;
    });
}
// Dispatch with bubbling: currentTarget walks up, stopPropagation stops it.
function fire(target, type, init) {
    const ev = Object.assign({ type: type, target: target, defaultPrevented: false,
        _stop: false, clientX: 0, clientY: 0, key: '', relatedTarget: null,
        dataTransfer: null }, init || {});
    ev.preventDefault = function () { ev.defaultPrevented = true; };
    ev.stopPropagation = function () { ev._stop = true; };
    ev.stopImmediatePropagation = function () { ev._stop = true; };
    let n = target;
    while (n && !ev._stop) {
        ev.currentTarget = n;
        for (const fn of (n._ls[type] || []).slice()) fn(ev);
        n = n.parentNode;
    }
    return ev;
}
function mkDT() {
    const data = {};
    return { data: data, effectAllowed: '', dropEffect: '',
        get types() { return Object.keys(data); },
        setData: function (k, v) { data[k] = String(v); },
        getData: function (k) { return data[k] || ''; } };
}
function mkDocument() {
    const doc = { activeElement: null };
    DOC = doc;
    doc.documentElement = new Node('html');
    doc.body = doc.documentElement.appendChild(new Node('body'));
    const taskbar = doc.body.appendChild(new Node('div')); taskbar.id = 'taskbar';
    const items = taskbar.appendChild(new Node('div')); items.id = 'taskbar-items';
    const hs = taskbar.appendChild(new Node('div')); hs.id = 'host-status';
    doc.activeElement = doc.body;
    doc._ls = {};
    doc.getElementById = function (id) {
        return descendants(doc.documentElement).filter(function (n) { return n.id === id; })[0] || null;
    };
    doc.createElement = function (t) { return new Node(t); };
    doc.querySelectorAll = function (sel) { return queryAll(doc.documentElement, sel); };
    doc.querySelector = function (sel) { return queryAll(doc.documentElement, sel)[0] || null; };
    doc.addEventListener = function (t, fn) { (doc._ls[t] = doc._ls[t] || []).push(fn); };
    doc.removeEventListener = function (t, fn) {
        const l = doc._ls[t] || []; const i = l.indexOf(fn); if (i >= 0) l.splice(i, 1);
    };
    return doc;
}

// ---- one world = document + layout + spies + one mod init -----------------
// seed: { list, activeId, stacks?, columns?, labelMode?, hidePager? }
function buildWorld(seed) {
    seed = seed || {};
    const w = { document: mkDocument() };
    const list = seed.list || [
        { id: 'W1', focusedCol: 0 }, { id: 'W2', focusedCol: 0 },
        { id: 'W3', focusedCol: 0 }];
    const store = { version: 1, activeId: seed.activeId || list[0].id, list: list };
    if (seed.stacks !== undefined) store.stacks = seed.stacks;
    w.L = { mode: 'tiling', focusedCol: 0, mods: { workspaces: store },
        columns: seed.columns || [
            { id: 'L1', widthPreset: '1/2', wsId: list[0].id, rows: [
                { height: 1, keys: ['local:21'], mode: 'single' } ] },
            { id: 'L2', widthPreset: '1/2', wsId: list[1 % list.length].id, rows: [
                { height: 1, keys: ['local:22'], mode: 'single' } ] },
            { id: 'L3', widthPreset: '1/2', wsId: list[2 % list.length].id, rows: [
                { height: 1, keys: ['local:23'], mode: 'single' } ] },
        ] };
    w.windows = new Map();
    for (const col of w.L.columns) for (const r of col.rows) for (const k of r.keys) {
        w.windows.set(k, { id: k, disposed: false, tiled: true, dom: new Node() });
    }
    w.sessions = new Map();
    w.prefs = {};
    w.modprefs = {};
    w.labelMode = seed.labelMode || 'number';
    w.settings = { hideTaskbarOtherWs: false, hideWsPager: !!seed.hidePager };
    w.spies = { prompt: [], confirm: [], notice: [], save: 0, front: [], relayout: 0 };
    w.menus = [];
    w.pops = [];
    w.unloads = [];
    w.leaseLost = false;
    const seq = new Function(MINT_SRC + ' return { mint: mintLayoutId, seed: seedLayoutIdSeq };')();
    seq.seed(w.L);           // exactly as 57 seeds it: from the stored columns
    w.mint = seq.mint;

    function findKeyInLayout(key) {
        for (let ci = 0; ci < w.L.columns.length; ci++) {
            const col = w.L.columns[ci];
            for (let ri = 0; ri < col.rows.length; ri++) {
                const ti = col.rows[ri].keys.indexOf(String(key));
                if (ti !== -1) return { desktop: w.L, col: col, colIndex: ci,
                    row: col.rows[ri], rowIndex: ri, cell: null, cellIndex: -1, tabIndex: ti };
            }
        }
        return null;
    }
    // CHANGE vs the commands harness: the mod's OWN filter decides.
    function visibleColumns() {
        return w.filter ? w.L.columns.filter(function (c) { return w.filter(c, w.L); }) : w.L.columns;
    }
    function columnKeysFn(col) {
        const out = [];
        for (const r of col.rows) for (const k of r.keys) out.push(k);
        return out;
    }
    function settingFor(key) {
        return { get: function () { return w.settings[key]; },
                 set: function (v) { w.settings[key] = v; },
                 onChange: function (fn) { w.onSetting = w.onSetting || {}; w.onSetting[key] = fn; } };
    }
    w.ctx = {
        events: { on: function (n, fn) { if (n === 'lease:changed') { w.leaseFn = fn; fn({ active: !w.leaseLost }); } } },
        settings: {
            select: function () {
                return { get: function () { return w.labelMode; },
                         set: function (v) { w.labelMode = v; },
                         onChange: function () {} };
            },
            boolean: function (key) { return settingFor(key); },
        },
        desktop: { columnFilter: function (fn) { w.filter = fn; },
                   onColumnCreated: function () {}, onPlaced: function () {},
                   onForgotten: function () {}, onReveal: function (fn) { w.reveal = fn; },
                   onLayoutRender: function (fn) { w.layoutRender = fn; } },
        taskbar: { onItemsRendered: function () {},
                   interceptActivate: function (fn) { w.intercept = fn; } },
        onUnload: function (fn) { w.unloads.push(fn); },
        popover: { anchor: function (node, anchor, opts) {
            const p = { node: node, anchor: anchor, opts: opts || {}, open: true };
            w.document.body.appendChild(node);
            p.close = function (why) {
                if (!p.open) return false;
                p.open = false; node.remove();
                if (p.opts.onClose) p.opts.onClose(why || 'close');
                return true;
            };
            w.pops.push(p);
            return { close: function () { return p.close('close'); },
                     reposition: function () {}, isOpen: function () { return p.open; } };
        } },
        prefs: {
            get: function (k, d) { return Object.prototype.hasOwnProperty.call(w.modprefs, k) ? w.modprefs[k] : d; },
            set: function (k, v) { w.modprefs[k] = v; },
        },
        commands: {
            register: function (id, spec) { (w.commands = w.commands || {})[id] = spec; return function () {}; },
            execute: function () { return Promise.resolve({ ok: false, reason: 'absent' }); },
        },
        registerWindowMenuItems: function (fn) { w.windowMenu = fn; },
        registerDesktopMenuItems: function (fn) { w.desktopMenu = fn; },
        registerKeyActions: function (acts) { w.keys = {}; for (const a of acts) w.keys[a.id] = a; },
    };
    let registered = null;
    const names = {
        registerMod: function (def) { registered = def; },
        document: w.document, prefs: w.prefs,
        getLayout: function () { return w.L; },
        savePrefs: function () { w.spies.save++; },
        savePrefsLocal: function () {},
        windows: w.windows, sessions: w.sessions, frontId: null,
        relayoutStrip: function () { w.spies.relayout++; },
        updateTaskbarActive: function () {},
        findKeyInLayout: findKeyInLayout,
        mintLayoutId: function () { return w.mint(); },   // CHANGE: the real 57 mint
        visibleColumns: visibleColumns,
        parkWindow: function () {},
        isLiveKey: function (k) { return w.windows.has(k); },
        refitSoon: function () {},
        rowKeys: function (row) { return (row && row.keys) ? row.keys.slice() : []; },
        colCurrentFrac: function () { return 0.5; },
        openConfirmDialog: function (o) { w.spies.confirm.push(o); return Promise.resolve(w.confirmAnswer !== undefined ? w.confirmAnswer : true); },
        columnKeys: columnKeysFn,
        openTextPrompt: function (o) { w.spies.prompt.push(o); return Promise.resolve(w.promptAnswer !== undefined ? w.promptAnswer : null); },
        renderMenu: function (items, x, y) { w.menus.push({ items: items, x: x, y: y }); },
        expelToNewColumn: function () {},
        requestRelayout: function () {},
        reconcileTiledFocus: function () {},
        visibleColIndex: function (col) { return visibleColumns().indexOf(col); },
        captureTiledFocusContext: function () { return null; },
        showNotice: function (m) { w.spies.notice.push(m); },
        bringToFront: function (k) { w.spies.front.push(k); },
        firstLiveKeyInColumn: function (col) { for (const r of col.rows) for (const k of r.keys) return k; return null; },
        reorderTaskbarItems: function () {},
        _deactivated: false,
        console: console,
        PALETTE: PALETTE,                                  // CHANGE: the real 50 palette
        window: { innerWidth: 1600, innerHeight: 900 },
    };
    w.seedJson = J(w.L.mods.workspaces);
    new Function(...Object.keys(names), src)(...Object.values(names));
    w.def = registered;
    w.def.init(w.ctx);
    w.st = function () { return w.L.mods.workspaces; };
    w.pager = function () { return w.document.getElementById('ws-pager'); };
    w.dots = function () { return w.pager().querySelectorAll('.ws-dot:not(.add)'); };
    w.render = function () { if (w.layoutRender) w.layoutRender(); };
    w.lastMenu = function () { return w.menus[w.menus.length - 1] || null; };
    w.item = function (menu, re) {
        return (menu ? menu.items : []).filter(function (it) {
            return typeof it.label === 'string' && re.test(it.label); })[0] || null;
    };
    return w;
}
function labels(w) { return w.dots().map(function (d) { return d.textContent; }); }
"""

_SCENARIOS = r"""
async function baseline() {
    // ---- BL: a workspace that is not in a stack behaves as before ----------
    let w = buildWorld();
    const dots = w.dots();
    const add = w.pager() && w.pager().querySelector('.add');
    ok(dots.length === 3 && add && J(labels(w)) === J(['1', '2', '3'])
        && add.textContent === '+',
        'BL1 one numbered dot per workspace plus the + dot');
    ok(dots.length === 3 && dots[0].classList.contains('active')
        && !dots[1].classList.contains('active'),
        'BL2 the active workspace\'s dot is highlighted');
    ok(dots.length === 3 && /^workspace 2\b/.test(dots[1].title)
        && dots[1].title.indexOf('right-click') !== -1,
        'BL3 a dot\'s tooltip names its workspace number');
    const saves = w.spies.save;
    if (dots[2]) fire(dots[2], 'click');
    ok(dots.length === 3 && w.st().activeId === 'W3' && w.spies.save > saves,
        'BL4 clicking a dot switches to that workspace and saves');

    w = buildWorld();
    w.labelMode = 'name';
    w.st().list[1].name = 'build';
    w.render();
    ok(w.dots().length === 3 && J(labels(w)) === J(['WS1', 'build', 'WS3']),
        'BL5 a rename repaints the dot label (names mode)');

    w = buildWorld();
    const k = w.keys || {};
    ok(!!(k['workspace-3'] && k['workspace-5']), 'BL6 precondition: the number keys are registered');
    if (k['workspace-3']) k['workspace-3'].run();
    const at3 = w.st().activeId;
    if (k['workspace-5']) k['workspace-5'].run();
    ok(at3 === 'W3' && w.st().activeId === 'W3',
        'BL6 workspace-3 goes to the third workspace; workspace-5 past the end does nothing');

    w = buildWorld();
    const kk = w.keys || {};
    if (kk['workspace-prev']) kk['workspace-prev'].run();
    const a0 = w.st().activeId;
    if (kk['workspace-next']) kk['workspace-next'].run();
    const a1 = w.st().activeId;
    if (kk['workspace-next']) kk['workspace-next'].run();
    if (kk['workspace-next']) kk['workspace-next'].run();
    ok(!!kk['workspace-next'] && a0 === 'W1' && a1 === 'W2' && w.st().activeId === 'W3',
        'BL7 previous/next step one workspace and clamp at both ends');

    w = buildWorld();
    const rows = w.desktopMenu ? w.desktopMenu({ tiling: true }) : [];
    const wsRows = rows.filter(function (it) { return /Workspace \d/.test(it.label || ''); });
    ok(wsRows.length === 3 && /^✓ Workspace 1 \(1\)$/.test(wsRows[0].label)
        && /^ +Workspace 2 \(1\)$/.test(wsRows[1].label),
        'BL8 the desktop menu lists every workspace with its count and a check on the current one');

    w = buildWorld();
    const tiled = w.windows.get('local:21');
    const sendItems = w.windowMenu ? w.windowMenu(tiled) : [];
    const sends = sendItems.filter(function (it) { return /^Send to workspace \d$/.test(it.label || ''); });
    ok(sends.length === 2 && sends[0].label === 'Send to workspace 2'
        && sends[1].label === 'Send to workspace 3',
        'BL9 a tiled window\'s menu offers Send to every other workspace by number');
}
"""

_SCENARIOS += r"""
async function heal() {
    // ---- H: the heal never writes a valid store, and canonicalises garbage --
    const stacked = [
        { id: 'W1', focusedCol: 0, color: '#4aa3ff' },
        { id: 'W2', focusedCol: 0, stack: 'S9', name: 'api' },
        { id: 'W3', focusedCol: 0, stack: 'S9', color: '#e96d6d' }];
    let w = buildWorld({ list: stacked, stacks: [
        { id: 'S9', name: 'dev', color: '#5fbf7f', lastId: 'W3' }] });
    const st = w.st();
    ok(Array.isArray(st.stacks) && st.stacks.length === 1 && st.list[2].stack === 'S9',
        'H1 precondition: the stacked seed is in the store');
    ok(w.spies.save === 0 && J(w.st()) === w.seedJson,
        'H1 a valid stacked store is left byte-equal and never saved');
    w = buildWorld();
    ok(w.spies.save === 0 && J(w.st()) === w.seedJson && !('stacks' in w.st()),
        'H1 a valid pre-#258 store is left byte-equal, never saved, and gains no stacks key');

    w = buildWorld({ list: [
        { id: 'W1', focusedCol: 0, color: 'red' }, { id: 'W2', focusedCol: 0, color: '#12' },
        { id: 'W3', focusedCol: 0, color: 7 }, { id: 'W4', focusedCol: 0, color: '#ABCDEF' },
        { id: 'W5', focusedCol: 0, color: '#abc' }] });
    const cs = w.st().list.map(function (x) { return x.color === undefined ? '-' : x.color; });
    ok(w.st().list.length === 5 && J(cs) === J(['-', '-', '-', '#abcdef', '#aabbcc'])
        && w.spies.save === 1,
        'H2 bad colours are dropped, hex is canonicalised, and the heal saves once');

    w = buildWorld({ list: [
        { id: 'W1', focusedCol: 0, stack: 'GONE' },
        { id: 'W2', focusedCol: 0, stack: 'S1' }, { id: 'W3', focusedCol: 0, stack: 'S1' }],
        stacks: [null, 5, { id: '' }, { id: 'S1', name: '  ops  ', color: 'blue' },
                 { id: 'S1', name: 'dup' }, { id: 'W1' }] });
    const s5 = w.st();
    ok(s5.list.length === 3 && Array.isArray(s5.stacks)
        && J(s5.stacks) === J([{ id: 'S1', name: 'ops' }])
        && s5.list[0].stack === undefined && s5.list[1].stack === 'S1'
        && w.spies.save === 1,
        'H5 garbage, duplicate and workspace-id stack rows and a dangling membership are healed');
    w = buildWorld({ stacks: 'junk' });
    ok(w.st().list.length === 3 && !('stacks' in w.st()) && w.spies.save === 1,
        'H5 a non-array stacks field is deleted');
}
"""

_EPILOGUE = r"""
(async function () {
    await baseline();
    await heal();
    console.log(pass + ' passed, ' + fail + ' failed');
    process.exitCode = fail ? 1 : 0;
})().catch(function (e) {
    console.error(e && e.stack || e);
    process.exitCode = 1;
});
"""


@pytest.fixture(scope="module")
def scenario_run(tmp_path_factory):
    harness = tmp_path_factory.mktemp("ws-258") / "harness.js"
    text = (_PRELUDE + _SCENARIOS + _EPILOGUE)
    text = text.replace("__SRC_PATH__", json.dumps(str(SRC_JS)))
    text = text.replace("__MODEL_PATH__", json.dumps(str(MODEL_JS)))
    text = text.replace("__CONST_PATH__", json.dumps(str(CONST_JS)))
    harness.write_text(text, encoding="utf-8")
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
        f"scenario {label!r} {verdict}\n"
        f"stdout: {run['stdout']}\nstderr: {run['stderr']}")


def test_unstacked_pager_behaves_as_before(scenario_run):
    for label in (
            "BL1 one numbered dot per workspace plus the + dot",
            "BL2 the active workspace's dot is highlighted",
            "BL3 a dot's tooltip names its workspace number",
            "BL4 clicking a dot switches to that workspace and saves",
            "BL5 a rename repaints the dot label (names mode)",
            "BL6 precondition: the number keys are registered",
            "BL6 workspace-3 goes to the third workspace; workspace-5 past the end does nothing",
            "BL7 previous/next step one workspace and clamp at both ends",
            "BL8 the desktop menu lists every workspace with its count and a check on the current one",
            "BL9 a tiled window's menu offers Send to every other workspace by number"):
        _ok(scenario_run, label)


def test_heal_never_writes_a_valid_store(scenario_run):
    _ok(scenario_run, "H1 precondition: the stacked seed is in the store")
    _ok(scenario_run, "H1 a valid stacked store is left byte-equal and never saved")
    _ok(scenario_run, "H1 a valid pre-#258 store is left byte-equal, never saved, and gains no stacks key")


def test_heal_colours(scenario_run):
    _ok(scenario_run, "H2 bad colours are dropped, hex is canonicalised, and the heal saves once")


def test_heal_stack_garbage(scenario_run):
    _ok(scenario_run, "H5 garbage, duplicate and workspace-id stack rows and a dangling membership are healed")
    _ok(scenario_run, "H5 a non-array stacks field is deleted")


def test_every_scenario_passed_and_node_exited_clean(scenario_run):
    run = scenario_run
    assert run["failed"] == 0, (
        f"harness reported failures\nstdout: {run['stdout']}\nstderr: {run['stderr']}")
    assert run["passed"] == EXPECTED_ASSERTS, (
        f"expected {EXPECTED_ASSERTS} asserts, harness ran {run['passed']} "
        f"-- scenario drift\nstdout: {run['stdout']}")
    assert len(run["results"]) == EXPECTED_ASSERTS
    assert run["rc"] == 0, f"node exited {run['rc']}\nstderr: {run['stderr']}"
