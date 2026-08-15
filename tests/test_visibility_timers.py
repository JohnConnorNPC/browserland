"""Behavioural tests for the visibility-timer block of
``webterm/broker/64_js_sessions_poll_control.js``.

The block at the top of that fragment -- onVisibility/_dispatchVisible with
the _wasHidden gate, visibilityInterval's hidden 10x cadence (HIDDEN_MULT)
with missed-coalesce, the half-interval floor and the due-based resume
catch-up, and makeModVisibilityApi's fail-closed creates with self-removing
rec.unloads records -- is pure event/clock plumbing, and every one of its
bugs is an ORDERING bug: a load-time 'pageshow' firing subscribers on a page
that was never hidden, a bfcache restore (pagehide + pageshow +
visibilitychange, order browser-dependent) firing them twice, a resume
catch-up followed back-to-back by the tick that was already queued behind
it, a frozen tab whose interval never ticked so the missed flag alone cannot
be trusted. A source-slice assert can prove the words are there; only
running the code against a hand-cranked clock and a scripted visibility
state can prove the dispatch happens exactly once.

So, like ``test_update_fleet.py``, this executes the SHIPPED file: the
fragment is cut AT RUNTIME at its own '// ---- shared state' section marker
-- never an embedded copy, so the test tracks the live source, and if the
marker vanishes the fixture FAILS (not skips), because a green result
against a stale slice would be a lie -- and the slice is evaluated in node
inside a function scope whose document/window/performance/setInterval are
stubs the scenario script drives by hand. The harness prints one PASS/FAIL
line per assert plus a machine-readable 'N passed, M failed' tail and exits
nonzero on any failure; the tests below parse that, so a regression fails
the pytest function that owns the scenario with node's full output in the
assertion message.

Skipped when node is absent, so the suite still runs on a box without it.
"""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from webterm.broker import ui

BROKER_DIR = Path(ui.__file__).resolve().parent
SRC_JS = BROKER_DIR / "64_js_sessions_poll_control.js"
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="node not installed")

# The block under test ends where the fragment's own next section begins.
# Everything before this marker is the visibility machinery and nothing else,
# so the slice needs no anchors of its own -- the marker IS the boundary.
MARKER = "// ---- shared state"

# Every ok() the scenario script makes. The summary test pins this so a
# scenario silently dropped from the harness cannot pass by absence.
EXPECTED_ASSERTS = 21

_HARNESS = r"""
'use strict';
// Stub the page, cut the LIVE fragment at its marker, replay the scenarios,
// print one PASS/FAIL line per assert and a machine-readable tail.
const fs = require('fs');
const src = fs.readFileSync(__SRC_PATH__, 'utf8');
const cut = src.indexOf('// ---- shared state');
if (cut < 0) { console.error('marker missing'); process.exit(1); }
const slice = src.slice(0, cut);

// ---- stubs --------------------------------------------------------------
// A hand-cranked page: the clock only moves when a scenario moves it, an
// interval only ticks when a scenario fires it, and visibility flips on
// demand -- so every ordering the browser could produce is produced here
// deliberately, including the ones (frozen timers, bfcache double events)
// no real browser produces on cue.
let now = 100000;
const docListeners = {}, winListeners = {};
const documentStub = {
    visibilityState: 'visible',
    addEventListener: (ev, fn) => { (docListeners[ev] = docListeners[ev] || []).push(fn); },
};
const windowStub = {
    addEventListener: (ev, fn) => { (winListeners[ev] = winListeners[ev] || []).push(fn); },
};
const performanceStub = { now: () => now };
let nextId = 1;
const intervals = new Map();
function setIntervalStub(fn, ms) { intervals.set(nextId, { fn, ms }); return nextId++; }
function clearIntervalStub(id) { intervals.delete(id); }
function fireTick(id) { const r = intervals.get(id); if (r) r.fn(); }
function fire(where, ev) { ((where === 'doc' ? docListeners : winListeners)[ev] || []).forEach(f => f()); }
function setVisibility(v) { documentStub.visibilityState = v; fire('doc', 'visibilitychange'); }

// The slice runs inside a function scope whose document/window/performance/
// setInterval ARE the stubs, so what executes is the shipped code verbatim.
const factory = new Function('document', 'window', 'performance', 'setInterval', 'clearInterval',
    slice + '\nreturn { onVisibility, visibilityInterval, makeModVisibilityApi };');
const api = factory(documentStub, windowStub, performanceStub, setIntervalStub, clearIntervalStub);

let pass = 0, fail = 0;
function ok(cond, label) {
    if (cond) { pass++; console.log('PASS: ' + label); }
    else { fail++; console.log('FAIL: ' + label); }
}

// S1: initial-load pageshow does NOT fire subscribers (page never hidden)
let s1 = 0; const off1 = api.onVisibility(() => s1++);
fire('win', 'pageshow');
ok(s1 === 0, 'S1 initial-load pageshow is a no-op');

// S2: plain hidden->visible transition fires exactly once
setVisibility('hidden'); setVisibility('visible');
ok(s1 === 1, 'S2 hidden->visible fires once');

// S3: bfcache restore (pagehide, then pageshow AND visibilitychange) fires
// exactly once -- _wasHidden is consumed by whichever dispatch lands first.
fire('win', 'pagehide'); documentStub.visibilityState = 'visible';
fire('win', 'pageshow'); fire('doc', 'visibilitychange');
ok(s1 === 2, 'S3 bfcache restore fires once, not twice');
off1();

// S4: visible cadence -- tick at ~ms runs fn
let runs = 0; const h = api.visibilityInterval(() => runs++, 1000);
const tid = nextId - 1;
now += 1000; fireTick(tid);
ok(runs === 1, 'S4 visible tick runs fn');

// S5: hidden ticks below the slow cadence set missed, do not run
setVisibility('hidden');
for (let i = 0; i < 5; i++) { now += 1000; fireTick(tid); }
ok(runs === 1, 'S5 hidden ticks below 10x cadence skip fn');

// S6: resume with missed runs exactly once; the immediately-due queued tick
// right behind the catch-up is swallowed by the half-interval floor.
setVisibility('visible');
ok(runs === 2, 'S6a resume catch-up ran fn once');
fireTick(tid); // already-queued tick right behind the catch-up (elapsed ~0)
ok(runs === 2, 'S6b back-to-back tick swallowed by half-interval floor');

// S7: hidden run at the slow cadence (>= 10x) fires without waiting for
// the page to come back.
setVisibility('hidden');
now += 10000; fireTick(tid);
ok(runs === 3, 'S7 hidden slow-cadence run fires at 10x');

// S8: frozen-timer resume -- hidden, NO ticks ever fire (bfcache/throttled
// tab), restore => due-based catch-up; missed alone would have said no.
now += 3000; // > ms, but no tick observed => missed stays false
setVisibility('visible');
ok(runs === 4, 'S8 frozen-timer resume catches up without a missed flag');

// S9: stop() unregisters -- later transitions do not run fn, and the
// underlying interval is gone.
h.stop();
setVisibility('hidden'); now += 50000; setVisibility('visible');
ok(runs === 4, 'S9 stop() severs the visibility hook');
ok(!intervals.has(tid), 'S9b stop() cleared the interval');

// S10: mod API -- a manual stop removes its own rec.unloads record
const rec = { unloads: [], unloading: false };
const mod = api.makeModVisibilityApi(rec);
const mh = mod.pausableInterval(() => {}, 500);
ok(rec.unloads.length === 1, 'S10a create registers one unload record');
mh.stop();
ok(rec.unloads.length === 0, 'S10b manual stop removes its record');
mh.stop(); // double-stop harmless
ok(rec.unloads.length === 0, 'S10c double-stop idempotent');

// S11: creates after unload began fail closed
rec.unloading = true;
const dead = mod.pausableInterval(() => { throw new Error('must never run'); }, 500);
ok(rec.unloads.length === 0, 'S11a post-unload create registers nothing');
ok(typeof dead.stop === 'function', 'S11b dead handle still has stop()');
ok(intervals.size === 0, 'S11c post-unload create started no timer');
// ...and the mod-facing onVisibility fails closed the same way: a callable
// unsubscribe comes back, but nothing is registered anywhere.
let deadVisRuns = 0;
const deadVis = mod.onVisibility(() => deadVisRuns++);
ok(rec.unloads.length === 0, 'S11d post-unload onVisibility registers nothing');
ok(typeof deadVis === 'function', 'S11e dead unsubscribe is still a function');
deadVis(); // calling the dead unsubscribe is harmless

// S12: callbacks stay quiet mid-teardown
rec.unloading = false;
let modRuns = 0;
mod.pausableInterval(() => modRuns++, 500);
const tid2 = nextId - 1;
rec.unloading = true;
now += 500; fireTick(tid2);
ok(modRuns === 0, 'S12 unloading guard silences the interval fn');

// S12b: the S11 dead subscription must STAY dead -- if it had secretly
// registered, this real hidden->visible transition (teardown over, the
// !rec.unloading guard open again) is exactly what would fire it.
rec.unloading = false;
setVisibility('hidden'); setVisibility('visible');
ok(deadVisRuns === 0, 'S12b dead subscription stayed dead after teardown ended');

console.log(pass + ' passed, ' + fail + ' failed');
process.exit(fail ? 1 : 0);
"""


@pytest.fixture(scope="module")
def scenario_run(tmp_path_factory):
    src = SRC_JS.read_text(encoding="utf-8")
    # FAIL, never skip: without the marker the slice is unmoored from the
    # live source and every green assert below would grade a stale copy.
    assert MARKER in src, (
        f"{MARKER!r} marker missing from {SRC_JS} -- the visibility-timer "
        "slice boundary moved; restore the marker or retarget this test")
    harness = tmp_path_factory.mktemp("visibility-timers") / "harness.js"
    harness.write_text(
        _HARNESS.replace("__SRC_PATH__", json.dumps(str(SRC_JS))),
        encoding="utf-8")
    # encoding pinned: node writes UTF-8, which the Windows console codepage
    # would otherwise mangle in a failure label.
    proc = subprocess.run([NODE, str(harness)],
                          capture_output=True, text=True, encoding="utf-8",
                          timeout=120)
    lines = (proc.stdout or "").strip().splitlines()
    tail = re.match(r"^(\d+) passed, (\d+) failed$",
                    lines[-1] if lines else "")
    assert tail is not None, (
        "harness produced no machine-readable tail (node crashed?)\n"
        f"rc={proc.returncode}\nstdout: {proc.stdout}\nstderr: {proc.stderr}")
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


# ---- the dispatch gate ----------------------------------------------------

def test_initial_load_pageshow_is_a_no_op(scenario_run):
    # Every page load gets a 'pageshow'. Without the _wasHidden gate that
    # event would fire every subscriber at boot on a page nobody ever hid.
    _ok(scenario_run, "S1 initial-load pageshow is a no-op")


def test_exactly_one_dispatch_per_hidden_to_visible_transition(scenario_run):
    # The plain transition fires once...
    _ok(scenario_run, "S2 hidden->visible fires once")
    # ...and a bfcache restore delivers pagehide, then pageshow AND
    # visibilitychange in a browser-dependent order. The first dispatch
    # consumes _wasHidden, so the second event is a no-op, not a re-fire.
    _ok(scenario_run, "S3 bfcache restore fires once, not twice")


# ---- the hidden cadence ---------------------------------------------------

def test_hidden_ticks_slow_to_hidden_mult_times_the_interval(scenario_run):
    # A visible tick at ~ms runs fn...
    _ok(scenario_run, "S4 visible tick runs fn")
    # ...but hidden ticks keep arriving at the base ms and must NOT run fn
    # until ms*HIDDEN_MULT has elapsed -- they only set the missed flag,
    # whose observable effect is the exactly-once resume in S6a below.
    _ok(scenario_run, "S5 hidden ticks below 10x cadence skip fn")
    # A tab left hidden long enough still gets its slow-cadence run: at
    # >= ms*HIDDEN_MULT the hidden tick fires fn without waiting for focus.
    _ok(scenario_run, "S7 hidden slow-cadence run fires at 10x")


def test_resume_runs_once_and_the_queued_tick_is_swallowed(scenario_run):
    # N missed hidden ticks coalesce into exactly ONE catch-up run on
    # resume -- never a queue of N.
    _ok(scenario_run, "S6a resume catch-up ran fn once")
    # The interval tick already queued behind that catch-up arrives with
    # ~0 elapsed; the half-interval floor is what keeps it from running fn
    # twice back-to-back.
    _ok(scenario_run, "S6b back-to-back tick swallowed by half-interval floor")


def test_a_frozen_timer_still_catches_up_on_elapsed_time(scenario_run):
    # bfcache and aggressive throttling freeze the interval outright: no
    # hidden tick ever fires, so `missed` stays false. The resume hook's
    # elapsed-time check (now - lastRun >= ms) is the only thing that can
    # notice, and it must.
    _ok(scenario_run, "S8 frozen-timer resume catches up without a missed flag")


def test_stop_clears_the_interval_and_the_visibility_hook(scenario_run):
    # stop() must sever BOTH registrations: a surviving interval keeps
    # ticking forever, and a surviving visibility hook fires a dead
    # callback on every future resume.
    _ok(scenario_run, "S9 stop() severs the visibility hook")
    _ok(scenario_run, "S9b stop() cleared the interval")


# ---- the mod-facing wrapper -----------------------------------------------

def test_a_manual_stop_removes_its_own_unload_record(scenario_run):
    # A churny mod (one timer per window) would otherwise grow rec.unloads
    # for its whole life; the record must die with the timer it guards.
    _ok(scenario_run, "S10a create registers one unload record")
    _ok(scenario_run, "S10b manual stop removes its record")
    _ok(scenario_run, "S10c double-stop idempotent")


def test_creates_after_unload_began_are_inert(scenario_run):
    # A surviving async continuation that calls pausableInterval after
    # teardown began would otherwise leave a permanent timer whose cleanup
    # can never drain. Fail closed: a usable handle, but nothing real.
    _ok(scenario_run, "S11a post-unload create registers nothing")
    _ok(scenario_run, "S11b dead handle still has stop()")
    _ok(scenario_run, "S11c post-unload create started no timer")
    # ...and the mod-facing onVisibility fails closed identically.
    _ok(scenario_run, "S11d post-unload onVisibility registers nothing")
    _ok(scenario_run, "S11e dead unsubscribe is still a function")
    # The proof it registered nowhere: a real transition after teardown
    # ends (guard open again) still does not fire it.
    _ok(scenario_run,
        "S12b dead subscription stayed dead after teardown ended")


def test_a_mid_teardown_interval_callback_is_silenced(scenario_run):
    # Between rec.unloading = true and the unloads drain, a queued tick can
    # still fire; the !rec.unloading guard is what keeps a half-dead mod
    # from running.
    _ok(scenario_run, "S12 unloading guard silences the interval fn")


# ---- the catch-all --------------------------------------------------------

def test_every_scenario_passed_and_node_exited_clean(scenario_run):
    # A label renamed in the harness would orphan one of the tests above
    # (its _ok would fail as "never ran"); the pinned count and the
    # zero-failures tail close the remaining gap, and the exit code proves
    # the harness itself agreed.
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
