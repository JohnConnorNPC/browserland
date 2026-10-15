"""#254: the Publish dialog shows a from-server host greyed out and unticked.

The dialog is outside the executed model range (it builds DOM), so a string
pin could only prove the note's text exists somewhere -- it would stay green
on a row that is still ticked, still tickable, or never gets the note. This
file runs the REAL ``mkRowCheck`` and ``openPublishDialog`` in node against a
small fake DOM and reads the rendered rows back, then hands what the dialog
passes on to the real ``buildValue``.

The stub browser and the model range are imported from
``test_host_registry_crypto`` (module level there is constants and source
text only), so there is no second copy of either to drift.
"""

import json
import subprocess

import pytest

from tests.test_host_registry_crypto import (
    _HARNESS as _CRYPTO_HARNESS, MOD_JS, NODE, _model_source)

pytestmark = pytest.mark.skipif(NODE is None, reason="node not installed")

CSS = MOD_JS.with_name("host-registry.css")
NOTE = "from server: not published"


def _between(src, start, end):
    assert src.count(start) == 1, f"{start!r} is not unique"
    assert src.count(end) == 1, f"{end!r} is not unique"
    a = src.index(start)
    b = src.index(end, a)
    return src[a:b]


def _dialog_source():
    src = MOD_JS.read_text(encoding="utf-8")
    return (_between(src, "function mkRowCheck(",
                     "// A real type=password field")
            + _between(src, "async function openPublishDialog() {",
                       "async function openPullDialog() {"))


_STUBS = _CRYPTO_HARNESS.split("__MODEL__")[0]

_DRIVER = r"""
// ---- fake DOM: className and classList share one string -----------------
function el(tag) {
    let cls = '';
    const e = { tagName: tag, children: [], textContent: '', title: '',
                type: '', checked: false, disabled: false, value: '',
                placeholder: '' };
    Object.defineProperty(e, 'className', {
        get() { return cls; }, set(v) { cls = String(v); } });
    e.classList = {
        add(...c) {
            const s = new Set(cls.split(/\s+/).filter(Boolean));
            c.forEach(x => s.add(x));
            cls = Array.from(s).join(' ');
        },
        contains(c) { return cls.split(/\s+/).indexOf(c) !== -1; },
    };
    e.appendChild = (c) => { e.children.push(c); return c; };
    return e;
}
globalThis.document = { createElement: el };

const TOKEN_P = 'TOKEN-PPP-proxied-peer-secret';
const FLEET = [
    { id: 'local', label: 'this broker', brokerId: 'BR-ME' },
    { id: 'd1', label: 'direct', url: 'https://one.example:4445', token: 'SECRET-D' },
    { id: 'pp', label: 'peer', url: 'http://192.168.1.50:4445', via: 'server',
      peerId: 'b', token: TOKEN_P },
];
globalThis.getHosts = () => FLEET;
globalThis.encPublishHint = () => 'hint';

let FORCE = false, DIALOG = null, CAPTURED = null;
globalThis.openDialog = async (o) => {
    const c = el('div');
    o.body(c);
    DIALOG = c;
    if (FORCE) {
        // A programmatic tick (defence in depth): a disabled checkbox can
        // still be set from script, so the model rule must hold on its own.
        const walk = (n) => { if (n.type === 'checkbox') n.checked = true;
                              n.children.forEach(walk); };
        walk(c);
    }
    return { value: 'publish' };
};
doPublish = async (...args) => { CAPTURED = args; };

function rowsOf(c) {
    const list = c.children.find(n => n.className === 'hostreg-list');
    return list.children.map(r => ({
        text: r.children[1].textContent,
        checked: r.children[0].checked,
        disabled: r.children[0].disabled,
        off: r.classList.contains('hostreg-off'),
        title: r.title,
        tags: r.children.filter(n => n.className === 'hostreg-tag')
                        .map(n => n.textContent),
    }));
}

(async () => {
    FORCE = process.argv[2] === 'forced';
    await openPublishDialog();
    const sel = CAPTURED[0];
    const v = buildValue(sel, true, 'https://me.example:4445');
    console.log(JSON.stringify({
        intro: DIALOG.children[0].textContent,
        rows: rowsOf(DIALOG),
        passed: sel.map(s => [s.host.id, s.checked]),
        ids: v.hosts.map(h => h.id),
        wire: JSON.stringify(v),
    }));
})().catch((e) => { console.error(e && e.stack || String(e)); process.exit(3); });
"""


@pytest.fixture(scope="module")
def harness(tmp_path_factory):
    path = tmp_path_factory.mktemp("hostreg-dialog") / "harness.js"
    path.write_text(_STUBS + _model_source() + _dialog_source() + _DRIVER,
                    encoding="utf-8")
    return path


def run(harness, mode):
    # utf-8, not the locale codepage: row labels carry an em dash.
    proc = subprocess.run([NODE, str(harness), mode], capture_output=True,
                          text=True, encoding="utf-8", timeout=120)
    assert proc.returncode == 0, (
        f"{mode} failed (rc={proc.returncode})\n"
        f"stdout: {proc.stdout}\nstderr: {proc.stderr}")
    return json.loads(proc.stdout.strip().splitlines()[-1])


def test_a_from_server_row_is_greyed_unticked_and_says_why(harness):
    r = run(harness, "plain")
    pp = r["rows"][2]
    assert pp["text"] == "peer — http://192.168.1.50:4445"
    assert pp["checked"] is False and pp["disabled"] is True
    assert pp["off"] is True
    assert pp["tags"] == [NOTE]
    assert "through this broker" in pp["title"]
    # ...and that is what the dialog hands on.
    assert r["passed"] == [["local", True], ["d1", True], ["pp", False]]


def test_direct_rows_are_untouched(harness):
    r = run(harness, "plain")
    for row in r["rows"][:2]:
        assert row["checked"] is True and row["disabled"] is False
        assert row["off"] is False and row["tags"] == [] and row["title"] == ""
    assert [row["text"] for row in r["rows"][:2]] == [
        "this broker", "direct — https://one.example:4445"]


def test_the_intro_names_from_server_hosts_not_proxying(harness):
    intro = run(harness, "plain")["intro"]
    assert "never proxies" not in intro
    assert 'Hosts marked "from server" are not published.' in intro


def test_a_forced_tick_still_publishes_no_from_server_host(harness):
    """Defence in depth: tick every box from script, the disabled one
    included. The model rule (buildValue) still leaves it out."""
    r = run(harness, "forced")
    assert ["pp", True] in r["passed"], "the forced tick did not take"
    assert r["ids"] == ["BR-ME", "d1"]
    assert "TOKEN-PPP-proxied-peer-secret" not in r["wire"]


def test_the_greyed_row_is_styled_dim():
    css = CSS.read_text(encoding="utf-8")
    rule = css[css.index(".hostreg-off"):]
    rule = rule[:rule.index("}")]
    assert "color: var(--fg-dim)" in rule
