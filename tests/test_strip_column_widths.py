"""#259: two 1/2 columns must fit the strip exactly, so moving focus between
them never scrolls it.

``relayoutStrip`` used to round each column on its own (``Math.round(frac *
availW)``). With an odd available width both halves are ``k + .5`` and both
round up, so the columns ended 1px past ``strip.clientWidth``. Then
``scrollColumnIntoView`` revealed the right column (scrollLeft 0 -> 1) and the
left one (1 -> 0) on every focus change.

These tests run the SHIPPED ``columnPixelWidths`` helper from
61_js_resize_gutters.js in node, with the real ``presetFraction`` from
57_js_tiling_model.js. Skipped when node is absent.
"""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from webterm.broker import ui

BROKER_DIR = Path(ui.__file__).resolve().parent
SRC_61 = BROKER_DIR / "61_js_resize_gutters.js"
SRC_57 = BROKER_DIR / "57_js_tiling_model.js"
CSS_13 = BROKER_DIR / "13_css_tiling.css"
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="node not installed")

HELPER_START = "        function colCurrentFrac(col) {"
HELPER_END = "        // Live-render predicate SHARED"
PRESET_START = "        function presetFraction(p) {"

_HARNESS = r"""
'use strict';
const fs = require('fs');
const S61 = fs.readFileSync(__SRC61__, 'utf8');
const S57 = fs.readFileSync(__SRC57__, 'utf8');
function slice(s, a, b) {
    const i = s.indexOf(a), j = s.indexOf(b, i + 1);
    if (i < 0 || j < i) throw new Error('boundary missing: ' + a);
    return s.slice(i, j);
}
// presetFraction ends at the first 8-space-indented closing brace.
const presetSrc = (function () {
    const i = S57.indexOf(__PSTART__);
    const j = S57.indexOf('\n        }\n', i);
    if (i < 0 || j < i) throw new Error('presetFraction missing');
    return S57.slice(i, j + 10);
})();
let pass = 0, fail = 0;
function ok(cond, label) {
    if (cond) pass++;
    else { fail++; console.log('FAIL: ' + label); }
}

function helperCase() {
    const H = new Function(presetSrc + slice(S61, __HSTART__, __HEND__) +
        '\nreturn { colCurrentFrac, columnPixelWidths, stripAvailWidth,' +
        ' STRIP_GUTTER_W, presetFraction };')();
    const P = H.presetFraction;
    // The oracle accumulates the prefix sum left to right, as the helper
    // does, so a float tie can't round differently on the two sides.
    function prefix(fracs) {
        const out = []; let s = 0;
        for (const f of fracs) { s += f; out.push(s); }
        return out;
    }
    const filling = {
        '2x1/2': ['1/2', '1/2'].map(P),
        '3x1/3': ['1/3', '1/3', '1/3'].map(P),
        '1/3+2/3': ['1/3', '2/3'].map(P),
        '2/3+1/3': ['2/3', '1/3'].map(P),
        '1/2+1/4+1/4': [0.5, 0.25, 0.25],
        '4x1/4': [0.25, 0.25, 0.25, 0.25],
    };
    const nonFilling = {
        '2x1/3': ['1/3', '1/3'].map(P),
        '1/2+1/3': ['1/2', '1/3'].map(P),
        '1x1/2': ['1/2'].map(P),
        '1x1/3': ['1/3'].map(P),
        '1xmax': ['max'].map(P),
    };
    for (let W = 1000; W <= 2560; W++) {
        for (const name in filling) {
            const f = filling[name];
            const w = H.columnPixelWidths(f, W);
            const A = H.stripAvailWidth(W, f.length);
            const sum = w.reduce((a, b) => a + b, 0);
            // C1a: the columns plus their gutters fill the strip exactly.
            ok(sum + (f.length - 1) * H.STRIP_GUTTER_W === W,
               'C1a ' + name + ' W=' + W + ' widths ' + w.join('+') +
               ' + gutters = ' + (sum + (f.length - 1) * H.STRIP_GUTTER_W));
            // C1b: each width within 1px of its exact share.
            w.forEach((wi, i) => ok(Math.abs(wi - f[i] * A) <= 1 + 1e-9,
               'C1b ' + name + ' W=' + W + ' col ' + i + ' width ' + wi +
               ' vs exact ' + (f[i] * A).toFixed(3)));
            ok(w.length === f.length, 'C1a ' + name + ' W=' + W + ' length');
        }
        for (const name in nonFilling) {
            const f = nonFilling[name];
            const w = H.columnPixelWidths(f, W);
            const A = H.stripAvailWidth(W, f.length);
            const sum = w.reduce((a, b) => a + b, 0);
            const pre = prefix(f);
            // C1c: a set that doesn't fill sums to round(availW * its total);
            // a lone column is today's round(frac * stripW).
            ok(sum === Math.round(A * pre[pre.length - 1]),
               'C1c ' + name + ' W=' + W + ' sum ' + sum);
            if (f.length === 1) {
                ok(w[0] === Math.round(f[0] * W),
                   'C1c ' + name + ' W=' + W + ' lone width ' + w[0]);
            }
            w.forEach((wi, i) => ok(Math.abs(wi - f[i] * A) <= 1 + 1e-9,
               'C1b ' + name + ' W=' + W + ' col ' + i + ' width ' + wi));
        }
    }
    // C1d: only a finite positive widthFrac overrides the preset.
    for (const bad of [Infinity, -Infinity, NaN, -1, 0, '0.4', null, undefined]) {
        ok(H.colCurrentFrac({ widthFrac: bad, widthPreset: '1/3' }) === 1 / 3,
           'C1d widthFrac ' + String(bad) + ' falls back to the preset');
    }
    ok(H.colCurrentFrac({ widthFrac: 0.4, widthPreset: '1/3' }) === 0.4,
       'C1d widthFrac 0.4 overrides the preset');
    ok(H.colCurrentFrac({ widthPreset: '2/3' }) === 2 / 3,
       'C1d no widthFrac uses the preset');
    console.log(JSON.stringify({ gutter: H.STRIP_GUTTER_W }));
}

const CASES = { helper: helperCase };
CASES[process.argv[2]]();
console.log('COUNT ' + pass + ' ' + fail);
"""


def _run(tmp_path, case):
    h = tmp_path / "h.js"
    h.write_text(
        _HARNESS.replace("__SRC61__", json.dumps(str(SRC_61)))
        .replace("__SRC57__", json.dumps(str(SRC_57)))
        .replace("__PSTART__", json.dumps(PRESET_START))
        .replace("__HSTART__", json.dumps(HELPER_START))
        .replace("__HEND__", json.dumps(HELPER_END)),
        encoding="utf-8")
    r = subprocess.run([NODE, str(h), case], capture_output=True, text=True,
                       timeout=120)
    out = r.stdout
    assert r.returncode == 0, out + r.stderr
    m = re.search(r"^COUNT (\d+) (\d+)$", out, re.M)
    assert m, out + r.stderr
    fails = [ln for ln in out.splitlines() if ln.startswith("FAIL")]
    assert not fails, "\n".join(fails[:20]) + "\n(%d failures)" % len(fails)
    return out, int(m.group(1))


# Every width 1000..2560 for 6 filling sets (sum + per-column bound + length)
# and 5 non-filling sets, plus the 10 colCurrentFrac checks. Pinned so a
# sweep that silently shrinks fails.
_FILLING_COLS = (2, 3, 2, 2, 3, 4)      # sum + length + one per column
_NONFILLING_COLS = (2, 2, 1, 1, 1)      # sum + one per column (+ lone width)
HELPER_ASSERTS = 1561 * (
    sum(2 + n for n in _FILLING_COLS)
    + sum(1 + n + (n == 1) for n in _NONFILLING_COLS)) + 10


def test_helper_columns_fill_the_strip_exactly(tmp_path):
    out, n = _run(tmp_path, "helper")
    assert n == HELPER_ASSERTS, out[-400:]


def test_gutter_width_matches_the_css():
    """C1e: the JS gutter reserve is the width the CSS actually gives a
    .strip-col-gutter, or the exact fill is off by the difference."""
    css = CSS_13.read_text(encoding="utf-8")
    blocks = re.findall(r"\n\s*\.strip-col-gutter\s*\{([^}]*)\}", css)
    assert len(blocks) == 1, blocks
    m = re.search(r"\bflex:\s*0\s+0\s+(\d+)px\s*;", blocks[0])
    assert m, blocks[0]
    js = SRC_61.read_text(encoding="utf-8")
    jm = re.findall(r"const STRIP_GUTTER_W = (\d+);", js)
    assert len(jm) >= 1, "STRIP_GUTTER_W missing"
    assert set(jm) == {m.group(1)}, (jm, m.group(1))
