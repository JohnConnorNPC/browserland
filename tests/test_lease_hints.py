"""Behavioural tests for the #248 lease HINT sites outside 64.

A page that lost its single-active lease without hearing about it (a dead
/control socket) still gets three signals elsewhere, and each must ask the
broker (``probeControl(hostId)``, 64) instead of being ignored:

- a terminal socket closed with 4409 (73 ``attachWebSocket`` ``onclose``);
- a terminal ``{type:'error', reason:'not_active'}`` frame: the broker dropped
  this socket's input or resize (73 ``onmessage``);
- a home ``PUT /state`` refused ``409`` with ``error:'not_active'`` (52
  ``pushState``), but NOT a stale-rev 409, which carries no such error.

Like ``tests/test_state_sync_adopt.py`` this runs the SHIPPED code in node:
73's ``attachWebSocket`` is cut at its own boundaries and driven with a fake
WebSocket; 52 is evaluated whole (plus ``savePrefs`` cut from 51) against an
in-memory broker. ``probeControl`` is a recorder. Skipped when node is absent.
"""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from webterm.broker import ui

BROKER_DIR = Path(ui.__file__).resolve().parent
RUNTIME_JS = BROKER_DIR / "73_js_window_runtime.js"
SYNC_JS = BROKER_DIR / "52_js_state_sync.js"
PREFS_JS = BROKER_DIR / "51_js_prefs.js"
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="node not installed")

ATTACH_START = "        function attachWebSocket(win) {"
ATTACH_END = "        function scheduleReattach(win) {"
SAVE_START = "        function savePrefs() {"
SAVE_END = "        function getPref(id) {"

EXPECTED_ASSERTS = 8

_HARNESS = r"""
'use strict';
const fs = require('fs');
function cut(path, a, b) {
    const s = fs.readFileSync(path, 'utf8');
    const i = s.indexOf(a), j = s.indexOf(b);
    if (i < 0 || j < i) throw new Error('boundary missing in ' + path);
    return s.slice(i, j);
}
const attachSrc = cut(__RUNTIME__, __ATTACH_START__, __ATTACH_END__);
const sync = fs.readFileSync(__SYNC__, 'utf8');
const savePrefsSrc = cut(__PREFS__, __SAVE_START__, __SAVE_END__);

let pass = 0, fail = 0;
function ok(cond, label) {
    if (cond) { pass++; console.log('PASS: ' + label); }
    else { fail++; console.log('FAIL: ' + label); }
}
const J = JSON.stringify;
function clone(o) { return JSON.parse(J(o)); }
async function flush() {
    for (let i = 0; i < 10; i++) await new Promise(function (r) { setImmediate(r); });
}

// ---- 73: the terminal socket ----------------------------------------------
function mkTerm() {
    const t = { probes: [], writes: [], lastWs: null, notices: [] };
    class WebSocket {
        constructor(url) { this.url = url; this.readyState = 0; this.sent = []; t.lastWs = this; }
        send(m) { this.sent.push(m); }
        close() { this.closed = true; }
    }
    WebSocket.CONNECTING = 0; WebSocket.OPEN = 1; WebSocket.CLOSED = 3;
    const env = {
        WebSocket: WebSocket, WS_CONNECT_TIMEOUT_MS: 5000, CLIENT_ID: 'page',
        setTimeout: function () { return 0; }, clearTimeout: function () {},
        Date: Date,
        hostById: function (id) { return { id: id, label: id }; },
        hostWsUrl: function (h, p) { return 'ws://x' + p; },
        closeWindow: function () {}, dispatchTermOut: function () {},
        sessions: new Map(), showNotice: function (s) { t.notices.push(s); },
        isDetachedSurface: function () { return false; },
        isSizeLocked: function () { return false; },
        readCellDims: function () { return null; },
        maybeSendInitialResize: function () {}, flashMcpBtn: function () {},
        formatTitle: function () { return ''; }, updateTaskbarLabel: function () {},
        pollStateFor: function () { return {}; }, showAuthOverlay: function () {},
        renderHostStatus: function () {}, scheduleReattach: function () { t.reattach = true; },
        controlKey: function (h, s) { return h + ':' + s; },
        applyControlView: function () {},
        probeControl: function (hid) { t.probes.push(hid); },
    };
    const factory = new Function('env',
        'with (env) {\n' + attachSrc + '\nvar __a = attachWebSocket;\n}\nreturn __a;\n');
    const attach = factory(env);
    t.win = { id: 'rb:5', hostId: 'rb', sid: '5', controlled: null, disposed: false,
              tiled: true,
              term: { write: function (d) { t.writes.push(d); }, resize: function () {} } };
    attach(t.win);
    t.ws = t.lastWs;
    t.ws.readyState = 1;
    t.ws.onopen();
    t.writes.length = 0;
    return t;
}
const F = function (o) { return { data: J(o) }; };

let t = mkTerm();
t.ws.onclose({ code: 4409 });
ok(J(t.probes) === J(['rb']), 'T1 a terminal 4409 close probes that host');
ok(!t.reattach, 'T2 ...and still schedules no reattach of its own');

t = mkTerm();
t.ws.onmessage(F({ type: 'control', state: 'free', gen: 1 }));
t.ws.onmessage(F({ type: 'error', reason: 'not_active', session: 5 }));
ok(J(t.probes) === J(['rb']) && t.writes.length === 0,
    'T3 an error not_active frame probes that host and writes nothing to the terminal');

t = mkTerm();
t.ws.onmessage(F({ type: 'error', reason: 'unknown_session', session_id: 5 }));
ok(t.probes.length === 0 && t.win.staleSession === true,
    'T4 an unknown_session error is unchanged (stale session, no probe)');

t = mkTerm();
t.ws.onclose({ code: 1006 });
ok(t.probes.length === 0, 'T5 an ordinary close is not a lease hint');

// ---- 52: the home PUT /state ---------------------------------------------
function mkWorld(putReply) {
    const w = { server: { rev: 1, settings: {}, layout: {} }, timers: new Map(),
                nextTimer: 1, probes: [], prefs: {} };
    async function hostFetch(host, path, opts) {
        if (!opts || !opts.method) {
            const snap = clone(w.server);
            return { ok: true, status: 200, json: async function () { return snap; } };
        }
        const body = putReply(w);
        return { ok: false, status: 409, json: async function () { return body; } };
    }
    const env = {
        prefs: w.prefs, isDetachedSurface: function () { return false; },
        hostFetch: hostFetch, localHost: function () { return { id: 'local' }; },
        homeHostId: function () { return 'local'; },
        probeControl: function (hid) { w.probes.push(hid); },
        CLIENT_ID: 'cid', FETCH_TIMEOUT_MS: 5000,
        getSettings: function () { return w.prefs._settings; },
        getLayout: function () { return w.prefs._layout; },
        savePrefsLocal: function () {},
        document: { body: { classList: { toggle: function () {} } } },
        isSizeLocked: function () { return false; },
        applyDisplaySettings: function () {}, requestRelayout: function () {},
        refreshOpenSectionUIs: function () {},
        setTimeout: function (fn, ms) {
            const id = w.nextTimer++; w.timers.set(id, { fn: fn, ms: ms }); return id;
        },
        clearTimeout: function (id) { w.timers.delete(id); },
    };
    Object.defineProperty(env, '_deactivated', { get: function () { return false; } });
    const factory = new Function('env',
        'with (env) {\n' + sync + '\n' + savePrefsSrc + '\n' +
        'var __api = { pullState: pullState, savePrefs: savePrefs,\n' +
        '  setReady: function (v) { _stateReady = v; } };\n' +
        '}\nreturn __api;\n');
    w.api = factory(env);
    w.firePush = async function () {
        for (const [id, tm] of Array.from(w.timers)) {
            if (tm.ms === 600) { w.timers.delete(id); tm.fn(); }
        }
        await flush();
    };
    return w;
}

(async function () {
    let w = mkWorld(function (w) {
        return { ok: false, error: 'not_active', rev: w.server.rev,
                 settings: {}, layout: {} };
    });
    await w.api.pullState(true); w.api.setReady(true);
    w.prefs._settings.theme = 'dusk';
    w.api.savePrefs();
    await w.firePush();
    ok(J(w.probes) === J(['local']), 'S1 a home PUT refused not_active probes the home host');

    w = mkWorld(function (w) {
        return { rev: w.server.rev + 1, settings: {}, layout: {} };   // stale rev
    });
    await w.api.pullState(true); w.api.setReady(true);
    w.prefs._settings.theme = 'dusk';
    w.api.savePrefs();
    await w.firePush();
    ok(w.probes.length === 0, 'S2 a stale-rev 409 (no error field) is not a lease hint');

    w = mkWorld(function () { return null; });
    await w.api.pullState(true); w.api.setReady(true);
    w.prefs._settings.theme = 'dusk';
    w.api.savePrefs();
    await w.firePush();
    ok(w.probes.length === 0, 'S3 a 409 with an unusable body probes nothing');

    console.log(pass + ' passed, ' + fail + ' failed');
})().catch(function (e) { console.log('CRASH ' + (e && e.stack)); process.exit(1); });
"""


@pytest.fixture(scope="module")
def run(tmp_path_factory):
    rt = RUNTIME_JS.read_text(encoding="utf-8")
    for m in (ATTACH_START, ATTACH_END):
        assert rt.count(m) == 1, f"boundary {m!r} must occur exactly once"
    pr = PREFS_JS.read_text(encoding="utf-8")
    for m in (SAVE_START, SAVE_END):
        assert pr.count(m) == 1, f"boundary {m!r} must occur exactly once"
    h = tmp_path_factory.mktemp("lease-hints") / "harness.js"
    src = _HARNESS
    for k, v in (("__RUNTIME__", str(RUNTIME_JS)), ("__SYNC__", str(SYNC_JS)),
                 ("__PREFS__", str(PREFS_JS)), ("__ATTACH_START__", ATTACH_START),
                 ("__ATTACH_END__", ATTACH_END), ("__SAVE_START__", SAVE_START),
                 ("__SAVE_END__", SAVE_END)):
        src = src.replace(k, json.dumps(v))
    h.write_text(src, encoding="utf-8")
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
    "T1 a terminal 4409 close probes that host",
    "T2 ...and still schedules no reattach of its own",
    "T3 an error not_active frame probes that host and writes nothing to the terminal",
    "T4 an unknown_session error is unchanged (stale session, no probe)",
    "T5 an ordinary close is not a lease hint",
    "S1 a home PUT refused not_active probes the home host",
    "S2 a stale-rev 409 (no error field) is not a lease hint",
    "S3 a 409 with an unusable body probes nothing",
])
def test_lease_hint_sites(run, label):
    got = run["results"].get(label)
    assert got is True, (
        f"{label!r} {'failed' if got is False else 'never ran'}\n"
        f"{run['stdout']}\n{run['stderr']}")


def test_every_scenario_passed_and_node_exited_clean(run):
    assert run["failed"] == 0, run["stdout"] + run["stderr"]
    assert run["passed"] == EXPECTED_ASSERTS, run["stdout"]
    assert run["rc"] == 0, run["stderr"]
