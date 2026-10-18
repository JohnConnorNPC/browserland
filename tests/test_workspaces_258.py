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

EXPECTED_ASSERTS = 67

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
        savePrefs: function () { w.spies.save++; w.spies.saved = J(w.L.mods.workspaces); },
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
    // A stack's entry dot, and the row of a member in its open list.
    w.stackDot = function (sid) {
        return w.dots().filter(function (x) { return x.dataset.stackId === sid; })[0] || null;
    };
    w.openList = function (sid) {
        const d = w.stackDot(sid);
        if (!d) return null;
        fire(d, 'click');
        const p = w.pops[w.pops.length - 1];
        return (p && p.open && p.node.classList.contains('ws-stack-list')) ? p : null;
    };
    w.row = function (pop, wsId) {
        return pop ? pop.node.querySelectorAll('.wsl-row').filter(function (b) { return b.dataset.wsId === wsId; })[0] || null : null;
    };
    // A workspace's own menu, by id: right-click its pager dot, or -- for a
    // stack member -- its row in the stack's list.
    w.openWsMenu = function (wsId) {
        const d = w.dots().filter(function (x) { return x.dataset.wsId === wsId; })[0];
        if (d) {
            fire(d, 'contextmenu', { clientX: 10, clientY: 20 });
            return w.lastMenu();
        }
        const ws = w.st().list.filter(function (x) { return x.id === wsId; })[0];
        const r = w.row(ws && ws.stack ? w.openList(ws.stack) : null, wsId);
        if (!r) return null;
        fire(r, 'contextmenu', { clientX: 10, clientY: 20 });
        return w.lastMenu();
    };
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

    function ws(id, stack) { const o = { id: id, focusedCol: 0 }; if (stack) o.stack = stack; return o; }
    function ids(w) { return w.st().list.map(function (x) { return x.id; }).join(','); }
    const SST = [{ id: 'SA' }, { id: 'SB' }, { id: 'SC' }];
    w = buildWorld({ list: [ws('A1', 'SA'), ws('B1', 'SB'), ws('A2', 'SA'), ws('C1', 'SC'),
                            ws('B2', 'SB'), ws('C2', 'SC')], stacks: SST.map(function (s) { return Object.assign({}, s); }) });
    const r1 = ids(w);
    const w2 = buildWorld({ list: [ws('P1'), ws('A1', 'SA'), ws('P2'), ws('A2', 'SA'), ws('A3', 'SA')],
                            stacks: [{ id: 'SA' }] });
    const w3 = buildWorld({ list: [ws('B1', 'SB'), ws('A1', 'SA'), ws('B2', 'SB'), ws('A2', 'SA'), ws('P1')],
                            stacks: [{ id: 'SA' }, { id: 'SB' }] });
    ok(r1 === 'A1,A2,B1,B2,C1,C2' && ids(w2) === 'P1,A1,A2,A3,P2' && ids(w3) === 'B1,B2,A1,A2,P1'
        && w.spies.save === 1 && w.st().stacks.length === 3,
        'H3 interleaved stack members are regrouped where each stack first appears, order kept');

    w = buildWorld({ list: [ws('W1'), ws('W2', 'S1'), ws('W3')],
                     stacks: [{ id: 'S1', name: 'solo', lastId: 'W2' }] });
    ok(w.st().list.length === 3 && w.st().list[1].stack === undefined
        && J(w.st().stacks) === J([]) && w.spies.save === 1,
        'H4 a stack left with one workspace dissolves (row and membership)');

    w = buildWorld({ list: [ws('W1'), ws('W2', 'S1'), ws('W3', 'S1')],
                     stacks: [{ id: 'S1', lastId: 'W1' }] });
    ok(w.st().stacks.length === 1 && w.st().list[2].stack === 'S1'
        && w.st().stacks[0].lastId === undefined && w.spies.save === 1,
        'H6 a lastId naming a workspace outside its stack is dropped');

    // H7: every malformed seed above, healed again, is byte-identical and unsaved.
    const seeds = [
        { list: [ws('A1', 'SA'), ws('B1', 'SB'), ws('A2', 'SA'), ws('C1', 'SC'), ws('B2', 'SB'), ws('C2', 'SC')],
          stacks: [{ id: 'SA', name: ' x ', color: '#ABC' }, { id: 'SB', lastId: 'A1' }, { id: 'SC' }] },
        { list: [ws('W1', 'GONE'), ws('W2', 'S1'), ws('W3')], stacks: [{ id: 'S1' }, { id: 'S1' }, 7] },
        { list: [{ id: 'W1', focusedCol: 0, color: '#FFF' }, ws('W2'), ws('W3')], stacks: 'junk' },
    ];
    let idem = 0;
    for (const s of seeds) {
        const ww = buildWorld(s);
        const j1 = J(ww.st()), s1 = ww.spies.save;
        ww.render(); ww.render();
        if (s1 === 1 && J(ww.st()) === j1 && ww.spies.save === s1) idem++;
    }
    ok(idem === seeds.length, 'H7 a healed store heals to itself: a second pass changes nothing and saves nothing');
}
"""

_SCENARIOS += r"""
async function minting() {
    // ---- ID: a new workspace never re-mints a stored workspace id ----------
    // Columns L1..L3 seed 57's counter to 3, so its next mint is L4 -- which
    // is ALREADY a workspace here (the state after a reload).
    const seed = function () { return { list: [
        { id: 'L4', focusedCol: 0 }, { id: 'W2', focusedCol: 0 }, { id: 'W3', focusedCol: 0 }] }; };
    let w = buildWorld(seed());
    ok(w.mint() === 'L4', "ID precondition: 57's next mint is the stored workspace id L4");
    w = buildWorld(seed());
    const plus = w.pager().querySelector('.add');
    if (plus) fire(plus, 'click');
    let l = w.st().list, nu = l[3];
    ok(!!plus && l.length === 4 && nu && ['L1', 'L2', 'L3', 'L4', 'W2', 'W3'].indexOf(nu.id) === -1
        && w.st().activeId === nu.id,
        'ID1 + adds a workspace with a fresh id that survives the heal, and switches to it');
    w = buildWorld(seed());
    const items = w.windowMenu ? w.windowMenu(w.windows.get('local:21')) : [];
    const toNew = items.filter(function (it) { return it.label === 'Send to new workspace'; })[0];
    if (toNew) toNew.action();
    l = w.st().list; nu = l[3];
    ok(!!toNew && l.length === 4 && nu && ['L1', 'L2', 'L3', 'L4', 'W2', 'W3'].indexOf(nu.id) === -1
        && w.L.columns.filter(function (c) { return c.id === 'L1'; })[0].wsId === nu.id,
        "ID2 Send to new workspace mints a fresh id and the window's column moves onto it");
}
"""

_SCENARIOS += r"""
async function pagerNumbers() {
    // ---- PM: numbers count PAGER entries; a stack member reads N.k ---------
    function stackedSeed() {
        return { list: [
            { id: 'W1', focusedCol: 0 }, { id: 'W2', focusedCol: 0, stack: 'S1' },
            { id: 'W3', focusedCol: 0, stack: 'S1' }, { id: 'W4', focusedCol: 0 }],
            stacks: [{ id: 'S1' }] };
    }
    let w = buildWorld(stackedSeed());
    const items = w.windowMenu ? w.windowMenu(w.windows.get('local:21')) : [];
    const sends = items.filter(function (it) { return /^Send to workspace /.test(it.label || ''); })
        .map(function (it) { return it.label; });
    ok(J(sends) === J(['Send to workspace 2.1', 'Send to workspace 2.2', 'Send to workspace 3']),
        'PM1 Send to names stack members N.k and the workspace after the stack by its entry number');

    w = buildWorld(stackedSeed());
    const rows = (w.desktopMenu ? w.desktopMenu({ tiling: true }) : [])
        .filter(function (it) { return /Workspace \d/.test(it.label || ''); })
        .map(function (it) { return it.label.trim(); });
    ok(J(rows) === J(['✓ Workspace 1 (1)', 'Workspace 2.1 (1)', 'Workspace 2.2 (1)', 'Workspace 3 (0)']),
        'PM2 the desktop menu numbers rows by pager entry, members as N.k');

    w = buildWorld(stackedSeed());
    const chip = w.document.getElementById('taskbar-items').appendChild(new Node('div'));
    chip.className = 'taskbar-item';
    chip.dataset.sessionId = 'local:23';           // column L3 lives on W3 (2.2)
    w.render();
    const badge = chip.querySelector('.ti-ws');
    ok(!!badge && badge.textContent === 'ws2.2', 'PM3 the taskbar badge names a stack member N.k');

    w = buildWorld(stackedSeed());
    const d3 = w.dots().filter(function (d) { return d.textContent === '3'; })[0];
    if (d3) fire(d3, 'mouseenter');
    const pop = w.pops[w.pops.length - 1];
    const title = pop && pop.node.children[0] ? pop.node.children[0].textContent : '';
    ok(!!d3 && /^workspace 3\b/.test(d3.title) && title === '(workspace 3)',
        'PM4 the workspace after a stack is entry 3 on its dot, tooltip and preview');
}
"""

_SCENARIOS += r"""
async function signature() {
    // ---- SG: every visible field rebuilds the pager; TN: the tint ----------
    function seed() {
        return { activeId: 'W3', list: [
            { id: 'W1', focusedCol: 0 }, { id: 'W2', focusedCol: 0 }, { id: 'W3', focusedCol: 0 }] };
    }
    let w = buildWorld(seed());
    let before = w.dots()[0];
    const l = w.st().list;
    const t = l[0]; l[0] = l[1]; l[1] = t;               // an adopt reordered W1/W2
    w.render();
    let after = w.dots()[0];
    ok(!!before && !!after && before.dataset.wsId === 'W1' && after !== before
        && after.dataset.wsId === 'W2' && after.textContent === '1',
        'SG1 a reorder rebuilds the dots, each carrying its moved workspace');

    w = buildWorld(seed());
    before = w.dots()[1];
    w.st().list[1].color = '#e96d6d';
    w.render();
    after = w.dots()[1];
    ok(!!before && !before.classList.contains('tinted') && after !== before
        && after.classList.contains('tinted')
        && after.style.getPropertyValue('--ws-color') === '#e96d6d' && after.textContent === '2',
        'SG2 a colour change rebuilds the dot with its tint, label kept');

    // Only W3's membership changes: the stack row S1 already exists.
    w = buildWorld({ activeId: 'W4', stacks: [{ id: 'S1' }], list: [
        { id: 'W1', focusedCol: 0, stack: 'S1' }, { id: 'W2', focusedCol: 0, stack: 'S1' },
        { id: 'W3', focusedCol: 0 }, { id: 'W4', focusedCol: 0 }] });
    before = w.dots().filter(function (d) { return d.dataset.wsId === 'W3'; })[0];
    w.st().list[2].stack = 'S1';
    w.render();
    after = w.dots();
    ok(!!before && before.textContent === '2' && !before.isConnected && after.length > 0
        && !after.some(function (d) { return d.dataset.wsId === 'W3' && d.textContent === '2'; }),
        'SG3 joining a stack rebuilds the pager and relabels the member');

    w = buildWorld({ list: [{ id: 'W1', focusedCol: 0, color: '#4aa3ff' },
                            { id: 'W2', focusedCol: 0 }, { id: 'W3', focusedCol: 0 }] });
    const d0 = w.dots()[0], d1 = w.dots()[1];
    ok(!!d0 && d0.classList.contains('tinted') && d0.style.getPropertyValue('--ws-color') === '#4aa3ff'
        && d0.textContent === '1' && d0.classList.contains('active'),
        'TN1 a coloured workspace\'s dot is tinted with its colour and keeps its number');
    ok(!!d1 && !d1.classList.contains('tinted') && d1.style.getPropertyValue('--ws-color') === '',
        'TN2 an uncoloured dot carries no tint');
}
"""

_SCENARIOS += r"""
async function moving() {
    // ---- MV / KM: moving workspaces (driven through the two move actions) --
    function ids(w) { return w.st().list.map(function (x) { return x.id; }).join(','); }
    function key(w, id) { const k = w.keys || {}; return k[id] || null; }
    function press(w, id) { const a = key(w, id); if (a) a.run(); }
    function press(w, id) { const a = key(w, id); if (a) a.run(); }
    function seed(active) {
        return { activeId: active, stacks: [{ id: 'S1' }], list: [
            { id: 'W1', focusedCol: 0 }, { id: 'W2', focusedCol: 1, stack: 'S1' },
            { id: 'W3', focusedCol: 2, stack: 'S1' }, { id: 'W4', focusedCol: 3 },
            { id: 'W5', focusedCol: 4 }] };
    }
    let w = buildWorld({ activeId: 'W1' });
    ok(!!key(w, 'workspace-move-left') && !!key(w, 'workspace-move-right')
        && key(w, 'workspace-move-left').label === 'Move workspace left',
        'KM1 the two move actions are registered');
    let s0 = w.spies.save;
    press(w, 'workspace-move-right');
    ok(ids(w) === 'W2,W1,W3' && w.st().activeId === 'W1' && w.spies.save === s0 + 1
        && w.dots()[1] && w.dots()[1].classList.contains('active'),
        'MV1 move right swaps with the next workspace, stays current, saves once and repaints');

    w = buildWorld(seed('W4'));
    press(w, 'workspace-move-left');
    ok(ids(w) === 'W1,W4,W2,W3,W5', 'MV2 a plain workspace jumps over a whole stack');

    w = buildWorld(seed('W2'));
    press(w, 'workspace-move-right');
    const l = w.st().list;
    ok(ids(w) === 'W1,W3,W2,W4,W5' && l[1].stack === 'S1' && l[2].stack === 'S1',
        'MV4 a stack member moves inside its stack');

    w = buildWorld(seed('W3'));
    s0 = w.spies.save;
    press(w, 'workspace-move-right');                  // last member: no further
    const at1 = ids(w), sv1 = (w.spies.save === s0);
    w = buildWorld(seed('W1'));
    const s1 = w.spies.save;
    press(w, 'workspace-move-left');                   // first entry
    ok(sv1 && at1 === 'W1,W2,W3,W4,W5' && ids(w) === 'W1,W2,W3,W4,W5' && w.spies.save === s1,
        'MV5 a move past either end, or out of a stack, does nothing and saves nothing');

    // MV8: each workspace keeps its own focus memory through moves.
    w = buildWorld(seed('W4'));
    press(w, 'workspace-move-left');
    press(w, 'workspace-move-left');
    const fc = w.st().list.map(function (x) { return x.id + ':' + x.focusedCol; }).join(',');
    ok(fc === 'W4:3,W1:0,W2:1,W3:2,W5:4' && w.st().activeId === 'W4',
        'MV8 a moved workspace carries its own remembered focus');
}
"""

_SCENARIOS += r"""
async function wsMenu() {
    // ---- MN: the pager-dot menu, built for an id --------------------------
    function ids(w) { return w.st().list.map(function (x) { return x.id; }).join(','); }
    function openMenu(w, wsId) {
        const d = w.dots().filter(function (x) { return x.dataset.wsId === wsId; })[0];
        if (!d) return null;
        fire(d, 'contextmenu', { clientX: 10, clientY: 20 });
        return w.lastMenu();
    }
    function act(it) { if (it && it.enabled && it.action) return it.action(); }
    let w = buildWorld();
    let m = openMenu(w, 'W2');
    const lbls = m ? m.items.filter(function (it) { return !it.sep; }).map(function (it) { return it.label.trim(); }) : [];
    ok(!!m && lbls[0] === 'Workspace 2' && lbls.indexOf('Rename…') > 0
        && lbls.indexOf('Remove workspace') > 0 && lbls.indexOf('Move left') > 0
        && lbls.indexOf('Move right') > 0 && lbls.indexOf('New workspace') > 0,
        'MN1 a dot\'s menu names its workspace and offers rename, remove, move left/right and new');
    const mFirst = openMenu(w, 'W1'), mLast = openMenu(w, 'W3');
    ok(!!mFirst && !!mLast && w.item(mFirst, /^Move left$/) && !w.item(mFirst, /^Move left$/).enabled
        && w.item(mFirst, /^Move right$/).enabled && !w.item(mLast, /^Move right$/).enabled
        && w.item(mLast, /^Move left$/).enabled,
        'MN2 Move left is off on the first entry and Move right on the last');
    m = openMenu(w, 'W2');
    act(w.item(m, /^Move right$/));
    ok(!!m && ids(w) === 'W1,W3,W2', 'MN3 Move right moves the workspace the menu was opened on');

    // The menu sits open while a peer's adopt reorders the list.
    w = buildWorld();
    m = openMenu(w, 'W1');
    const l = w.st().list; const t = l[0]; l[0] = l[1]; l[1] = t;    // W2,W1,W3
    act(w.item(m, /^Move right$/));
    const afterMove = ids(w);
    w.promptAnswer = 'renamed';
    await act(w.item(m, /^Rename/));
    ok(!!m && afterMove === 'W2,W3,W1'
        && w.st().list.filter(function (x) { return x.id === 'W1'; })[0].name === 'renamed',
        'MN4 an open menu acts on its workspace by id after a peer reorders the list');
}
"""

_SCENARIOS += r"""
async function dragging() {
    // ---- DR: drag a pager entry to reorder ---------------------------------
    const T = 'application/x-browserland-ws';
    function ids(w) { return w.st().list.map(function (x) { return x.id; }).join(','); }
    function dot(w, id) { return w.dots().filter(function (d) { return d.dataset.wsId === id; })[0] || null; }
    function place(w) { w.dots().forEach(function (d, i) { d.rect = { left: 100 * i, top: 0, right: 100 * i + 40, bottom: 26, width: 40, height: 26 }; }); }
    // start on `from`, optionally let `between` run (a poll), then hover + drop on `to` at clientX
    function drag(w, from, to, x, between) {
        place(w);
        const dt = mkDT();
        const s = dot(w, from), t = dot(w, to);
        if (!s || !t) return false;
        fire(s, 'dragstart', { dataTransfer: dt });
        if (between) between();
        fire(t, 'dragover', { dataTransfer: dt, clientX: x });
        fire(t, 'drop', { dataTransfer: dt, clientX: x });
        fire(s, 'dragend', { dataTransfer: dt });
        return true;
    }
    let w = buildWorld();
    let d1 = drag(w, 'W1', 'W3', 230);                    // W3 spans 200..240: right half
    ok(d1 && ids(w) === 'W2,W3,W1' && w.st().activeId === 'W1',
        'DR1 dropping on the right half of an entry puts the dragged one after it');
    w = buildWorld();
    d1 = drag(w, 'W1', 'W3', 205);
    ok(d1 && ids(w) === 'W2,W1,W3', 'DR2 dropping on the left half puts it before');

    w = buildWorld();
    d1 = drag(w, 'W1', 'W3', 230, function () {          // a peer reorders mid-drag
        const l = w.st().list; const t = l[0]; l[0] = l[1]; l[1] = t; });
    ok(d1 && ids(w) === 'W2,W3,W1', 'DR4 a reorder during the drag still moves the dragged workspace');

    w = buildWorld();
    let s0 = w.spies.save;
    d1 = drag(w, 'W1', 'W3', 230, function () { w.st().list.splice(0, 1); });
    ok(d1 && ids(w) === 'W2,W3' && w.spies.save === s0,
        'DR5 a workspace removed during the drag is not moved and nothing is saved');

    w = buildWorld({ list: [{ id: 'W1', focusedCol: 0 }, { id: 'W2', focusedCol: 0 },
                            { id: 'W3', focusedCol: 0 }, { id: 'W4', focusedCol: 0 }] });
    s0 = w.spies.save;
    d1 = drag(w, 'W1', 'W4', 330, function () {
        w.st().stacks = [{ id: 'S1' }];
        w.st().list[0].stack = 'S1'; w.st().list[1].stack = 'S1'; });
    ok(d1 && ids(w) === 'W1,W2,W3,W4' && w.spies.save === s0,
        'DR6 a workspace stacked during the drag is not moved on its own');

    // DR7: hovering a CHILD of the dot uses the dot's rect; foreign drags are ignored.
    w = buildWorld();
    place(w);
    const tgt = dot(w, 'W3'), src = dot(w, 'W1');
    const span = tgt ? tgt.appendChild(new Node('span')) : null;
    if (span) span.rect = { left: 220, top: 0, right: 230, bottom: 26, width: 10, height: 26 };
    const dt = mkDT();
    if (src) fire(src, 'dragstart', { dataTransfer: dt });
    const ev = span ? fire(span, 'dragover', { dataTransfer: dt, clientX: 224 }) : null;
    const afterCls = !!tgt && tgt.classList.contains('drop-after');
    if (span) fire(tgt, 'dragleave', { dataTransfer: dt, relatedTarget: span });
    const kept = !!tgt && tgt.classList.contains('drop-after');
    const os = mkDT(); os.setData('Files', 'x');
    const ev2 = tgt ? fire(tgt, 'dragover', { dataTransfer: os, clientX: 230 }) : null;
    ok(!!span && ev && ev.defaultPrevented && afterCls && kept && ev2 && !ev2.defaultPrevented,
        'DR7 the drop side comes from the dot under a child span, and a foreign drag is ignored');
}
"""

_SCENARIOS += r"""
async function colours() {
    // ---- CL: a workspace's colour, from the palette -------------------------
    function openMenu(w, wsId) {
        const d = w.dots().filter(function (x) { return x.dataset.wsId === wsId; })[0];
        if (!d) return null;
        fire(d, 'contextmenu', { clientX: 10, clientY: 20 });
        return w.lastMenu();
    }
    function act(it) { if (it && it.enabled && it.action) return it.action(); }
    let w = buildWorld();
    let m = openMenu(w, 'W2');
    act(w.item(m, /^Colour…$/));
    let pal = w.lastMenu();
    const rows = (pal && pal !== m) ? pal.items : [];
    const swatches = rows.filter(function (it) { return it.swatch; });
    ok(!!m && rows.length === PALETTE.length + 1 && /^✓ None$/.test(rows[0].label)
        && !rows[0].swatch && J(swatches.map(function (it) { return it.swatch.color; })) === J(PALETTE)
        && swatches.every(function (it) { return it.label.trim() === it.swatch.color; }),
        'CL1 Colour… opens None plus one labelled swatch row per palette colour, None checked');

    const s0 = w.spies.save;
    act(w.item(pal, new RegExp(PALETTE[2])));
    const d2 = w.dots().filter(function (x) { return x.dataset.wsId === 'W2'; })[0];
    ok(w.st().list[1].color === PALETTE[2] && w.spies.save === s0 + 1 && !!d2
        && d2.classList.contains('tinted'),
        'CL2 picking a colour stores it, saves once and tints the dot');

    m = openMenu(w, 'W2');
    act(w.item(m, /^Colour…$/));
    pal = w.lastMenu();
    const checked = pal ? pal.items.filter(function (it) { return /^✓ /.test(it.label); }) : [];
    ok(checked.length === 1 && checked[0].label === '✓ ' + PALETTE[2],
        'CL4 the palette menu checks the current colour');
    act(w.item(pal, /None$/));
    ok(!('color' in w.st().list[1]), 'CL3 None removes the colour');
}
"""

_SCENARIOS += r"""
async function stacking() {
    // ---- SK / SM: Stack with… and Remove from stack ------------------------
    function ids(w) {
        return w.st().list.map(function (x) { return x.id + (x.stack ? '@' + x.stack : ''); }).join(',');
    }
    function act(it) { if (it && it.enabled && it.action) return it.action(); }
    function stackVia(w, wsId, re) {
        const m = w.openWsMenu(wsId);
        act(w.item(m, /^Stack with…$/));
        const tm = w.lastMenu();
        if (!m || tm === m) return null;
        act(w.item(tm, re));
        return tm;
    }
    function four(extra) {
        return Object.assign({ list: [
            { id: 'W1', focusedCol: 0 }, { id: 'W2', focusedCol: 0 },
            { id: 'W3', focusedCol: 0 }, { id: 'W4', focusedCol: 0 }] }, extra || {});
    }
    let w = buildWorld(four());
    const tm = stackVia(w, 'W1', /^Workspace 3$/);
    const sid = w.st().list[1].stack;
    ok(!!tm && J(tm.items.map(function (it) { return it.label; })) === J(['Workspace 2', 'Workspace 3', 'Workspace 4'])
        && typeof sid === 'string' && ['L1', 'L2', 'L3', 'W1', 'W2', 'W3', 'W4'].indexOf(sid) === -1
        && ids(w) === 'W2,W3@' + sid + ',W1@' + sid + ',W4'
        && w.st().stacks.length === 1 && w.st().stacks[0].id === sid,
        'SK1 Stack with a plain workspace forms a new stack at the target, the chosen one after it');

    w = buildWorld(four({ stacks: [{ id: 'S1', name: 'dev' }], list: [
        { id: 'W1', focusedCol: 0 }, { id: 'W2', focusedCol: 0, stack: 'S1' },
        { id: 'W3', focusedCol: 0, stack: 'S1' }, { id: 'W4', focusedCol: 0 }] }));
    const tm2 = stackVia(w, 'W4', /^Stack dev \(2\)$/);
    ok(!!tm2 && ids(w) === 'W1,W2@S1,W3@S1,W4@S1',
        'SK2 Stack with a stack joins it at the end of its block');

    w = buildWorld(four({ stacks: [{ id: 'S1' }, { id: 'S2' }], list: [
        { id: 'W1', focusedCol: 0, stack: 'S1' }, { id: 'W2', focusedCol: 0, stack: 'S1' },
        { id: 'W3', focusedCol: 0, stack: 'S2' }, { id: 'W4', focusedCol: 0, stack: 'S2' }] }));
    const tm3 = stackVia(w, 'W1', /^Stack 2 \(2\)$/);
    ok(!!tm3 && ids(w) === 'W2,W3@S2,W4@S2,W1@S2' && J(w.st().stacks) === J([{ id: 'S2' }])
        && J(JSON.parse(w.spies.saved || '{}').stacks) === J([{ id: 'S2' }]),
        'SK3 a workspace leaving a two-member stack for another dissolves the one it left, before the save');

    w = buildWorld(four({ stacks: [{ id: 'S1' }], list: [
        { id: 'W1', focusedCol: 0, stack: 'S1' }, { id: 'W2', focusedCol: 0, stack: 'S1' },
        { id: 'W3', focusedCol: 0, stack: 'S1' }, { id: 'W4', focusedCol: 0 }] }));
    let m = w.openWsMenu('W1');
    act(w.item(m, /^Remove from stack$/));
    ok(!!m && ids(w) === 'W2@S1,W3@S1,W1,W4',
        'SK4 Remove from stack puts the first of three just after the stack');

    w = buildWorld(four({ stacks: [{ id: 'S1' }], list: [
        { id: 'W1', focusedCol: 0 }, { id: 'W2', focusedCol: 0, stack: 'S1' },
        { id: 'W3', focusedCol: 0, stack: 'S1' }, { id: 'W4', focusedCol: 0 }] }));
    const s0 = w.spies.save;
    m = w.openWsMenu('W2');
    act(w.item(m, /^Remove from stack$/));
    ok(!!m && ids(w) === 'W1,W3,W2,W4' && J(w.st().stacks) === J([]) && w.spies.save > s0
        && J(JSON.parse(w.spies.saved || '{}').stacks) === J([]),
        'SK5 removing one of two dissolves the stack before it saves');

    w = buildWorld(four());
    m = w.openWsMenu('W2');
    ok(!!m && !!w.item(m, /^Stack with…$/) && !w.item(m, /^Remove from stack$/),
        'SM2 Remove from stack is offered only to a stacked workspace');
}
"""

_SCENARIOS += r"""
async function stackMembers() {
    // ---- SK8: last-used member; RS: removing a stack member -----------------
    function ids(w) {
        return w.st().list.map(function (x) { return x.id + (x.stack ? '@' + x.stack : ''); }).join(',');
    }
    function act(it) { if (it && it.enabled && it.action) return it.action(); }
    function seed(stacked, active) {
        return { activeId: active || 'W1', stacks: [{ id: 'S1' }], list: ['W1', 'W2', 'W3', 'W4'].map(function (id) {
            const o = { id: id, focusedCol: 0 };
            if (stacked.indexOf(id) !== -1) o.stack = 'S1';
            return o; }) };
    }
    let w = buildWorld(seed(['W2', 'W3']));
    const rows = w.desktopMenu ? w.desktopMenu({ tiling: true }) : [];
    act(rows.filter(function (it) { return /Workspace 2\.2/.test(it.label || ''); })[0]);
    const saved = JSON.parse(w.spies.saved || '{}');
    const plain = rows.filter(function (it) { return /Workspace 3 /.test(it.label || ''); })[0];
    act(plain);
    ok(saved.activeId === 'W3' && saved.stacks && saved.stacks[0].lastId === 'W3'
        && w.st().activeId === 'W4' && w.st().stacks[0].lastId === 'W3',
        'SK8 switching to a stack member records it as the stack\'s last-used member, in the same save');

    // RS: a member removed from the first / middle / last place.
    w = buildWorld(seed(['W1', 'W2', 'W3'], 'W1'));
    let m = w.openWsMenu('W1');
    await act(w.item(m, /^Remove workspace$/));
    ok(!!m && ids(w) === 'W2@S1,W3@S1,W4' && w.st().activeId === 'W2'
        && w.L.columns[0].wsId === 'W2',
        'RS1 removing the first of three members moves its windows to the next one and keeps the stack');
    w = buildWorld(seed(['W2', 'W3', 'W4'], 'W1'));
    m = w.openWsMenu('W3');
    await act(w.item(m, /^Remove workspace$/));
    ok(!!m && ids(w) === 'W1,W2@S1,W4@S1' && w.L.columns[2].wsId === 'W2',
        'RS2 removing a middle member moves its windows to the member before it');
    w = buildWorld(seed(['W2', 'W3'], 'W1'));          // a member that is NOT current
    m = w.openWsMenu('W3');
    await act(w.item(m, /^Remove workspace$/));
    const sv = JSON.parse(w.spies.saved || '{}');
    ok(!!m && ids(w) === 'W1,W2,W4' && w.st().activeId === 'W1'
        && J(sv.stacks) === J([]) && sv.list && sv.list[1].stack === undefined,
        'RS3 removing one of two members dissolves the stack in the saved state');
}
"""

_SCENARIOS += r"""
async function stackEntry() {
    // ---- SE / TN3 / SG4-SG7 / SL1-SL4: the stack's pager entry and its list --
    function seed(active, extra) {
        return Object.assign({ activeId: active || 'W1',
            stacks: [{ id: 'S1', name: 'dev', color: '#5fbf7f' }], list: [
            { id: 'W1', focusedCol: 0 }, { id: 'W2', focusedCol: 0, stack: 'S1', color: '#e96d6d' },
            { id: 'W3', focusedCol: 0, stack: 'S1', name: 'api' }, { id: 'W4', focusedCol: 0 }] }, extra || {});
    }
    function own(d) { return d ? d._text : ''; }
    let w = buildWorld(seed());
    let dots = w.dots();
    let sd = w.stackDot('S1');
    const cnt = sd ? sd.querySelector('.ws-count') : null;
    ok(dots.length === 3 && !!sd && dots[1] === sd && own(sd) === '2' && !!cnt && cnt.textContent === '2'
        && dots[2].dataset.wsId === 'W4' && dots[2].textContent === '3',
        'SE1 a stack is ONE pager entry with its number and member count');
    ok(!!sd && /stack 2 of 2 workspaces/.test(sd.title) && /^dev — /.test(sd.title)
        && !sd.classList.contains('active'),
        'SE3 the stack entry\'s tooltip names the stack and its size');
    ok(!!sd && sd.classList.contains('tinted') && sd.style.getPropertyValue('--ws-color') === '#5fbf7f',
        'TN3 the stack entry is tinted with the STACK\'s colour, not a member\'s');

    w = buildWorld(seed('W3'));
    sd = w.stackDot('S1');
    ok(!!sd && sd.classList.contains('active') && own(sd) === '2.2',
        'SE2 when you are on a member the stack entry is current and names it N.k');
    w.labelMode = 'name'; w.render();
    sd = w.stackDot('S1');
    ok(!!sd && own(sd) === 'dev › api', 'SE2 in names mode it reads stack › member');

    // SG4-SG7: each stack-level field rebuilds the entry with its visible effect.
    w = buildWorld(seed('W1', { labelMode: 'name' }));
    let before = w.stackDot('S1');
    w.st().stacks[0].name = 'ops'; w.render();
    let after = w.stackDot('S1');
    ok(!!before && own(before) === 'dev' && !!after && after !== before && own(after) === 'ops',
        'SG4 renaming a stack rebuilds its entry with the new name');
    w = buildWorld(seed());
    before = w.stackDot('S1');
    w.st().stacks[0].color = '#b78bf0'; w.render();
    after = w.stackDot('S1');
    ok(!!before && !!after && after !== before && after.style.getPropertyValue('--ws-color') === '#b78bf0',
        'SG5 recolouring a stack rebuilds its entry with the new tint');
    w = buildWorld(seed('W2'));
    before = w.stackDot('S1');
    w.st().activeId = 'W3'; w.render();                 // an adopt moved you to the other member
    after = w.stackDot('S1');
    ok(!!before && own(before) === '2.1' && !!after && own(after) === '2.2',
        'SG7 a switch between members of one stack repaints which member is current');

}
async function stackList() {
    // ---- SL1-SL4: a stack's list --------------------------------------------
    function seed(active) {
        return { activeId: active || 'W1',
            stacks: [{ id: 'S1', name: 'dev', color: '#5fbf7f' }], list: [
            { id: 'W1', focusedCol: 0 }, { id: 'W2', focusedCol: 0, stack: 'S1', color: '#e96d6d' },
            { id: 'W3', focusedCol: 0, stack: 'S1', name: 'api' }, { id: 'W4', focusedCol: 0 }] };
    }
    let w, sd;
    w = buildWorld(seed('W3'));
    sd = w.stackDot('S1');
    let pop = w.openList('S1');
    const rows = pop ? pop.node.querySelectorAll('.wsl-row') : [];
    ok(!!pop && pop.anchor === sd && rows.length === 2 && rows[0].dataset.wsId === 'W2'
        && rows[0].tagName === 'BUTTON' && /2\.1$/.test(rows[0].textContent)
        && rows[1].textContent === '✓ 2.2  api' && rows[1].classList.contains('current')
        && w.pops.filter(function (p) { return p.open; }).length === 1,
        'SL1 clicking a stack opens one list of its members, anchored to it, the current one checked');
    const sw = rows[0] ? rows[0].querySelector('.wsl-swatch') : null;
    ok(!!sw && sw.style.background === '#e96d6d' && sw.getAttribute('aria-hidden') === 'true',
        'SL1 each row carries its member\'s colour as a decorative swatch');
    if (rows[0]) fire(rows[0], 'click');
    ok(!!pop && w.st().activeId === 'W2' && !pop.open, 'SL2 choosing a row switches to it and closes the list');

    w = buildWorld(seed('W1'));
    pop = w.openList('S1');
    const r3 = w.row(pop, 'W3');
    const l = w.st().list; const t = l[1]; l[1] = l[2]; l[2] = t;     // a peer reorders the stack
    if (r3) fire(r3, 'click');
    ok(!!r3 && w.st().activeId === 'W3', 'SL3 a row switches to ITS workspace after a peer reorders the stack');

    w = buildWorld(seed('W1'));
    pop = w.openList('S1');
    const r2 = w.row(pop, 'W2');
    const n0 = w.menus.length;
    if (r2) fire(r2, 'contextmenu', { clientX: 5, clientY: 6 });
    const mm = w.menus.length > n0 ? w.lastMenu() : null;
    ok(!!r2 && !!mm && /^Workspace 2\.1/.test(mm.items[0].label) && !!w.item(mm, /^Move down$/)
        && !!w.item(mm, /^Remove from stack$/) && !pop.open,
        'SL4 right-clicking a row closes the list and opens that member\'s menu');
}
"""

_SCENARIOS += r"""
async function stackDrag() {
    // ---- DR3 / MV3: drags that involve a whole stack ----------------------
    function ids(w) { return w.st().list.map(function (x) { return x.id; }).join(','); }
    function seed() {
        return { stacks: [{ id: 'S1' }], list: [
            { id: 'W1', focusedCol: 0 }, { id: 'W2', focusedCol: 0, stack: 'S1' },
            { id: 'W3', focusedCol: 0, stack: 'S1' }, { id: 'W4', focusedCol: 0 }] };
    }
    function place(w) { w.dots().forEach(function (d, i) { d.rect = { left: 100 * i, top: 0, right: 100 * i + 40, bottom: 26, width: 40, height: 26 }; }); }
    function drag(w, s, t, x) {
        place(w);
        if (!s || !t) return false;
        const dt = mkDT();
        fire(s, 'dragstart', { dataTransfer: dt });
        fire(t, 'dragover', { dataTransfer: dt, clientX: x });
        fire(t, 'drop', { dataTransfer: dt, clientX: x });
        fire(s, 'dragend', { dataTransfer: dt });
        return true;
    }
    function dot(w, id) { return w.dots().filter(function (d) { return d.dataset.wsId === id; })[0] || null; }
    let w = buildWorld(seed());
    let d = drag(w, dot(w, 'W4'), w.stackDot('S1'), 105);   // the stack dot spans 100..140
    ok(d && ids(w) === 'W1,W4,W2,W3' && w.st().list[2].stack === 'S1',
        'DR3 dropping on a stack lands beside the whole stack, never inside it');
    w = buildWorld(seed());
    d = drag(w, w.stackDot('S1'), dot(w, 'W1'), 5);
    const saved = JSON.parse(w.spies.saved || '{}').list || [];
    ok(d && ids(w) === 'W2,W3,W1,W4'
        && saved.map(function (x) { return x.id; }).join(',') === 'W2,W3,W1,W4',
        'MV3 dragging a stack moves all its members as one block');
}
"""

_EPILOGUE = r"""
(async function () {
    await baseline();
    await heal();
    await minting();
    await pagerNumbers();
    await signature();
    await moving();
    await wsMenu();
    await dragging();
    await colours();
    await stacking();
    await stackMembers();
    await stackList();
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


def test_heal_stack_contiguity(scenario_run):
    _ok(scenario_run, "H3 interleaved stack members are regrouped where each stack first appears, order kept")


def test_heal_dissolves_single_stacks(scenario_run):
    _ok(scenario_run, "H4 a stack left with one workspace dissolves (row and membership)")


def test_heal_last_used_member(scenario_run):
    _ok(scenario_run, "H6 a lastId naming a workspace outside its stack is dropped")


def test_heal_is_canonical_and_idempotent(scenario_run):
    _ok(scenario_run, "H7 a healed store heals to itself: a second pass changes nothing and saves nothing")


def test_mint_never_reuses_a_workspace_id(scenario_run):
    _ok(scenario_run, "ID precondition: 57's next mint is the stored workspace id L4")
    _ok(scenario_run, "ID1 + adds a workspace with a fresh id that survives the heal, and switches to it")
    _ok(scenario_run, "ID2 Send to new workspace mints a fresh id and the window's column moves onto it")


def test_pager_numbers_count_entries(scenario_run):
    _ok(scenario_run, "PM1 Send to names stack members N.k and the workspace after the stack by its entry number")
    _ok(scenario_run, "PM2 the desktop menu numbers rows by pager entry, members as N.k")
    _ok(scenario_run, "PM3 the taskbar badge names a stack member N.k")
    _ok(scenario_run, "PM4 the workspace after a stack is entry 3 on its dot, tooltip and preview")


def test_pager_signature_covers_every_visible_field(scenario_run):
    _ok(scenario_run, "SG1 a reorder rebuilds the dots, each carrying its moved workspace")
    _ok(scenario_run, "SG2 a colour change rebuilds the dot with its tint, label kept")
    _ok(scenario_run, "SG3 joining a stack rebuilds the pager and relabels the member")


def test_tint(scenario_run):
    _ok(scenario_run, "TN1 a coloured workspace's dot is tinted with its colour and keeps its number")
    _ok(scenario_run, "TN2 an uncoloured dot carries no tint")


def test_move(scenario_run):
    _ok(scenario_run, "KM1 the two move actions are registered")
    _ok(scenario_run, "MV1 move right swaps with the next workspace, stays current, saves once and repaints")
    _ok(scenario_run, "MV2 a plain workspace jumps over a whole stack")
    _ok(scenario_run, "MV4 a stack member moves inside its stack")
    _ok(scenario_run, "MV5 a move past either end, or out of a stack, does nothing and saves nothing")
    _ok(scenario_run, "MV8 a moved workspace carries its own remembered focus")


def test_move_actions_are_unbound_by_default():
    # KM2: new ids, no default combo -- the user binds them (DQ1).
    store = (BROKER_DIR / "54_js_app_windows_store.js").read_text(encoding="utf-8")
    ws = SRC_JS.read_text(encoding="utf-8")
    for aid in ("workspace-move-left", "workspace-move-right"):
        assert f"id: '{aid}'" in ws, f"{aid} is not registered by the mod"
        assert f"'{aid}'" not in store, f"{aid} must stay unbound by default"


def test_workspace_menu(scenario_run):
    _ok(scenario_run, "MN1 a dot's menu names its workspace and offers rename, remove, move left/right and new")
    _ok(scenario_run, "MN2 Move left is off on the first entry and Move right on the last")
    _ok(scenario_run, "MN3 Move right moves the workspace the menu was opened on")
    _ok(scenario_run, "MN4 an open menu acts on its workspace by id after a peer reorders the list")


def test_pager_drag(scenario_run):
    _ok(scenario_run, "DR1 dropping on the right half of an entry puts the dragged one after it")
    _ok(scenario_run, "DR2 dropping on the left half puts it before")
    _ok(scenario_run, "DR4 a reorder during the drag still moves the dragged workspace")
    _ok(scenario_run, "DR5 a workspace removed during the drag is not moved and nothing is saved")
    _ok(scenario_run, "DR6 a workspace stacked during the drag is not moved on its own")
    _ok(scenario_run, "DR7 the drop side comes from the dot under a child span, and a foreign drag is ignored")


def test_colour(scenario_run):
    _ok(scenario_run, "CL1 Colour… opens None plus one labelled swatch row per palette colour, None checked")
    _ok(scenario_run, "CL2 picking a colour stores it, saves once and tints the dot")
    _ok(scenario_run, "CL4 the palette menu checks the current colour")
    _ok(scenario_run, "CL3 None removes the colour")


def test_stack_with_and_remove_from_stack(scenario_run):
    _ok(scenario_run, "SK1 Stack with a plain workspace forms a new stack at the target, the chosen one after it")
    _ok(scenario_run, "SK2 Stack with a stack joins it at the end of its block")
    _ok(scenario_run, "SK3 a workspace leaving a two-member stack for another dissolves the one it left, before the save")
    _ok(scenario_run, "SK4 Remove from stack puts the first of three just after the stack")
    _ok(scenario_run, "SK5 removing one of two dissolves the stack before it saves")
    _ok(scenario_run, "SM2 Remove from stack is offered only to a stacked workspace")


def test_last_used_member(scenario_run):
    _ok(scenario_run, "SK8 switching to a stack member records it as the stack's last-used member, in the same save")


def test_remove_a_stack_member(scenario_run):
    _ok(scenario_run, "RS1 removing the first of three members moves its windows to the next one and keeps the stack")
    _ok(scenario_run, "RS2 removing a middle member moves its windows to the member before it")
    _ok(scenario_run, "RS3 removing one of two members dissolves the stack in the saved state")


def test_stack_list(scenario_run):
    _ok(scenario_run, "SL1 clicking a stack opens one list of its members, anchored to it, the current one checked")
    _ok(scenario_run, "SL1 each row carries its member's colour as a decorative swatch")
    _ok(scenario_run, "SL2 choosing a row switches to it and closes the list")
    _ok(scenario_run, "SL3 a row switches to ITS workspace after a peer reorders the stack")
    _ok(scenario_run, "SL4 right-clicking a row closes the list and opens that member's menu")


def test_every_scenario_passed_and_node_exited_clean(scenario_run):
    run = scenario_run
    assert run["failed"] == 0, (
        f"harness reported failures\nstdout: {run['stdout']}\nstderr: {run['stderr']}")
    assert run["passed"] == EXPECTED_ASSERTS, (
        f"expected {EXPECTED_ASSERTS} asserts, harness ran {run['passed']} "
        f"-- scenario drift\nstdout: {run['stdout']}")
    assert len(run["results"]) == EXPECTED_ASSERTS
    assert run["rc"] == 0, f"node exited {run['rc']}\nstderr: {run['stderr']}"
