"""Behavioural tests for #248 C1: a lease holder re-sends its PTY size.

When this page newly holds a host's lease (64 ``_noteLeaseHolding``),
``reclaimHostSize`` (73) marks every terminal of that host and refits it. The
mark makes ``sendResize`` send even when the grid equals ``lastSentDims`` --
that dedup records what the page SENT, and a send the broker dropped (the
page did not hold the lease) left the PTY at another device's size (the
owner's phone-sized terminals). The mark is spent only by an actual send, so a
window that cannot send at reclaim time (minimized, on another workspace,
socket not open yet) sends on its next ordinary resize (audit M4).

Runs the SHIPPED functions in node: ``refitSoon`` and the ``isResizable`` ..
``reclaimHostSize`` block are cut from 73 at their own boundaries.
Skipped when node is absent.
"""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from webterm.broker import ui

BROKER_DIR = Path(ui.__file__).resolve().parent
SRC_JS = BROKER_DIR / "73_js_window_runtime.js"
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="node not installed")

CUTS = [
    ("        function refitSoon(win) {", "        function maybeSendInitialResize(win) {"),
    ("        function isResizable(win) {", "        function scheduleResize(win) {"),
]

EXPECTED_ASSERTS = 9

_HARNESS = r"""
'use strict';
const fs = require('fs');
const s = fs.readFileSync(__SRC__, 'utf8');
let code = '';
for (const [a, b] of __CUTS__) {
    const i = s.indexOf(a), j = s.indexOf(b);
    if (i < 0 || j < i) throw new Error('boundary missing: ' + a);
    code += s.slice(i, j) + '\n';
}
let pass = 0, fail = 0;
function ok(cond, label) {
    if (cond) { pass++; console.log('PASS: ' + label); }
    else { fail++; console.log('FAIL: ' + label); }
}
const windows = new Map();
function mkWin(id, hostId, o) {
    const win = Object.assign({
        id: id, hostId: hostId, type: 'term', disposed: false, minimized: false,
        detached: false, controlled: null, lastSentDims: { cols: 80, rows: 20 },
        box: { width: 808, height: 408 },             // the page's own 80x20 grid
        ws: { readyState: 1, sent: [], send: function (m) { this.sent.push(JSON.parse(m)); } },
    }, o || {});
    win.body = { getBoundingClientRect: function () { return win.hidden ? { width: 0, height: 0 } : win.box; } };
    windows.set(id, win);
    return win;
}
const env = {
    windows: windows, WebSocket: { OPEN: 1, CONNECTING: 0 },
    readCellDims: function () { return { w: 10, h: 20 }; },
    requestAnimationFrame: function (fn) { fn(); },
};
const api = new Function('env', 'with (env) {\n' + code +
    '\nvar __a = { sendResize: sendResize, reclaimHostSize: reclaimHostSize };\n}\nreturn __a;\n')(env);
const R = function (w) { return w.ws.sent.filter(m => m.type === 'resize'); };

// The S1 trap: lastSentDims already equals the page's own grid (its earlier
// send was dropped by the broker), so an ordinary refit sends nothing.
const a = mkWin('local:1', 'local');
api.sendResize(a, false);
ok(R(a).length === 0, 'Z0 precondition: the dedup swallows an ordinary refit at the same grid');
const b = mkWin('rb:2', 'rb');
const m = mkWin('local:3', 'local', { minimized: true });
const c = mkWin('local:4', 'local', { ws: { readyState: 0, sent: [], send: function (x) { this.sent.push(JSON.parse(x)); } } });
const app = mkWin('app:note:1', 'local', { type: 'app' });
api.reclaimHostSize('local');
ok(R(a).length === 1 && R(a)[0].cols === 80 && R(a)[0].rows === 20,
    'Z1 a reclaim sends one resize at the page own grid despite the dedup');
ok(R(b).length === 0 && !b.needsReclaim, 'Z2 another host windows are untouched');
ok(R(app).length === 0, 'Z3 an app window is untouched');
ok(R(m).length === 0 && m.needsReclaim === true,
    'Z4 a minimized window sends nothing at reclaim and keeps its mark');
m.minimized = false;
api.sendResize(m, false);
ok(R(m).length === 1, 'Z5 restored, its ORDINARY refit sends exactly once at its own grid');
api.sendResize(m, false);
ok(R(m).length === 1, 'Z6 ...and the next ordinary refit sends nothing (mark spent)');
c.ws.readyState = 1;
api.sendResize(c, false);
ok(R(c).length === 1, 'Z7 a window whose socket opens later sends on its next refit');
api.sendResize(a, false);
ok(R(a).length === 1, 'Z8 the dedup is otherwise unchanged (no mark, same grid, nothing sent)');

console.log(pass + ' passed, ' + fail + ' failed');
"""


@pytest.fixture(scope="module")
def run(tmp_path_factory):
    src = SRC_JS.read_text(encoding="utf-8")
    for a, b in CUTS:
        assert src.count(a) == 1 and src.count(b) == 1, (a, b)
    h = tmp_path_factory.mktemp("size-reclaim") / "harness.js"
    h.write_text(_HARNESS.replace("__SRC__", json.dumps(str(SRC_JS)))
                 .replace("__CUTS__", json.dumps(CUTS)), encoding="utf-8")
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
    "Z0 precondition: the dedup swallows an ordinary refit at the same grid",
    "Z1 a reclaim sends one resize at the page own grid despite the dedup",
    "Z2 another host windows are untouched",
    "Z3 an app window is untouched",
    "Z4 a minimized window sends nothing at reclaim and keeps its mark",
    "Z5 restored, its ORDINARY refit sends exactly once at its own grid",
    "Z6 ...and the next ordinary refit sends nothing (mark spent)",
    "Z7 a window whose socket opens later sends on its next refit",
    "Z8 the dedup is otherwise unchanged (no mark, same grid, nothing sent)",
])
def test_size_reclaim(run, label):
    got = run["results"].get(label)
    assert got is True, (f"{label!r} {'failed' if got is False else 'never ran'}\n"
                         f"{run['stdout']}\n{run['stderr']}")


def test_every_scenario_passed_and_node_exited_clean(run):
    assert run["failed"] == 0, run["stdout"] + run["stderr"]
    assert run["passed"] == EXPECTED_ASSERTS, run["stdout"]
    assert run["rc"] == 0, run["stderr"]
