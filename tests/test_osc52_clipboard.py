"""Behavioural tests for the OSC 52 clipboard bridge (#153).

Source-slice asserts (in ``test_ui_assets.py``) prove a string is present or
absent. They do NOT prove a gate holds, and this feature is almost entirely
gates. So these tests *execute* the shipped policy code.

The harness slices ``63_js_clipboard_auth.js`` from the top of the file down to
the ``---- auth ----`` marker and runs it in node with a stub browser. That
range is exactly the clipboard helpers plus the OSC 52 policy, and every
statement in it is a declaration -- nothing in it touches the DOM at load time,
which is what makes it runnable outside a browser at all. Running the REAL file
(rather than a copy of the logic) is the point: the tests cannot drift from
what ships.

Skipped when node is absent, so the suite still runs on a box without it.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from webterm.broker import ui

BROKER_DIR = Path(ui.__file__).resolve().parent
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="node not installed")

_SLICE_START = "// ---- clipboard ---"
_OSC_START = "// ---- OSC 52:"
_SLICE_END = "// ---- auth ---"


def _policy_source() -> str:
    """The shipped clipboard + OSC 52 code, verbatim — everything the harness
    has to execute (the OSC policy leans on canWriteClipboardModern and the
    #106 observer registry above it)."""
    src = (BROKER_DIR / "63_js_clipboard_auth.js").read_text(encoding="utf-8")
    start = src.index(_SLICE_START)
    end = src.index(_SLICE_END)
    assert start < end, "fragment markers out of order"
    body = src[start:end]
    assert "osc52Request" in body, "OSC 52 policy missing from the slice"
    return body


def _osc52_source() -> str:
    """Just the OSC 52 policy — the narrower slice the write-only asserts need.
    The clipboard section above it legitimately mentions readText (the
    right-click paste path), so those asserts must not see it."""
    src = (BROKER_DIR / "63_js_clipboard_auth.js").read_text(encoding="utf-8")
    return src[src.index(_OSC_START):src.index(_SLICE_END)]


_HARNESS = r"""
'use strict';
// ---- stub browser -------------------------------------------------------
const NOW = { t: 1_000_000 };
const _realNow = Date.now;
Date.now = () => NOW.t;

// defineProperty, not assignment: node ships its own read-only `navigator`.
function def(name, value) {
    Object.defineProperty(globalThis, name,
        { value, writable: true, configurable: true });
}

const store = new Map();
def('localStorage', {
    getItem: (k) => (store.has(k) ? store.get(k) : null),
    setItem: (k, v) => { store.set(k, String(v)); },
});
const clipboard = { writes: [], fail: false, present: true };
def('window', { isSecureContext: true });
def('navigator', {
    get clipboard() {
        if (!clipboard.present) return undefined;
        return {
            writeText: (t) => {
                if (clipboard.fail) return Promise.reject(new Error('denied'));
                clipboard.writes.push(t);
                return Promise.resolve();
            },
            readText: () => Promise.resolve(''),
            read: () => Promise.resolve([]),
        };
    },
});
const focus = { on: true };
def('document', { hasFocus: () => focus.on });
const notices = [];
globalThis.showNotice = (text, opts) => { notices.push({ text, opts: opts || null }); };
const hosts = new Map([
    ['local', { id: 'local', label: 'this broker', url: '' }],
    ['remote1', { id: 'remote1', label: 'HOSTLABEL-remote1',
                  url: 'https://box-one:4445' }],
]);
globalThis.hostById = (id) => hosts.get(id) || null;
globalThis.frontId = 'w1';

__POLICY__

// ---- driver -------------------------------------------------------------
function reset(opts) {
    opts = opts || {};
    store.clear();
    clipboard.writes.length = 0;
    clipboard.fail = false;
    clipboard.present = true;
    globalThis.window.isSecureContext = true;
    focus.on = true;
    globalThis.frontId = 'w1';
    notices.length = 0;
    NOW.t += 60_000;                 // clear any rolling window / throttle
    _osc52.accepted.length = 0;
    _osc52.noticedAt.clear();
    _osc52.offeredHosts.clear();
    if (opts.allow !== false) setOsc52Allowed(opts.host || 'local', true);
}

function req(over) {
    return Object.assign(
        { hostId: 'local', sid: '7', winId: 'w1', lastInputAt: NOW.t - 100 },
        over || {});
}

const CASES = {};

// --- the ? query form is refused in code, never behind a setting ---------
CASES.query_form_refused = () => {
    reset();
    osc52Request(req(), 'c;?');
    return { writes: clipboard.writes, notices };
};

// --- default OFF ----------------------------------------------------------
CASES.default_off_blocks_and_tells_you = () => {
    reset({ allow: false });
    osc52Request(req(), 'c;aGVsbG8=');
    return { writes: clipboard.writes, notices };
};
CASES.default_off_notice_is_one_shot_per_host = () => {
    reset({ allow: false });
    for (let i = 0; i < 4; i++) osc52Request(req(), 'c;aGVsbG8=');
    return { writes: clipboard.writes, noticeCount: notices.length };
};
CASES.authorization_is_per_host = () => {
    reset({ allow: false });
    setOsc52Allowed('local', true);
    osc52Request(req({ hostId: 'remote1', winId: 'w1' }), 'c;aGVsbG8=');
    const blocked = clipboard.writes.slice();
    osc52Request(req(), 'c;aGVsbG8=');
    return { blocked, allowed: clipboard.writes };
};
CASES.authorization_is_not_in_prefs = () => {
    reset();
    return { keys: Array.from(store.keys()) };
};

// --- the happy path -------------------------------------------------------
CASES.accepted_write = () => {
    reset();
    osc52Request(req(), 'c;aGVsbG8gd29ybGQ=');
    return { writes: clipboard.writes };
};

// --- Pc is a LIST, not a scalar ------------------------------------------
CASES.pc_forms = () => {
    const out = {};
    for (const pc of ['c', 's', '', 'p', 'q', '3', 'cp', 'c0', 'pc', 'qs']) {
        reset();
        osc52Request(req(), pc + ';aGVsbG8=');
        out[pc === '' ? '(empty)' : pc] = clipboard.writes.length;
    }
    return out;
};

// --- gates ----------------------------------------------------------------
CASES.background_tab_blocked = () => {
    reset();
    focus.on = false;
    osc52Request(req(), 'c;aGVsbG8=');
    return { writes: clipboard.writes };
};
CASES.non_front_terminal_blocked = () => {
    reset();
    globalThis.frontId = 'someone-else';
    osc52Request(req(), 'c;aGVsbG8=');
    return { writes: clipboard.writes };
};
CASES.stale_activity_blocked_with_notice = () => {
    reset();
    osc52Request(req({ lastInputAt: NOW.t - 60_000 }), 'c;aGVsbG8=');
    return { writes: clipboard.writes, notices };
};
CASES.gate_order_auth_before_focus = () => {
    // An unauthorised host in a BACKGROUND tab must report the authorisation
    // problem -- the one the user can act on -- not fail silently on focus.
    reset({ allow: false });
    focus.on = false;
    osc52Request(req(), 'c;aGVsbG8=');
    return { writes: clipboard.writes, notices };
};

// --- payload handling -----------------------------------------------------
const bigB64 = Buffer.from('x'.repeat(2 * 1024 * 1024)).toString('base64');
CASES.oversize_refused_not_truncated = () => {
    reset();
    osc52Request(req(), 'c;' + bigB64);
    return { writes: clipboard.writes, notices };
};
CASES.one_byte_under_cap_accepted = () => {
    reset();
    const b64 = Buffer.from('y'.repeat(1024 * 1024)).toString('base64');
    osc52Request(req(), 'c;' + b64);
    return { count: clipboard.writes.length,
             len: clipboard.writes[0] ? clipboard.writes[0].length : 0 };
};
CASES.non_utf8_refused = () => {
    reset();
    // 0xff is never valid UTF-8. Non-fatal decoding would substitute U+FFFD
    // and report success -- i.e. claim a copy while mutating it.
    osc52Request(req(), 'c;' + Buffer.from([0xff, 0xfe, 0x41]).toString('base64'));
    return { writes: clipboard.writes, notices };
};
CASES.control_chars_rejected_whole = () => {
    reset();
    osc52Request(req(), 'c;' + Buffer.from('safe\x1b[31mtext').toString('base64'));
    return { writes: clipboard.writes, notices };
};
CASES.tab_newline_cr_are_kept = () => {
    reset();
    osc52Request(req(), 'c;' + Buffer.from('a\tb\nc\rd').toString('base64'));
    return { writes: clipboard.writes };
};
CASES.wrapped_base64_accepted = () => {
    reset();
    // GNU base64(1) wraps at 76 columns.
    const raw = Buffer.from('z'.repeat(200)).toString('base64');
    const wrapped = raw.replace(/(.{76})/g, '$1\n');
    osc52Request(req(), 'c;' + wrapped);
    return { count: clipboard.writes.length,
             len: clipboard.writes[0] ? clipboard.writes[0].length : 0 };
};
CASES.unpadded_base64_accepted = () => {
    reset();
    osc52Request(req(), 'c;' + Buffer.from('hello').toString('base64').replace(/=+$/, ''));
    return { writes: clipboard.writes };
};
CASES.urlsafe_base64_accepted = () => {
    reset();
    // "??>???" base64s to "Pz8+Pz8/" -- it exercises BOTH non-alphanumeric
    // characters, and decodes to plain ASCII so the UTF-8 gate is not what is
    // being measured here.
    const raw = Buffer.from('??>???').toString('base64');
    if (!/\+/.test(raw) || !/\//.test(raw)) return { count: -1, raw };
    osc52Request(req(), 'c;' + raw.replace(/\+/g, '-').replace(/\//g, '_'));
    return { count: clipboard.writes.length };
};
CASES.invalid_base64_refused = () => {
    reset();
    osc52Request(req(), 'c;!!!not base64!!!');
    return { writes: clipboard.writes, notices };
};
CASES.empty_payload_does_not_clear = () => {
    reset();
    osc52Request(req(), 'c;');
    return { writes: clipboard.writes, notices };
};
CASES.malformed_no_semicolon_ignored = () => {
    reset();
    osc52Request(req(), 'garbage-with-no-separator');
    return { writes: clipboard.writes, notices };
};

// --- rate limit -----------------------------------------------------------
CASES.rate_limit_is_browser_global = () => {
    reset();
    setOsc52Allowed('remote1', true);
    const accepted = [];
    // Alternate between two DIFFERENT terminals: a per-terminal limiter would
    // let them evade the cap in parallel.
    for (let i = 0; i < 8; i++) {
        const from = (i % 2 === 0)
            ? req()
            : req({ hostId: 'remote1', sid: '9' });
        osc52Request(from, 'c;' + Buffer.from('n' + i).toString('base64'));
        accepted.push(clipboard.writes.length);
    }
    return { finalCount: clipboard.writes.length, progression: accepted,
             notices: notices.map(n => n.text) };
};
CASES.rate_limit_recovers_after_window = () => {
    reset();
    for (let i = 0; i < 8; i++) {
        osc52Request(req(), 'c;' + Buffer.from('a' + i).toString('base64'));
    }
    const capped = clipboard.writes.length;
    NOW.t += 20_000;                       // window rolls over
    osc52Request(req({ lastInputAt: NOW.t - 100 }), 'c;' + Buffer.from('later').toString('base64'));
    return { capped, after: clipboard.writes.length };
};
CASES.refusals_do_not_consume_quota = () => {
    reset();
    // Twenty refusals (bad base64) must not exhaust the budget for the real
    // copy that follows -- otherwise a hostile program can lock the user out.
    for (let i = 0; i < 20; i++) osc52Request(req(), 'c;!!!!');
    osc52Request(req(), 'c;' + Buffer.from('real').toString('base64'));
    return { writes: clipboard.writes };
};

// --- secure context / permission ------------------------------------------
CASES.insecure_origin_distinct_notice = () => {
    reset();
    globalThis.window.isSecureContext = false;
    clipboard.present = false;
    osc52Request(req(), 'c;aGVsbG8=');
    return { writes: clipboard.writes, notices };
};
CASES.no_legacy_textarea_fallback = () => {
    // copyTextLegacy steals focus (and can abort an IME composition). If the
    // OSC path ever reached it, this would throw on the absent `document.body`.
    reset();
    globalThis.window.isSecureContext = false;
    clipboard.present = false;
    let threw = null;
    try { osc52Request(req(), 'c;aGVsbG8='); } catch (e) { threw = String(e); }
    return { threw, writes: clipboard.writes };
};

// --- attribution ----------------------------------------------------------
CASES.notice_attributes_without_the_title = () => {
    reset();
    setOsc52Allowed('remote1', true);
    osc52Request(req({ hostId: 'remote1', sid: '42' }), 'c;aGVsbG8=');
    return {};
};

// --- host identity: a re-pointed id must not inherit trust ----------------
CASES.repointing_a_host_url_revokes_it = () => {
    reset({ allow: false });
    setOsc52Allowed('remote1', true);
    osc52Request(req({ hostId: 'remote1' }), 'c;aGVsbG8=');
    const before = clipboard.writes.slice();
    // The #65 registry mod republishes prefs._hosts WITH ids, so the same id
    // can come back attached to a different machine.
    hosts.get('remote1').url = 'https://somewhere-else:4445';
    osc52Request(req({ hostId: 'remote1' }), 'c;aGVsbG8=');
    return { before, after: clipboard.writes };
};
CASES.unknown_host_fails_shut = () => {
    reset();
    setOsc52Allowed('local', true);
    const out = {};
    for (const id of ['ghost', undefined, null, '', 'app']) {
        clipboard.writes.length = 0;
        osc52Request(req({ hostId: id }), 'c;aGVsbG8=');
        out[String(id)] = clipboard.writes.length;
    }
    return out;
};

// --- the activity stamp must not be self-servable -------------------------
CASES.read_form_before_pc_filter = () => {
    reset();
    // `p;?` is a read attempt too -- the Pc filter must not swallow it before
    // the user is told something tried to exfiltrate their clipboard.
    osc52Request(req(), 'p;?');
    return { writes: clipboard.writes };
};
CASES.nbsp_payload_does_not_sneak_through_as_whitespace = () => {
    reset();
    // U+00A0 matches JS \s but is \xc2\xa0 in bytes, where the agent-side strip
    // stops. If the decoder treated it as padding whitespace the two would
    // disagree about the same sequence.
    osc52Request(req(), 'c;aGVs bG8=');
    return { writes: clipboard.writes };
};

// --- #106 observers -------------------------------------------------------
CASES.accepted_write_notifies_observers = () => {
    reset();
    const seen = [];
    addClipboardObserver((dir, text) => seen.push([dir, text]));
    osc52Request(req(), 'c;aGVsbG8=');
    return { seen, writes: clipboard.writes };
};
CASES.refused_write_notifies_nothing = () => {
    reset({ allow: false });
    const seen = [];
    addClipboardObserver((dir, text) => seen.push([dir, text]));
    osc52Request(req(), 'c;aGVsbG8=');
    return { seen };
};

// --- copyTextToClipboard now tells the truth ------------------------------
CASES.copy_returns_true_on_success = () => {
    reset();
    return copyTextToClipboard('hi').then(ok => ({ ok }));
};
CASES.copy_returns_false_on_empty = () => {
    reset();
    return copyTextToClipboard('').then(ok => ({ ok }));
};

// ---- run -----------------------------------------------------------------
const want = process.argv[2];
if (!(want in CASES)) {
    console.error('unknown case: ' + want);
    process.exit(2);
}
Promise.resolve(CASES[want]()).then((r) => {
    // Let the writeText promises (and their notices) settle first.
    setTimeout(() => {
        const out = Object.assign({}, r);
        if (out.notices === notices) out.notices = notices.map(n => n.text);
        out.__allNotices = notices.map(n => n.text);
        out.__stickyNotices = notices
            .filter(n => n.opts && n.opts.sticky).map(n => n.text);
        out.__clipboard = clipboard.writes;
        console.log(JSON.stringify(out));
    }, 0);
}).catch((e) => { console.error(e && e.stack || String(e)); process.exit(3); });
"""


@pytest.fixture(scope="module")
def harness(tmp_path_factory):
    path = tmp_path_factory.mktemp("osc52") / "harness.js"
    path.write_text(_HARNESS.replace("__POLICY__", _policy_source()),
                    encoding="utf-8")
    return path


def run(harness, case):
    proc = subprocess.run([NODE, str(harness), case],
                          capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, (
        f"case {case} failed (rc={proc.returncode})\n"
        f"stdout: {proc.stdout}\nstderr: {proc.stderr}")
    return json.loads(proc.stdout.strip().splitlines()[-1])


# ---- the non-goal: PTY-driven clipboard READ -------------------------------

def test_query_form_is_refused_and_writes_nothing(harness):
    r = run(harness, "query_form_refused")
    assert r["__clipboard"] == []
    assert any("READ your clipboard" in t for t in r["__allNotices"])


def test_policy_source_cannot_reply_to_the_pty(harness):
    """The `?` form must be unanswerable, not merely unanswered: the policy has
    no way to reach the producer, and no way to read a clipboard, at all."""
    body = _osc52_source()
    for forbidden in ("sendChunked", "ws.send", "readText", "clipboard.read("):
        assert forbidden not in body, f"OSC 52 policy must not reference {forbidden}"


# ---- default off, per host, browser-local ----------------------------------

def test_default_off_blocks_the_write_but_says_so(harness):
    r = run(harness, "default_off_blocks_and_tells_you")
    assert r["__clipboard"] == []
    joined = " ".join(r["__allNotices"])
    assert "Control Panel" in joined and "Clipboard (OSC 52)" in joined
    assert r["__stickyNotices"], "the one-shot offer must not auto-dismiss"


def test_blocked_notice_is_one_shot_per_host(harness):
    r = run(harness, "default_off_notice_is_one_shot_per_host")
    assert r["writes"] == []
    assert r["noticeCount"] == 1, "a program in a loop must not spam the desktop"


def test_authorizing_one_host_does_not_authorize_another(harness):
    r = run(harness, "authorization_is_per_host")
    assert r["blocked"] == []
    assert r["allowed"] == ["hello"]


def test_authorization_never_lands_in_the_synced_prefs_blob(harness):
    """A broker must not be able to grant itself clipboard write by editing the
    /state blob it owns, so the flag lives under its own local key."""
    r = run(harness, "authorization_is_not_in_prefs")
    assert r["keys"] == ["webterm:osc52Hosts:v1"]
    assert "webterm:prefs:v1" not in r["keys"]


# ---- the accepted path ------------------------------------------------------

def test_accepted_write_reaches_the_clipboard_verbatim(harness):
    r = run(harness, "accepted_write")
    assert r["__clipboard"] == ["hello world"]


def test_every_accepted_write_is_announced(harness):
    r = run(harness, "accepted_write")
    assert any("copied" in t for t in r["__allNotices"])


def test_pc_is_a_list_not_a_scalar(harness):
    """tmux really emits `ESC]52;;<b64>` with Pc EMPTY, so an equality check
    against {c,s} would reject the commonest real emitter. `p`-only is ignored
    because the browser has one clipboard and no X11 PRIMARY to write."""
    r = run(harness, "pc_forms")
    assert r["c"] == 1 and r["s"] == 1 and r["(empty)"] == 1
    assert r["cp"] == 1 and r["c0"] == 1 and r["pc"] == 1 and r["qs"] == 1
    assert r["p"] == 0 and r["q"] == 0 and r["3"] == 0


# ---- gates -------------------------------------------------------------------

def test_backgrounded_tab_is_not_written_to(harness):
    assert run(harness, "background_tab_blocked")["__clipboard"] == []


def test_only_the_front_terminal_may_write(harness):
    assert run(harness, "non_front_terminal_blocked")["__clipboard"] == []


def test_write_without_recent_user_input_is_refused_with_a_notice(harness):
    r = run(harness, "stale_activity_blocked_with_notice")
    assert r["__clipboard"] == []
    assert any("no recent activity" in t for t in r["__allNotices"])


def test_authorization_is_reported_ahead_of_the_focus_gate(harness):
    """Gate ordering is behaviour, not taste: the user can act on "it's off",
    and cannot act on "your tab wasn't focused"."""
    r = run(harness, "gate_order_auth_before_focus")
    assert r["__clipboard"] == []
    assert any("Control Panel" in t for t in r["__allNotices"])


# ---- payload -----------------------------------------------------------------

def test_oversize_payload_is_refused_whole_and_noticed(harness):
    """Never truncated: a half-copied `rm -rf /home/me/project` is a foot-gun of
    exactly the wrong shape. And never silent -- a silent size rejection is the
    very symptom this feature exists to remove."""
    r = run(harness, "oversize_refused_not_truncated")
    assert r["__clipboard"] == []
    assert any("1 MiB" in t and "not truncated" in t for t in r["__allNotices"])


def test_a_payload_at_the_cap_is_accepted(harness):
    r = run(harness, "one_byte_under_cap_accepted")
    assert r["count"] == 1 and r["len"] == 1024 * 1024


def test_non_utf8_payload_is_refused_not_silently_substituted(harness):
    r = run(harness, "non_utf8_refused")
    assert r["__clipboard"] == []
    assert any("not valid UTF-8" in t for t in r["__allNotices"])


def test_control_characters_reject_the_whole_request(harness):
    """Reject, never strip: a silent C0 strip corrupts legitimate copies, can
    concatenate text that was separated, and keeps CR/LF/TAB -- the characters
    that actually make pastejacking dangerous."""
    r = run(harness, "control_chars_rejected_whole")
    assert r["__clipboard"] == []
    assert any("control characters" in t for t in r["__allNotices"])


def test_tab_newline_and_cr_survive_a_copy(harness):
    assert run(harness, "tab_newline_cr_are_kept")["__clipboard"] == ["a\tb\nc\rd"]


def test_line_wrapped_base64_is_accepted(harness):
    r = run(harness, "wrapped_base64_accepted")
    assert r["count"] == 1 and r["len"] == 200


def test_unpadded_base64_is_accepted(harness):
    assert run(harness, "unpadded_base64_accepted")["__clipboard"] == ["hello"]


def test_url_safe_base64_is_accepted(harness):
    r = run(harness, "urlsafe_base64_accepted")
    assert r["count"] == 1, r
    assert r["__clipboard"] == ["??>???"]


def test_invalid_base64_is_refused_with_a_notice(harness):
    r = run(harness, "invalid_base64_refused")
    assert r["__clipboard"] == []
    assert any("malformed payload" in t for t in r["__allNotices"])


def test_empty_payload_does_not_clear_the_clipboard(harness):
    """Deviation from xterm, on purpose: it treats an empty Pd as "clear". We do
    not -- "a hostile program can erase your clipboard" is a nuisance primitive
    we decline to implement."""
    r = run(harness, "empty_payload_does_not_clear")
    assert r["__clipboard"] == []
    assert r["__allNotices"] == []


def test_sequence_without_a_separator_is_ignored(harness):
    r = run(harness, "malformed_no_semicolon_ignored")
    assert r["__clipboard"] == [] and r["__allNotices"] == []


# ---- rate limit ---------------------------------------------------------------

def test_rate_limit_is_browser_global_not_per_terminal(harness):
    """Two terminals must not be able to evade the cap in parallel: the
    clipboard belongs to the document, not to a terminal."""
    r = run(harness, "rate_limit_is_browser_global")
    assert r["finalCount"] == 5, r["progression"]
    assert any("too fast" in t for t in r["notices"])


def test_rate_limit_recovers_once_the_window_rolls(harness):
    r = run(harness, "rate_limit_recovers_after_window")
    assert r["capped"] == 5 and r["after"] == 6


def test_refused_attempts_do_not_consume_quota(harness):
    """Otherwise a hostile program locks the user out of their own copy just by
    failing repeatedly."""
    assert run(harness, "refusals_do_not_consume_quota")["__clipboard"] == ["real"]


# ---- secure context -----------------------------------------------------------

def test_plain_http_origin_gets_its_own_advice(harness):
    r = run(harness, "insecure_origin_distinct_notice")
    assert r["__clipboard"] == []
    assert any("secure origin" in t for t in r["__allNotices"])


def test_osc_path_never_falls_back_to_the_focus_stealing_legacy_copy(harness):
    """copyTextLegacy selects a hidden textarea and can abort an in-flight IME
    composition. Right for a user gesture; wrong for unprompted PTY output."""
    r = run(harness, "no_legacy_textarea_fallback")
    assert r["threw"] is None and r["writes"] == []


# ---- attribution ---------------------------------------------------------------

def test_notice_attribution_comes_from_app_owned_facts(harness):
    """Never the session title -- OSC 0/2 makes titles PTY-settable, so a
    hostile program could otherwise forge the attribution on its own write."""
    r = run(harness, "notice_attributes_without_the_title")
    joined = " ".join(r["__allNotices"])
    assert "#42" in joined and "HOSTLABEL-remote1" in joined


# ---- #106 clipboard observers ----------------------------------------------------

def test_accepted_write_is_recorded_as_a_copy_out(harness):
    r = run(harness, "accepted_write_notifies_observers")
    assert r["seen"] == [["out", "hello"]]


def test_refused_write_is_not_recorded(harness):
    assert run(harness, "refused_write_notifies_nothing")["seen"] == []


# ---- copyTextToClipboard's new contract -------------------------------------------

def test_copy_text_to_clipboard_resolves_true_when_it_worked(harness):
    assert run(harness, "copy_returns_true_on_success")["ok"] is True


def test_copy_text_to_clipboard_resolves_false_for_empty_text(harness):
    assert run(harness, "copy_returns_false_on_empty")["ok"] is False


# ---- registration topology --------------------------------------------------------

def test_the_only_osc52_registration_is_the_live_terminal_one():
    """Recorder playback builds its own Terminals (recorder.js has two
    `new Terminal(` sites); registering inside openWindow is what keeps replay
    OSC-52-inert. Honest about what this proves: it pins the topology -- one
    registration, one call into the policy, neither of them in the recorder --
    which is the structural fact the inertness rests on."""
    page = ui.INDEX_HTML
    assert page.count("registerOscHandler(52") == 1
    # One definition (63) + exactly one call site (67). Any third occurrence
    # means something else learned to drive a clipboard write.
    assert page.count("osc52Request(") == 2

    life = (BROKER_DIR / "67_js_window_lifecycle.js").read_text(encoding="utf-8")
    assert "registerOscHandler(52" in life
    assert "osc52Request(" in life

    rec = (BROKER_DIR / "mods/recorder/recorder.js").read_text(encoding="utf-8")
    assert "registerOscHandler" not in rec
    assert "osc52Request" not in rec
    assert rec.count("new Terminal(") == 2, (
        "recorder playback terminal count changed -- re-check that no new "
        "Terminal site registers an OSC 52 handler")


def test_registration_is_disposed_with_the_window():
    """Not hygiene: xterm tries OSC handlers newest-first with fallthrough, so a
    leaked registration from a reopened terminal would stack."""
    life = (BROKER_DIR / "67_js_window_lifecycle.js").read_text(encoding="utf-8")
    assert "oscDisp.dispose()" in life
    disp = life.index("oscDisp.dispose()")
    push = life.rindex("win.cleanups.push", 0, disp)
    assert disp - push < 200, "oscDisp.dispose must be inside a win.cleanups push"


def test_handler_always_reports_handled():
    """Returning false would fall through to whatever registered earlier."""
    life = (BROKER_DIR / "67_js_window_lifecycle.js").read_text(encoding="utf-8")
    start = life.index("registerOscHandler(52")
    body = life[start:start + 400]
    assert "return true;" in body
    assert "return false;" not in body.split("});")[0]


def test_policy_reads_the_local_store_not_the_synced_host_cache():
    body = _osc52_source()
    assert "localStorage" in body
    assert "hostStateCache" not in body
    assert "getSettings()" not in body, (
        "OSC 52 authorization must not ride the synced settings blob")


def test_the_settings_row_writes_the_local_store_not_the_state_blob():
    """The Control Panel row is per host like the ones around it, but must not
    go through settingsTarget.save() -- that PUTs the broker's /state, which is
    exactly the thing a compromised broker controls."""
    panel = (BROKER_DIR / "81_js_control_panel.js").read_text(encoding="utf-8")
    i = panel.index("setOsc52.addEventListener")
    handler = panel[i:panel.index("});", i)]
    assert "setOsc52Allowed(t.hostId" in handler
    assert "t.save()" not in handler
    assert "t.s." not in handler


def test_defaults_to_off():
    """Absent or junk entry must read as OFF, so a corrupted store fails shut."""
    body = _osc52_source()
    assert "=== true" in body, "authorization must be an explicit opt-in"


# ---- host identity ------------------------------------------------------------

def test_repointing_a_host_url_revokes_its_authorization(harness):
    """Host ids are not as immutable as they look: the #65 registry mod
    republishes prefs._hosts WITH ids between brokers, so an id you once
    trusted can come back attached to a different machine. What the user
    authorized was that machine."""
    r = run(harness, "repointing_a_host_url_revokes_it")
    assert r["before"] == ["hello"]
    assert r["after"] == ["hello"], "the re-pointed host must not write again"


def test_unknown_or_missing_host_fails_shut(harness):
    """Including a missing hostId: a confused security gate must not fall back
    to the host the user is most likely to have authorized."""
    r = run(harness, "unknown_host_fails_shut")
    counts = {k: v for k, v in r.items() if not k.startswith("__")}
    assert counts == {"ghost": 0, "undefined": 0, "null": 0, "": 0, "app": 0}


# ---- the activity gate cannot be self-served ------------------------------------

def test_activity_stamp_is_not_taken_from_term_ondata():
    """onData looks like the obvious seam and is the wrong one: it carries
    xterm's OWN automatic replies, so a program could emit a DA1 query
    (`ESC[c`), collect xterm's answer through onData, and stamp the activity
    gate by itself before sending its clipboard request."""
    life = (BROKER_DIR / "67_js_window_lifecycle.js").read_text(encoding="utf-8")
    start = life.index("const onDataDisp = term.onData(")
    body = life[start:life.index("\n", life.index(";", start))]
    assert "markUserInput" not in body, (
        "term.onData must not stamp the OSC 52 activity gate -- it fires for "
        "xterm's automatic query replies too")
    assert "isTrusted" in life, "the activity stamp must require a trusted event"
    for ev in ("keydown", "mousedown", "touchstart", "compositionend"):
        assert f"'{ev}'" in life, f"user-input stamp is missing {ev}"


def test_read_form_is_reported_even_for_a_primary_only_request(harness):
    r = run(harness, "read_form_before_pc_filter")
    assert r["__clipboard"] == []
    assert any("READ your clipboard" in t for t in r["__allNotices"])


def test_only_ascii_whitespace_is_stripped_from_the_payload(harness):
    """\\s would also match U+00A0, which the byte-level agent strip stops at --
    the two must not disagree about the same sequence."""
    r = run(harness, "nbsp_payload_does_not_sneak_through_as_whitespace")
    assert r["__clipboard"] == []
