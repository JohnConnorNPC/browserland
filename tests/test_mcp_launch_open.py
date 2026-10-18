"""Behavioural tests for #262's browser halves outside the desktop relay.

* ``webterm/broker/75_js_taskbar_hosts.js``: the one bounded catch-up a desktop
  op waits for when it names a window the page has not polled yet
  (``refreshSessionsFor``), together with the REAL ``refreshTaskbar`` whose
  mutex and run promise it waits on.

Like ``tests/test_desktop_rpc.py`` this executes the SHIPPED source: each
block is cut AT RUNTIME between its own markers -- never an embedded copy --
and evaluated in node inside a ``with (env)`` scope whose every outer name
(polls, the inner refresh, timers, the lease flag) is a stub the scenario
drives by hand. The fixture FAILS if a marker vanishes. Skipped when node is
absent.
"""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from webterm.broker import ui

BROKER_DIR = Path(ui.__file__).resolve().parent
SRC_75 = BROKER_DIR / "75_js_taskbar_hosts.js"
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="node not installed")

# The catch-up block: from refreshTaskbar's watchdog comment to its end marker.
CATCHUP_START = "// Hard ceiling on how long ONE refresh run may hold refreshInFlight."
CATCHUP_END = "// ---- end #262 session catch-up"

# Every ok() the scenario script makes; pinned so a scenario silently dropped
# from the harness cannot pass by absence.
EXPECTED_ASSERTS = 12

_HARNESS = r"""
'use strict';
const fs = require('fs');
function cut(src, a, z) {
    const i = src.indexOf(a);
    const j = i < 0 ? -1 : src.indexOf(z, i);
    if (i < 0 || j < 0) throw new Error('marker missing: ' + (i < 0 ? a : z));
    return src.slice(i, j + z.length);
}
const slice75 = cut(fs.readFileSync(__SRC75__, 'utf8'), __CU_A__, __CU_Z__);

let pass = 0, fail = 0;
function ok(cond, label) {
    if (cond) { pass++; console.log('PASS: ' + label); }
    else { fail++; console.log('FAIL: ' + label); }
}
let unhandled = 0;
process.on('unhandledRejection', function () { unhandled++; });
function sleep(ms) { return new Promise(function (r) { setTimeout(r, ms); }); }
async function ticks(n) { for (let i = 0; i < (n || 20); i++) await Promise.resolve(); }
function gate() {
    let open = null;
    const p = new Promise(function (r) { open = r; });
    return { p: p, open: open };
}

// ---- the catch-up world -------------------------------------------------
// Timers are counted so a leaked deadline timer is observable; polls and the
// inner refresh are scriptable per scenario.
const pending = new Set();
function fakeSetTimeout(fn, ms) {
    const h = setTimeout(function () { pending.delete(h); fn(); }, ms);
    pending.add(h);
    return h;
}
function fakeClearTimeout(h) { pending.delete(h); clearTimeout(h); }
const W = {};
const pollSt = {};
function resetCU() {
    W.deactivated = false; W.polls = 0; W.inners = 0;
    W.pollImpl = null; W.innerImpl = null;
    for (const k of Object.keys(pollSt)) delete pollSt[k];
}
resetCU();
function pollStateFor(h) {
    if (!pollSt[h]) pollSt[h] = { pollPromise: null };
    return pollSt[h];
}
function pollHostOnce(host) {
    W.polls++;
    const s = pollStateFor(host.id);
    if (s.pollPromise) return null;
    const p = Promise.resolve(W.pollImpl ? W.pollImpl(host) : undefined);
    s.pollPromise = p;
    p.then(function () { s.pollPromise = null; },
           function () { s.pollPromise = null; });
    return p;
}
const envCU = {
    POLL_MS: 20,
    refreshInFlight: false, refreshSeq: 0,
    refreshTaskbarInner: function (seq) {
        W.inners++;
        return W.innerImpl ? W.innerImpl(seq) : Promise.resolve();
    },
    pollStateFor: pollStateFor, pollHostOnce: pollHostOnce,
    hostById: function (id) { return id === 'local' ? { id: 'local' } : null; },
    setTimeout: fakeSetTimeout, clearTimeout: fakeClearTimeout,
};
Object.defineProperty(envCU, '_deactivated', {
    get: function () { return W.deactivated; },
});
const cu = new Function('env',
    'with (env) {\n' + slice75 + '\n' +
    'var __exports = { refreshTaskbar: refreshTaskbar,\n' +
    '  refreshSessionsFor: refreshSessionsFor,\n' +
    '  getRun: function () { return refreshRunPromise; } };\n' +
    '}\nreturn __exports;\n')(envCU);

(async function () {
    // ---- H: the catch-up ---------------------------------------------------
    resetCU();
    W.pollImpl = function () { return new Promise(function () {}); };
    let t0 = Date.now();
    await cu.refreshSessionsFor('local', 60);
    const dt = Date.now() - t0;
    ok(dt >= 50 && dt < 1000 && W.inners === 1,
        'H1 a poll that never settles costs at most the budget, then the refresh still runs');
    ok(pending.size === 0, 'H2 no timer is left behind on the slow path');

    resetCU();
    await cu.refreshSessionsFor('local', 5000);
    ok(W.polls === 1 && W.inners === 1 && pending.size === 0,
        'H3 the fast path polls once, refreshes once and leaves no timer behind');

    // A run already in flight may have merged before the poll landed: the
    // catch-up waits for it, THEN runs a fresh refresh of its own.
    resetCU();
    const g1 = gate();
    let firstDone = false, secondAfterFirst = false;
    W.innerImpl = function () {
        if (W.inners === 1) return g1.p.then(function () { firstDone = true; });
        secondAfterFirst = firstDone;
        return Promise.resolve();
    };
    const runX = cu.refreshTaskbar();
    const h3 = cu.refreshSessionsFor('local', 2000);
    await ticks();
    const innersWhileHeld = W.inners;
    g1.open();
    await h3; await runX;
    ok(innersWhileHeld === 1 && W.inners === 2 && secondAfterFirst,
        'H4 an in-flight run is waited for, then a fresh refresh runs after it');

    // A run that takes the mutex just before the catch-up's own call turns
    // that call into the coalesced no-op: the catch-up waits for THAT run.
    resetCU();
    const gX = gate(), gY = gate();
    let yDone = false;
    W.innerImpl = function () {
        if (W.inners === 1) return gX.p;
        return gY.p.then(function () { yDone = true; });
    };
    const runX2 = cu.refreshTaskbar();
    cu.getRun().then(function () { cu.refreshTaskbar(); });   // the foreign run
    let h4done = false;
    const h4 = cu.refreshSessionsFor('local', 2000).then(function () {
        h4done = true;
        return yDone;
    });
    await ticks();
    gX.open();
    await ticks(40);
    const earlyReturn = h4done;
    gY.open();
    const sawY = await h4;
    await runX2;
    ok(!earlyReturn && sawY === true && W.inners === 2,
        'H5 a run that took the mutex first is waited for, not skipped');

    // The watchdog retires a wedged run: its promise goes with it, and the
    // wedged run finishing late never clears the NEXT run's promise.
    resetCU();
    const gW = gate(), gN = gate();
    W.innerImpl = function () { return W.inners === 1 ? gW.p : gN.p; };
    const wedged = cu.refreshTaskbar();
    await sleep(160);                 // > REFRESH_WATCHDOG_MS (POLL_MS * 5)
    const retired = cu.getRun() === null;
    const next = cu.refreshTaskbar();
    gW.open();
    await wedged;
    const kept = cu.getRun() !== null;
    gN.open();
    await next;
    ok(retired, 'H6 the watchdog retires the wedged run\'s promise with its mutex');
    ok(kept && cu.getRun() === null,
        'H7 a retired run finishing late never clears the current run\'s promise');

    // Two ops for one host in the same tick share ONE catch-up.
    resetCU();
    const pA = cu.refreshSessionsFor('local', 2000);
    const pB = cu.refreshSessionsFor('local', 2000);
    await pA; await pB;
    ok(pA === pB && W.polls === 1 && W.inners === 1,
        'H8 concurrent catch-ups for one host share one poll and one refresh');

    resetCU();
    W.pollImpl = function () { return Promise.reject(new Error('poll down')); };
    W.innerImpl = function () { return Promise.reject(new Error('refresh down')); };
    let threw = false;
    try { await cu.refreshSessionsFor('local', 2000); } catch (_) { threw = true; }
    ok(!threw && W.inners === 1,
        'H9 a rejecting poll and refresh never throw, and the refresh still runs');

    resetCU();
    await cu.refreshSessionsFor('elsewhere', 2000);
    ok(W.polls === 0 && W.inners === 0 && pending.size === 0,
        'H10 an unknown host polls nothing and leaves no timer');
    resetCU();
    W.deactivated = true;
    await cu.refreshSessionsFor('local', 2000);
    ok(W.polls === 0 && W.inners === 0,
        'H11 a torn-down view polls nothing');

    await sleep(20);
    ok(unhandled === 0, 'H12 no unhandled rejection escaped any scenario');

    console.log(pass + ' passed, ' + fail + ' failed');
    process.exitCode = fail ? 1 : 0;
})().catch(function (e) {
    console.error(e && e.stack || e);
    process.exitCode = 1;
});
"""


@pytest.fixture(scope="module")
def scenario_run(tmp_path_factory):
    src75 = SRC_75.read_text(encoding="utf-8")
    # FAIL, never skip: without the markers the slice is unmoored from the
    # live source and every green assert below would grade a stale copy.
    for marker in (CATCHUP_START, CATCHUP_END):
        assert src75.count(marker) == 1, (
            f"{marker!r} occurs {src75.count(marker)}x in {SRC_75}; the "
            "slice boundary is missing or ambiguous")
    harness = tmp_path_factory.mktemp("mcp-launch-open") / "harness.js"
    harness.write_text(
        _HARNESS.replace("__SRC75__", json.dumps(str(SRC_75)))
                .replace("__CU_A__", json.dumps(CATCHUP_START))
                .replace("__CU_Z__", json.dumps(CATCHUP_END)),
        encoding="utf-8")
    proc = subprocess.run([NODE, str(harness)],
                          capture_output=True, text=True, encoding="utf-8",
                          timeout=120)
    lines = (proc.stdout or "").strip().splitlines()
    tail = re.match(r"^(\d+) passed, (\d+) failed$",
                    lines[-1] if lines else "")
    assert tail is not None, (
        "harness produced no machine-readable tail (node crashed?)\n"
        f"rc={proc.returncode}\nstdout={proc.stdout}\nstderr={proc.stderr}")
    results = {}
    for line in lines[:-1]:
        if line.startswith("PASS: "):
            results[line[len("PASS: "):]] = True
        elif line.startswith("FAIL: "):
            results[line[len("FAIL: "):]] = False
    return {"rc": proc.returncode, "stdout": proc.stdout,
            "stderr": proc.stderr, "passed": int(tail.group(1)),
            "failed": int(tail.group(2)), "results": results}


def _ok(run, label):
    got = run["results"].get(label)
    verdict = "failed" if got is False else "never ran"
    assert got is True, (
        f"scenario {label!r} {verdict}\n"
        f"stdout: {run['stdout']}\nstderr: {run['stderr']}")


# ---- the catch-up (refreshSessionsFor) --------------------------------------


def test_the_catch_up_is_bounded_and_leaves_no_timer(scenario_run):
    _ok(scenario_run, "H1 a poll that never settles costs at most the budget, then the refresh still runs")
    _ok(scenario_run, "H2 no timer is left behind on the slow path")
    _ok(scenario_run, "H3 the fast path polls once, refreshes once and leaves no timer behind")


def test_the_catch_up_waits_for_the_run_that_holds_the_mutex(scenario_run):
    # refreshTaskbar coalesces (`if (refreshInFlight) return;`), so a bare
    # call can be a no-op; the catch-up waits on the owning run instead.
    _ok(scenario_run, "H4 an in-flight run is waited for, then a fresh refresh runs after it")
    _ok(scenario_run, "H5 a run that took the mutex first is waited for, not skipped")


def test_the_run_promise_is_owned_like_the_mutex(scenario_run):
    _ok(scenario_run, "H6 the watchdog retires the wedged run's promise with its mutex")
    _ok(scenario_run, "H7 a retired run finishing late never clears the current run's promise")


def test_concurrent_catch_ups_coalesce_per_host(scenario_run):
    _ok(scenario_run, "H8 concurrent catch-ups for one host share one poll and one refresh")


def test_the_catch_up_never_throws_and_skips_what_it_cannot_reach(scenario_run):
    _ok(scenario_run, "H9 a rejecting poll and refresh never throw, and the refresh still runs")
    _ok(scenario_run, "H10 an unknown host polls nothing and leaves no timer")
    _ok(scenario_run, "H11 a torn-down view polls nothing")
    _ok(scenario_run, "H12 no unhandled rejection escaped any scenario")


def test_every_scenario_passed_and_node_exited_clean(scenario_run):
    run = scenario_run
    assert run["failed"] == 0, (
        f"harness reported failures\nstdout: {run['stdout']}\n"
        f"stderr: {run['stderr']}")
    assert run["passed"] == EXPECTED_ASSERTS, (
        f"expected {EXPECTED_ASSERTS} asserts, harness ran {run['passed']} "
        f"-- scenario drift\nstdout: {run['stdout']}")
    assert len(run["results"]) == EXPECTED_ASSERTS, (
        f"expected {EXPECTED_ASSERTS} labelled results, "
        f"parsed {len(run['results'])}\nstdout: {run['stdout']}")
    assert run["rc"] == 0, (
        f"node exited {run['rc']}\nstdout: {run['stdout']}\n"
        f"stderr: {run['stderr']}")
