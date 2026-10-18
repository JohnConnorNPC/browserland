"""#259: two 1/2 columns must fit the strip exactly, so moving focus between
them never scrolls it.

``relayoutStrip`` used to round each column on its own (``Math.round(frac *
availW)``). With an odd available width both halves are ``k + .5`` and both
round up, so the columns ended 1px past ``strip.clientWidth``. Then
``scrollColumnIntoView`` revealed the right column (scrollLeft 0 -> 1) and the
left one (1 -> 0) on every focus change.

These tests run the SHIPPED ``columnPixelWidths`` helper from
61_js_resize_gutters.js in node, with the real ``presetFraction`` from
57_js_tiling_model.js. Skipped when node is absent.
"""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from webterm.broker import ui

BROKER_DIR = Path(ui.__file__).resolve().parent
SRC_61 = BROKER_DIR / "61_js_resize_gutters.js"
SRC_57 = BROKER_DIR / "57_js_tiling_model.js"
CSS_13 = BROKER_DIR / "13_css_tiling.css"
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="node not installed")

HELPER_START = "        function colCurrentFrac(col) {"
HELPER_END = "        // Live-render predicate SHARED"
PRESET_START = "        function presetFraction(p) {"

_HARNESS = r"""
'use strict';
const fs = require('fs');
const S61 = fs.readFileSync(__SRC61__, 'utf8');
const S57 = fs.readFileSync(__SRC57__, 'utf8');
function slice(s, a, b) {
    const i = s.indexOf(a), j = s.indexOf(b, i + 1);
    if (i < 0 || j < i) throw new Error('boundary missing: ' + a);
    return s.slice(i, j);
}
// presetFraction ends at the first 8-space-indented closing brace.
const presetSrc = (function () {
    const i = S57.indexOf(__PSTART__);
    const j = S57.indexOf('\n        }\n', i);
    if (i < 0 || j < i) throw new Error('presetFraction missing');
    return S57.slice(i, j + 10);
})();
let pass = 0, fail = 0;
function ok(cond, label) {
    if (cond) pass++;
    else { fail++; console.log('FAIL: ' + label); }
}

function helperCase() {
    const H = new Function(presetSrc + slice(S61, __HSTART__, __HEND__) +
        '\nreturn { colCurrentFrac, columnPixelWidths, stripAvailWidth,' +
        ' STRIP_GUTTER_W, presetFraction };')();
    const P = H.presetFraction;
    // The oracle accumulates the prefix sum left to right, as the helper
    // does, so a float tie can't round differently on the two sides.
    function prefix(fracs) {
        const out = []; let s = 0;
        for (const f of fracs) { s += f; out.push(s); }
        return out;
    }
    // C1f's oracle: each width is the step between two rounded cumulative
    // edges, the prefix taken in that same order. Exact, so a helper that
    // rounds the edges any other way (floor, ceil, last-edge-only) is red.
    function oracle(fracs, A) {
        let prev = 0;
        return prefix(fracs).map(p => {
            const e = Math.round(A * p), w = e - prev;
            prev = e;
            return w;
        });
    }
    const filling = {
        '2x1/2': ['1/2', '1/2'].map(P),
        '3x1/3': ['1/3', '1/3', '1/3'].map(P),
        '1/3+2/3': ['1/3', '2/3'].map(P),
        '2/3+1/3': ['2/3', '1/3'].map(P),
        '1/2+1/4+1/4': [0.5, 0.25, 0.25],
        '4x1/4': [0.25, 0.25, 0.25, 0.25],
    };
    const nonFilling = {
        '2x1/3': ['1/3', '1/3'].map(P),
        '1/2+1/3': ['1/2', '1/3'].map(P),
        '1x1/2': ['1/2'].map(P),
        '1x1/3': ['1/3'].map(P),
        '1xmax': ['max'].map(P),
    };
    for (let W = 1000; W <= 2560; W++) {
        for (const name in filling) {
            const f = filling[name];
            const w = H.columnPixelWidths(f, W);
            const A = H.stripAvailWidth(W, f.length);
            const sum = w.reduce((a, b) => a + b, 0);
            // C1a: the columns plus their gutters fill the strip exactly.
            ok(sum + (f.length - 1) * H.STRIP_GUTTER_W === W,
               'C1a ' + name + ' W=' + W + ' widths ' + w.join('+') +
               ' + gutters = ' + (sum + (f.length - 1) * H.STRIP_GUTTER_W));
            // C1b: each width within 1px of its exact share.
            w.forEach((wi, i) => ok(Math.abs(wi - f[i] * A) <= 1 + 1e-9,
               'C1b ' + name + ' W=' + W + ' col ' + i + ' width ' + wi +
               ' vs exact ' + (f[i] * A).toFixed(3)));
            ok(w.length === f.length, 'C1a ' + name + ' W=' + W + ' length');
            ok(w.join() === oracle(f, A).join(), 'C1f ' + name + ' W=' + W +
               ' widths ' + w.join('+') + ' == cumulative-round ' + oracle(f, A).join('+'));
        }
        for (const name in nonFilling) {
            const f = nonFilling[name];
            const w = H.columnPixelWidths(f, W);
            const A = H.stripAvailWidth(W, f.length);
            const sum = w.reduce((a, b) => a + b, 0);
            const pre = prefix(f);
            // C1c: a set that doesn't fill sums to round(availW * its total);
            // a lone column is today's round(frac * stripW).
            ok(sum === Math.round(A * pre[pre.length - 1]),
               'C1c ' + name + ' W=' + W + ' sum ' + sum);
            if (f.length === 1) {
                ok(w[0] === Math.round(f[0] * W),
                   'C1c ' + name + ' W=' + W + ' lone width ' + w[0]);
            }
            w.forEach((wi, i) => ok(Math.abs(wi - f[i] * A) <= 1 + 1e-9,
               'C1b ' + name + ' W=' + W + ' col ' + i + ' width ' + wi));
            ok(w.join() === oracle(f, A).join(), 'C1f ' + name + ' W=' + W +
               ' widths ' + w.join('+') + ' == cumulative-round ' + oracle(f, A).join('+'));
        }
    }
    // C1d: only a finite positive widthFrac overrides the preset.
    for (const bad of [Infinity, -Infinity, NaN, -1, 0, '0.4', null, undefined]) {
        ok(H.colCurrentFrac({ widthFrac: bad, widthPreset: '1/3' }) === 1 / 3,
           'C1d widthFrac ' + String(bad) + ' falls back to the preset');
    }
    ok(H.colCurrentFrac({ widthFrac: 0.4, widthPreset: '1/3' }) === 0.4,
       'C1d widthFrac 0.4 overrides the preset');
    ok(H.colCurrentFrac({ widthPreset: '2/3' }) === 2 / 3,
       'C1d no widthFrac uses the preset');
}

// ---- the whole 61 fragment in a sandbox ---------------------------------
// Every identifier the fragment reads from outside itself comes from ENV
// below. The `with` scope is a Proxy that THROWS on any other name, so a
// missing stub can't read as undefined and let a check pass vacuously.
// Built-ins the fragment uses are passed through by name.
const BUILTINS = ['Math', 'Array', 'Map', 'Set', 'Number', 'String', 'Object',
    'JSON', 'Error', 'console', 'undefined', 'NaN', 'Infinity', 'isFinite'];
const GUTTER_CSS_W = 6;     // .strip-col-gutter { flex: 0 0 6px } (C1e pins it)

class FakeEl {
    constructor(tag) {
        this.tagName = tag; this.children = []; this.parentElement = null;
        this.dataset = {}; this.style = {}; this._cls = new Set();
        this.listeners = {};
        const cls = this._cls;
        this.classList = {
            add: c => cls.add(c), remove: c => cls.delete(c),
            contains: c => cls.has(c),
            toggle: (c, f) => {
                const on = f === undefined ? !cls.has(c) : !!f;
                if (on) cls.add(c); else cls.delete(c);
                return on;
            },
        };
    }
    set className(v) {
        this._cls.clear();
        String(v).split(/\s+/).filter(Boolean).forEach(c => this._cls.add(c));
    }
    get className() { return Array.from(this._cls).join(' '); }
    get firstChild() { return this.children[0] || null; }
    get nextSibling() {
        const p = this.parentElement;
        return p ? (p.children[p.children.indexOf(this) + 1] || null) : null;
    }
    get isConnected() {
        let e = this;
        while (e.parentElement) e = e.parentElement;
        return e.isRoot === true;
    }
    insertBefore(el, ref) {
        if (el === ref) return el;
        if (el.parentElement) el.remove();
        const i = ref ? this.children.indexOf(ref) : -1;
        if (ref && i < 0) throw new Error('insertBefore: ref is not a child');
        if (i < 0) this.children.push(el); else this.children.splice(i, 0, el);
        el.parentElement = this;
        return el;
    }
    appendChild(el) { return this.insertBefore(el, null); }
    remove() {
        const p = this.parentElement;
        if (!p) return;
        p.children.splice(p.children.indexOf(this), 1);
        this.parentElement = null;
    }
    addEventListener(t, fn) { (this.listeners[t] = this.listeners[t] || []).push(fn); }
    contains(node) {
        for (let e = node; e; e = e.parentElement) if (e === this) return true;
        return false;
    }
    querySelector(sel) {
        const m = /^\.strip-col\[data-col-id="(.*)"\]$/.exec(sel);
        if (!m) throw new Error('querySelector: unsupported ' + sel);
        return this.children.find(c => c._cls.has('strip-col')
            && c.dataset.colId === m[1]) || null;
    }
    querySelectorAll(sel) {
        const m = /^(?::scope > )?\.([\w-]+)$/.exec(sel);
        if (!m) throw new Error('querySelectorAll: unsupported ' + sel);
        return this.children.filter(c => c._cls.has(m[1]));
    }
    // Layout: a column is its inline width, a gutter the CSS 6px; offsets
    // run left to right from the strip's padding edge (no padding/margins).
    get offsetWidth() {
        if (this._cls.has('strip-col-gutter')) return GUTTER_CSS_W;
        const w = parseInt(this.style.width, 10);
        return Number.isFinite(w) ? w : 0;
    }
    get offsetLeft() {
        let x = 0;
        for (const c of this.parentElement.children) {
            if (c === this) return x;
            x += c.offsetWidth;
        }
        throw new Error('offsetLeft: detached');
    }
}

// A strip whose real (layout) width is wf: clientWidth is that rounded, and
// scrollLeft clamps like a browser to [0, content - wf]. Every write is
// logged, so "nothing scrolled" is observed, not inferred.
function makeStrip(wf) {
    const strip = new FakeEl('div');
    strip.id = 'strip';
    let sl = 0;
    strip.writes = [];
    strip.contentW = () => strip.children.reduce((a, c) => a + c.offsetWidth, 0);
    Object.defineProperty(strip, 'clientWidth', { get: () => Math.round(wf) });
    Object.defineProperty(strip, 'scrollWidth', {
        get: () => Math.max(Math.round(wf), strip.contentW()) });
    Object.defineProperty(strip, 'scrollLeft', {
        get: () => sl,
        set: v => {
            strip.writes.push(v);
            sl = Math.max(0, Math.min(Math.max(0, strip.contentW() - wf), v));
        },
    });
    return strip;
}

// cols: [{id, preset?, frac?, live}] in layout order. A col with live:false
// has only dead keys, so relayoutStrip leaves it DORMANT (not drawn) while
// visibleColumns() still lists it: visible index != drawn index.
// opts.realLayout loads the WHOLE 57 fragment too, so getLayout (with its
// reconcileLayout), visibleColumns and rowKeys are the shipped ones, reading
// prefs._layout; otherwise getLayout hands the layout back as is (C4 injects
// a widthFrac that reconcile would drop).
function makeSandbox(wf, cols, opts) {
    opts = opts || {};
    const root = new FakeEl('html'); root.isRoot = true;
    const body = new FakeEl('body'); root.appendChild(body);
    const desktop = new FakeEl('div'); desktop.id = 'desktop';
    Object.defineProperty(desktop, 'clientWidth', { get: () => Math.round(wf) });
    body.appendChild(desktop);
    const strip = makeStrip(wf);
    desktop.appendChild(strip);
    const docListeners = {};
    const document = {
        body,
        activeElement: null,
        getElementById: id => (id === 'strip' ? strip : id === 'desktop' ? desktop : null),
        createElement: tag => new FakeEl(tag),
        addEventListener: (t, fn) => { (docListeners[t] = docListeners[t] || []).push(fn); },
        removeEventListener: (t, fn) => {
            docListeners[t] = (docListeners[t] || []).filter(f => f !== fn);
        },
    };
    const windows = new Map();
    const layout = { mode: 'tiling', focusedCol: 0, columns: cols.map(c => {
        const key = 'local:' + c.id;
        if (c.live) windows.set(key, { id: key, disposed: false, minimized: false, hostId: 'local' });
        const col = { id: c.id, widthPreset: c.preset || '1/2',
            rows: [{ id: 'r-' + c.id, keys: [key], mode: 'single' }] };
        if (c.frac !== undefined) col.widthFrac = c.frac;
        return col;
    }) };
    let rafQ = [], rafId = 0, ts = 1000;
    const settings = { slideScreenMs: opts.slideMs || 0 };
    const t = { strip, desktop, layout, settings, docListeners, resized: 0,
                liveIds: cols.filter(c => c.live).map(c => c.id) };
    const ENV = {
        document,
        // t.selection (a node in the strip) makes relayoutStrip defer.
        window: { getSelection: () => (t.selection
            ? { isCollapsed: false, rangeCount: 1, anchorNode: t.selection }
            : { isCollapsed: true, rangeCount: 0 }) },
        windows,
        prefs: { _layout: layout },
        getLayout: () => layout,
        visibleColumns: () => layout.columns,
        hostHidden: () => false,
        rowKeys: row => row.keys,
        layoutColumnRows: () => {},          // no rows drawn: widths only
        cellTileEls: new Map(),
        scheduleResize: () => { t.resized++; },
        updateStripScrollbar: () => {},
        reorderTaskbarItems: () => {},
        getSettings: () => settings,
        savePrefs: () => {},
        // Runs the real relayout at once, except while a deferred pass asks
        // for its retry (the selection is still live then).
        requestRelayout: () => {
            t.relayouts = (t.relayouts || 0) + 1;
            if (!t.selection) t.api.relayoutStrip();
        },
        requestAnimationFrame: cb => { rafQ.push({ id: ++rafId, cb }); return rafId; },
        cancelAnimationFrame: id => { rafQ = rafQ.filter(r => r.id !== id); },
    };
    const unknown = [];
    const scope = new Proxy(ENV, {
        has: (o, k) => typeof k === 'string',
        get: (o, k) => {
            if (typeof k !== 'string') return undefined;   // Symbol.unscopables
            if (Object.prototype.hasOwnProperty.call(o, k)) return o[k];
            if (BUILTINS.indexOf(k) !== -1) return globalThis[k];
            unknown.push(k);
            throw new ReferenceError('sandbox: unstubbed identifier ' + k);
        },
        set: (o, k) => { throw new Error('sandbox: write to outer ' + String(k)); },
    });
    t.api = new Function('env', 'with (env) {\n' +
        (opts.realLayout ? S57 : presetSrc) + S61 +
        '\nreturn { relayoutStrip, scrollColumnIntoView, columnPixelWidths,' +
        ' colFrac: colCurrentFrac, getLayout };\n}\n')(scope);
    t.unknown = unknown;
    // rAF callbacks run with strictly increasing timestamps until none remain.
    t.flush = () => {
        for (let guard = 0; rafQ.length; guard++) {
            if (guard > 10000) throw new Error('rAF never settles');
            const q = rafQ; rafQ = []; ts += 16;
            for (const r of q) r.cb(ts);
        }
    };
    t.colEls = () => strip.children.filter(c => c._cls.has('strip-col'));
    t.gutters = () => strip.children.filter(c => c._cls.has('strip-col-gutter'));
    t.fire = (type, ev) => (docListeners[type] || []).slice().forEach(fn => fn(ev));
    return t;
}

// Draw the layout and settle its own focus scroll (the rAF tail scrolls the
// focused column into view), assert that tail wrote no scrollLeft, then clear
// the write log. Also asserts the sandbox really drew the columns (widths
// written) and that they fill the strip exactly when the set fills it.
function drawn(t, label, filling) {
    t.api.relayoutStrip();
    t.flush();
    const els = t.colEls();
    ok(els.map(e => e.dataset.colId).join() === t.liveIds.join()
       && els.every(e => /^\d+px$/.test(e.style.width)),
       label + ' relayoutStrip drew ' + els.map(e => e.dataset.colId) + ' sized');
    ok(t.resized === t.liveIds.length, label + ' relayout tail ran (' + t.resized + ' resizes)');
    if (filling) {
        ok(t.strip.contentW() === t.strip.clientWidth,
           label + ' columns+gutters ' + t.strip.contentW() + ' == clientWidth ' +
           t.strip.clientWidth + ' (' + els.map(e => e.style.width).join(',') + ')');
    }
    ok(t.strip.writes.length === 0,
       label + ' relayout tail wrote no scrollLeft (' + t.strip.writes + ')');
    t.strip.writes.length = 0;
}
function visibleIndexOf(t, id) {
    return t.layout.columns.findIndex(c => c.id === id);
}
function noUnknown(t, label) {
    ok(t.unknown.length === 0, label + ' sandbox read no unstubbed name ' + t.unknown);
}

// C2: focus moving between columns that fill the strip scrolls nothing.
// 1919 and 1365 make 1/2 a non-integer; 1535.2/1535.6 are a fractional
// strip whose clientWidth rounds down / up. Dormant '2/3' columns sit before
// and between the drawn ones, so visibleColumns() indices differ from drawn
// order and a frac taken from the wrong list changes the widths.
function focusCase() {
    const sets = {
        '2x1/2': ['1/2', '1/2'], '3x1/3': ['1/3', '1/3', '1/3'],
        '1/3+2/3': ['1/3', '2/3'], '2/3+1/3': ['2/3', '1/3'],
    };
    for (const name in sets) {
        for (const wf of [1919, 1920, 1365, 1366, 1535.2, 1535.6]) {
            for (const smooth of [false, true]) {
                const cols = [{ id: 'd0', preset: '2/3', live: false }];
                sets[name].forEach((p, i) => {
                    cols.push({ id: 'c' + i, preset: p, live: true });
                    if (i === 0) cols.push({ id: 'd1', preset: '2/3', live: false });
                });
              // Focus starts on the left column, then on the right one (the
              // relayout tail scrolls whichever is focused into view).
              for (const fromRight of [false, true]) {
                const t = makeSandbox(wf, cols, { slideMs: smooth ? 350 : 0 });
                const vis = t.liveIds.map(id => visibleIndexOf(t, id));
                const lastVis = vis.length - 1;
                t.layout.focusedCol = vis[fromRight ? lastVis : 0];
                const label = 'C2 ' + name + ' W=' + wf + (smooth ? ' smooth' : ' instant') +
                    (fromRight ? ' from right' : ' from left');
                drawn(t, label, true);
                const sl0 = t.strip.scrollLeft;
                const seq = fromRight
                    ? (vis.length === 2 ? [0, 1, 0, 1] : [1, 0, 1, 2])
                    : (vis.length === 2 ? [1, 0, 1, 0] : [1, 2, 1, 0]);
                for (const k of seq) {
                    t.api.scrollColumnIntoView(vis[k], smooth);
                    t.flush();
                    ok(t.strip.scrollLeft === sl0 && t.strip.writes.length === 0,
                       label + ' focus col ' + k + ': scrollLeft ' + sl0 + ' -> ' +
                       t.strip.scrollLeft + ' (' + t.strip.writes.length + ' writes)');
                }
                // Control in the same fixture: the last column 1px wider DOES
                // scroll when focused, so the 0 writes above were observed.
                const els = t.colEls(), last = els[els.length - 1];
                last.style.width = (parseInt(last.style.width, 10) + 1) + 'px';
                t.api.scrollColumnIntoView(vis[vis.length - 1], smooth);
                t.flush();
                ok(t.strip.writes.length > 0 && t.strip.scrollLeft > sl0,
                   label + ' control: a 1px-over last column scrolls');
                noUnknown(t, label);
              }
            }
        }
    }
}

// C4: a column wider than the viewport ("viewport narrowed after layout":
// reconcile drops a widthFrac > 1, so it is injected past getLayout here).
// Wholly off-screen it is brought in at its left edge; partly on-screen it
// is left alone.
function wideCase() {
    const t = makeSandbox(1920, [{ id: 'w', frac: 1.5, live: true },
        { id: 'a', live: true }, { id: 'b', live: true }, { id: 'c', live: true }]);
    t.layout.focusedCol = 0;
    const label = 'C4 viewport narrowed after layout';
    drawn(t, label, false);
    const els = t.colEls();
    ok(els.map(e => e.style.width).join() === '2853px,951px,951px,951px',
       label + ' widths ' + els.map(e => e.style.width));
    const w = els[0];
    for (const smooth of [false, true]) {
        t.settings.slideScreenMs = smooth ? 350 : 0;
        const l = label + (smooth ? ' smooth' : ' instant');
        t.strip.scrollLeft = 3804;
        t.strip.writes.length = 0;
        ok(w.offsetWidth > t.strip.clientWidth
           && w.offsetLeft + w.offsetWidth <= t.strip.scrollLeft,
           l + ' precondition: wide column wholly off the left');
        t.api.scrollColumnIntoView(0, smooth);
        t.flush();
        ok(t.strip.scrollLeft === 0, l + ' wholly off -> scrollLeft ' + t.strip.scrollLeft);
        t.strip.scrollLeft = 2000;
        t.strip.writes.length = 0;
        ok(w.offsetLeft + w.offsetWidth > 2000 && w.offsetLeft < 2000,
           l + ' precondition: wide column partly on screen');
        t.api.scrollColumnIntoView(0, smooth);
        t.flush();
        ok(t.strip.scrollLeft === 2000 && t.strip.writes.length === 0,
           l + ' partly on -> scrollLeft ' + t.strip.scrollLeft +
           ' (' + t.strip.writes.length + ' writes)');
    }
    noUnknown(t, label);
}

// C5: a column clipped by exactly 1px is still revealed, by exactly 1px.
function revealCase() {
    for (const wf of [1919, 1920]) {
        for (const smooth of [false, true]) {
            // Four halves, so the strip can scroll past col 1's left edge.
            const t = makeSandbox(wf, [{ id: 'a', live: true }, { id: 'b', live: true },
                { id: 'c', live: true }, { id: 'd', live: true }],
                { slideMs: smooth ? 350 : 0 });
            t.layout.focusedCol = 0;
            const label = 'C5 4x1/2 W=' + wf + (smooth ? ' smooth' : ' instant');
            drawn(t, label, false);
            const els = t.colEls(), cw = t.strip.clientWidth;
            const right = els[2].offsetLeft + els[2].offsetWidth;
            t.strip.scrollLeft = right - cw - 1;
            t.strip.writes.length = 0;
            ok(t.strip.scrollLeft + cw === right - 1,
               label + ' precondition: col 2 overhangs the right edge by 1px');
            t.api.scrollColumnIntoView(2, smooth);
            t.flush();
            ok(t.strip.scrollLeft === right - cw,
               label + ' right reveal -> scrollLeft ' + t.strip.scrollLeft + ' want ' + (right - cw));
            t.strip.scrollLeft = els[1].offsetLeft + 1;
            t.strip.writes.length = 0;
            ok(t.strip.scrollLeft === els[1].offsetLeft + 1,
               label + ' precondition: col 1 overhangs the left edge by 1px');
            t.api.scrollColumnIntoView(1, smooth);
            t.flush();
            ok(t.strip.scrollLeft === els[1].offsetLeft,
               label + ' left reveal -> scrollLeft ' + t.strip.scrollLeft);
            noUnknown(t, label);
        }
    }
}

// ---- C3: the gutter drag and the relayout on release agree ----------------
function mouse(x) {
    return { button: 0, clientX: x, preventDefault() {}, stopPropagation() {} };
}
function widthsOf(els) { return els.map(e => e.style.width).join(); }
function startDrag(t, gi) {
    const g = t.gutters()[gi];
    const ev = mouse(500);
    ev.currentTarget = g;
    g.listeners.mousedown.forEach(fn => fn(ev));
}

function dragCase() {
    const sets = { '2x1/2': ['1/2', '1/2'], '3x1/3': ['1/3', '1/3', '1/3'] };
    for (const name in sets) {
        for (const wf of [1919, 1920]) {
            for (let gi = 0; gi < sets[name].length - 1; gi++) {
                for (const dx of [1, 37, 300, -250]) {
                    const t = makeSandbox(wf, sets[name].map((p, i) =>
                        ({ id: 'c' + i, preset: p, live: true })));
                    const label = 'C3 ' + name + ' W=' + wf + ' gutter ' + gi + ' dx=' + dx;
                    drawn(t, label, true);
                    const els = t.colEls();
                    const A = wf - (els.length - 1) * GUTTER_CSS_W;
                    // The dragged gutter's exact position in column space
                    // (availW units, gutters left out), before the drag.
                    let P = 0;
                    for (let i = 0; i <= gi; i++) P += t.api.colFrac(t.layout.columns[i]);
                    const exactEdge = A * P;
                    startDrag(t, gi);
                    t.fire('mousemove', mouse(500 + dx));
                    const live = widthsOf(els);
                    ok(t.strip.contentW() === t.strip.clientWidth,
                       label + ' live columns+gutters ' + t.strip.contentW() +
                       ' == clientWidth ' + t.strip.clientWidth + ' (' + live + ')');
                    const edge = els[gi].offsetLeft + els[gi].offsetWidth - gi * GUTTER_CSS_W;
                    ok(Math.abs(edge - (exactEdge + dx)) <= 0.5 + 1e-9,
                       label + ' left edge ' + edge + ' follows the cursor to ' +
                       (exactEdge + dx).toFixed(2));
                    t.fire('mouseup', mouse(500 + dx));
                    t.flush();
                    ok(t.relayouts === 1, label + ' release relayout ran');
                    ok(widthsOf(t.colEls()) === live,
                       label + ' release ' + widthsOf(t.colEls()) + ' == drag ' + live);
                    noUnknown(t, label);
                }
            }
        }
    }
}

// C3 with a stale column: a deferred relayout (live selection) leaves the
// .strip-col of a column that just went dormant in the strip. The drag must
// size only the drawn columns, or the release relayout moves them.
function staleDragCase() {
    for (const wf of [1919, 1920]) {
        const t = makeSandbox(wf, ['a', 'b', 'c'].map(id =>
            ({ id, preset: '1/3', live: true })));
        const label = 'C3 stale W=' + wf;
        drawn(t, label, true);
        t.layout.columns[2].rows[0].keys = ['local:gone'];   // c goes dormant
        t.selection = t.colEls()[0];
        t.api.relayoutStrip();
        t.flush();
        t.selection = null;
        ok(t.colEls().map(e => e.dataset.colId).join() === 'a,b,c'
           && t.gutters().length === 1,
           label + ' precondition: stale c is still in the strip, one gutter');
        const els = t.colEls().slice(0, 2);
        startDrag(t, 0);
        t.fire('mousemove', mouse(537));
        const live = widthsOf(els);
        t.fire('mouseup', mouse(537));
        t.flush();
        ok(t.colEls().map(e => e.dataset.colId).join() === 'a,b',
           label + ' release relayout removed the stale column');
        ok(widthsOf(t.colEls()) === live,
           label + ' release ' + widthsOf(t.colEls()) + ' == drag ' + live);
        noUnknown(t, label);
    }
}

// C3 for a pair wider than the strip (max+1/2, 1/2+max, max+max) through the
// REAL getLayout/reconcileLayout (57), which drops a widthFrac > 1: the
// drag keeps each side <= 1, so what it shows is what release lands on.
function reconcileDragCase() {
    const pairs = [['max', '1/2'], ['1/2', 'max'], ['max', 'max']];
    for (const [p0, p1] of pairs) {
        for (const wf of [1919, 1920]) {
            for (const dx of [50, -300, 300, -1000]) {
                const t = makeSandbox(wf, [{ id: 'a', preset: p0, live: true },
                    { id: 'b', preset: p1, live: true }], { realLayout: true });
                const label = 'C3r ' + p0 + '+' + p1 + ' W=' + wf + ' dx=' + dx;
                drawn(t, label, false);
                const els = t.colEls();
                startDrag(t, 0);
                t.fire('mousemove', mouse(500 + dx));
                const live = widthsOf(els);
                const fr = t.api.getLayout().columns.map(c => c.widthFrac);
                ok(fr.every(f => typeof f === 'number' && f > 0 && f <= 1),
                   label + ' both widthFracs survive reconcile in (0,1]: ' + fr);
                t.fire('mouseup', mouse(500 + dx));
                t.flush();
                ok(t.relayouts === 1, label + ' release relayout ran');
                ok(widthsOf(t.colEls()) === live,
                   label + ' release ' + widthsOf(t.colEls()) + ' == drag ' + live);
                noUnknown(t, label);
            }
        }
    }
}

// C3 with a gutter naming a column the layout no longer has: removed between
// a mutation and its relayout frame. The drag skips it and still works on
// the rest, and the release relayout lands on what the drag showed.
function missingColDragCase() {
    for (const wf of [1919, 1920]) {
        const t = makeSandbox(wf, ['a', 'b', 'c'].map(id =>
            ({ id, preset: '1/3', live: true })));
        const label = 'C3m missing col W=' + wf;
        drawn(t, label, true);
        const els = t.colEls().slice(0, 2);
        const before = widthsOf(els);
        t.layout.columns.splice(2, 1);                   // c gone, no relayout yet
        ok(t.gutters().length === 2 && t.gutters()[1].dataset.rightColId === 'c',
           label + ' precondition: a gutter still names the removed column');
        startDrag(t, 0);
        t.fire('mousemove', mouse(537));
        const live = widthsOf(els);
        ok(live !== before, label + ' the drag moved a|b: ' + before + ' -> ' + live);
        t.fire('mouseup', mouse(537));
        t.flush();
        ok(t.relayouts === 1, label + ' release relayout ran');
        ok(widthsOf(t.colEls()) === live,
           label + ' release ' + widthsOf(t.colEls()) + ' == drag ' + live);
        noUnknown(t, label);
    }
}

const CASES = { helper: helperCase, focus: focusCase, wide: wideCase,
                reveal: revealCase, drag: dragCase, stale: staleDragCase,
                reconcile: reconcileDragCase, missing: missingColDragCase };
CASES[process.argv[2]]();
console.log('COUNT ' + pass + ' ' + fail);
"""


def _run(tmp_path, case):
    h = tmp_path / "h.js"
    h.write_text(
        _HARNESS.replace("__SRC61__", json.dumps(str(SRC_61)))
        .replace("__SRC57__", json.dumps(str(SRC_57)))
        .replace("__PSTART__", json.dumps(PRESET_START))
        .replace("__HSTART__", json.dumps(HELPER_START))
        .replace("__HEND__", json.dumps(HELPER_END)),
        encoding="utf-8")
    r = subprocess.run([NODE, str(h), case], capture_output=True, text=True,
                       timeout=120)
    out = r.stdout
    assert r.returncode == 0, out + r.stderr
    m = re.search(r"^COUNT (\d+) (\d+)$", out, re.M)
    assert m, out + r.stderr
    fails = [ln for ln in out.splitlines() if ln.startswith("FAIL")]
    assert not fails, "\n".join(fails[:20]) + "\n(%d failures)" % len(fails)
    return out, int(m.group(1))


# Every width 1000..2560 for 6 filling sets (sum + per-column bound + length)
# and 5 non-filling sets, plus the 10 colCurrentFrac checks. Pinned so a
# sweep that silently shrinks fails.
_FILLING_COLS = (2, 3, 2, 2, 3, 4)      # sum + length + oracle + one per column
_NONFILLING_COLS = (2, 2, 1, 1, 1)      # sum + oracle + one per column (+ lone)
HELPER_ASSERTS = 1561 * (
    sum(3 + n for n in _FILLING_COLS)
    + sum(2 + n + (n == 1) for n in _NONFILLING_COLS)) + 10


def test_helper_columns_fill_the_strip_exactly(tmp_path):
    out, n = _run(tmp_path, "helper")
    assert n == HELPER_ASSERTS, out[-400:]


def test_gutter_width_matches_the_css():
    """C1e: the JS gutter reserve is the width the CSS actually gives a
    .strip-col-gutter, or the exact fill is off by the difference."""
    css = CSS_13.read_text(encoding="utf-8")
    blocks = re.findall(r"\n\s*\.strip-col-gutter\s*\{([^}]*)\}", css)
    assert len(blocks) == 1, blocks
    m = re.search(r"\bflex:\s*0\s+0\s+(\d+)px\s*;", blocks[0])
    assert m, blocks[0]
    js = SRC_61.read_text(encoding="utf-8")
    jm = re.findall(r"const STRIP_GUTTER_W = (\d+);", js)
    assert len(jm) >= 1, "STRIP_GUTTER_W missing"
    assert set(jm) == {m.group(1)}, (jm, m.group(1))


# Per (set, width, mode, start side): drew + tail + exact fill + no tail
# write, 4 focus steps, the control, no-unknown = 10; 4 sets x 6 widths x
# 2 modes x 2 start sides.
FOCUS_ASSERTS = 4 * 6 * 2 * 2 * 10
# drew + tail + no tail write + widths, 2 modes x (2 preconditions + 2
# results), no-unknown.
WIDE_ASSERTS = 4 + 2 * 4 + 1
# Per (width, mode): drew + tail + no tail write, 2 preconditions + 2
# results, no-unknown.
REVEAL_ASSERTS = 2 * 2 * (3 + 4 + 1)


def test_focus_between_filling_columns_scrolls_nothing(tmp_path):
    out, n = _run(tmp_path, "focus")
    assert n == FOCUS_ASSERTS, out[-400:]


def test_wide_column_still_scrolls_into_view(tmp_path):
    out, n = _run(tmp_path, "wide")
    assert n == WIDE_ASSERTS, out[-400:]


def test_one_pixel_clipped_column_is_revealed(tmp_path):
    out, n = _run(tmp_path, "reveal")
    assert n == REVEAL_ASSERTS, out[-400:]


# Per (set, width, gutter, dx): drew + tail + exact fill + no tail write,
# live fill, edge, release ran, release == drag, no-unknown = 9; 2x1/2 has
# 1 gutter, 3x1/3 2.
DRAG_ASSERTS = 2 * 4 * (1 + 2) * 9
# Per width: drew + tail + exact fill + no tail write, precondition, stale
# removed, release == drag, no-unknown.
STALE_ASSERTS = 2 * (4 + 4)


def test_gutter_drag_and_release_relayout_agree(tmp_path):
    out, n = _run(tmp_path, "drag")
    assert n == DRAG_ASSERTS, out[-400:]


def test_gutter_drag_ignores_a_stale_deferred_column(tmp_path):
    out, n = _run(tmp_path, "stale")
    assert n == STALE_ASSERTS, out[-400:]


# Per (pair, width, dx): drew + tail + no tail write, fracs in (0,1],
# release ran, release == drag, no-unknown; 3 pairs x 2 widths x 4 dx.
RECONCILE_ASSERTS = 3 * 2 * 4 * 7


def test_gutter_drag_wider_pair_survives_the_real_reconcile(tmp_path):
    out, n = _run(tmp_path, "reconcile")
    assert n == RECONCILE_ASSERTS, out[-400:]


# Per width: drew + tail + exact fill + no tail write, precondition, moved,
# release ran, release == drag, no-unknown.
MISSING_ASSERTS = 2 * (4 + 5)


def test_gutter_drag_skips_a_column_removed_before_its_relayout(tmp_path):
    out, n = _run(tmp_path, "missing")
    assert n == MISSING_ASSERTS, out[-400:]
