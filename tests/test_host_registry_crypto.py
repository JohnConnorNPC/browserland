"""Behavioural tests for the host-registry client-side encryption (#175).

Source-slice asserts (in ``test_ui_assets.py``) prove a string is present or
absent. They do NOT prove that a round trip round-trips, that a tampered
envelope is rejected, or that the insecure-context gate holds. So these tests
*execute* the shipped code.

The harness slices ``mods/host-registry/host-registry.js`` between the
``---- pure model + crypto ----`` and ``---- dialogs ----`` markers and runs
that range in node against a stub browser. Everything in it is a declaration —
nothing runs at load, nothing touches the DOM — which is what makes it runnable
outside a browser at all, and the mod's own comment says so, so the range stays
honest. Running the REAL file (rather than a copy of the logic) is the point:
these tests cannot drift from what ships.

node has WebCrypto (``globalThis.crypto.subtle``) natively, so the AES-GCM /
PBKDF2 paths are the same primitives the browser uses.

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
    """The shipped pure-model + crypto range, verbatim."""
    src = MOD_JS.read_text(encoding="utf-8")
    start = src.index(_SLICE_START)
    end = src.index(_SLICE_END)
    assert start < end, "slice markers out of order"
    body = src[start:end]
    for needed in ("function encSeal", "function encOpen", "function encProblem",
                   "function encPlan", "function sealTokens", "function sealAll",
                   "function stripTokens", "function publicHostsOf"):
        assert needed in body, f"{needed} missing from the sliced range"
    return body


_HARNESS = r"""
'use strict';
// ---- stub browser -------------------------------------------------------
// defineProperty, not assignment: node ships its own read-only `navigator`.
function def(name, value) {
    Object.defineProperty(globalThis, name,
        { value, writable: true, configurable: true });
}
def('window', {
    isSecureContext: true,
    crypto: globalThis.crypto,
    location: { origin: 'http://me.example:4445' },
});
// Core closure functions the sliced range reaches for. Stubs, but faithful
// enough that classify()/normEntry() behave like the real thing.
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
const PASS = 'correct horse battery staple';
const TOKEN_A = 'TOKEN-AAA-secret-do-not-store-in-the-clear';
const TOKEN_B = 'TOKEN-BBB-secret-do-not-store-in-the-clear';

function sample(withTokens) {
    return buildValue([
        { checked: true, host: { id: 'h1', label: 'box one',
                                 url: 'https://one.example:4445',
                                 color: '#112233', hidden: false,
                                 brokerId: 'BR-ONE', token: TOKEN_A } },
        { checked: true, host: { id: 'h2', label: 'box two',
                                 url: 'https://two.example:4445',
                                 color: '', hidden: true,
                                 brokerId: 'BR-TWO', token: TOKEN_B } },
        { checked: true, host: { id: 'h3', label: 'no password',
                                 url: 'https://three.example:4445',
                                 color: '', hidden: false, brokerId: '',
                                 token: '' } },
    ], !!withTokens, '');
}
async function caught(fn) {
    try { await fn(); return { threw: false }; }
    catch (e) { return { threw: true, kind: e && e.encError, msg: String(e && e.message) }; }
}

const CASES = {};

// --- round trip: passwords-only ------------------------------------------
CASES.roundtrip_tokens = async () => {
    const sealed = await sealTokens(sample(true), PASS);
    const opened = await encOpen(sealed.enc, PASS);
    return {
        wire: JSON.stringify(sealed),
        v: sealed.v,
        scope: sealed.enc.scope,
        alg: sealed.enc.alg,
        kdf: sealed.enc.kdf,
        iter: sealed.enc.iter,
        publicHasTokenField: sealed.hosts.some(h => 'token' in h),
        hasTokenMarks: sealed.hosts.map(h => h.hasToken === true),
        publicLabels: sealed.hosts.map(h => h.label),
        openedTokens: opened.hosts.map(h => h.token || ''),
        openedUrls: opened.hosts.map(h => h.url),
    };
};

// --- round trip: whole list ----------------------------------------------
CASES.roundtrip_all = async () => {
    const sealed = await sealAll(sample(true), PASS);
    const opened = await encOpen(sealed.enc, PASS);
    return {
        wire: JSON.stringify(sealed),
        v: sealed.v,
        scope: sealed.enc.scope,
        origin: sealed.origin,
        hasHostsKey: 'hosts' in sealed,
        nonceIsPlain: typeof sealed.nonce === 'string' && !!sealed.nonce,
        openedOrigin: opened.origin,
        openedUrls: opened.hosts.map(h => h.url),
        openedTokens: opened.hosts.map(h => h.token || ''),
        publicHosts: publicHostsOf(sealed),
        scopeOf: encScope(sealed),
    };
};

// --- salt/iv are fresh per publish ---------------------------------------
CASES.fresh_salt_and_iv = async () => {
    const a = await sealTokens(sample(true), PASS);
    const b = await sealTokens(sample(true), PASS);
    return { saltSame: a.enc.salt === b.enc.salt,
             ivSame: a.enc.iv === b.enc.iv,
             ctSame: a.enc.ct === b.enc.ct };
};

// --- wrong passphrase, and tampering, are the SAME failure ---------------
CASES.wrong_passphrase = async () => {
    const sealed = await sealTokens(sample(true), PASS);
    const out = {};
    out.wrong = await caught(() => encOpen(sealed.enc, PASS + 'x'));
    out.empty = await caught(() => encOpen(sealed.enc, ''));
    // ciphertext byte flipped (still valid base64, still long enough)
    const ct = sealed.enc.ct;
    const i = 4;
    const swap = ct[i] === 'A' ? 'B' : 'A';
    const bent = Object.assign({}, sealed.enc,
        { ct: ct.slice(0, i) + swap + ct.slice(i + 1) });
    out.bentCt = await caught(() => encOpen(bent, PASS));
    // header fields are AAD-bound: editing them must fail the tag, not sail past
    out.bentIter = await caught(() => encOpen(
        Object.assign({}, sealed.enc, { iter: 500000 }), PASS));
    out.bentScope = await caught(() => encOpen(
        Object.assign({}, sealed.enc, { scope: 'all' }), PASS));
    // and the right one still works after all that
    out.stillOpens = (await encOpen(sealed.enc, PASS)).hosts.length;
    return out;
};

// --- structural rejection happens BEFORE any passphrase is involved ------
CASES.malformed_envelope = async () => {
    const sealed = await sealTokens(sample(true), PASS);
    const bad = (over) => encProblem(Object.assign({}, sealed.enc, over));
    return {
        good: encProblem(sealed.enc),
        notObject: [encProblem(null), encProblem('x'), encProblem([])],
        alg: bad({ alg: 'AES-CBC' }),
        kdf: bad({ kdf: 'scrypt' }),
        iterLow: bad({ iter: 1000 }),
        iterHigh: bad({ iter: 100000000 }),
        iterFloat: bad({ iter: 600000.5 }),
        iterString: bad({ iter: '600000' }),
        scope: bad({ scope: 'everything' }),
        saltShort: bad({ salt: 'AAAA' }),
        saltNotB64: bad({ salt: 'not base64!!' }),
        saltWhitespace: bad({ salt: ' ' + sealed.enc.salt.slice(1) }),
        ivShort: bad({ iv: 'AAAA' }),
        ctShort: bad({ ct: 'AAAAAAAAAAAAAAAAAAAAAA==' }),
        ctMissing: bad({ ct: undefined }),
        // an unreadable envelope must be reported as such, never as a passphrase
        // problem — otherwise the user is sent round a loop they cannot win
        throwKind: (await caught(() => encOpen(bad2(sealed), PASS))).kind,
    };
};
function bad2(sealed) { return Object.assign({}, sealed.enc, { alg: 'AES-CBC' }); }

// --- secure-context gating ------------------------------------------------
CASES.insecure_context_gating = async () => {
    const out = {};
    out.secure = encAvailable();
    globalThis.window.isSecureContext = false;
    out.insecure = encAvailable();
    globalThis.window.isSecureContext = true;
    const real = globalThis.window.crypto;
    globalThis.window.crypto = undefined;
    out.noCrypto = encAvailable();
    globalThis.window.crypto = { getRandomValues: () => {} };   // no subtle
    out.noSubtle = encAvailable();
    globalThis.window.crypto = real;
    out.restored = encAvailable();
    out.why = encWhyUnavailable();
    return out;
};

// --- the publish decision: fail closed, never silently plaintext ----------
CASES.enc_plan = async () => {
    const withTok = sample(true);
    const noTok = sample(false);
    const row = (mode, value, available) => {
        const p = encPlan(mode, value, available);
        return p.seal + '/' + (p.blocked ? 'blocked' : 'ok');
    };
    return {
        hasPlainTokens: [valueHasPlainTokens(withTok), valueHasPlainTokens(noTok)],
        off_tok_ok: row('off', withTok, true),
        off_tok_insecure: row('off', withTok, false),
        tokens_tok_ok: row('tokens', withTok, true),
        tokens_tok_insecure: row('tokens', withTok, false),
        tokens_notok_ok: row('tokens', noTok, true),
        tokens_notok_insecure: row('tokens', noTok, false),
        all_tok_ok: row('all', withTok, true),
        all_notok_insecure: row('all', noTok, false),
    };
};

// --- v1 back-compat -------------------------------------------------------
CASES.v1_back_compat = async () => {
    const plain = sample(true);
    LOCAL_HOSTS = [{ id: 'local', brokerId: 'BROKER-ME', url: '' }];
    const rows = classifyLocal(publicHostsOf(plain));
    return {
        scope: encScope(plain),
        scopeOfEmpty: [encScope(null), encScope({}), encScope({ enc: 'x' }),
                       encScope({ enc: { scope: 'nope' } })],
        // a plain v1 registry is passed through untouched, tokens and all
        passthrough: publicHostsOf(plain) === plain.hosts,
        kinds: rows.map(r => r.kind),
        tokens: rows.map(r => r.entry.token),
        hasToken: rows.map(r => r.entry.hasToken),
        v2Unreadable: publicHostsOf({ v: 2, hosts: [{ url: 'https://x:1' }] }),
    };
};

// --- a writer cannot smuggle a token onto the readable side --------------
CASES.public_side_token_is_dropped = async () => {
    const sealed = await sealTokens(sample(true), PASS);
    // attacker edits the plaintext half of the stored value
    sealed.hosts[2].token = 'ATTACKER-CHOSEN-TOKEN';
    LOCAL_HOSTS = [{ id: 'local', brokerId: 'BROKER-ME', url: '' }];
    const rows = classifyLocal(publicHostsOf(sealed));
    return {
        pub: publicHostsOf(sealed).map(h => ('token' in h)),
        rowTokens: rows.map(r => r.entry.token),
        // …but the row still says a password exists, so skipping is informed
        rowHasToken: rows.map(r => r.entry.hasToken),
    };
};

// --- a writer cannot repoint a live credential ---------------------------
CASES.no_credential_redirect = async () => {
    const sealed = await sealTokens(sample(true), PASS);
    const beforeMatch = encPublicMatches(sealed, (await encOpen(sealed.enc, PASS)).hosts);
    // attacker repoints entry 0 at their own broker on the readable side
    sealed.hosts[0].url = 'https://attacker.example:4445';
    const opened = await encOpen(sealed.enc, PASS);
    return {
        beforeMatch: beforeMatch,
        afterMatch: encPublicMatches(sealed, opened.hosts),
        // the authenticated array is untouched: TOKEN_A still belongs to box one
        openedPairs: opened.hosts.map(h => h.url + '=' + (h.token || '')),
        publicUrls: publicHostsOf(sealed).map(h => h.url),
    };
};

// --- Forget passwords works with NO passphrase ---------------------------
CASES.forget_on_encrypted = async () => {
    const tokensVal = await sealTokens(sample(true), PASS);
    const allVal = await sealAll(sample(true), PASS);
    const plainVal = sample(true);
    const plainNoTok = sample(false);
    const st = stripTokens(tokensVal);
    const sa = stripTokens(allVal);
    const sp = stripTokens(plainVal);
    return {
        hasTokens: [valueHasTokens(tokensVal), valueHasTokens(allVal),
                    valueHasTokens(plainVal), valueHasTokens(plainNoTok),
                    valueHasTokens(null)],
        tokensStripped: {
            v: st.v, enc: ('enc' in st), note: ('encNote' in st),
            hosts: st.hosts.length,
            urls: st.hosts.map(h => h.url),
            anyToken: st.hosts.some(h => 'token' in h),
            anyMark: st.hosts.some(h => 'hasToken' in h),
            wire: JSON.stringify(st),
            hasTokensAfter: valueHasTokens(st),
        },
        allStripped: {
            v: sa.v, enc: ('enc' in sa), hosts: sa.hosts.length,
            wire: JSON.stringify(sa), hasTokensAfter: valueHasTokens(sa),
        },
        plainStripped: {
            v: sp.v, hosts: sp.hosts.length,
            anyToken: sp.hosts.some(h => 'token' in h),
            wire: JSON.stringify(sp), hasTokensAfter: valueHasTokens(sp),
        },
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
    path = tmp_path_factory.mktemp("hostreg") / "harness.js"
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


TOKEN_A = "TOKEN-AAA-secret-do-not-store-in-the-clear"
TOKEN_B = "TOKEN-BBB-secret-do-not-store-in-the-clear"


# ---- the round trip --------------------------------------------------------

def test_passwords_only_round_trips_and_stores_no_readable_token(harness):
    r = run(harness, "roundtrip_tokens")
    # The bytes the broker would store carry NO token.
    assert TOKEN_A not in r["wire"] and TOKEN_B not in r["wire"]
    assert not r["publicHasTokenField"], "the readable half must carry no token"
    # …but they do carry the readable list, which is the whole point of this
    # mode: an older build (and the discovery notice) can still use it.
    assert r["v"] == 1
    assert r["publicLabels"] == ["box one", "box two", "no password"]
    assert r["hasTokenMarks"] == [True, True, False]
    # Declared parameters are what we say they are.
    assert r["alg"] == "AES-GCM"
    assert r["kdf"] == "PBKDF2-SHA-256"
    assert r["iter"] == 600000
    assert r["scope"] == "tokens"
    # Unlocking gives the tokens back, still attached to their own hosts.
    assert r["openedTokens"] == [TOKEN_A, TOKEN_B, ""]
    assert r["openedUrls"] == ["https://one.example:4445",
                               "https://two.example:4445",
                               "https://three.example:4445"]


def test_whole_list_leaves_nothing_readable(harness):
    r = run(harness, "roundtrip_all")
    assert TOKEN_A not in r["wire"] and TOKEN_B not in r["wire"]
    for leak in ("one.example", "two.example", "box one", "BR-ONE",
                 "BROKER-ME"):
        assert leak not in r["wire"], f"{leak!r} leaked outside the envelope"
    assert r["v"] == 2
    assert r["scope"] == "all"
    assert r["origin"] == ""            # not even which broker published it
    assert not r["hasHostsKey"]
    # The nonce stays outside: the load-time discovery notice keys off it and
    # must work with no passphrase.
    assert r["nonceIsPlain"]
    # Nothing is readable without the passphrase.
    assert r["publicHosts"] == []
    assert r["scopeOf"] == "all"
    # With it, everything comes back.
    assert r["openedOrigin"] == "BROKER-ME"
    assert r["openedTokens"] == [TOKEN_A, TOKEN_B, ""]
    assert len(r["openedUrls"]) == 3


def test_every_publish_gets_a_fresh_salt_and_iv(harness):
    r = run(harness, "fresh_salt_and_iv")
    assert not r["saltSame"], "a reused salt makes one derive cover two publishes"
    assert not r["ivSame"], "an iv reused under one key breaks AES-GCM outright"
    assert not r["ctSame"]


# ---- failure is reported as the right kind of failure ----------------------

def test_wrong_passphrase_and_tampering_report_as_passphrase_not_corruption(harness):
    r = run(harness, "wrong_passphrase")
    for key in ("wrong", "empty", "bentCt", "bentIter", "bentScope"):
        assert r[key]["threw"], f"{key} must not decrypt"
        assert r[key]["kind"] == "passphrase", (
            f"{key} reported as {r[key]['kind']!r}; a GCM auth failure is "
            "always 'wrong passphrase or modified', never 'corrupt'")
    # bentIter/bentScope are the AAD binding doing its job: the header is
    # authenticated, so editing it cannot change how a reader treats the value.
    assert r["stillOpens"] == 3


def test_malformed_envelope_is_rejected_before_a_passphrase_is_asked_for(harness):
    r = run(harness, "malformed_envelope")
    assert r["good"] is None
    assert all(x is not None for x in r["notObject"])
    assert r["alg"] == "unsupported cipher"
    assert r["kdf"] == "unsupported key derivation"
    # The read-side iteration band is a DoS cap: a stored registry is untrusted
    # input, and the KDF is paid before the tag is ever checked.
    assert r["iterLow"] == "unsupported iteration count"
    assert r["iterHigh"] == "unsupported iteration count"
    assert r["iterFloat"] == "unsupported iteration count"
    assert r["iterString"] == "unsupported iteration count"
    assert r["scope"] == "unknown scope"
    assert r["saltShort"] == "bad salt"
    assert r["saltNotB64"] == "bad salt"
    assert r["saltWhitespace"] == "bad salt"   # strict base64, not atob's mercy
    assert r["ivShort"] == "bad iv"
    assert r["ctShort"] == "bad ciphertext"
    assert r["ctMissing"] == "bad ciphertext"
    # …and it surfaces as 'envelope', so the caller never re-prompts on it.
    assert r["throwKind"] == "envelope"


# ---- the insecure-context gate ---------------------------------------------

def test_encryption_is_gated_on_a_secure_context(harness):
    r = run(harness, "insecure_context_gating")
    assert r["secure"] is True
    # http://<lan-ip>:4445 — the way most of these brokers actually run.
    assert r["insecure"] is False
    assert r["noCrypto"] is False
    assert r["noSubtle"] is False
    assert r["restored"] is True
    assert "secure context" in r["why"]


def test_publish_fails_closed_and_never_downgrades_to_plaintext(harness):
    r = run(harness, "enc_plan")
    assert r["hasPlainTokens"] == [True, False]
    # off: today's behaviour, everywhere.
    assert r["off_tok_ok"] == "/ok"
    assert r["off_tok_insecure"] == "/ok"
    # passwords-only with a password in the value: seal, or refuse — the one
    # thing it must never do is publish the token in the clear because this
    # page happens not to be able to encrypt it.
    assert r["tokens_tok_ok"] == "tokens/ok"
    assert r["tokens_tok_insecure"] == "/blocked"
    # passwords-only with nothing to encrypt is a true no-op: there is no
    # secret in the value, so it goes out as today's plain v1 with no prompt.
    assert r["tokens_notok_ok"] == "/ok"
    assert r["tokens_notok_insecure"] == "/ok"
    # whole-list always seals, and always refuses when it cannot.
    assert r["all_tok_ok"] == "all/ok"
    assert r["all_notok_insecure"] == "/blocked"


# ---- back-compat and the writer's reach ------------------------------------

def test_a_plain_v1_registry_reads_exactly_as_before(harness):
    r = run(harness, "v1_back_compat")
    assert r["scope"] == ""
    assert r["scopeOfEmpty"] == ["", "", "", "unknown"]
    assert r["passthrough"] is True, (
        "a registry with no envelope must not be copied or filtered")
    assert r["kinds"] == ["new", "new", "new"]
    assert r["tokens"] == [TOKEN_A, TOKEN_B, ""]
    assert r["hasToken"] == [True, True, False]
    # A v2 whole-list envelope exposes nothing to a reader with no passphrase.
    assert r["v2Unreadable"] == []


def test_a_token_added_to_the_readable_half_is_dropped(harness):
    # The plaintext side of a 'tokens' registry is written token-free, so a
    # token in it is somebody else's addition — and adopting an attacker-chosen
    # token is how you get talked into authenticating to their broker.
    r = run(harness, "public_side_token_is_dropped")
    assert r["pub"] == [False, False, False]
    assert r["rowTokens"] == ["", "", ""]
    # The row still says a password exists, so "Without passwords" is informed.
    assert r["rowHasToken"] == [True, True, False]


def test_a_writer_cannot_repoint_an_encrypted_credential(harness):
    # The attack the format is shaped around: if the encrypted token were
    # merged onto the plaintext url beside it (by index, or by id), editing
    # that url would hand a live credential to an address of the writer's
    # choosing while the tag still verified.
    r = run(harness, "no_credential_redirect")
    assert r["beforeMatch"] is True
    assert r["afterMatch"] is False, "the edit must be visible"
    assert r["publicUrls"][0] == "https://attacker.example:4445"
    # …and the authenticated array is what an unlocked pull uses.
    assert r["openedPairs"][0] == f"https://one.example:4445={TOKEN_A}"
    assert all("attacker" not in p for p in r["openedPairs"])


# ---- the emergency path ----------------------------------------------------

def test_forget_passwords_works_on_an_encrypted_registry_with_no_passphrase(harness):
    r = run(harness, "forget_on_encrypted")
    # An envelope is opaque, so it is assumed to hold a password.
    assert r["hasTokens"] == [True, True, True, False, False]

    tok = r["tokensStripped"]
    assert tok["v"] == 1
    assert not tok["enc"] and not tok["note"], (
        "the purge must drop the ciphertext — a fresh object is built, never "
        "the stored one edited, which is what makes that automatic")
    assert tok["hosts"] == 3, "passwords-only keeps its readable list"
    assert tok["urls"][0] == "https://one.example:4445"
    assert not tok["anyToken"] and not tok["anyMark"]
    assert TOKEN_A not in tok["wire"] and TOKEN_B not in tok["wire"]
    assert tok["hasTokensAfter"] is False

    allv = r["allStripped"]
    assert allv["v"] == 1 and not allv["enc"]
    assert allv["hosts"] == 0, (
        "a whole-list registry cannot be separated from its passwords without "
        "the passphrase, so the emergency purge drops both — and doForget's "
        "confirmation says so before the user agrees")
    assert TOKEN_A not in allv["wire"]
    assert allv["hasTokensAfter"] is False

    plain = r["plainStripped"]
    assert plain["v"] == 1 and plain["hosts"] == 3 and not plain["anyToken"]
    assert TOKEN_A not in plain["wire"]
    assert plain["hasTokensAfter"] is False
