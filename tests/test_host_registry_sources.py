"""Behavioural tests for host-registry multi-broker Pull (#174).

Pull used to read only the broker whose page was open. It now reads any
configured broker, which means every failure has to be *distinguishable* (a
401 is not an empty registry), several lists have to merge deterministically,
and a list somebody else wrote gets a say in this browser's host config for the
first time. None of that is provable by asserting on source text, so — like
``test_host_registry_crypto.py`` and ``test_osc52_clipboard.py`` — these tests
execute the shipped code: the same declaration-only range of the real mod file,
run in node against a stub browser.

Skipped when node is absent, so the suite still runs on a box without it.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from webterm.broker import ui

BROKER_DIR = Path(ui.__file__).resolve().parent
MOD_JS = BROKER_DIR / "mods" / "host-registry" / "host-registry.js"
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="node not installed")

_SLICE_START = "// ---- pure model + crypto ---"
_SLICE_END = "// ---- dialogs ---"


def _model_source() -> str:
    src = MOD_JS.read_text(encoding="utf-8")
    start = src.index(_SLICE_START)
    end = src.index(_SLICE_END)
    assert start < end, "slice markers out of order"
    body = src[start:end]
    for needed in ("function mergeSources", "function classify",
                   "function sourceState", "function sourceTransportState",
                   "function sourceContentState", "function matchLocal",
                   "function tokenDiffers", "function isLoopbackOrigin"):
        assert needed in body, f"{needed} missing from the sliced range"
    return body


_HARNESS = r"""
'use strict';
function def(name, value) {
    Object.defineProperty(globalThis, name,
        { value, writable: true, configurable: true });
}
def('window', {
    isSecureContext: true,
    crypto: globalThis.crypto,
    location: { origin: 'http://me.example:4445' },
});
globalThis.strictHex = (v) => (/^#[0-9a-fA-F]{6}$/.test(v) ? v.toLowerCase() : '');
globalThis.normalizeHostUrl = (u) => {
    try {
        const x = new URL(String(u));
        if (x.protocol !== 'http:' && x.protocol !== 'https:') return '';
        return x.origin;
    } catch (_) { return ''; }
};
let LOCAL_HOSTS = [];
globalThis.getHosts = () => LOCAL_HOSTS;
globalThis.localHost = () => ({ id: 'local', brokerId: 'BROKER-ME' });
globalThis.hostById = (id) => LOCAL_HOSTS.find(h => h.id === id) || null;
let _mint = 0;
globalThis.mintHostId = () => 'minted' + (++_mint);
const notices = [];
globalThis.showNotice = (text, opts) => { notices.push({ text, opts: opts || null }); };

__MODEL__

// ---- driver -------------------------------------------------------------
function ent(over) {
    return Object.assign({ id: 'x', label: 'box', url: 'https://a.example:4445',
                           color: '', hidden: false, brokerId: '' }, over || {});
}
function src(name, local, hosts) { return { name, local, hosts }; }
function rowsOf(sources, opts) {
    return classify(mergeSources(sources, opts).items);
}

const CASES = {};

// --- the per-source state table ------------------------------------------
CASES.source_state_table = async () => {
    const v1 = { v: 1, hosts: [ent({ url: 'https://a.example:4445' })] };
    const call = (got) => sourceState(got);
    const enc = { alg: 'AES-GCM', kdf: 'PBKDF2-SHA-256', iter: 600000,
                  scope: 'tokens', salt: 'AAAAAAAAAAAAAAAAAAAAAA==',
                  iv: 'AAAAAAAAAAAAAAAA',
                  ct: 'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA' };
    const out = {};
    // transport
    out.unauthorized = call({ status: 401, ok: false, error: 'auth_required' });
    out.forbidden = call({ status: 403 });
    out.unsupported = call({ status: 404, error: 'HTTP 404' });
    out.serverError = call({ status: 500 });
    out.teapot = call({ status: 418 });
    out.noHost = call({ status: 0, error: 'no_host' });
    out.timeout = call({ status: 0, error: 'timeout' });
    out.transport = call({ status: 0, error: 'TypeError: failed' });
    out.missing = call(null);
    // content
    out.empty = call({ status: 200, rev: 0, value: null });
    out.emptyList = call({ status: 200, rev: 3, value: { v: 1, hosts: [] } });
    out.junk = call({ status: 200, rev: 3, value: 'nope' });
    out.ok = call({ status: 200, rev: 3, value: v1 });
    out.locked = call({ status: 200, rev: 3,
        value: { v: 1, hosts: [ent({ hasToken: true })], enc: enc } });
    out.lockedAll = call({ status: 200, rev: 3, value: { v: 2, enc:
        Object.assign({}, enc, { scope: 'all' }) } });
    out.unreadable = call({ status: 200, rev: 3, value: { v: 2, enc:
        Object.assign({}, enc, { salt: 'AAAA' }) } });
    out.unknownScope = call({ status: 200, rev: 3, value: { v: 2, enc:
        Object.assign({}, enc, { scope: 'sideways' }) } });
    // 200 wins only when the body is usable; every state's usability
    out.usable = {};
    for (const k of Object.keys(out)) {
        if (k === 'usable') continue;
        out.usable[k] = sourceUsable(out[k].state);
    }
    return out;
};

// --- cross-source de-dupe -------------------------------------------------
CASES.merge_dedupe = async () => {
    const A = src('A', false, [
        ent({ url: 'https://one.example:4445', label: 'from A' }),
        ent({ url: 'https://two.example:4445', label: 'only in A' }),
    ]);
    const B = src('B', false, [
        ent({ url: 'https://one.example:4445', label: 'from B' }),
        ent({ url: 'https://three.example:4445', label: 'only in B' }),
    ]);
    const fwd = mergeSources([A, B]).items;
    const rev = mergeSources([B, A]).items;
    return {
        forward: fwd.map(i => i.entry.url + '|' + i.entry.label + '|' + i.src
                              + '|' + i.also.join('+') + '|' + i.diverged),
        reverse: rev.map(i => i.entry.url + '|' + i.entry.label + '|' + i.src
                              + '|' + i.also.join('+') + '|' + i.diverged),
        // identical copies in two sources do NOT read as a disagreement
        agree: mergeSources([
            src('A', false, [ent({ url: 'https://q.example:4445' })]),
            src('B', false, [ent({ url: 'https://q.example:4445' })]),
        ]).items.map(i => i.diverged + '|' + i.also.join('+')),
    };
};

// --- one broker_id, two addresses = a conflict, not a duplicate ----------
CASES.merge_identity_conflict = async () => {
    const items = mergeSources([
        src('A', false, [ent({ url: 'https://evil.example:4445',
                               brokerId: 'BR-ONE', label: 'evil' })]),
        src('B', false, [ent({ url: 'https://good.example:4445',
                               brokerId: 'BR-ONE', label: 'good' })]),
    ]).items;
    // and a chain that would collapse two brokers if broker_id were a merge key
    const chain = mergeSources([
        src('A', false, [ent({ url: 'https://u1.example:4445', brokerId: 'X' })]),
        src('B', false, [ent({ url: 'https://u2.example:4445', brokerId: 'Y' })]),
        src('C', false, [ent({ url: 'https://u2.example:4445', brokerId: 'X' })]),
    ]).items;
    return {
        urls: items.map(i => i.entry.url),
        labels: items.map(i => i.entry.label),
        conflicts: items.map(i => i.conflict),
        chainUrls: chain.map(i => i.entry.url),
        chainBrokers: chain.map(i => i.entry.brokerId),
    };
};

// --- what a REMOTE source is not allowed to hand us ----------------------
CASES.remote_hardening = async () => {
    const rows = [
        ent({ url: 'http://127.0.0.1:4445', label: 'loop v4' }),
        ent({ url: 'http://localhost:4445', label: 'loop name' }),
        ent({ url: 'http://[::1]:4445', label: 'loop v6' }),
        ent({ url: 'https://hidden.example:4445', label: 'hidden', hidden: true }),
        ent({ url: 'https://tok.example:4445', label: 'tok', token: 'REMOTE-TOK' }),
    ];
    const remote = mergeSources([src('B', false, rows)]);
    const remoteOk = mergeSources([src('B', false, rows)],
        { acceptRemoteTokens: true });
    const local = mergeSources([src('this broker', true, rows)]);
    const shape = (m) => ({
        urls: m.items.map(i => i.entry.url),
        hidden: m.items.map(i => i.entry.hidden),
        tokens: m.items.map(i => i.entry.token),
        hasToken: m.items.map(i => i.entry.hasToken),
        refused: m.items.map(i => i.pwRefused),
        dropped: m.droppedLoopback,
    });
    return { remote: shape(remote), remoteOk: shape(remoteOk),
             local: shape(local) };
};

// --- loopback spellings ---------------------------------------------------
CASES.loopback_spellings = async () => {
    const out = {};
    for (const u of ['http://127.0.0.1:4445', 'http://127.1.2.3:4445',
                     'http://localhost:4445', 'http://LOCALHOST:4445',
                     'http://localhost.:4445', 'http://[::1]:4445',
                     'http://0.0.0.0:4445', 'http://[::ffff:127.0.0.1]:4445',
                     'https://one.example:4445', 'http://192.168.1.9:4445',
                     'http://localhostx.example:4445', 'not a url']) {
        out[u] = isLoopbackOrigin(u);
    }
    return out;
};

// --- classification vs the live local hosts ------------------------------
CASES.classify_kinds = async () => {
    LOCAL_HOSTS = [
        { id: 'local', brokerId: 'BROKER-ME', url: '' },
        { id: 'loc1', label: 'box', url: 'https://a.example:4445',
          color: '', hidden: false, brokerId: '', token: 'LOCAL-TOK' },
    ];
    const rows = rowsOf([src('A', false, [
        ent({ url: 'https://a.example:4445' }),                       // identical
        ent({ url: 'https://b.example:4445', label: 'brand new' }),   // new
        ent({ url: 'http://me.example:4445', label: 'us by url' }),   // dropped
        ent({ url: 'https://c.example:4445', brokerId: 'BROKER-ME' }),// dropped
    ])]);
    return { kinds: rows.map(r => r.kind), labels: rows.map(r => r.entry.label),
             srcs: rows.map(r => r.src) };
};

// --- a rotated password on an otherwise identical host ------------------
CASES.token_only_differs = async () => {
    LOCAL_HOSTS = [
        { id: 'local', brokerId: 'BROKER-ME', url: '' },
        { id: 'loc1', label: 'box', url: 'https://a.example:4445',
          color: '', hidden: false, brokerId: '', token: 'OLD-TOK' },
    ];
    const one = ent({ url: 'https://a.example:4445' });
    const withNew = ent({ url: 'https://a.example:4445', token: 'NEW-TOK' });
    const withSame = ent({ url: 'https://a.example:4445', token: 'OLD-TOK' });
    const kind = (e, opts) => {
        const r = rowsOf([src('A', false, [e])], opts)[0];
        return r.kind + '/' + (r.tokenOnly ? 'tokenOnly' : '-')
             + '/' + (r.checked ? 'checked' : 'unchecked');
    };
    return {
        noToken: kind(one),
        newTokenAccepted: kind(withNew, { acceptRemoteTokens: true }),
        newTokenRefused: kind(withNew),
        sameToken: kind(withSame, { acceptRemoteTokens: true }),
    };
};

// --- an incoming `id` no longer selects a local host to repoint ---------
CASES.no_id_matching = async () => {
    LOCAL_HOSTS = [
        { id: 'local', brokerId: 'BROKER-ME', url: '' },
        { id: 'hrtqme8cma', label: 'mine', url: 'https://mine.example:4445',
          color: '', hidden: false, brokerId: '', token: 'MY-TOK' },
    ];
    // A registry entry that names my browser-local host id (which is published
    // in every list I publish) but points somewhere else.
    const rows = rowsOf([src('A', false, [
        ent({ id: 'hrtqme8cma', url: 'https://attacker.example:4445',
              label: 'mine' })])]);
    // …and the legitimate matches that must still work.
    const byUrl = rowsOf([src('A', false, [
        ent({ url: 'https://mine.example:4445', label: 'renamed' })])]);
    LOCAL_HOSTS[1].brokerId = 'BR-MINE';
    const byBroker = rowsOf([src('A', false, [
        ent({ url: 'https://moved.example:4445', brokerId: 'BR-MINE' })])]);
    return {
        byId: rows.map(r => r.kind),
        byIdLocal: rows.map(r => (r.local ? r.local.id : null)),
        byUrl: byUrl.map(r => r.kind + '/' + (r.local ? r.local.id : null)),
        byBroker: byBroker.map(r => r.kind + '/' + (r.local ? r.local.id : null)
                                    + '/' + r.urlChanged),
    };
};

const want = process.argv[2];
if (!CASES[want]) { console.error('no such case: ' + want); process.exit(2); }
Promise.resolve(CASES[want]()).then((r) => {
    const out = Object.assign({}, r);
    out.__notices = notices.map(n => n.text);
    console.log(JSON.stringify(out));
}).catch((e) => { console.error(e && e.stack || String(e)); process.exit(3); });
"""


@pytest.fixture(scope="module")
def harness(tmp_path_factory):
    path = tmp_path_factory.mktemp("hostreg-src") / "harness.js"
    path.write_text(_HARNESS.replace("__MODEL__", _model_source()),
                    encoding="utf-8")
    return path


def run(harness, case):
    proc = subprocess.run([NODE, str(harness), case],
                          capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, (
        f"case {case} failed (rc={proc.returncode})\n"
        f"stdout: {proc.stdout}\nstderr: {proc.stderr}")
    return json.loads(proc.stdout.strip().splitlines()[-1])


# ---- per-source failures are distinguishable -------------------------------

def test_a_401_is_not_an_empty_registry(harness):
    # The old code treated any non-200 as "no registry yet". That is right for
    # the local broker and wrong for every other one: a remote 401 means "it
    # refused our password", and telling the user their list is empty when it
    # is merely locked is the difference between a fix and a wild goose chase.
    r = run(harness, "source_state_table")
    assert r["unauthorized"]["state"] == "unauthorized"
    assert "refused our password" in r["unauthorized"]["why"]
    assert r["forbidden"]["state"] == "unauthorized"
    assert r["empty"]["state"] == "empty"
    assert r["emptyList"]["state"] == "empty"
    assert r["ok"]["state"] == "ok" and r["ok"]["count"] == 1


def test_every_transport_outcome_has_its_own_state(harness):
    r = run(harness, "source_state_table")
    assert r["unsupported"]["state"] == "unsupported"      # broker predates it
    assert r["noHost"]["state"] == "no_host"               # deleted mid-dialog
    assert r["timeout"]["state"] == "unreachable"
    assert "in time" in r["timeout"]["why"]
    assert r["transport"]["state"] == "unreachable"
    assert r["missing"]["state"] == "unreachable"
    # No silent bucket for the ones nobody thought about.
    assert r["serverError"]["state"] == "error"
    assert r["serverError"]["why"] == "answered HTTP 500"
    assert r["teapot"]["state"] == "error"
    assert r["junk"]["state"] == "empty"


def test_locked_is_only_reported_for_an_envelope_that_could_open(harness):
    # Sending someone to a passphrase prompt that cannot possibly succeed is
    # worse than telling them the block is unreadable.
    r = run(harness, "source_state_table")
    assert r["locked"]["state"] == "locked"
    assert "passwords encrypted" in r["locked"]["why"]
    assert r["lockedAll"]["state"] == "locked"
    assert "needs a passphrase" in r["lockedAll"]["why"]
    assert r["unreadable"]["state"] == "unreadable"
    assert r["unknownScope"]["state"] == "unreadable"


def test_only_readable_sources_are_selectable(harness):
    r = run(harness, "source_state_table")
    usable = r["usable"]
    assert usable["ok"] is True
    assert usable["locked"] is True and usable["lockedAll"] is True
    for dead in ("unauthorized", "forbidden", "unsupported", "serverError",
                 "teapot", "noHost", "timeout", "transport", "missing",
                 "empty", "emptyList", "junk", "unreadable", "unknownScope"):
        assert usable[dead] is False, f"{dead} must not be selectable"


# ---- merging several registries -------------------------------------------

def test_sources_dedupe_by_url_and_the_first_one_wins(harness):
    r = run(harness, "merge_dedupe")
    assert r["forward"] == [
        "https://one.example:4445|from A|A|B|true",
        "https://two.example:4445|only in A|A||false",
        "https://three.example:4445|only in B|B||false",
    ]
    # Deterministic, and the losing source stays visible rather than vanishing:
    # reversing the source order swaps the winner and says so.
    assert r["reverse"] == [
        "https://one.example:4445|from B|B|A|true",
        "https://three.example:4445|only in B|B||false",
        "https://two.example:4445|only in A|A||false",
    ]
    # Two sources that agree are not a disagreement.
    assert r["agree"] == ["false|B"]


def test_one_broker_id_at_two_addresses_is_a_conflict_not_a_merge(harness):
    # broker_id is unverified input. Keying the merge on it would let one
    # source's record swallow another's by claiming its identity — and a
    # bridging record could collapse two genuinely different brokers into one.
    r = run(harness, "merge_identity_conflict")
    assert r["urls"] == ["https://evil.example:4445", "https://good.example:4445"]
    assert r["labels"] == ["evil", "good"]          # neither is suppressed
    assert r["conflicts"] == ["https://good.example:4445",
                              "https://evil.example:4445"]
    # The bridge case: three records, two addresses, still two rows.
    assert r["chainUrls"] == ["https://u1.example:4445", "https://u2.example:4445"]
    assert r["chainBrokers"] == ["X", "Y"]


def test_a_remote_registry_cannot_hand_us_loopback_hidden_or_passwords(harness):
    r = run(harness, "remote_hardening")
    remote, remote_ok, local = r["remote"], r["remoteOk"], r["local"]
    # Loopback rows name the PUBLISHER's machine; imported here they would
    # point a host at MY local broker carrying somebody else's password.
    assert remote["urls"] == ["https://hidden.example:4445",
                              "https://tok.example:4445"]
    assert remote["dropped"] == 3
    # `hidden` takes a host out of the UI while the poll loop keeps talking to
    # it — never inherited from somebody else's list.
    assert remote["hidden"] == [False, False]
    # Passwords are refused by default, and the row still knows one was offered.
    assert remote["tokens"] == ["", ""]
    assert remote["hasToken"] == [False, True]
    assert remote["refused"] == [False, True]
    # …and accepted only on the explicit opt-in.
    assert remote_ok["tokens"] == ["", "REMOTE-TOK"]
    assert remote_ok["refused"] == [False, False]
    # The LOCAL registry is untouched by all of it — same behaviour as before.
    assert local["dropped"] == 0
    assert len(local["urls"]) == 5
    assert local["hidden"] == [False, False, False, True, False]
    assert local["tokens"] == ["", "", "", "", "REMOTE-TOK"]


def test_loopback_detection_covers_the_spellings_that_reach_loopback(harness):
    r = run(harness, "loopback_spellings")
    for loop in ("http://127.0.0.1:4445", "http://127.1.2.3:4445",
                 "http://localhost:4445", "http://LOCALHOST:4445",
                 "http://localhost.:4445", "http://[::1]:4445",
                 "http://0.0.0.0:4445", "http://[::ffff:127.0.0.1]:4445"):
        assert r[loop] is True, f"{loop} reaches loopback"
    # A LAN address is exactly what these brokers run on — never filtered.
    for keep in ("https://one.example:4445", "http://192.168.1.9:4445",
                 "http://localhostx.example:4445", "not a url"):
        assert r[keep] is False, f"{keep} must stay importable"


# ---- classification --------------------------------------------------------

def test_classify_drops_this_broker_and_tags_provenance(harness):
    r = run(harness, "classify_kinds")
    assert r["kinds"] == ["identical", "new"]
    assert r["labels"] == ["box", "brand new"]
    # Provenance comes from the SOURCE LIST, not from a field on the entry —
    # a row that lies about where it came from is the wrong thing to show.
    assert r["srcs"] == ["A", "A"]


def test_a_rotated_password_is_offered_instead_of_being_greyed_out(harness):
    # hostDiffers() ignores the token on purpose, so an otherwise identical
    # host used to classify as 'identical' with a disabled checkbox — and a
    # password could never be pulled back at all, which is half the reason to
    # read another broker's registry.
    r = run(harness, "token_only_differs")
    assert r["noToken"] == "identical/-/unchecked"
    assert r["newTokenAccepted"] == "differs/tokenOnly/unchecked"
    # Refused remote passwords leave nothing to apply, so no row is offered.
    assert r["newTokenRefused"] == "identical/-/unchecked"
    assert r["sameToken"] == "identical/-/unchecked"


def test_an_incoming_id_can_no_longer_repoint_a_local_host(harness):
    # Publishing puts this browser's own host ids in that broker's store, so a
    # registry somebody else controls could name one and repoint the host it
    # belongs to. url and broker_id are the only identities that match now.
    r = run(harness, "no_id_matching")
    assert r["byId"] == ["new"], "an id match must not select a local host"
    assert r["byIdLocal"] == [None]
    # The identities that do mean something still match, in place.
    assert r["byUrl"] == ["differs/hrtqme8cma"]
    assert r["byBroker"] == ["differs/hrtqme8cma/true"]
