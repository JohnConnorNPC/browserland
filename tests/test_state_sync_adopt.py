"""Behavioural tests for ``pullState``'s dirty skip in
``webterm/broker/52_js_state_sync.js`` (#246).

A background ``pullState(false)`` keeps an unpushed local edit instead of
adopting newer ``/state`` -- correct while a push is on its way (it rebases
onto the server's rev and wins), but a PERMANENT freeze when nothing is
queued: the rev is taken on the skip, so no later pull retries, and the next
unrelated ``savePrefs()`` pushes the stale layout over the newer one (the
workspaces mod's ``syncFocusedCol`` dirtied the layout that way). The fix
honours the skip only while ``_statePushPending()`` is true.

Those are ordering facts across timers, an in-flight PUT and a poll, so --
like ``tests/test_desktop_rpc.py`` -- this executes the SHIPPED fragment in
node: the whole of 52 plus ``savePrefs`` cut from 51 at its own boundaries,
evaluated inside a ``with (env)`` scope whose outer names (``prefs``,
``hostFetch`` against a scriptable in-memory broker, ``setTimeout`` as a
hand-cranked queue) are stubs. One PASS/FAIL line per assert plus a
machine-readable tail; the fixture FAILS if a boundary vanishes. Skipped when
node is absent.
"""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from webterm.broker import ui

BROKER_DIR = Path(ui.__file__).resolve().parent
SYNC_JS = BROKER_DIR / "52_js_state_sync.js"
PREFS_JS = BROKER_DIR / "51_js_prefs.js"
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="node not installed")

# savePrefs is cut from 51 between these two (each must be unique).
SAVE_START = "        function savePrefs() {"
SAVE_END = "        function getPref(id) {"

EXPECTED_ASSERTS = 43

_HARNESS = r"""
'use strict';
const fs = require('fs');
const sync = fs.readFileSync(__SYNC_PATH__, 'utf8');
const prefsSrc = fs.readFileSync(__PREFS_PATH__, 'utf8');
const a = prefsSrc.indexOf(__SAVE_START__), b = prefsSrc.indexOf(__SAVE_END__);
if (a < 0 || b < a) throw new Error('savePrefs boundary missing');
const savePrefsSrc = prefsSrc.slice(a, b);

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

const L0 = { mode: 'tiling', focusedCol: 2, columns: [
    { id: 'c1', rows: [] }, { id: 'c2', rows: [] }, { id: 'c3', rows: [] } ] };
// Another tab (or an MCP move_column landing there) reorders the columns.
const L1 = { mode: 'tiling', focusedCol: 0, columns: [
    { id: 'c2', rows: [] }, { id: 'c1', rows: [] }, { id: 'c3', rows: [] } ] };

// One world = a fresh evaluation of the shipped fragment against a fresh
// in-memory broker, so no `let` state leaks between scenarios.
function mkWorld() {
    const w = { server: { rev: 1, settings: {}, layout: clone(L0) },
                puts: [], gate: null, timers: new Map(), nextTimer: 1,
                relayouts: 0, localSaves: 0 };
    w.prefs = {};
    async function hostFetch(host, path, opts) {
        if (path !== '/state') throw new Error('unexpected path ' + path);
        if (!opts || !opts.method) {
            const snap = clone(w.server);
            return { ok: true, status: 200, json: async function () { return snap; } };
        }
        const body = JSON.parse(opts.body);
        w.puts.push(body);
        if (w.gate) await w.gate.promise;
        if (w.putFail === 'throw') throw new Error('offline');
        if (w.putFail === '409null') {
            return { ok: false, status: 409, json: async function () { return null; } };
        }
        if (/^\d+$/.test(w.putFail || '')) {     // 500, 401, 400, 413 ...
            const st = Number(w.putFail);
            return { ok: false, status: st, json: async function () { return {}; } };
        }
        if (body.baseRev !== w.server.rev) {
            const snap = clone(w.server);
            return { ok: false, status: 409, json: async function () { return snap; } };
        }
        w.server.rev++;
        w.server.settings = clone(body.settings);
        w.server.layout = clone(body.layout);
        const rev = w.server.rev;
        // The PUT landed but its response never arrives (connection dropped).
        if (w.putFail === 'commitThrow') throw new Error('response lost');
        return { ok: true, status: 200, json: async function () { return { rev: rev }; } };
    }
    const env = {
        prefs: w.prefs,
        isDetachedSurface: function () { return false; },
        hostFetch: hostFetch,
        localHost: function () { return 'local'; },
        CLIENT_ID: 'cid-test',
        FETCH_TIMEOUT_MS: 5000,
        getSettings: function () { return w.prefs._settings; },
        getLayout: function () { return w.prefs._layout; },
        savePrefsLocal: function () { w.localSaves++; },
        document: { body: { classList: { toggle: function () {} } } },
        isSizeLocked: function () { return false; },
        applyDisplaySettings: function () {},
        requestRelayout: function () { w.relayouts++; },
        refreshOpenSectionUIs: function () {},
        setTimeout: function (fn, ms) {
            const id = w.nextTimer++;
            w.timers.set(id, { fn: fn, ms: ms });
            return id;
        },
        clearTimeout: function (id) { w.timers.delete(id); },
    };
    Object.defineProperty(env, '_deactivated', { get: function () { return false; } });
    const factory = new Function('env',
        'with (env) {\n' + sync + '\n' + savePrefsSrc + '\n' +
        'var __api = { pullState: pullState, savePrefs: savePrefs,\n' +
        '  setReady: function (v) { _stateReady = v; },\n' +
        '  bumpEpoch: function () { _viewEpoch++; },\n' +
        '  st: function () { return { rev: _stateRev, timer: _statePushTimer,\n' +
        '    inFlight: _statePushInFlight, again: _statePushAgain,\n' +
        '    pending: _statePendingPush, last: _stateLastSerialized }; } };\n' +
        '}\nreturn __api;\n');
    w.api = factory(env);
    // Fire the debounced push timer(s) only (the pull's abort timer stays).
    w.firePush = async function () {
        for (const [id, t] of Array.from(w.timers)) {
            if (t.ms === 600) { w.timers.delete(id); t.fn(); }
        }
        await flush();
    };
    w.otherTabWrites = function (layout) {
        w.server.rev++;
        w.server.layout = clone(layout);
    };
    w.boot = async function () {
        await w.api.pullState(true);
        w.api.setReady(true);          // what initStateSync does next
    };
    return w;
}

(async function () {
    // ---- A: dirty with NOTHING pending adopts newer server state ---------
    let w = mkWorld();
    await w.boot();
    // syncFocusedCol's old shape: a synced write with no savePrefs().
    w.prefs._layout.focusedCol = 1;
    const s0 = w.api.st();
    ok(J({ settings: w.prefs._settings, layout: w.prefs._layout }) !== s0.last
        && !s0.timer && !s0.inFlight && !s0.again && !s0.pending,
        'A0 precondition: the tab is dirty and no push is pending');
    w.otherTabWrites(L1);
    const relBefore = w.relayouts;
    await w.api.pullState(false);
    ok(J(w.prefs._layout) === J(L1),
        'A1 dirty with nothing pending adopts the newer server layout');
    ok(w.relayouts === relBefore + 1, 'A2 the adopt re-renders (requestRelayout)');
    ok(w.puts.length === 0, 'A3 adopting pushes nothing');
    // The live-repro ordering: pullState takes the new rev BEFORE the dirty
    // guard, so a skip here would be final (every later poll is "unchanged").
    // The pull that takes rev 2 must be the one that adopts, leaving it clean.
    const sA = w.api.st();
    await w.api.pullState(false);
    ok(sA.rev === 2 && J({ settings: w.prefs._settings, layout: w.prefs._layout })
        === sA.last && J(w.prefs._layout) === J(L1),
        'A4 the pull that takes the new rev is the one that adopts (tab clean at rev 2)');

    // ---- C: after the adopt, the next save never pushes the stale layout -
    w.api.savePrefs();
    await w.firePush();
    ok(w.puts.length === 0, 'C1 an unchanged save after the adopt sends no PUT');
    w.prefs._settings.theme = 'dusk';
    w.api.savePrefs();
    await w.firePush();
    const lastPut = w.puts[w.puts.length - 1];
    ok(w.puts.length === 1 && lastPut && J(lastPut.layout) === J(L1),
        'C2 the next real save pushes the ADOPTED layout, not the stale one');
    ok(lastPut && lastPut.baseRev === 2 && w.server.rev === 3
        && J(w.server.layout) === J(L1),
        'C3 that push lands on the newer rev and /state keeps the other write');

    // ---- B1: dirty with a debounced push pending keeps the local edit -----
    w = mkWorld();
    await w.boot();
    w.prefs._layout.columns.push({ id: 'c4', rows: [] });
    w.api.savePrefs();
    ok(!!w.api.st().timer, 'B1 precondition: the debounced push is pending');
    w.otherTabWrites(L1);
    await w.api.pullState(false);
    ok(w.prefs._layout.columns.length === 4
        && w.prefs._layout.columns[3].id === 'c4'
        && w.prefs._layout.columns[0].id === 'c1',
        'B1a a pending debounced push keeps the local edit (no adopt)');
    await w.firePush();
    ok(w.puts.length === 1 && w.puts[0].baseRev === 2
        && w.puts[0].layout.columns.length === 4,
        'B1b the pending push rebases onto the server rev and carries the edit');

    // ---- B2: dirty with a push IN FLIGHT keeps the local edit -------------
    w = mkWorld();
    await w.boot();
    w.prefs._layout.columns.push({ id: 'c4', rows: [] });
    w.api.savePrefs();
    let release;
    w.gate = { promise: new Promise(function (r) { release = r; }) };
    await w.firePush();
    const s2 = w.api.st();
    ok(s2.inFlight && !s2.timer && !s2.pending,
        'B2 precondition: the push is in flight, nothing else queued');
    w.otherTabWrites(L1);
    await w.api.pullState(false);
    ok(w.prefs._layout.columns.length === 4
        && w.prefs._layout.columns[0].id === 'c1',
        'B2a a push in flight keeps the local edit (no adopt)');
    release();
    await flush();

    // ---- B3: dirty with a save held before the initial pull --------------
    w = mkWorld();
    await w.boot();
    w.api.setReady(false);
    w.prefs._layout.columns.push({ id: 'c4', rows: [] });
    w.api.savePrefs();
    const s3 = w.api.st();
    ok(s3.pending && !s3.timer && !s3.inFlight,
        'B3 precondition: only _statePendingPush is set');
    w.otherTabWrites(L1);
    await w.api.pullState(false);
    ok(w.prefs._layout.columns.length === 4
        && w.prefs._layout.columns[0].id === 'c1',
        'B3a a held pre-ready push keeps the local edit (no adopt)');

    // ---- E: a FAILED push still owes the edit ("next save retries") -----
    // A helper world: boot, make a real edit, save, and let its push fail.
    async function failedEdit(mode) {
        const fw = mkWorld();
        await fw.boot();
        fw.prefs._layout.columns.push({ id: 'c4', rows: [] });
        fw.api.savePrefs();
        fw.putFail = mode;
        await fw.firePush();
        fw.putFail = null;
        return fw;
    }
    function keptEdit(fw) {
        return fw.prefs._layout.columns.length === 4
            && fw.prefs._layout.columns[0].id === 'c1';
    }
    for (const mode of ['500', '401', 'throw']) {
        w = await failedEdit(mode);
        const s4 = w.api.st();
        ok(w.puts.length === 1 && !s4.timer && !s4.inFlight && !s4.again
            && !s4.pending && w.server.rev === 1,
            'E0 precondition (' + mode + '): the push failed, nothing re-queued');
        w.otherTabWrites(L1);
        await w.api.pullState(false);
        ok(keptEdit(w), 'E1 (' + mode + ') a failed push keeps the local edit (no adopt)');
        w.api.savePrefs();
        await w.firePush();
        ok(w.puts.length === 2 && w.puts[1].baseRev === 2
            && w.puts[1].layout.columns.length === 4 && w.server.rev === 3,
            'E2 (' + mode + ') the next save retries the failed edit on the rebased rev');
    }
    w.prefs._layout.focusedCol = 1;                // dirty, nothing pending
    w.otherTabWrites(L1);
    await w.api.pullState(false);
    ok(J(w.prefs._layout) === J(L1),
        'E2b once the retry lands, unsaved dirt adopts again');
    // An adopt supersedes the failed edit: the owed flag clears with it.
    w = await failedEdit('500');
    await w.api.pullState(true);                   // a rebuild's re-adopt
    w.prefs._layout.focusedCol = 1;                // dirty, nothing pending
    w.otherTabWrites(L1);
    await w.api.pullState(false);
    ok(J(w.prefs._layout) === J(L1),
        'E3 after an adopt supersedes a failed edit, unsaved dirt adopts again');

    // ---- G: the owed flag never outlives what it owes (codex round 2) -----
    // G1: the PUT landed, its response was lost. The next pull finds the
    // server holding exactly this tab's state: acknowledged, nothing owed.
    w = await failedEdit('commitThrow');
    ok(w.server.rev === 2 && w.server.layout.columns.length === 4,
        'G1 precondition: the PUT landed although the client saw it fail');
    await w.api.pullState(false);
    ok(J({ settings: w.prefs._settings, layout: w.prefs._layout })
        === w.api.st().last && w.api.st().rev === 2,
        'G1a a pull finding the server equal to the tab records it as the baseline');
    w.prefs._layout.focusedCol = 1;
    const L1c4 = clone(L1);
    L1c4.columns.push({ id: 'c4', rows: [] });
    w.otherTabWrites(L1c4);
    await w.api.pullState(false);
    ok(J(w.prefs._layout) === J(L1c4),
        'G1b after the lost-response PUT is acknowledged, unsaved dirt adopts again');
    // G2: the failed edit is undone and saved: the push finds nothing to send.
    w = await failedEdit('500');
    w.prefs._layout.columns.pop();
    w.api.savePrefs();
    await w.firePush();
    ok(w.puts.length === 1, 'G2 precondition: the undo sends no PUT');
    w.prefs._layout.focusedCol = 1;
    w.otherTabWrites(L1);
    await w.api.pullState(false);
    ok(J(w.prefs._layout) === J(L1), 'G2a an undone failed edit owes nothing (adopts again)');
    // G3: a PUT that outlives its view (teardown -> rebuild) never sets it.
    for (const mode of ['500', 'throw']) {
        w = mkWorld();
        await w.boot();
        w.prefs._layout.columns.push({ id: 'c4', rows: [] });
        w.api.savePrefs();
        let releaseG3;
        w.gate = { promise: new Promise(function (r) { releaseG3 = r; }) };
        w.putFail = mode;
        await w.firePush();
        w.api.bumpEpoch();                         // the lease flip
        await w.api.pullState(true);               // the rebuild's re-adopt
        releaseG3();
        await flush();
        w.gate = null; w.putFail = null;
        ok(w.prefs._layout.columns.length === 3 && !w.api.st().inFlight,
            'G3 precondition (' + mode + '): the rebuild adopted and the stale PUT has failed');
        w.prefs._layout.focusedCol = 1;
        w.otherTabWrites(L1);
        await w.api.pullState(false);
        ok(J(w.prefs._layout) === J(L1),
            'G3a (' + mode + ') a failed PUT from a superseded view owes nothing');
    }
    // G4: a 400/413 is a verdict on the payload: it can never land.
    for (const mode of ['400', '413']) {
        w = await failedEdit(mode);
        w.otherTabWrites(L1);
        await w.api.pullState(false);
        ok(J(w.prefs._layout) === J(L1),
            'G4 (' + mode + ') an edit the broker refuses outright is not owed');
    }
    // G5: a 409 whose body is unusable neither adopts nor re-queues: owed.
    w = await failedEdit('409null');
    w.otherTabWrites(L1);
    await w.api.pullState(false);
    ok(keptEdit(w), 'G5 an unusable 409 body keeps the edit owed (no adopt)');

    // G6: an older failure, then a retry the broker refuses outright: the
    // refusal settles the debt too (codex round 3).
    w = await failedEdit('500');
    w.api.savePrefs();
    w.putFail = '413';
    await w.firePush();
    w.putFail = null;
    ok(w.puts.length === 2, 'G6 precondition: the retry was refused with 413');
    w.otherTabWrites(L1);
    await w.api.pullState(false);
    ok(J(w.prefs._layout) === J(L1), 'G6a a refused retry clears the older debt');
    // G7: the failed edit is undone WITHOUT a save; a poll sees the tab back
    // at its baseline and drops the debt before any later unsaved dirt.
    w = await failedEdit('500');
    w.prefs._layout.columns.pop();
    await w.api.pullState(false);                  // same rev: a quiet poll
    w.prefs._layout.focusedCol = 1;                // later dirt, never saved
    w.otherTabWrites(L1);
    await w.api.pullState(false);
    ok(J(w.prefs._layout) === J(L1), 'G7 an unsaved undo owes nothing once a poll sees it');

    // ---- D: the unchanged paths ------------------------------------------
    w = mkWorld();
    await w.boot();
    w.prefs._layout.focusedCol = 1;               // dirty, nothing pending
    const relD = w.relayouts;
    await w.api.pullState(false);                  // server rev unchanged
    ok(w.prefs._layout.focusedCol === 1 && w.relayouts === relD,
        'D1 an unchanged server rev never re-adopts');
    w = mkWorld();
    await w.boot();
    w.otherTabWrites(L1);
    await w.api.pullState(false);
    ok(J(w.prefs._layout) === J(L1), 'D2 a clean tab adopts newer state');

    console.log(pass + ' passed, ' + fail + ' failed');
    process.exit(fail ? 1 : 0);
})().catch(function (e) {
    console.log('HARNESS ERROR: ' + (e && e.stack || e));
    console.log(pass + ' passed, ' + (fail + 1) + ' failed');
    process.exit(1);
});
"""


@pytest.fixture(scope="module")
def scenario_run(tmp_path_factory):
    psrc = PREFS_JS.read_text(encoding="utf-8")
    for marker in (SAVE_START, SAVE_END):
        # FAIL, never skip: a moved boundary would grade a stale cut.
        assert psrc.count(marker) == 1, (
            f"{marker!r} occurs {psrc.count(marker)}x in {PREFS_JS}; the "
            "savePrefs cut is unmoored -- retarget this test")
    assert psrc.index(SAVE_START) < psrc.index(SAVE_END)
    harness = tmp_path_factory.mktemp("state-sync-adopt") / "harness.js"
    harness.write_text(
        _HARNESS
        .replace("__SYNC_PATH__", json.dumps(str(SYNC_JS)))
        .replace("__PREFS_PATH__", json.dumps(str(PREFS_JS)))
        .replace("__SAVE_START__", json.dumps(SAVE_START))
        .replace("__SAVE_END__", json.dumps(SAVE_END)),
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
        f"{label!r} {verdict}\nstdout: {run['stdout']}\n"
        f"stderr: {run['stderr']}")


def test_dirty_with_nothing_pending_adopts(scenario_run):
    # The #246 freeze: a synced write nobody saved must not block adoption.
    _ok(scenario_run, "A0 precondition: the tab is dirty and no push is pending")
    _ok(scenario_run, "A1 dirty with nothing pending adopts the newer server layout")
    _ok(scenario_run, "A2 the adopt re-renders (requestRelayout)")
    _ok(scenario_run, "A3 adopting pushes nothing")
    _ok(scenario_run, "A4 the pull that takes the new rev is the one that adopts (tab clean at rev 2)")


def test_next_save_after_adopt_never_reverts(scenario_run):
    # The visible half of #246: the stuck tab's next save reverted /state.
    _ok(scenario_run, "C1 an unchanged save after the adopt sends no PUT")
    _ok(scenario_run, "C2 the next real save pushes the ADOPTED layout, not the stale one")
    _ok(scenario_run, "C3 that push lands on the newer rev and /state keeps the other write")


def test_a_pending_push_still_wins(scenario_run):
    # The Codex lost-update guard stays: each pending-push leg keeps the edit.
    _ok(scenario_run, "B1 precondition: the debounced push is pending")
    _ok(scenario_run, "B1a a pending debounced push keeps the local edit (no adopt)")
    _ok(scenario_run, "B1b the pending push rebases onto the server rev and carries the edit")
    _ok(scenario_run, "B2 precondition: the push is in flight, nothing else queued")
    _ok(scenario_run, "B2a a push in flight keeps the local edit (no adopt)")
    _ok(scenario_run, "B3 precondition: only _statePendingPush is set")
    _ok(scenario_run, "B3a a held pre-ready push keeps the local edit (no adopt)")


def test_a_failed_push_still_owes_the_edit(scenario_run):
    # Codex review: "next save retries" is the failure contract, so a failed
    # push must not turn a real edit into adoptable dirt.
    for mode in ("500", "401", "throw"):
        _ok(scenario_run, f"E0 precondition ({mode}): the push failed, nothing re-queued")
        _ok(scenario_run, f"E1 ({mode}) a failed push keeps the local edit (no adopt)")
        _ok(scenario_run, f"E2 ({mode}) the next save retries the failed edit on the rebased rev")
    _ok(scenario_run, "E2b once the retry lands, unsaved dirt adopts again")
    _ok(scenario_run, "E3 after an adopt supersedes a failed edit, unsaved dirt adopts again")


def test_the_owed_flag_never_outlives_what_it_owes(scenario_run):
    # Codex review round 2: each way the flag could stay set with nothing
    # owed would recreate the #246 freeze.
    _ok(scenario_run, "G1 precondition: the PUT landed although the client saw it fail")
    _ok(scenario_run, "G1a a pull finding the server equal to the tab records it as the baseline")
    _ok(scenario_run, "G1b after the lost-response PUT is acknowledged, unsaved dirt adopts again")
    _ok(scenario_run, "G2 precondition: the undo sends no PUT")
    _ok(scenario_run, "G2a an undone failed edit owes nothing (adopts again)")
    for mode in ("500", "throw"):
        _ok(scenario_run, f"G3 precondition ({mode}): the rebuild adopted and the stale PUT has failed")
        _ok(scenario_run, f"G3a ({mode}) a failed PUT from a superseded view owes nothing")
    for mode in ("400", "413"):
        _ok(scenario_run, f"G4 ({mode}) an edit the broker refuses outright is not owed")
    _ok(scenario_run, "G5 an unusable 409 body keeps the edit owed (no adopt)")
    _ok(scenario_run, "G6 precondition: the retry was refused with 413")
    _ok(scenario_run, "G6a a refused retry clears the older debt")
    _ok(scenario_run, "G7 an unsaved undo owes nothing once a poll sees it")


def test_unchanged_paths(scenario_run):
    _ok(scenario_run, "D1 an unchanged server rev never re-adopts")
    _ok(scenario_run, "D2 a clean tab adopts newer state")


def test_every_scenario_passed_and_node_exited_clean(scenario_run):
    run = scenario_run
    assert run["failed"] == 0, (
        f"harness reported failures\nstdout: {run['stdout']}\n"
        f"stderr: {run['stderr']}")
    assert run["passed"] == EXPECTED_ASSERTS, (
        f"expected {EXPECTED_ASSERTS} asserts, harness ran {run['passed']} "
        f"-- scenario drift\nstdout: {run['stdout']}")
    assert len(run["results"]) == EXPECTED_ASSERTS
    assert run["rc"] == 0, (
        f"node exited {run['rc']}\nstdout: {run['stdout']}\n"
        f"stderr: {run['stderr']}")
