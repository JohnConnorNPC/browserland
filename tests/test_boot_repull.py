"""Behavioural test for #248 B3: ``bootActiveView`` re-pulls ``/state``.

``bootActiveView`` (84) runs on the page's FIRST home ``{active:true}``, which
can come long after load: a page that loaded behind the "Become active"
overlay and was activated later (the takeover device in #248) restored from
the ``/state`` it pulled AT LOAD (85 ``initStateSync``), so it reopened a stale
``openTerms`` -- w3 saw the phone take over and show no terminals. The fix
re-pulls after ``_stateReadyPromise``, like ``rebuildView``, with the epoch
re-check across the await.

Runs the SHIPPED function in node: ``bootActiveView`` is cut at its own
boundaries and evaluated in a ``with (env)`` scope whose names are recorders.
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
SRC_JS = BROKER_DIR / "84_js_active_view_lifecycle.js"
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="node not installed")

START = "        async function bootActiveView() {"
END = "        // Lost the HOME lease: dispose THIS browser's entire view"

EXPECTED_ASSERTS = 6

_HARNESS = r"""
'use strict';
const fs = require('fs');
const s = fs.readFileSync(__SRC__, 'utf8');
const a = s.indexOf(__START__), b = s.indexOf(__END__);
if (a < 0 || b < a) throw new Error('boundary missing');
const fnSrc = s.slice(a, b);

let pass = 0, fail = 0;
function ok(cond, label) {
    if (cond) { pass++; console.log('PASS: ' + label); }
    else { fail++; console.log('FAIL: ' + label); }
}

function mkWorld(pullImpl) {
    const w = { log: [], pulls: [], seenLayout: null,
                local: { openTerms: ['local:1'] } };          // the LOAD blob
    const env = {
        _booted: false, _deactivated: true, _viewEpoch: 0,
        _stateReadyPromise: Promise.resolve(),
        hideBecomeActiveOverlay: function () {},
        document: { getElementById: function () { return { style: {} }; } },
        isDetachedSurface: function () { return false; },
        bootDetachedView: function () {},
        params: new URLSearchParams(''),
        pendingOpens: new Map(), AUTO_OPEN_TIMEOUT_MS: 1000,
        startFastPoll: function () {},
        restoreAppWindows: function () { w.log.push('restoreApps'); },
        seedRestoreQueue: function () {
            w.log.push('seed'); w.seenLayout = w.local.openTerms.slice();
        },
        refreshTaskbar: function () {}, startSlowPoll: function () {},
        pruneDetachedSet: function () {},
        pullState: function (force) {
            w.pulls.push(force);
            return pullImpl(w, env);
        },
    };
    const factory = new Function('env',
        'with (env) {\n' + fnSrc + '\nvar __f = bootActiveView;\n}\nreturn __f;\n');
    w.boot = factory(env);
    w.env = env;
    return w;
}

(async function () {
    // A newer /state was written while this page sat behind the overlay.
    let w = mkWorld(async function (w) { w.local.openTerms = ['local:1', 'local:7']; });
    await w.boot();
    ok(w.pulls.length === 1 && w.pulls[0] === true,
        'R1 the first activation re-pulls /state (forced) after the load pull');
    ok(JSON.stringify(w.seenLayout) === JSON.stringify(['local:1', 'local:7']),
        'R2 the restore seeds from the NEWER state, not the load-time one');

    w = mkWorld(async function (w, env) { env._deactivated = true; });
    await w.boot();
    ok(w.log.indexOf('seed') === -1 && w.log.indexOf('restoreApps') === -1,
        'R3 a lease loss during the re-pull aborts the restore');

    w = mkWorld(async function (w, env) { env._viewEpoch++; });
    await w.boot();
    ok(w.log.indexOf('seed') === -1,
        'R4 a newer boot/rebuild during the re-pull supersedes this one');

    w = mkWorld(async function () { throw new Error('offline'); });
    await w.boot();
    ok(JSON.stringify(w.seenLayout) === JSON.stringify(['local:1']),
        'R5 a failed re-pull still restores, from the load-time state');
    ok(w.log.indexOf('restoreApps') !== -1 && w.log.indexOf('restoreApps') < w.log.indexOf('seed'),
        'R6 the restore order is unchanged (app windows, then the queue)');

    console.log(pass + ' passed, ' + fail + ' failed');
})().catch(function (e) { console.log('CRASH ' + (e && e.stack)); process.exit(1); });
"""


@pytest.fixture(scope="module")
def run(tmp_path_factory):
    src = SRC_JS.read_text(encoding="utf-8")
    for m in (START, END):
        assert src.count(m) == 1, f"boundary {m!r} must occur exactly once"
    h = tmp_path_factory.mktemp("boot-repull") / "harness.js"
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
    "R1 the first activation re-pulls /state (forced) after the load pull",
    "R2 the restore seeds from the NEWER state, not the load-time one",
    "R3 a lease loss during the re-pull aborts the restore",
    "R4 a newer boot/rebuild during the re-pull supersedes this one",
    "R5 a failed re-pull still restores, from the load-time state",
    "R6 the restore order is unchanged (app windows, then the queue)",
])
def test_boot_repull(run, label):
    got = run["results"].get(label)
    assert got is True, (f"{label!r} {'failed' if got is False else 'never ran'}\n"
                         f"{run['stdout']}\n{run['stderr']}")


def test_every_scenario_passed_and_node_exited_clean(run):
    assert run["failed"] == 0, run["stdout"] + run["stderr"]
    assert run["passed"] == EXPECTED_ASSERTS, run["stdout"]
    assert run["rc"] == 0, run["stderr"]
