"""#254: the Publish dialog shows a from-server host greyed out and unticked.

The dialog is outside the executed model range (it builds DOM), so a string
pin could only prove the note's text exists somewhere -- it would stay green
on a row that is still ticked, still tickable, or never gets the note. This
file runs the REAL ``mkRowCheck`` and ``openPublishDialog`` in node against a
small fake DOM and reads the rendered rows back, then hands what the dialog
passes on to the real ``buildValue``.

The stub browser and the model range are imported from
``test_host_registry_crypto`` (module level there is constants and source
text only), so there is no second copy of either to drift. Core's own
``hostIsProxied`` is sliced from 56_js_hosts.js for the parity check.
"""

import json
import subprocess

import pytest

from tests.test_host_registry_crypto import (
    _HARNESS as _CRYPTO_HARNESS, MOD_JS, NODE, _model_source)

pytestmark = pytest.mark.skipif(NODE is None, reason="node not installed")

CSS = MOD_JS.with_name("host-registry.css")
MOD_JSON = MOD_JS.with_name("mod.json")
HELP_MD = MOD_JS.with_name("help.md")
CORE_HOSTS = MOD_JS.parents[2] / "56_js_hosts.js"
NOTE = "from server: not published"
# The FLEET rows (driver below) the dialog must grey out: via exactly 'server',
# with a peer id, without one, and hidden. Every other row is a direct control.
PROXIED = ["pp", "pn", "ph"]
DIRECT = ["local", "d1", "dp", "dj", "dd", "ds"]


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


def _core_predicate_source():
    """Core's hostIsProxied, verbatim, renamed so it sits beside the mod's."""
    fn = _between(CORE_HOSTS.read_text(encoding="utf-8"),
                  "function hostIsProxied(h) {",
                  "// The words every identity surface appends")
    return fn.replace("function hostIsProxied(h) {",
                      "function coreHostIsProxied(h) {") + "\n"


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
// The same edge rows as the crypto matrix, through the dialog this time.
const FLEET = [
    { id: 'local', label: 'this broker', brokerId: 'BR-ME' },
    { id: 'd1', label: 'direct', url: 'https://one.example:4445', token: 'SECRET-D' },
    { id: 'pp', label: 'peer', url: 'http://192.168.1.50:4445', via: 'server',
      peerId: 'b', token: TOKEN_P },
    { id: 'pn', label: 'no peer id', url: 'http://10.0.0.7:4445', via: 'server',
      token: TOKEN_P },
    { id: 'ph', label: 'hidden peer', url: 'http://10.0.0.8:4445', via: 'server',
      peerId: 'c', hidden: true, token: TOKEN_P },
    { id: 'dp', label: 'peer fields, no via', url: 'https://three.example:4445',
      peerId: 'x', peerOrigin: 'https://three.example:4445' },
    { id: 'dj', label: 'junk via', url: 'https://four.example:4445', via: 'SERVER' },
    { id: 'dd', label: 'direct via', url: 'https://five.example:4445', via: 'direct' },
    { id: 'ds', label: 'spaced via', url: 'https://six.example:4445', via: ' server' },
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
    return list.children.map((r, i) => ({
        id: FLEET[i].id,
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
    if (process.argv[2] === 'parity') {
        // The mod's rule against core's, row by row, and what buildValue
        // actually leaves out of a fully ticked list.
        const v = buildValue(FLEET.map(h => ({ host: h, checked: true })), false,
                             'https://me.example:4445');
        console.log(JSON.stringify({
            rows: FLEET.map(h => [h.id, hostFromServer(h), coreHostIsProxied(h)]),
            ids: v.hosts.map(h => h.id),
        }));
        return;
    }
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
    path.write_text(_STUBS + _model_source() + _dialog_source()
                    + _core_predicate_source() + _DRIVER, encoding="utf-8")
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
    rows = {row["id"]: row for row in r["rows"]}
    assert rows["pp"]["text"] == "peer — http://192.168.1.50:4445"
    for hid in PROXIED:
        row = rows[hid]
        assert row["checked"] is False and row["disabled"] is True, hid
        assert row["off"] is True, hid
        assert row["tags"] == [NOTE], hid
        assert "through this broker" in row["title"], hid
    # ...and that is what the dialog hands on.
    assert r["passed"] == [[row["id"], row["id"] not in PROXIED]
                           for row in r["rows"]]


def test_direct_rows_are_untouched(harness):
    r = run(harness, "plain")
    rows = {row["id"]: row for row in r["rows"]}
    assert sorted(rows) == sorted(PROXIED + DIRECT)
    for hid in DIRECT:
        row = rows[hid]
        assert row["checked"] is True and row["disabled"] is False, hid
        assert row["off"] is False and row["tags"] == [], hid
        assert row["title"] == "", hid
    assert rows["local"]["text"] == "this broker"
    assert rows["d1"]["text"] == "direct — https://one.example:4445"


def test_the_mod_rule_matches_core_and_is_what_publish_leaves_out(harness):
    """Parity: the mod's hostFromServer and core's hostIsProxied give the same
    answer on every row, and buildValue leaves out exactly the rows core calls
    proxied. A core change that diverges (say, a case-insensitive match) reds
    here rather than letting the dialog and the publish disagree with the
    Hosts row."""
    r = run(harness, "parity")
    for hid, mod, core in r["rows"]:
        assert mod == core, f"{hid}: mod says {mod}, core says {core}"
    core_proxied = [hid for hid, _mod, core in r["rows"] if core]
    assert core_proxied == PROXIED
    published = ["local" if i == "BR-ME" else i for i in r["ids"]]
    assert published == [hid for hid, _mod, core in r["rows"] if not core]


def test_the_intro_names_from_server_hosts_not_proxying(harness):
    intro = run(harness, "plain")["intro"]
    assert "never proxies" not in intro
    assert 'Hosts marked "from server" are not published.' in intro


def test_a_forced_tick_still_publishes_no_from_server_host(harness):
    """Defence in depth: tick every box from script, the disabled ones
    included. The model rule (buildValue) still leaves them out."""
    r = run(harness, "forced")
    assert all(checked for _hid, checked in r["passed"]), \
        "the forced tick did not take"
    assert r["ids"] == ["BR-ME"] + DIRECT[1:]
    assert "TOKEN-PPP-proxied-peer-secret" not in r["wire"]


def test_the_greyed_row_is_styled_dim():
    """A SOURCE PIN, not rendered state: the fake DOM has no cascade, so this
    only proves the rule exists with the dim colour."""
    css = CSS.read_text(encoding="utf-8")
    rule = css[css.index(".hostreg-off"):]
    rule = rule[:rule.index("}")]
    assert "color: var(--fg-dim)" in rule


def test_no_registry_text_still_claims_it_never_proxies():
    """A SOURCE PIN (#254 round 1). Since #252 a host can be reached through
    the broker serving the page, so "never proxies -- every browser connects
    directly" is false. The settings-pane hint says what is true: the broker
    stores a copy and relays nothing for the registry."""
    src = MOD_JS.read_text(encoding="utf-8")
    pane = _between(src, "function renderPane() {", "wrap.appendChild(desc);")
    assert "the broker only stores a copy and relays nothing " in pane
    assert "'for the registry.';" in pane
    for path in (MOD_JS, MOD_JSON, HELP_MD):
        text = path.read_text(encoding="utf-8")
        assert "never proxies" not in text, path.name
        assert "connects directly" not in text, path.name
