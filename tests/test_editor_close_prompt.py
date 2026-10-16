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
}

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
// Cleanup for a case that only LOOKED at the prompt.
async function dismiss(w, call) {
    await terminal(w, call, function () { w.key('Escape'); });
}

const CASES = [];
function C(label, fn) { CASES.push([label, fn]); }

C("H0 the closeWindow stub records only real windows", async function (w) {
    must(w.closes().length === 0 && w.attempted.length === 0, 'the log starts empty');
    const clean = w.editor({ dirty: false });
    await w.requestClose(clean.id);
    must(w.closes().join() === clean.id, 'a real close is recorded once');
    must(!w.windows.has(clean.id), 'a real close removes the window');
    await w.requestClose('app:nope');   // not a window: attempted, not effective
    must(w.attempted.join() === clean.id + ',app:nope', 'attempted: ' + w.attempted);
    must(w.closes().length === 1, 'an id that is not a window was recorded as closed');
});

C("K0 a clean editor closes with no prompt", async function (w) {
    const win = w.editor({ dirty: false });
    await w.requestClose(win.id);
    must(w.overlays().length === 0, 'a dialog was shown for a clean editor');
    must(w.closes().join() === win.id, 'closes: ' + JSON.stringify(w.closes()));
    must(!w.windows.has(win.id), 'the window is still open');
});

C("K1 a dirty non-editor app window closes with no prompt", async function (w) {
    const win = w.editor({ kind: 'file-manager' });
    await w.requestClose(win.id);
    must(w.overlays().length === 0, 'a dialog was shown for a non-editor');
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
