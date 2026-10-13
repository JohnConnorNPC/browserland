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
                "        function isTilingMode() {"),
    "rowKeys": ("57_js_tiling_model.js",
                "        function rowKeys(row) {",
                "        // Keyed mirror of normalizeRowWidths"),
    "gc": ("75_js_taskbar_hosts.js",
           "            let layoutDropped = 0;",
           "            for (const k of Object.keys(prefs)) {"),
}

EXPECTED_ASSERTS = 1

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
    };
    w.env = env;
    Object.assign(w, load(S.openTerms + S.findKey + S.rowKeys, env,
        ['getOpenTerms', 'addOpenTerm', 'removeOpenTerm', 'findKeyInLayout', 'rowKeys']));
    Object.assign(env, { getOpenTerms: w.getOpenTerms, addOpenTerm: w.addOpenTerm,
        removeOpenTerm: w.removeOpenTerm, findKeyInLayout: w.findKeyInLayout,
        rowKeys: w.rowKeys });
    return w;
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
        removeKeyFromLayout: (k) => {
            const L = w.layout();
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
    });
    const gc = load('function runGc() {\n' + S.gc + '\nreturn layoutDropped;\n}', w.env, ['runGc']);
    const dropped = gc.runGc();
    ok(dropped === 1 && J(w.colKeys()) === J(['h:2', 'h:3'])
        && J(w.layout().openTerms) === J(['h:2', 'h:3', 'h:9']) && w.saves >= 1,
        'G1 the layout GC drops a dead key from openTerms with its column; live, pending and float keys stay');
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


def test_every_scenario_passed_and_node_exited_clean(run):
    assert run["failed"] == 0, run["stdout"] + run["stderr"]
    assert run["passed"] == EXPECTED_ASSERTS, run["stdout"]
    assert len(run["results"]) == EXPECTED_ASSERTS
    assert run["rc"] == 0, run["stderr"]
