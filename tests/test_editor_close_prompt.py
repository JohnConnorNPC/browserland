"""Behavioural tests for #257: closing a dirty text editor never discards its
text on Cancel, Escape, a click outside the box, or a replaced dialog.

The cells execute the SHIPPED code in node, cut from the fragments at named
boundaries and evaluated inside a ``with (env)`` scope:

* 69 ``openDialog`` (+ ``openConfirmDialog``) -- the REAL dialog singleton,
  Escape / backdrop / Enter handling and button classes, over a tiny fake DOM;
* 73 ``requestCloseAppWindow`` -- the REAL close prompt.

Only ``windows``, ``closeWindow`` and ``saveAppWindow`` (recording stubs) and
the window fixtures are stand-ins. Every case gets a FRESH world, arms itself
first (exactly one ``Unsaved changes`` overlay on screen, the question still
unanswered) and THROWS ``not armed`` if the prompt is not there, so a missing
subject is a ``FAIL``, never a vacuous pass. The set of labels is declared
below and compared with what ran: a label that never ran, or one that is not
declared, fails the file. One ``PASS:``/``FAIL:`` line per label plus an
``N passed, M failed`` tail; a node run that produced no tail is an assertion
error here, not a skip. Skipped only when node itself is absent.
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
    # The REAL dialog singleton, openTextPrompt and openConfirmDialog. The
    # `let _dlgFinish` is IN the slice, so it is private to each world.
    "dialog": ("69_js_dialog.js",
               "        let _dlgFinish = null;",
               "        // True while a styled dialog is live (the _dlgFinish singleton is set)."),
    "close": ("73_js_window_runtime.js",
              "        // User-initiated close for an app window",
              "        // Permanently discard an app document."),
    # atom 4: the registry's validation + the Delete confirm's sentence. The
    # registry slice stops BEFORE lookupWindowKind, so DM1 reads
    # _windowKindRegistry() directly and claims no lookup coverage.
    "registry": ("54_js_app_windows_store.js",
                 "        function _windowKindRegistry() {",
                 "        function lookupWindowKind(appKind) {"),
    "delmsg": ("78_js_keybindings.js",
               "        function deleteConfirmMessage(kind) {",
               "        function buildWindowMenu(win, x, y) {"),
}
EDITOR_JS = "mods/editor/editor.js"

# Declared in advance: the emitted labels must equal this list, exactly.
EXPECTED_LABELS = [
    "H0 the closeWindow stub records only real windows",
    "K0 a clean editor closes with no prompt",
    "K1 a dirty non-editor app window closes with no prompt",
    "P1a the prompt asks 'Save changes to <file> before closing?' under 'Unsaved changes'",
    "F1 the live document is captured and flushed to the store before the prompt shows",
    "S1 Save writes the file, then closes",
    "S2 Save with a failed write keeps the window open",
    "S3 Save with a throwing write keeps the window open",
    "E1 Enter commits Save",
    "X1 a window reaped during the prompt is never saved",
    "T1 a multi-tab editor names the open documents and saves them all",
    "I1 a mousedown inside the box leaves the prompt open",
    # atom 2: the three-way prompt (red before it)
    "C1 Cancel keeps the editor and its text",
    "C2 Escape keeps the editor and its text",
    "C3 a click outside the box keeps the editor and its text",
    "D1 Don't save closes without writing",
    "P1b the buttons are Save, Don't save, Cancel; Save is primary, Don't save is danger and not primary",
    "R2 a dialog opened over the prompt answers Cancel",
    "R2b a second close request makes the first one Cancel",
    "X2 a window reaped during the prompt: Don't save writes nothing, Cancel closes nothing",
    "T2 a multi-tab editor honours Cancel, Escape and Don't save",
    # atom 4: the kind's own Delete sentence
    "DM1 a registered kind keeps its deleteMessage; one without it holds null",
    "DM2 a non-string or empty deleteMessage is refused and registers nothing",
    "DM3 a deleteMessage without a deleteLabel is refused and registers nothing",
    "DM4 the Delete sentence is the kind's own, else the note sentence unchanged",
    "DM5 the real editor.js declares the message once, and the registry takes it",
]

EXPECTED_ASSERTS = len(EXPECTED_LABELS)

_HARNESS = r"""
'use strict';
const S = JSON.parse(require('fs').readFileSync(__SLICES_PATH__, 'utf8'));

let pass = 0, fail = 0;
function must(cond, msg) { if (!cond) throw new Error(msg); }
const tick = () => new Promise(r => setTimeout(r, 0));

// Evaluate `code` (sloppy, so `with` works) with env's names in scope and
// return the named functions it declares.
function load(code, env, names) {
    const ret = '{' + names.map(n => n + ': typeof ' + n + " === 'function' ? " + n
        + ' : undefined').join(', ') + '}';
    return (new Function('env', 'with (env) { return (function () {\n' + code
        + '\nreturn ' + ret + ';\n})(); }'))(env);
}

// ---- the DOM these slices touch, and nothing else (the 86m dialog harness's
// El, verbatim) -------------------------------------------------------------
function El(tag) {
    this.tag = tag;
    this.children = [];
    this.parentNode = null;
    this.classes = new Set();
    this.handlers = Object.create(null);
    this.textContent = '';
    this.value = '';
    this.type = '';
    this.placeholder = '';
}
Object.defineProperty(El.prototype, 'className', {
    get: function () { return Array.from(this.classes).join(' '); },
    set: function (v) {
        this.classes = new Set(String(v).split(/\s+/).filter(Boolean));
    },
});
Object.defineProperty(El.prototype, 'classList', {
    get: function () {
        const self = this;
        return {
            add: function (c) { self.classes.add(c); },
            remove: function (c) { self.classes.delete(c); },
            contains: function (c) { return self.classes.has(c); },
        };
    },
});
El.prototype.appendChild = function (k) {
    k.parentNode = this; this.children.push(k); return k;
};
El.prototype.removeChild = function (k) {
    const i = this.children.indexOf(k);
    if (i >= 0) this.children.splice(i, 1);
    k.parentNode = null;
    return k;
};
El.prototype.addEventListener = function (t, fn) {
    (this.handlers[t] = this.handlers[t] || []).push(fn);
};
El.prototype.removeEventListener = function (t, fn) {
    const a = this.handlers[t] || [];
    const i = a.indexOf(fn);
    if (i >= 0) a.splice(i, 1);
};
El.prototype.focus = function () {};
El.prototype.select = function () {};
El.prototype.click = function () {
    const ev = { target: this, preventDefault: function () {},
                 stopPropagation: function () {} };
    for (const fn of (this.handlers.click || []).slice()) fn(ev);
};
El.prototype.walk = function (out) {
    out = out || [];
    for (const k of this.children) { out.push(k); k.walk(out); }
    return out;
};
El.prototype.querySelector = function (sel) {
    const m = /^([\w]+)(?:\.([\w-]+))?$/.exec(sel);
    if (!m) return null;
    for (const k of this.walk()) {
        if (k.tag === m[1] && (!m[2] || k.classes.has(m[2]))) return k;
    }
    return null;
};
El.prototype.text = function () {
    let s = this.textContent || '';
    if (this.value) s += ' ' + this.value;
    for (const k of this.children) s += ' ' + k.text();
    return s;
};

// ---- one fresh world per case ---------------------------------------------
function mkWorld() {
    const w = { log: [], attempted: [], windows: new Map(),
                body: new El('body'), handlers: Object.create(null) };
    w.document = {
        body: w.body,
        createElement: function (t) { return new El(t); },
        addEventListener: function (t, fn) {
            (w.handlers[t] = w.handlers[t] || []).push(fn);
        },
        removeEventListener: function (t, fn) {
            const a = w.handlers[t] || [];
            const i = a.indexOf(fn);
            if (i >= 0) a.splice(i, 1);
        },
    };
    // What is ON SCREEN: the overlays actually attached to <body>.
    w.overlays = function () {
        return w.body.children.filter(function (e) {
            return e.classes.has('app-dialog-overlay');
        });
    };
    const top = function () {
        const o = w.overlays();
        if (o.length !== 1) throw new Error('expected one dialog on screen, saw ' + o.length);
        return o[0];
    };
    w.title = function () {
        const h = top().querySelector('div.set-head');
        return h ? h.textContent : '';
    };
    w.message = function () {
        const m = top().querySelector('div.app-dialog-msg');
        return m ? m.textContent : null;
    };
    w.buttons = function () {
        return top().walk().filter(function (e) { return e.tag === 'button'; })
            .map(function (e) {
                return { label: e.textContent, classes: Array.from(e.classes) };
            });
    };
    w.click = function (label) {
        for (const e of top().walk()) {
            if (e.tag === 'button' && e.textContent === label) { e.click(); return; }
        }
        throw new Error('no button ' + label);
    };
    w.key = function (k) {
        for (const fn of (w.handlers.keydown || []).slice()) {
            fn({ key: k, target: null, preventDefault: function () {},
                 stopPropagation: function () {} });
        }
    };
    // A mousedown bubbles from its target up through the parents, until a
    // handler stops it (the modal swallows its own).
    w.mousedown = function (target) {
        let stopped = false;
        const ev = { target: target, preventDefault: function () {},
                     stopPropagation: function () { stopped = true; } };
        for (let n = target; n && !stopped; n = n.parentNode) {
            for (const fn of (n.handlers.mousedown || []).slice()) fn(ev);
        }
    };
    w.backdrop = function () { const o = top(); w.mousedown(o); };
    w.insideClick = function () { w.mousedown(top().children[0]); };

    const env = {
        windows: w.windows,
        document: w.document,
        // attempted = every call; the log only gets effective closes.
        closeWindow: function (id) {
            w.attempted.push(id);
            if (!w.windows.has(id)) return;
            w.windows.delete(id);
            w.log.push(['close', id]);
        },
        saveAppWindow: function (win) { w.log.push(['flush', win.id]); },
    };
    const fns = load(S.dialog + '\n' + S.close, env,
                     ['requestCloseAppWindow', 'openDialog', 'openConfirmDialog']);
    w.requestClose = fns.requestCloseAppWindow;
    w.openDialog = fns.openDialog;
    must(typeof w.requestClose === 'function', 'requestCloseAppWindow is not in the slice');
    must(typeof w.openDialog === 'function', 'openDialog is not in the slice');

    // saver: true | false | 'throw'
    const saverOf = function (which, saver) {
        return async function () {
            w.log.push(['save', which]);
            if (saver === 'throw') throw new Error('disk full');
            return saver !== false;
        };
    };
    w.editor = function (o) {
        o = o || {};
        const win = { id: 'app:ed1', type: 'app', appKind: o.kind || 'text-editor',
                      dirty: o.dirty !== false, filePath: '/w/a.txt', disposed: false };
        const saver = ('saver' in o) ? o.saver : true;
        win._saveToServer = saverOf('single', saver);
        if (o.tabs) {
            win.tabs = [{ kind: 'file', dirty: true }, { kind: 'file', dirty: false }];
            win._captureActiveDoc = function () { w.log.push(['capture', win.id]); };
            win._saveAllDirty = saverOf('all', saver);
        }
        w.windows.set(win.id, win);
        return win;
    };
    w.closes = function () {
        return w.log.filter(function (e) { return e[0] === 'close'; })
            .map(function (e) { return e[1]; });
    };
    w.saves = function () {
        return w.log.filter(function (e) { return e[0] === 'save'; })
            .map(function (e) { return e[1]; });
    };
    return w;
}

// Start a close request and ARM: exactly one 'Unsaved changes' dialog on
// screen, the question still unanswered (and, when given, its button labels).
async function prompt(w, win, labels) {
    const call = { settled: false, threw: null };
    call.p = w.requestClose(win.id).then(
        function () { call.settled = true; },
        function (e) { call.settled = true; call.threw = e; });
    await tick();
    call.armLog = w.log.slice();
    const n = w.overlays().length;
    must(n === 1, 'not armed: ' + n + ' dialogs on screen after the close request');
    must(w.title() === 'Unsaved changes', 'not armed: title is ' + JSON.stringify(w.title()));
    must(!call.settled, 'not armed: the request settled before any answer');
    if (labels) {
        const got = w.buttons().map(function (b) { return b.label; }).join('|');
        must(got === labels.join('|'), 'not armed: buttons are ' + got);
    }
    return call;
}
// One action that ANSWERS the prompt: the request settles, cleanly, and the
// dialog is gone.
async function terminal(w, call, action) {
    action();
    await tick();
    must(call.settled, 'the request never settled');
    must(call.threw === null, 'the request threw: ' + (call.threw && call.threw.message));
    must(w.overlays().length === 0, 'the dialog is still on screen');
}
// A close that must NOT prompt: it settles on its own, with no dialog up. A
// request left waiting on a prompt is a failed case, not a hung run.
async function closeNow(w, id) {
    let settled = false;
    const p = w.requestClose(id).then(function () { settled = true; });
    await tick();
    must(w.overlays().length === 0, 'a dialog was shown');
    must(settled, 'the close request is waiting on something');
    await p;
}
// Cleanup for a case that only LOOKED at the prompt.
async function dismiss(w, call) {
    await terminal(w, call, function () { w.key('Escape'); });
}

const CASES = [];
function C(label, fn) { CASES.push([label, fn]); }

C("H0 the closeWindow stub records only real windows", async function (w) {
    must(w.closes().length === 0 && w.attempted.length === 0, 'the log starts empty');
    const clean = w.editor({ dirty: false });
    await closeNow(w, clean.id);
    must(w.closes().join() === clean.id, 'a real close is recorded once');
    must(!w.windows.has(clean.id), 'a real close removes the window');
    await closeNow(w, 'app:nope');   // not a window: attempted, not effective
    must(w.attempted.join() === clean.id + ',app:nope', 'attempted: ' + w.attempted);
    must(w.closes().length === 1, 'an id that is not a window was recorded as closed');
});

C("K0 a clean editor closes with no prompt", async function (w) {
    const win = w.editor({ dirty: false });
    await closeNow(w, win.id);
    must(w.closes().join() === win.id, 'closes: ' + JSON.stringify(w.closes()));
    must(!w.windows.has(win.id), 'the window is still open');
});

C("K1 a dirty non-editor app window closes with no prompt", async function (w) {
    const win = w.editor({ kind: 'file-manager' });
    await closeNow(w, win.id);
    must(w.closes().join() === win.id, 'closes: ' + JSON.stringify(w.closes()));
    must(w.saves().length === 0, 'a save ran for a non-editor');
});

C("P1a the prompt asks 'Save changes to <file> before closing?' under 'Unsaved changes'",
  async function (w) {
    const win = w.editor();
    const call = await prompt(w, win);
    must(w.message() === 'Save changes to /w/a.txt before closing?',
         'message is ' + JSON.stringify(w.message()));
    await dismiss(w, call);
});

C("F1 the live document is captured and flushed to the store before the prompt shows",
  async function (w) {
    const win = w.editor({ tabs: true });
    const call = await prompt(w, win);
    const names = call.armLog.map(function (e) { return e[0]; });
    must(names.indexOf('capture') >= 0, 'no capture before the prompt: ' + names);
    must(names.indexOf('flush') > names.indexOf('capture'),
         'the flush does not follow the capture: ' + names);
    await dismiss(w, call);
});

C("S1 Save writes the file, then closes", async function (w) {
    const win = w.editor();
    const call = await prompt(w, win);
    await terminal(w, call, function () { w.click('Save'); });
    const eff = w.log.filter(function (e) { return e[0] === 'save' || e[0] === 'close'; });
    must(JSON.stringify(eff) === JSON.stringify([['save', 'single'], ['close', win.id]]),
         'order: ' + JSON.stringify(eff));
});

C("S2 Save with a failed write keeps the window open", async function (w) {
    const win = w.editor({ saver: false });
    const call = await prompt(w, win);
    await terminal(w, call, function () { w.click('Save'); });
    must(w.saves().join() === 'single', 'the write was not attempted');
    must(w.closes().length === 0, 'closed after a failed write');
    must(w.windows.has(win.id), 'the window is gone');
});

C("S3 Save with a throwing write keeps the window open", async function (w) {
    const win = w.editor({ saver: 'throw' });
    const call = await prompt(w, win);
    await terminal(w, call, function () { w.click('Save'); });
    must(w.saves().join() === 'single', 'the write was not attempted');
    must(w.closes().length === 0, 'closed after a throwing write');
    must(w.windows.has(win.id), 'the window is gone');
});

C("E1 Enter commits Save", async function (w) {
    const win = w.editor();
    const call = await prompt(w, win);
    await terminal(w, call, function () { w.key('Enter'); });
    const eff = w.log.filter(function (e) { return e[0] === 'save' || e[0] === 'close'; });
    must(JSON.stringify(eff) === JSON.stringify([['save', 'single'], ['close', win.id]]),
         'Enter did not Save then close: ' + JSON.stringify(eff));
});

C("X1 a window reaped during the prompt is never saved", async function (w) {
    const win = w.editor();
    const call = await prompt(w, win);
    win.disposed = true;                // the /sessions reaper tore it down
    w.windows.delete(win.id);
    await terminal(w, call, function () { w.click('Save'); });
    must(w.saves().length === 0, 'saved a dead window: ' + w.saves());
    must(w.closes().length === 0, 'closed a dead window');
});

C("T1 a multi-tab editor names the open documents and saves them all", async function (w) {
    const win = w.editor({ tabs: true });
    const call = await prompt(w, win);
    must(w.message() === 'Save changes to the open documents before closing?',
         'message is ' + JSON.stringify(w.message()));
    await terminal(w, call, function () { w.click('Save'); });
    must(w.saves().join() === 'all', 'saves: ' + w.saves());
    must(w.closes().join() === win.id, 'closes: ' + JSON.stringify(w.closes()));
});

C("I1 a mousedown inside the box leaves the prompt open", async function (w) {
    const win = w.editor();
    const call = await prompt(w, win);
    w.insideClick();
    await tick();
    must(!call.settled, 'a click inside the box answered the prompt');
    must(w.overlays().length === 1, 'the dialog went away');
    await dismiss(w, call);
});

const THREE = ['Save', "Don't save", 'Cancel'];
// Nothing was closed, written or discarded: the window and its dirty text stay.
function mustKeep(w, win) {
    must(w.closes().length === 0, 'closed: ' + JSON.stringify(w.closes()));
    must(w.attempted.length === 0, 'closeWindow was called: ' + w.attempted);
    must(w.windows.has(win.id), 'the window left the desktop');
    must(w.windows.get(win.id) === win, 'the window was replaced');
    must(w.saves().length === 0, 'something was written: ' + w.saves());
    must(win.tabs ? win.tabs[0].dirty === true : win.dirty === true,
         'the unsaved text lost its dirty mark');
}

C("C1 Cancel keeps the editor and its text", async function (w) {
    const win = w.editor();
    const call = await prompt(w, win, THREE);
    await terminal(w, call, function () { w.click('Cancel'); });
    mustKeep(w, win);
});

C("C2 Escape keeps the editor and its text", async function (w) {
    const win = w.editor();
    const call = await prompt(w, win, THREE);
    await terminal(w, call, function () { w.key('Escape'); });
    mustKeep(w, win);
});

C("C3 a click outside the box keeps the editor and its text", async function (w) {
    const win = w.editor();
    const call = await prompt(w, win, THREE);
    await terminal(w, call, function () { w.backdrop(); });
    mustKeep(w, win);
});

C("D1 Don't save closes without writing", async function (w) {
    const win = w.editor();
    const call = await prompt(w, win, THREE);
    await terminal(w, call, function () { w.click("Don't save"); });
    must(w.closes().join() === win.id, 'closes: ' + JSON.stringify(w.closes()));
    must(w.saves().length === 0, 'something was written: ' + w.saves());
    must(!w.windows.has(win.id), 'the window is still open');
});

C("P1b the buttons are Save, Don't save, Cancel; Save is primary, Don't save is danger and not primary",
  async function (w) {
    const win = w.editor();
    const call = await prompt(w, win, THREE);
    const by = {};
    w.buttons().forEach(function (b) { by[b.label] = b.classes; });
    const has = function (label, cls) { return by[label].indexOf(cls) >= 0; };
    must(has('Save', 'primary') && !has('Save', 'danger'), 'Save: ' + by['Save']);
    must(has("Don't save", 'danger') && !has("Don't save", 'primary'),
         "Don't save: " + by["Don't save"]);
    must(!has('Cancel', 'primary') && !has('Cancel', 'danger'), 'Cancel: ' + by['Cancel']);
    await dismiss(w, call);
});

C("R2 a dialog opened over the prompt answers Cancel", async function (w) {
    const win = w.editor();
    const call = await prompt(w, win, THREE);
    w.openDialog({ title: 'Other', buttons: [{ label: 'Fine', value: 1, primary: true }] });
    await tick();
    must(call.settled && call.threw === null, 'the prompt did not settle cleanly');
    mustKeep(w, win);
    must(w.overlays().length === 1 && w.title() === 'Other',
         'the replacing dialog is not the one on screen');
    w.key('Escape');
    await tick();
    must(w.overlays().length === 0, 'the replacing dialog would not close');
});

C("R2b a second close request makes the first one Cancel", async function (w) {
    const win = w.editor();
    const first = await prompt(w, win, THREE);
    const second = await prompt(w, win, THREE);
    must(first.settled && first.threw === null, 'the first request is still pending');
    mustKeep(w, win);
    await terminal(w, second, function () { w.click("Don't save"); });
    must(w.closes().join() === win.id, 'closes: ' + JSON.stringify(w.closes()));
});

C("X2 a window reaped during the prompt: Don't save writes nothing, Cancel closes nothing",
  async function (w) {
    const win = w.editor();
    const call = await prompt(w, win, THREE);
    win.disposed = true;
    w.windows.delete(win.id);
    await terminal(w, call, function () { w.click("Don't save"); });
    must(w.saves().length === 0, 'saved a dead window: ' + w.saves());
    must(w.attempted.join() === win.id, 'attempted: ' + w.attempted);
    must(w.closes().length === 0, 'a dead window was recorded as closed');

    const w2 = mkWorld();
    const win2 = w2.editor();
    const call2 = await prompt(w2, win2, THREE);
    win2.disposed = true;
    w2.windows.delete(win2.id);
    await terminal(w2, call2, function () { w2.click('Cancel'); });
    must(w2.attempted.length === 0 && w2.saves().length === 0,
         'Cancel touched a dead window: ' + w2.attempted + ' / ' + w2.saves());
});

C("T2 a multi-tab editor honours Cancel, Escape and Don't save", async function (w) {
    const win = w.editor({ tabs: true });
    const call = await prompt(w, win, THREE);
    await terminal(w, call, function () { w.click('Cancel'); });
    mustKeep(w, win);

    const wb = mkWorld();
    const winb = wb.editor({ tabs: true });
    const callb = await prompt(wb, winb, THREE);
    await terminal(wb, callb, function () { wb.key('Escape'); });
    mustKeep(wb, winb);

    const wc = mkWorld();
    const winc = wc.editor({ tabs: true });
    const callc = await prompt(wc, winc, THREE);
    await terminal(wc, callc, function () { wc.click("Don't save"); });
    must(wc.closes().join() === winc.id, 'closes: ' + JSON.stringify(wc.closes()));
    must(wc.saves().length === 0, 'something was written: ' + wc.saves());
});

const EDITOR_MSG = 'Close this editor and discard any unsaved text? '
    + 'The file on disk is not touched.';
function mkRegistry() {
    const env = { registerBuiltinWindowKinds: function () {},
                  ModConflictError: function (m) { return new Error(m); } };
    const r = load(S.registry, env, ['_windowKindRegistry', 'registerWindowKind']);
    must(typeof r.registerWindowKind === 'function', 'registerWindowKind is not in the slice');
    return r;
}
function refused(fn) {
    try { fn(); } catch (e) { return String(e.message); }
    return null;
}
const fileKind = function (extra) {
    return Object.assign({ appKind: 'ed', factory: function () { return null; },
                           deleteLabel: 'file' }, extra);
};

C("DM1 a registered kind keeps its deleteMessage; one without it holds null", async function () {
    const reg = mkRegistry();
    const e = reg.registerWindowKind(fileKind({ deleteMessage: 'X' }));
    must(e.deleteMessage === 'X', 'entry holds ' + JSON.stringify(e.deleteMessage));
    must(reg._windowKindRegistry().get('ed') === e, 'the entry is not the registered one');
    const n = reg.registerWindowKind(fileKind({ appKind: 'note', deleteLabel: 'note' }));
    must(n.deleteMessage === null, 'an unset message holds ' + JSON.stringify(n.deleteMessage));
    must(n.deleteLabel === 'note', 'deleteLabel was disturbed');
});

C("DM2 a non-string or empty deleteMessage is refused and registers nothing", async function () {
    const reg = mkRegistry();
    for (const bad of ['', 42, {}, true]) {
        const m = refused(function () {
            reg.registerWindowKind(fileKind({ deleteMessage: bad }));
        });
        must(m && m.indexOf('deleteMessage must be a non-empty string') >= 0,
             'for ' + JSON.stringify(bad) + ': ' + m);
    }
    must(reg._windowKindRegistry().size === 0, 'a refused spec was registered');
});

C("DM3 a deleteMessage without a deleteLabel is refused and registers nothing", async function () {
    const reg = mkRegistry();
    const m = refused(function () {
        reg.registerWindowKind({ appKind: 'x', factory: function () { return null; },
                                 deleteMessage: 'X' });
    });
    must(m && m.indexOf('deleteMessage requires deleteLabel') >= 0, 'got: ' + m);
    must(reg._windowKindRegistry().size === 0, 'a refused spec was registered');
});

C("DM4 the Delete sentence is the kind's own, else the note sentence unchanged", async function () {
    const dm = load(S.delmsg, {}, ['deleteConfirmMessage']).deleteConfirmMessage;
    must(typeof dm === 'function', 'deleteConfirmMessage is not in the slice');
    const old = 'Delete this note permanently? Its saved content is removed.';
    must(dm({ deleteLabel: 'note' }) === old, 'note: ' + dm({ deleteLabel: 'note' }));
    must(dm({ deleteLabel: 'note', deleteMessage: null }) === old, 'a null message');
    must(dm({ deleteLabel: 'file', deleteMessage: 'X' }) === 'X', 'a kind message');
    must(dm({ deleteLabel: 'file', deleteMessage: EDITOR_MSG }) === EDITOR_MSG,
         'the editor sentence');
});

C("DM5 the real editor.js declares the message once, and the registry takes it", async function () {
    const src = S.editor_src;
    must((src.match(/deleteMessage:/g) || []).length === 1, 'deleteMessage: is not declared once');
    const m = /deleteMessage:\s*((?:'[^'\n]*'\s*\+\s*)*'[^'\n]*')\s*,/.exec(src);
    must(m, 'the deleteMessage value is not a string literal expression');
    const value = (new Function('return (' + m[1] + ');'))();
    must(value === EDITOR_MSG, 'editor.js says ' + JSON.stringify(value));
    const reg = mkRegistry();
    const e = reg.registerWindowKind(fileKind({ deleteMessage: value }));
    must(e.deleteMessage === EDITOR_MSG, 'the registry did not keep it');
});

(async function () {
    for (const [label, fn] of CASES) {
        try {
            await fn(mkWorld());
            pass++; console.log('PASS: ' + label);
        } catch (e) {
            fail++; console.log('FAIL: ' + label);
            console.log('WHY: ' + label + ' :: ' + (e && e.message || e));
        }
    }
    console.log(pass + ' passed, ' + fail + ' failed');
})().catch(function (e) { console.log('HARNESS CRASH: ' + (e && e.stack || e)); });
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
    d = tmp_path_factory.mktemp("editor-close-prompt")
    slices = d / "slices.json"
    cuts = {n: _cut(n) for n in SLICES}
    cuts["editor_src"] = (BROKER_DIR / EDITOR_JS).read_text(encoding="utf-8")
    slices.write_text(json.dumps(cuts), encoding="utf-8")
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


@pytest.mark.parametrize("label", EXPECTED_LABELS)
def test_257_close_prompt(run, label):
    got = run["results"].get(label)
    verdict = "failed" if got is False else "never ran"
    assert got is True, (
        f"{label!r} {verdict}\nstdout: {run['stdout']}\nstderr: {run['stderr']}")


def test_every_declared_label_ran_and_node_exited_clean(run):
    assert set(run["results"]) == set(EXPECTED_LABELS), (
        "emitted labels differ from the declared set: "
        f"missing {sorted(set(EXPECTED_LABELS) - set(run['results']))}, "
        f"undeclared {sorted(set(run['results']) - set(EXPECTED_LABELS))}")
    assert len(EXPECTED_LABELS) == len(set(EXPECTED_LABELS))
    assert run["failed"] == 0, run["stdout"] + run["stderr"]
    assert run["passed"] == EXPECTED_ASSERTS, run["stdout"]
    assert run["rc"] == 0, run["stderr"]
    assert "HARNESS CRASH" not in run["stdout"], run["stdout"]


def test_257_deleteMessage_owners_and_the_one_call():
    """U1 (source shape): only the registry (54), the Delete item's helper (78)
    and the editor kind name ``deleteMessage`` -- a second owner would be an
    unreviewed way to word a destructive confirm -- and the Delete item hands
    the helper the whole kind, exactly once, so the editor's sentence is the one
    it shows."""
    owners = sorted(
        str(p.relative_to(BROKER_DIR)).replace("\\", "/")
        for p in BROKER_DIR.rglob("*.js")
        if "deleteMessage" in p.read_text(encoding="utf-8"))
    assert owners == ["54_js_app_windows_store.js", "78_js_keybindings.js",
                      EDITOR_JS], owners
    keys = (BROKER_DIR / "78_js_keybindings.js").read_text(encoding="utf-8")
    assert keys.count("deleteConfirmMessage(_deleteKind)") == 1
    assert keys.count("function deleteConfirmMessage(kind) {") == 1
