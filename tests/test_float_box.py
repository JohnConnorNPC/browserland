"""Behavioural test for #248 C2 (owner O3): a broadcast ``resized`` resizes the
terminal and NEVER snaps or persists a floating window's box.

Before #248, 73's ``applyResized`` snapped a float's ``.term-window`` to the
broadcast grid and saved that geometry (``getPref(win.id).geom = …;
savePrefs()``), so another device's grid (the owner's phone) shrank the
desktop's floats permanently, and every later refit measured the shrunken box
and re-sent the phone's grid. The owner's rule: the terminal follows, the box
keeps its own size -- with no own-echo exception (audit S10).

Runs the SHIPPED ``attachWebSocket`` (with its nested ``applyResized``) in node
against a fake WebSocket and a float whose cell dims are real, so the old snap
branch would have run. Skipped when node is absent.
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

START = "        function attachWebSocket(win) {"
END = "        function scheduleReattach(win) {"

EXPECTED_ASSERTS = 5

_HARNESS = r"""
'use strict';
const fs = require('fs');
const s = fs.readFileSync(__SRC__, 'utf8');
const i = s.indexOf(__START__), j = s.indexOf(__END__);
if (i < 0 || j < i) throw new Error('boundary missing');
const attachSrc = s.slice(i, j);
let pass = 0, fail = 0;
function ok(cond, label) {
    if (cond) { pass++; console.log('PASS: ' + label); }
    else { fail++; console.log('FAIL: ' + label); }
}
function mk(extra) {
    const t = { saves: 0, prefs: {}, resizes: [], lastWs: null };
    class WebSocket {
        constructor(url) { this.url = url; this.readyState = 0; t.lastWs = this; }
        send() {} close() {}
    }
    WebSocket.CONNECTING = 0; WebSocket.OPEN = 1; WebSocket.CLOSED = 3;
    const env = {
        WebSocket: WebSocket, WS_CONNECT_TIMEOUT_MS: 5000, CLIENT_ID: 'page',
        setTimeout: function () { return 0; }, clearTimeout: function () {}, Date: Date,
        hostById: function (id) { return { id: id }; },
        hostWsUrl: function (h, p) { return 'ws://x' + p; },
        closeWindow: function () {}, dispatchTermOut: function () {},
        sessions: new Map(), showNotice: function () {},
        isDetachedSurface: function () { return false; },
        isSizeLocked: function () { return false; },
        readCellDims: function () { return { w: 10, h: 20 }; },     // real dims
        maybeSendInitialResize: function () {}, flashMcpBtn: function () {},
        formatTitle: function () { return ''; }, updateTaskbarLabel: function () {},
        pollStateFor: function () { return {}; }, showAuthOverlay: function () {},
        renderHostStatus: function () {}, scheduleReattach: function () {},
        controlKey: function (h, s) { return h + ':' + s; },
        applyControlView: function () {},
        getPref: function (id) { return t.prefs[id] || (t.prefs[id] = { geom: { width: 812, height: 438 } }); },
        savePrefs: function () { t.saves++; },
        document: { getElementById: function () { return { clientWidth: 1600, clientHeight: 900 }; } },
        MIN_W: 200, MIN_H: 120,
    };
    const attach = new Function('env', 'with (env) {\n' + attachSrc +
        '\nvar __a = attachWebSocket;\n}\nreturn __a;\n')(env);
    // An 80x20 float (outer 812x438), as this device sized it.
    t.dom = { offsetWidth: 812, offsetHeight: 438, offsetLeft: 10, offsetTop: 10,
              style: { width: '808px', height: '434px' } };
    t.win = Object.assign({ id: 'local:5', hostId: 'local', sid: '5', controlled: null,
        disposed: false, tiled: false, dom: t.dom,
        geom: { left: 10, top: 10, width: 812, height: 438 },
        lastSentDims: { cols: 80, rows: 20 },
        term: { write: function () {}, resize: function (c, r) { t.resizes.push(c + 'x' + r); } } },
        extra || {});
    attach(t.win);
    t.ws = t.lastWs;
    t.ws.readyState = 1; t.ws.onopen();
    // The attach order: `resized` (held), then the first `control` (flushes it).
    t.ws.onmessage(RES(80, 20));
    t.ws.onmessage({ data: JSON.stringify({ type: 'control', state: 'free', gen: 1 }) });
    t.resizes.length = 0;
    return t;
}
function RES(c, r) { return { data: JSON.stringify({ type: 'resized', cols: c, rows: r }) }; }

let t = mk();
t.ws.onmessage(RES(61, 30));                // the phone's grid
ok(t.resizes.indexOf('61x30') !== -1, 'F1 the terminal follows a foreign grid');
ok(t.dom.style.width === '808px' && t.dom.style.height === '434px'
    && t.win.geom.width === 812 && t.win.geom.height === 438,
    'F2 ...and the float box keeps its own size');
ok(t.saves === 0 && t.prefs['local:5'] === undefined,
    'F3 ...and nothing is persisted');

t = mk({ lastSentDims: { cols: 50, rows: 15 } });
t.ws.onmessage(RES(50, 15));                // equals this page's own last send
ok(t.dom.style.width === '808px' && t.saves === 0,
    'F4 an own-echo grid snaps nothing either (no echo exception, S10)');

t = mk({ tiled: true });
t.ws.onmessage(RES(61, 30));
ok(t.resizes.indexOf('61x30') !== -1 && t.saves === 0, 'F5 a tiled window is unchanged: grid only');

console.log(pass + ' passed, ' + fail + ' failed');
"""


@pytest.fixture(scope="module")
def run(tmp_path_factory):
    src = SRC_JS.read_text(encoding="utf-8")
    for m in (START, END):
        assert src.count(m) == 1, m
    h = tmp_path_factory.mktemp("float-box") / "harness.js"
    h.write_text(_HARNESS.replace("__SRC__", json.dumps(str(SRC_JS)))
                 .replace("__START__", json.dumps(START))
                 .replace("__END__", json.dumps(END)), encoding="utf-8")
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
    "F1 the terminal follows a foreign grid",
    "F2 ...and the float box keeps its own size",
    "F3 ...and nothing is persisted",
    "F4 an own-echo grid snaps nothing either (no echo exception, S10)",
    "F5 a tiled window is unchanged: grid only",
])
def test_float_box(run, label):
    got = run["results"].get(label)
    assert got is True, (f"{label!r} {'failed' if got is False else 'never ran'}\n"
                         f"{run['stdout']}\n{run['stderr']}")


def test_every_scenario_passed_and_node_exited_clean(run):
    assert run["failed"] == 0, run["stdout"] + run["stderr"]
    assert run["passed"] == EXPECTED_ASSERTS, run["stdout"]
    assert run["rc"] == 0, run["stderr"]
