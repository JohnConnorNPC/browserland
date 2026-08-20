"""Behavioural tests for the shipped ``mods/clock/clock.js`` (#209).

clock is the reference mod, and until #209 it carried two pieces of
defensive code that existed only because the platform used to be brittle:
a ``ctx.visibility ? … : setInterval(…)`` fallback, and a ``new Set``
dedup over the engine's time-zone list, because a duplicate option value
used to throw out of ``_normChoiceOptions`` and roll the WHOLE mod back
(no chip at all). #198 made ``ctx.visibility`` a guarantee and #203 made a
malformed *suggestions* list cost the datalist and nothing else, so both
guards came out.

Deleting a guard is exactly the kind of change that a source-slice assert
cannot police: the interesting question is not "is the word gone" but
"does the mod still mount a chip, still tick once a second through the
pausable timer, still honour a pinned zone, and does it now hand its zone
list to the platform VERBATIM". So this executes the SHIPPED file -- read
from disk, never an embedded copy -- inside a node function scope whose
``document`` / ``Date`` / ``Intl`` / ``setInterval`` are stubs a scenario
drives by hand, and whose ``registerMod`` captures the definition so the
test can call ``init(ctx)`` against a recording ctx.

The harness prints one PASS/FAIL line per assert plus a machine-readable
``N passed, M failed`` tail and exits nonzero on any failure; the tests
below parse that, so a regression fails the pytest function that owns the
scenario with node's full output in the assertion message.

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
SRC_JS = BROKER_DIR / "mods" / "clock" / "clock.js"
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="node not installed")

# Every ok() the scenario script makes. Pinned so a scenario silently
# dropped from the harness cannot pass by absence.
EXPECTED_ASSERTS = 15

_HARNESS = r"""
'use strict';
// Stub the page, load the LIVE mod file, replay the scenarios, print one
// PASS/FAIL line per assert and a machine-readable tail.
const fs = require('fs');
const src = fs.readFileSync(__SRC_PATH__, 'utf8');

let pass = 0, fail = 0;
function ok(cond, label) {
    if (cond) { pass++; console.log('PASS: ' + label); }
    else { fail++; console.log('FAIL: ' + label); }
}

// ---- stubs --------------------------------------------------------------
// A hand-cranked page: elements are plain recorders, the timer only ticks
// when a scenario ticks it, and Date renders a label that ENCODES the
// timeZone it was handed, so an assert can see whether a pinned zone
// actually reached Intl instead of trusting that it did.
function makeDoc() {
    return {
        created: [],
        createElement: function (tag) {
            const el = {
                tag: tag, id: '', title: '', textContent: '',
                style: { cssText: '', display: '' },
            };
            this.created.push(el);
            return el;
        },
    };
}

// Zones this fake engine can format. Anything else throws, exactly as
// Intl.DateTimeFormat does for an unknown zone -- that is what clock's
// validate() and its render() fallback both lean on.
const KNOWN = ['UTC', 'Europe/London', 'Asia/Tokyo', 'America/New_York',
    'America/Los_Angeles', 'America/Denver', 'America/Chicago',
    'America/Sao_Paulo', 'Europe/Paris', 'Europe/Moscow', 'Asia/Dubai',
    'Asia/Kolkata', 'Asia/Shanghai', 'Australia/Sydney', 'Pacific/Auckland'];

function makeEnv(opts) {
    opts = opts || {};
    const zoneList = opts.zoneList;   // undefined -> no supportedValuesOf
    const IntlStub = {
        DateTimeFormat: function (loc, o) {
            const z = o && o.timeZone;
            if (z != null && KNOWN.indexOf(z) === -1) {
                throw new RangeError('Invalid time zone specified: ' + z);
            }
            return {};
        },
    };
    if (zoneList) IntlStub.supportedValuesOf = function () { return zoneList; };

    // A clock the scenario moves. The rendered text carries the tick count
    // and the zone, so 'did it repaint' and 'in which zone' are both
    // readable off chip.textContent.
    const clock = { tick: 0 };
    function DateStub() {
        const tick = clock.tick;
        function fmt(kind, loc, o) {
            const z = o && o.timeZone;
            if (z != null && KNOWN.indexOf(z) === -1) {
                throw new RangeError('Invalid time zone specified: ' + z);
            }
            return kind + '@' + tick + '[' + (z == null ? 'local' : z) + ']';
        }
        this.toLocaleDateString = function (loc, o) { return fmt('D', loc, o); };
        this.toLocaleTimeString = function (loc, o) { return fmt('T', loc, o); };
    }
    return { Intl: IntlStub, Date: DateStub, clock: clock };
}

// A recording ctx. Only what clock touches is present -- notably
// `visibility` is ALWAYS supplied, because #198 made it a guarantee and
// the whole point of this migration is that clock may now rely on it.
function makeCtx() {
    const rec = {
        statusItems: [], unloads: [], timers: [],
        textCalls: [], changeHandlers: [],
        stored: '',
    };
    rec.ctx = {
        taskbar: {
            addStatusItem: function (el) { rec.statusItems.push(el); },
        },
        visibility: {
            pausableInterval: function (fn, ms) {
                const t = { fn: fn, ms: ms, stopped: false };
                t.stop = function () { t.stopped = true; };
                rec.timers.push(t);
                return t;
            },
        },
        onUnload: function (fn) { rec.unloads.push(fn); },
        settings: {
            text: function (key, o) {
                rec.textCalls.push({ key: key, opts: o });
                const acc = {
                    ok: true,
                    get: function () { return rec.stored; },
                    set: function (v) { rec.stored = v; },
                    onChange: function (fn) { rec.changeHandlers.push(fn); },
                };
                return acc;
            },
        },
    };
    return rec;
}

// Load the shipped file into a scope whose globals are the stubs. A
// `setInterval` is threaded in ONLY so a scenario can prove the mod never
// reaches for it any more.
function load(env, rec) {
    let rawIntervals = 0;
    const factory = new Function(
        'registerMod', 'document', 'Intl', 'Date', 'setInterval', 'clearInterval',
        src + '\n');
    let def = null;
    factory(function (d) { def = d; }, rec.doc, env.Intl, env.Date,
        function () { rawIntervals++; return 1; },
        function () {});
    return { def: def, rawIntervals: function () { return rawIntervals; } };
}

function boot(opts) {
    opts = opts || {};
    const env = makeEnv(opts);
    const rec = makeCtx();
    rec.doc = makeDoc();
    if (opts.stored) rec.stored = opts.stored;
    const loaded = load(env, rec);
    loaded.def.init(rec.ctx);
    return { env: env, rec: rec, loaded: loaded,
             chip: rec.statusItems[0], def: loaded.def };
}

// ---- S1: the chip mounts and ticks through ctx.visibility ---------------
{
    const b = boot({ zoneList: ['UTC', 'Europe/London'] });
    ok(b.chip && b.chip.id === 'clock-chip', 'S1 a chip is mounted on the taskbar');
    ok(b.chip.style.display === 'inline-flex', 'S1 the chip is shown');
    ok(b.chip.textContent === 'D@0[local]  T@0[local]',
        'S1 the first render is browser-local');
    ok(b.rec.timers.length === 1 && b.rec.timers[0].ms === 1000,
        'S1 exactly one 1s ticker, via ctx.visibility.pausableInterval');
    ok(b.loaded.rawIntervals() === 0,
        'S1 the raw setInterval fallback is gone (#198)');

    // The tick is what keeps the chip live; nothing else repaints it.
    b.env.clock.tick = 7;
    b.rec.timers[0].fn();
    ok(b.chip.textContent === 'D@7[local]  T@7[local]', 'S1 the ticker repaints');
}

// ---- S2: a pinned zone reaches Intl, and teardown stops the tick --------
{
    const b = boot({ zoneList: ['UTC', 'Asia/Tokyo'], stored: 'Asia/Tokyo' });
    ok(b.chip.textContent === 'D@0[Asia/Tokyo]  T@0[Asia/Tokyo]',
        'S2 a stored zone is threaded into the render');

    // A cross-browser /state convergence lands through onChange, and must
    // repaint immediately rather than waiting for the next second.
    b.env.clock.tick = 3;
    b.rec.changeHandlers[0]('Europe/London');
    ok(b.chip.textContent === 'D@3[Europe/London]  T@3[Europe/London]',
        'S2 onChange repaints in the new zone');

    b.rec.unloads.forEach(function (fn) { fn(); });
    ok(b.rec.timers[0].stopped === true, 'S2 onUnload stops the ticker');
}

// ---- S3: an unknown stored zone degrades to local, never freezes --------
// The value outlives the mod, so a zone stored by another browser (or a
// zone a tzdata update removed) can arrive here unformattable.
{
    const b = boot({ zoneList: ['UTC'], stored: 'Mars/Olympus' });
    ok(b.chip.textContent === 'D@0[local]  T@0[local]',
        'S3 an unformattable zone falls back to a local render');
    b.env.clock.tick = 2;
    b.rec.timers[0].fn();
    ok(b.chip.textContent === 'D@2[local]  T@2[local]',
        'S3 the tick survives an unformattable zone');
}

// ---- S4: the engine's zone list is handed over VERBATIM (#203/#209) -----
// This is the deleted dedup. clock no longer second-guesses the platform:
// for text(), a duplicate in an OPTIONAL suggestions list costs the
// datalist and earns a warning, and the accessor stays healthy -- so the
// mod passes what the engine said and lets the platform judge it.
{
    const dup = ['UTC', 'UTC', 'Europe/London'];
    const b = boot({ zoneList: dup });
    const call = b.rec.textCalls[0];
    ok(call && call.key === 'clockTz', 'S4 the clockTz key is still owned here');
    ok(call.opts.options.map(function (o) { return o.value; }).join(',')
        === dup.join(','),
        'S4 a duplicate-bearing engine list is passed through unchanged');
    // And the mod is still alive around it -- the guard was removed, not
    // traded for a new failure.
    ok(b.chip && b.chip.style.display === 'inline-flex' && b.rec.timers.length === 1,
        'S4 the chip still mounts and ticks with a duplicate in the list');
}

// ---- S5: no supportedValuesOf -> the curated fallback still works -------
{
    const b = boot({});   // engine cannot enumerate zones
    const opts = b.rec.textCalls[0].opts.options;
    ok(opts.length > 1 && opts.some(function (o) { return o.value === 'UTC'; }),
        'S5 the curated fallback list is offered when the engine cannot enumerate');
}

console.log(pass + ' passed, ' + fail + ' failed');
process.exit(fail ? 1 : 0);
"""


def _run():
    script = _HARNESS.replace("__SRC_PATH__", json.dumps(str(SRC_JS)))
    return subprocess.run(
        [NODE, "-e", script], capture_output=True, text=True, timeout=60
    )


@pytest.fixture(scope="module")
def result():
    assert SRC_JS.is_file(), f"missing shipped mod file: {SRC_JS}"
    return _run()


def _tail(out):
    m = re.search(r"(\d+) passed, (\d+) failed", out)
    assert m, f"harness produced no summary:\n{out}"
    return int(m.group(1)), int(m.group(2))


def _assert_scenario(result, prefix):
    """Fail on any FAIL line belonging to one scenario."""
    bad = [ln for ln in result.stdout.splitlines()
           if ln.startswith("FAIL: " + prefix)]
    assert not bad, (
        "clock scenario " + prefix + " failed:\n" + result.stdout + result.stderr
    )


def test_chip_mounts_and_ticks_through_ctx_visibility(result):
    _assert_scenario(result, "S1")


def test_pinned_zone_and_teardown(result):
    _assert_scenario(result, "S2")


def test_unknown_zone_degrades_to_local(result):
    _assert_scenario(result, "S3")


def test_zone_list_passed_through_verbatim(result):
    _assert_scenario(result, "S4")


def test_curated_fallback_without_supported_values_of(result):
    _assert_scenario(result, "S5")


def test_every_scenario_ran(result):
    passed, failed = _tail(result.stdout)
    assert passed + failed == EXPECTED_ASSERTS, (
        f"expected {EXPECTED_ASSERTS} asserts, saw {passed + failed}; "
        "update EXPECTED_ASSERTS deliberately when adding a scenario:\n"
        + result.stdout
    )
    assert failed == 0, result.stdout + result.stderr
    assert result.returncode == 0, result.stdout + result.stderr
