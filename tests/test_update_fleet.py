"""Behavioural tests for the update mod's fleet-wide state model (#182/#185).

Source-slice asserts prove a string is present. They do NOT prove that a 503
lands on 'not-opted-in' rather than on 'unreachable', that a peer which predates
GET /update/check is never asked for it, or that the chip may only say "up to
date" once every configured broker has actually answered. Those are the whole
point of this mod — "NEVER claim up to date when the truth is I could not
check" — and they are only provable by running the code.

So, like ``test_host_registry_sources.py`` and ``test_osc52_clipboard.py``,
this executes the SHIPPED file: a set of declaration-only ranges cut out of the
real ``mods/update/update.js`` and run in node against a stub browser. The mod
body lives inside ``init: function (ctx) {...}``, so the slice is taken as
several contiguous declaration-only chunks (the reason table + per-host records,
the state derivation, the capability probe, poll and pollTick) which are
concatenated and evaluated at module scope. Everything they reach for that the
page would normally supply — allHosts/hostById, hostFetch, fetchModCatalog and
its shared modCatalogCache, renderAll — is stubbed, so what runs here is the
mod's own logic and nothing else. Pasting a copy of that logic into the test
would pass while the mod was broken; this cannot.

Three files, one model. The A29/A30 post-apply + apply-gate helpers moved out
of update.js into ``mods/update/update-apply.js``, and (atom A4) the #183
restart-reason words moved into ``mods/update/update-policy.js`` -- companion
scripts with no ``registerMod`` call, spliced immediately BEFORE update.js in
ui.py's ``_MODS`` in that order (policy, apply, update.js -- the same split
``mods/editor/codemirror.js``/``editor.js`` already use). The model source
below reads both companions WHOLE (each is pure top-level declarations
already, nothing to slice) and prepends them to update.js's own sliced
chunks, in that same _MODS order, so the concatenation node evaluates
matches what the served page actually loads.

Skipped when node is absent, so the suite still runs on a box without it.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from webterm.broker import ui

BROKER_DIR = Path(ui.__file__).resolve().parent
MOD_JS = BROKER_DIR / "mods" / "update" / "update.js"
MOD_POLICY_JS = BROKER_DIR / "mods" / "update" / "update-policy.js"
MOD_APPLY_JS = BROKER_DIR / "mods" / "update" / "update-apply.js"
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="node not installed")

# (start, end) anchors for each declaration-only range, in file order. Every
# chunk begins on a declaration and ends immediately before the next thing that
# is not one, so the concatenation runs at module scope with no side effects.
_CHUNKS = (
    # REASONS + the per-host record map (hostChecks/checkStateFor) + updHost
    ("const REASONS = {", "let timer = null;"),
    # state/reasonCode/bandFor/peerState/hostRows (answered/stateWords/
    # WORST_FIRST/aggregate/chipLabel moved WHOLE to the update-policy.js
    # companion in A6, so they ride the prepended read below instead)
    ("function state(st) {", "// ONE chip for the whole fleet."),
    ("function chipTitle(st) {", "// ---- can this broker even be asked? (#182) ----"),
    # the capability probe
    ("function servesUpdateMod(mods) {", "// ---- the poll ----"),
    ("async function poll(hostId, opts) {", "// The driver. EVERY configured host"),
    ("function pollTick(opts) {", "function start() {"),
    # #182: the opt-in — updateCapFor/policyMutableFor/hostFingerprint/
    # setChecking/offerConsent. Declaration-only like the rest; the row that
    # renders it and its confirm dialog are NOT in range (they touch the DOM).
    ("const policyOps = new Map();", "// ---- self-restart (#183) ---"),
    # #183: the restart reason words (RESTART_REASONS + restartReasonWords)
    # moved out of update.js into the mods/update/update-policy.js companion
    # (atom A4) -- read whole below, the same way MOD_APPLY_JS already is --
    # so the R6 cooldown sentence is still proven against the SHIPPED table.
)

_REQUIRED = (
    "const REASONS", "function checkStateFor", "function updHost",
    "function state(st)", "function reasonCode", "function bandFor",
    "const PEER_FAILURES", "function peerState", "function answered",
    "function stateWords", "const WORST_FIRST", "function hostRows",
    "function aggregate", "function chipLabel", "function chipTitle",
    "function servesUpdateMod", "function capabilityFrom",
    "async function capabilityFor", "async function poll",
    "function pollTick", "function pruneChecks", "function recheck",
    "function updateCapFor", "function policyMutableFor",
    "function hostFingerprint",
    "async function setChecking", "async function offerConsent",
    # #182 Part 2 (A5): the three-grant consent. The pure half ships in the
    # update-policy.js companion (read whole, above); setPolicy is the
    # machinery in update.js's own sliced range that the check switch and
    # the consent click both go through.
    "function policyKeysFor", "function consentBody",
    "function policyWriteOutcome", "async function setPolicy",
    # freshTerminalHost is the one function that did NOT move to
    # update-apply.js -- it reads updHost/LOCAL_HOST_ID off THIS closure, so
    # it stays between the opt-in block and the self-restart marker, and the
    # existing final chunk still carries it without a new anchor.
    "function freshTerminalHost",
    # #182 Part 2 (A29) + atom A30: the post-apply model and the
    # Update-apply client-side gate/refusal parser. Pure, and now shipped in
    # the companion mods/update/update-apply.js (read whole, below) rather
    # than sliced out of update.js -- no anchor needed for these six.
    "function deployOutcome", "function shortSha",
    "function staleSurvivors", "function deployStrip",
    "function applyTargetSha", "function applyGateFromFacts",
    "function applyGateWords", "function applyRefusalOutcome",
    # The forced-refresh refusal sentences: a "Check now" the broker declined
    # must never read as "just checked".
    "function refreshRefusedWords",
    # #183 R6: the restart reason words, now shipped in the companion
    # mods/update/update-policy.js (read whole, above) rather than sliced
    # out of update.js -- so the cooldown sentence tested is still the
    # shipped one.
    "const RESTART_REASONS", "function restartReasonWords",
    # #182 Part 2 (A6): the self-update row. The pure model and its
    # word-builders ship in update-policy.js; the non-DOM closure wiring
    # (needed/model-for/the post-dialog commit) rides update.js's sliced
    # opt-in range so the harness runs the shipped code paths a click
    # exercises, minus the button itself.
    "function selfUpdateRowModel", "function policyChangesFor",
    "function selfUpdateBusyNote", "function selfUpdateConfirmWords",
    "function policyCheckSource",
    "function selfUpdateRowNeeded", "function selfUpdateModelFor",
    "async function commitRemoteSelfUpdate",
)


def _model_source() -> str:
    # Both companions are read WHOLE -- each is already pure top-level
    # declarations with nothing to slice, the same way a real page load
    # includes the whole file -- and prepended in their _MODS order
    # (policy, then apply, both immediately BEFORE update.js).
    policy = MOD_POLICY_JS.read_text(encoding="utf-8")
    apply_companion = MOD_APPLY_JS.read_text(encoding="utf-8")
    src = MOD_JS.read_text(encoding="utf-8")
    out = [policy, apply_companion]
    last = -1
    for start_marker, end_marker in _CHUNKS:
        start = src.index(start_marker)
        end = src.index(end_marker)
        assert last < start < end, (
            f"slice markers out of order around {start_marker!r}")
        last = end
        out.append(src[start:end])
    body = "\n".join(out)
    for needed in _REQUIRED:
        assert needed in body, f"{needed} missing from the sliced range"
    # A slice that dragged in the renderers would need a DOM to load, and the
    # chip element is built at init top level — proof the ranges stayed pure.
    assert "document.createElement" not in body, "slice reached the DOM"
    return body


_HARNESS = r"""
'use strict';
// ---- stub page ----------------------------------------------------------
// Everything the mod reaches for from core, and nothing more. The stubs are
// modelled on the real ones: allHosts()/hostById() over the live host list
// (56_js_hosts.js), hostFetch() rejecting on a transport failure and resolving
// with a Response-alike otherwise (63_js_clipboard_auth.js), and
// fetchModCatalog() landing its outcome — failures included — in the shared
// modCatalogCache (81_js_control_panel.js).
let HOSTS = [];
let INFO = {};          // hostId -> cached /info record, or 'throw'
let CHECK = {};         // hostId -> {status, ok, body} | 'throw'
const infoCalls = [];   // one entry per capability probe that hit the network
const checkCalls = [];  // one entry per GET /update/check actually issued
let renderCalls = 0;

globalThis.allHosts = () => HOSTS;
globalThis.hostById = (id) => HOSTS.find((h) => h.id === id) || null;
globalThis.modCatalogCache = new Map();
globalThis.renderAll = () => { renderCalls++; };

// The five shapes fetchModCatalog can cache, verbatim from its own doc block.
const INFO_DOWN = { state: 'unreachable', mods: [], modsEnabled: true,
                    policy: {}, update: null };
const INFO_401 = { state: 'unauthorized', mods: [], modsEnabled: true,
                   policy: {}, update: null };
const INFO_PRE157 = { state: 'unsupported', mods: [], modsEnabled: true,
                      policy: {}, update: null };
const INFO_HEADLESS = { state: 'headless', mods: [], modsEnabled: true,
                        policy: {}, update: null };
// A build that predates #182: answers /info, serves a UI, no update mod, no
// `update` key.
const INFO_OLD = { state: 'ok', mods: [{ id: 'editor' }, { id: 'aistatus' }],
                   modsEnabled: true, policy: {}, update: null };
// Has the route (the mod is in its catalog) but published no capability key.
const INFO_NOCAP = { state: 'ok', mods: [{ id: 'editor' }, { id: 'update' }],
                     modsEnabled: true, policy: {}, update: null };
// A current broker, with the capability key saying yes or no.
// #182: `source`/`mutable` ride the same key. `mutable` is false only when the
// broker's own config names update_check_enabled, so the default here — the
// common case — is a gate this browser is allowed to change.
// A5: the three-key `policy` block rides the same view. Derived from the
// MERGED flat fields — so an `over` that locks the check cannot leave the
// block contradicting it — with apply/restart defaulting to config-owned-off
// here, which keeps the pre-A5 consent cases meaning what they meant (one
// grantable gate). A case about the other gates overrides `policy` itself;
// `policy: undefined` in `over` models the flat-only build that predates it.
const INFO_MODERN = (on, over) => {
    const upd = Object.assign(
        { check_enabled: on !== false, apply_enabled: false,
          source: on !== false ? 'stored' : 'default', mutable: true,
          remote_writable: true },
        over || {});
    if (!('policy' in upd)) {
        upd.policy = {
            check: { enabled: upd.check_enabled, source: upd.source,
                     mutable: upd.source !== 'config' },
            apply: { enabled: upd.apply_enabled, source: 'config',
                     mutable: false },
            restart: { enabled: false, source: 'config', mutable: false },
        };
    }
    return { state: 'ok', mods: [{ id: 'update' }], modsEnabled: true,
             policy: {}, update: upd };
};

const OK200 = (check) => ({ status: 200, ok: true, body: { ok: true, check } });
// What a broker that never opted in actually sends (app.py's gate).
const R503 = { status: 503, ok: false,
               body: { ok: false, error: 'update_check_disabled' } };

globalThis.fetchModCatalog = async (host) => {
    if (!host) throw new Error('fetchModCatalog got a null host');
    infoCalls.push(host.id);
    const rec = INFO[host.id];
    // The real one never throws; this proves the probe's own guard anyway.
    if (rec === 'throw') throw new Error('probe blew up');
    modCatalogCache.set(host.id, rec || INFO_DOWN);
};
// #182: what POST /update/policy answers, per host, and every write that was
// attempted. `policyCalls` is the evidence for the rule that matters most here
// — nothing may ask a broker to open its gate except a human's click.
// `policyBodies` (A5) is the WHOLE posted body, one entry per policyCalls
// entry at the same index — the three-grant consent is about exactly which
// keys ride one request, which the `want` shorthand cannot show.
let POLICY = {};
const policyCalls = [];
const policyBodies = [];

globalThis.hostFetch = async (host, path, opts) => {
    // A null host would silently hit the SERVING origin — the exact lie the
    // mod is written to prevent, so it is a hard failure here.
    if (!host) throw new Error('hostFetch got a null host');
    if (path === '/update/policy') {
        const sent = JSON.parse((opts && opts.body) || '{}');
        policyCalls.push({ id: host.id, method: (opts && opts.method) || 'GET',
                           want: sent.check_enabled });
        policyBodies.push(sent);
        const spec = POLICY[host.id];
        if (!spec || spec === 'throw') throw new TypeError('Failed to fetch');
        return {
            status: spec.status,
            ok: spec.status >= 200 && spec.status < 300,
            json: async () => spec.body,
        };
    }
    if (path !== '/update/check') throw new Error('unexpected path: ' + path);
    checkCalls.push(host.id);
    const spec = CHECK[host.id];
    if (!spec || spec === 'throw') throw new TypeError('Failed to fetch');
    return {
        status: spec.status,
        ok: (spec.ok !== undefined) ? spec.ok
            : (spec.status >= 200 && spec.status < 300),
        json: async () => {
            if (spec.badJson) throw new Error('not json');
            return spec.body;
        },
    };
};

__MODEL__

// ---- driver -------------------------------------------------------------
function host(id, over) {
    return Object.assign({
        id: id, label: (id === 'local' ? 'this broker' : id),
        url: (id === 'local' ? '' : 'https://' + id + '.example:4445'),
        token: '', color: '', hidden: false,
    }, over || {});
}
function fleet(ids, over) {
    HOSTS = ids.map((id) => host(id, (over || {})[id]));
}
// The record shapes poll() writes, so the aggregate cases can stand up a fleet
// without a round trip. Written THROUGH checkStateFor — the mod's own accessor.
const CUR = { check: { state: 'current' }, error: null, checkedAt: 1000 };
const BEHIND = (n) => ({ check: { state: 'behind', behindBy: n },
                         error: null, checkedAt: 1000 });
const AHEAD = { check: { state: 'ahead-or-diverged', aheadBy: 2, behindBy: 0 },
                error: null, checkedAt: 1000 };
const FAIL = (code) => ({ check: null, error: code, checkedAt: 1000 });
function setRec(id, rec) { Object.assign(checkStateFor(id), rec); }
function reset() {
    hostChecks.clear(); modCatalogCache.clear(); HOSTS = [];
    POLICY = {}; policyCalls.length = 0; policyBodies.length = 0;
    infoCalls.length = 0;
    checkCalls.length = 0; consentSent = false;
    policyOps.clear(); lastWrite.clear();
}
// policyOps is keyed hostId + '|' + kind since A5; every case here is about
// the checking switch, so the reads go through this.
const opFor = (id) => policyOps.get(id + '|check');
function rowLine(r) {
    return r.id + '|' + r.ps + '|' + r.words + '|' + (reasonCode(r.st) || '-');
}
function snapshot() {
    const rows = hostRows();
    const agg = aggregate(rows);
    return {
        rows: rows.map(rowLine),
        states: rows.map((r) => r.ps),
        worst: agg.worst, allCurrent: agg.allCurrent, text: agg.text,
        lines: agg.lines,
    };
}

const CASES = {};

// --- every peer state, read off a record ---------------------------------
CASES.peer_state_table = async () => {
    const rec = (over) => Object.assign(
        { hostId: 'h', check: null, error: null, checkedAt: 1000,
          inFlight: false }, over || {});
    const read = (st) => ({
        ps: peerState(st), words: stateWords(peerState(st), st),
        answered: answered(peerState(st)), coarse: state(st),
        reason: reasonCode(st), band: bandFor(peerState(st)),
    });
    const out = {};
    // The four ways a PEER leaves us without an answer…
    out.routeAbsent = read(rec({ error: 'route-absent' }));
    out.notOptedIn = read(rec({ error: 'not-opted-in' }));
    out.unauthorized = read(rec({ error: 'unauthorized' }));
    out.unreachable = read(rec({ error: 'unreachable' }));
    // …and the check that RAN and failed, carrying its reason.
    out.unknownRateLimited = read(rec({
        check: { state: 'unknown', reason: 'rate-limited' } }));
    out.unknownNoGit = read(rec({
        check: { state: 'unknown', reason: 'no-git' } }));
    out.unknownOffline = read(rec({
        check: { state: 'unknown', reason: 'offline' } }));
    // The fifth peer failure (#185): a headless peer that was asked and did not
    // answer, where "asleep" and "too old to have the route" are genuinely
    // indistinguishable from here.
    out.unreachableOrTooOld = read(rec({
        error: 'unreachable-or-too-old' }));
    out.brokerError = read(rec({ error: 'broker-error' }));
    out.noSuchHost = read(rec({ error: 'no-such-host' }));
    // Nothing has come back yet.
    out.pending = read(rec({ checkedAt: 0 }));
    out.pendingBeatsPayload = read(rec({
        checkedAt: 0, check: { state: 'current' } }));
    out.missing = read(null);
    // The three that DID answer.
    out.current = read(rec({ check: { state: 'current' } }));
    out.behind1 = read(rec({ check: { state: 'behind', behindBy: 1 } }));
    out.behind3 = read(rec({ check: { state: 'behind', behindBy: 3 } }));
    out.behindNoCount = read(rec({ check: { state: 'behind' } }));
    out.ahead = read(rec({
        check: { state: 'ahead-or-diverged', aheadBy: 2, behindBy: 1 } }));
    out.garbage = read(rec({ check: { nope: true } }));
    out.reasons = {};
    for (const k of ['route-absent', 'not-opted-in', 'unauthorized',
                     'unreachable', 'unreachable-or-too-old', 'broker-error',
                     'no-such-host', 'offline', 'rate-limited']) {
        out.reasons[k] = REASONS[k] || null;
    }
    return out;
};

// --- the capability probe's five outcomes --------------------------------
CASES.capability_table = async () => {
    const out = {};
    out.keyOn = capabilityFrom(INFO_MODERN(true));
    out.keyOff = capabilityFrom(INFO_MODERN(false));
    out.catalogOnly = capabilityFrom(INFO_NOCAP);
    out.old = capabilityFrom(INFO_OLD);
    out.pre157 = capabilityFrom(INFO_PRE157);
    out.headless = capabilityFrom(INFO_HEADLESS);
    out.headlessKeyOff = capabilityFrom(Object.assign({}, INFO_HEADLESS,
        { update: { check_enabled: false } }));
    // A headless peer that DOES publish the capability is not ambiguous at all:
    // the key only exists on a build that registers the route.
    out.headlessKeyOn = capabilityFrom(Object.assign({}, INFO_HEADLESS,
        { update: { check_enabled: true } }));
    out.unauthorized = capabilityFrom(INFO_401);
    out.unreachable = capabilityFrom(INFO_DOWN);
    out.noRecord = capabilityFrom(undefined);
    // A peer's catalog is untrusted input: junk rows must be stepped over,
    // never thrown on.
    out.junkRows = capabilityFrom({ state: 'ok', update: null,
        mods: [null, 'x', 7, { id: 'update' }] });
    out.modsNotArray = capabilityFrom({ state: 'ok', update: null,
                                        mods: 'nope' });
    // A capability key on an UNAUTHORIZED record is our own placeholder, not
    // evidence — it must not be read back as if the peer had said it.
    out.unauthorizedWithKey = capabilityFrom(Object.assign({}, INFO_401,
        { update: { check_enabled: true } }));
    return out;
};

// --- the fleet, end to end through poll ----------------------------------
CASES.fleet_poll = async () => {
    fleet(['local', 'old', 'gated', 'capoff', 'nopw', 'asleep', 'garbled',
           'ratelimited', 'stale']);
    INFO = {
        local: INFO_MODERN(true),
        old: INFO_OLD,                 // predates the route
        gated: INFO_NOCAP,             // has the route, gate unknown from /info
        capoff: INFO_MODERN(false),    // /info already said it is switched off
        nopw: INFO_401,
        asleep: INFO_DOWN,
        garbled: INFO_MODERN(true),
        ratelimited: INFO_MODERN(true),
        stale: INFO_MODERN(true),
    };
    CHECK = {
        local: OK200({ state: 'current', local: { version: '1.0.0',
                                                  sha: 'abcdef0123' } }),
        gated: R503,                   // the wire says it: not opted in
        garbled: OK200(null),          // 200 with nothing usable in it
        ratelimited: OK200({ state: 'unknown', reason: 'rate-limited' }),
        stale: OK200({ state: 'behind', behindBy: 4 }),
    };
    await pollTick();
    const snap = snapshot();
    snap.infoCalls = infoCalls.slice();
    snap.checkCalls = checkCalls.slice();
    snap.rendered = renderCalls;
    return snap;
};

// --- a 503 is an ANSWER, not a failed trip -------------------------------
CASES.gate_503 = async () => {
    fleet(['local']);
    INFO = { local: INFO_NOCAP };      // route present, capability unpublished
    CHECK = { local: R503 };
    await pollTick();
    const st = checkStateFor('local');
    const ps = peerState(st);
    return {
        ps: ps, error: st.error, check: st.check, reason: reasonCode(st),
        words: stateWords(ps, st), coarse: state(st), band: bandFor(ps),
        answered: answered(ps), title: chipTitle(st),
        // the same verdict reached WITHOUT a request, from /info's key
        viaCapability: capabilityFrom(INFO_MODERN(false)),
        offlineReason: REASONS['offline'],
        notOptedInReason: REASONS['not-opted-in'],
        unreachableReason: REASONS['unreachable'],
        checkCalls: checkCalls.slice(),
    };
};

// --- a peer that predates the route is never asked ------------------------
CASES.route_absent_never_asked = async () => {
    fleet(['local', 'old', 'pre157']);
    INFO = { local: INFO_MODERN(true), old: INFO_OLD, pre157: INFO_PRE157 };
    CHECK = { local: OK200({ state: 'current' }),
              // If the mod ever DID ask, this would answer — so a zero count
              // below is the probe's doing, not the stub's.
              old: OK200({ state: 'current' }),
              pre157: OK200({ state: 'current' }) };
    await pollTick();
    const first = { info: infoCalls.slice(), check: checkCalls.slice(),
                    states: hostRows().map((r) => r.ps) };
    await pollTick();               // the cached failure must not be re-probed
    const second = { info: infoCalls.slice(), check: checkCalls.slice() };
    await recheck();                // "Check now" IS the deliberate retry
    const third = { info: infoCalls.slice(), check: checkCalls.slice() };
    return { first, second, third };
};

// --- a headless peer that does not answer (#185) -------------------------
// The record shapes here are the ones fetchModCatalog really caches, and the
// request really fails the way a dead preflight arrives (hostFetch REJECTS,
// not a status). Reading capabilityFrom's return value in isolation cannot see
// this: 'unproven' is an internal tag that never reaches a record, so the only
// thing worth grading is the state that lands on the SCREEN.
CASES.headless_ambiguity = async () => {
    fleet(['local', 'headless', 'ghosted', 'down', 'old']);
    INFO = {
        local: INFO_MODERN(true),
        headless: INFO_HEADLESS,      // answered /info, serves no page, no key
        ghosted: INFO_MODERN(true),   // answered /info, then went away
        down: INFO_DOWN,              // never answered /info at all
        old: INFO_OLD,                // serves a UI, predates the route
    };
    // Only 'local' answers; every other CHECK entry is absent, so the stub
    // rejects exactly as a black-holed broker or a dead preflight does.
    CHECK = { local: OK200({ state: 'current' }) };
    await pollTick();
    const read = (id) => {
        const st = checkStateFor(id);
        const ps = peerState(st);
        return { ps: ps, error: st.error, check: st.check,
                 reason: reasonCode(st), words: stateWords(ps, st),
                 coarse: state(st), band: bandFor(ps), answered: answered(ps),
                 title: chipTitle(st) };
    };
    const out = { snap: snapshot(), checkCalls: checkCalls.slice() };
    for (const id of ['local', 'headless', 'ghosted', 'down', 'old']) {
        out[id] = read(id);
    }
    out.reasons = {
        ambiguous: REASONS['unreachable-or-too-old'],
        unreachable: REASONS['unreachable'],
        routeAbsent: REASONS['route-absent'],
    };
    return out;
};

// --- …and one that DOES answer is not left ambiguous ---------------------
// 'unproven' must ask like 'ready' asks. If it ever refused instead, every
// headless broker in the fleet would report a failure it never had.
CASES.headless_answers = async () => {
    fleet(['local', 'hcur', 'hbehind', 'hgate', 'hnopw']);
    INFO = { local: INFO_MODERN(true), hcur: INFO_HEADLESS,
             hbehind: INFO_HEADLESS, hgate: INFO_HEADLESS,
             hnopw: INFO_HEADLESS };
    CHECK = {
        local: OK200({ state: 'current' }),
        hcur: OK200({ state: 'current', local: { version: '1.0.0' } }),
        hbehind: OK200({ state: 'behind', behindBy: 2 }),
        // The pre-loaded ambiguity must never survive a real answer.
        hgate: R503,
        hnopw: { status: 401, ok: false, body: { ok: false } },
    };
    await pollTick();
    const snap = snapshot();
    snap.checkCalls = checkCalls.slice();
    return snap;
};

// --- a 401 on the WIRE is a refused password, not an unreadable answer ---
// Reachable in practice because modCatalogCache has no TTL: a token that goes
// stale after page load leaves a cached 'ok' record, so the probe says 'ready'
// and every later check 401s.
CASES.wire_unauthorized = async () => {
    fleet(['local', 'stale401', 'forbidden403', 'broken500']);
    INFO = { local: INFO_MODERN(true), stale401: INFO_MODERN(true),
             forbidden403: INFO_MODERN(true), broken500: INFO_MODERN(true) };
    CHECK = {
        local: OK200({ state: 'current' }),
        stale401: { status: 401, ok: false,
                    body: { ok: false, error: 'unauthorized' } },
        forbidden403: { status: 403, ok: false, body: { ok: false } },
        // The mapping must stay NARROW: any other unreadable answer is still
        // the broker's answer being unusable.
        broken500: { status: 500, ok: false, body: { ok: false } },
    };
    await pollTick();
    const read = (id) => {
        const st = checkStateFor(id);
        const ps = peerState(st);
        return { ps: ps, error: st.error, check: st.check,
                 reason: reasonCode(st), words: stateWords(ps, st),
                 coarse: state(st), band: bandFor(ps), answered: answered(ps),
                 title: chipTitle(st) };
    };
    const out = { snap: snapshot(), checkCalls: checkCalls.slice(),
                  infoCalls: infoCalls.slice() };
    for (const id of ['local', 'stale401', 'forbidden403', 'broken500']) {
        out[id] = read(id);
    }
    out.reasons = { unauthorized: REASONS['unauthorized'],
                    brokerError: REASONS['broker-error'] };
    // What the probe alone made of these hosts: 'ready' every time, which is
    // why the 401 could only ever be caught on the wire path.
    out.cachedCapability = ['stale401', 'forbidden403', 'broken500'].map(
        (id) => capabilityFrom(modCatalogCache.get(id)));
    return out;
};

// --- an answer never survives the failure that follows it ----------------
CASES.no_current_without_an_answer = async () => {
    fleet(['local']);
    INFO = { local: INFO_MODERN(true) };
    CHECK = { local: OK200({ state: 'current' }) };
    await pollTick();
    const before = { ps: peerState(checkStateFor('local')),
                     agg: aggregate(hostRows()).allCurrent };
    // The same broker goes away. Its record must not keep saying 'current'.
    INFO = { local: INFO_DOWN };
    CHECK = {};
    modCatalogCache.clear();          // force a re-probe, as recheck() does
    await pollTick();
    const after = { ps: peerState(checkStateFor('local')),
                    check: checkStateFor('local').check,
                    agg: aggregate(hostRows()).allCurrent };
    // …and the same for a broker that answered and then refused our password.
    INFO = { local: INFO_401 };
    modCatalogCache.clear();
    await pollTick();
    const refused = { ps: peerState(checkStateFor('local')),
                      check: checkStateFor('local').check };
    // A host removed between scheduling and firing: no host to ask, and
    // emphatically no request to the serving origin under its name.
    HOSTS = [];
    await poll('ghost');
    const ghost = { ps: peerState(checkStateFor('ghost')),
                    error: checkStateFor('ghost').error,
                    check: checkStateFor('ghost').check };
    return { before, after, refused, ghost, checkCalls: checkCalls.slice() };
};

// --- the aggregate rule ---------------------------------------------------
CASES.aggregate_rules = async () => {
    const out = {};
    const run = (name, ids, recs, over) => {
        reset();
        fleet(ids, over);
        for (const id of Object.keys(recs)) setRec(id, recs[id]);
        out[name] = snapshot();
    };
    run('all_current', ['local', 'b', 'c'],
        { local: CUR, b: CUR, c: CUR });
    run('one_pending', ['local', 'b', 'c'],
        { local: CUR, b: CUR });                     // c never answered
    run('one_route_absent', ['local', 'b', 'c'],
        { local: CUR, b: CUR, c: FAIL('route-absent') });
    run('one_not_opted_in', ['local', 'b', 'c'],
        { local: CUR, b: CUR, c: FAIL('not-opted-in') });
    run('one_unauthorized', ['local', 'b', 'c'],
        { local: CUR, b: CUR, c: FAIL('unauthorized') });
    run('one_unreachable', ['local', 'b', 'c'],
        { local: CUR, b: CUR, c: FAIL('unreachable') });
    run('one_unreachable_or_too_old', ['local', 'b', 'c'],
        { local: CUR, b: CUR, c: FAIL('unreachable-or-too-old') });
    // …and it does not outrank the one state with something to DO about it.
    run('ambiguous_vs_behind', ['local', 'b', 'c'],
        { local: CUR, b: BEHIND(1), c: FAIL('unreachable-or-too-old') });
    run('one_unknown', ['local', 'b', 'c'],
        { local: CUR, b: CUR,
          c: { check: { state: 'unknown', reason: 'rate-limited' },
               error: null, checkedAt: 1000 } });
    run('one_behind', ['local', 'b', 'c'],
        { local: CUR, b: BEHIND(2), c: CUR });
    run('one_ahead', ['local', 'b', 'c'],
        { local: CUR, b: AHEAD, c: CUR });
    // A fault is never hidden behind a healthy majority, and one abnormal host
    // never masks another: the worst colours it, the text counts them all.
    run('worst_wins', ['local', 'b', 'c', 'd'],
        { local: CUR, b: BEHIND(1), c: FAIL('unreachable'),
          d: FAIL('route-absent') });
    // Hidden parks a broker on the desktop; it does not make its build fresher.
    run('hidden_still_counted', ['local', 'b'],
        { local: CUR, b: FAIL('unreachable') }, { b: { hidden: true } });
    run('all_current_hidden', ['local', 'b'],
        { local: CUR, b: CUR }, { b: { hidden: true } });
    // Vacuously true is still a claim this mod may not make.
    reset();
    out.empty = { rows: [], states: [] };
    const agg = aggregate([]);
    Object.assign(out.empty, { worst: agg.worst, allCurrent: agg.allCurrent,
                               text: agg.text, lines: agg.lines });
    return out;
};

// --- one broker still reads exactly as it did ----------------------------
CASES.single_host = async () => {
    const out = {};
    const one = (name, rec) => {
        reset();
        fleet(['local']);
        if (rec) setRec('local', rec);
        const rows = hostRows();
        const st = rows[0].st;
        const s = state(st);              // the coarse, single-broker vocabulary
        out[name] = {
            count: rows.length, coarse: s, ps: rows[0].ps,
            chip: chipLabel(st, s), title: chipTitle(st), band: bandFor(s),
            words: rows[0].words,
            // renderChip() hides the chip on `s === 'current'` for one host and
            // on aggregate().allCurrent for several. The two readings must
            // agree, or fanning out would have changed what one broker shows.
            quietOne: (s === 'current'),
            quietMany: aggregate(rows).allCurrent,
            aggText: aggregate(rows).text,
        };
    };
    one('current', CUR);
    one('behind2', BEHIND(2));
    one('behind0', { check: { state: 'behind' }, error: null,
                     checkedAt: 1000 });
    one('ahead', AHEAD);
    one('notOptedIn', FAIL('not-opted-in'));
    one('unreachable', FAIL('unreachable'));
    one('routeAbsent', FAIL('route-absent'));
    one('unauthorized', FAIL('unauthorized'));
    one('unknown', { check: { state: 'unknown', reason: 'rate-limited' },
                     error: null, checkedAt: 1000 });
    one('pending', null);
    return out;
};

// --- #182: opting the LOCAL broker in ------------------------------------
// The rule under test is not "does the button work" but "what is allowed to
// press it". Granting egress is irreversible in the only way that counts — an
// address, once disclosed, cannot be undisclosed — so anything that reaches
// POST /update/policy without a human behind it is a bug.
CASES.consent = async () => {
    const out = {};
    const OK = (on) => ({ status: 200, body: { ok: true, update: {
        check_enabled: on, apply_enabled: false,
        source: 'stored', mutable: true, remote_writable: true } } });

    // A human ticked the mod on and this broker is off, unlocked, ours.
    reset();
    fleet(['local']);
    INFO = { local: INFO_MODERN(false) };
    CHECK = { local: OK200({ state: 'current' }) };
    POLICY = { local: OK(true) };
    // Exactly what init() does on a freshly-ticked mod: consent, then the
    // ordinary fleet tick. The check must land in that same pass, and land
    // ONCE -- offerConsent skips its own poll precisely so this one is not a
    // duplicate of it.
    await offerConsent();
    await pollTick();
    out.click = { posted: policyCalls.slice(),
                  cap: updateCapFor('local'),
                  checked: checkCalls.slice(),
                  state: peerState(checkStateFor('local')) };

    // Same broker, but nobody clicked — this is what a plain page load, a
    // synced preference or a broker-side pin looks like from here.
    reset();
    fleet(['local']);
    INFO = { local: INFO_MODERN(false) };
    CHECK = { local: R503 };
    POLICY = { local: OK(true) };
    await pollTick();
    out.noClick = { posted: policyCalls.slice(),
                    state: peerState(checkStateFor('local')) };

    // Once per page load, whatever else happens.
    reset();
    fleet(['local']);
    INFO = { local: INFO_MODERN(false) };
    CHECK = { local: OK200({ state: 'current' }) };
    POLICY = { local: OK(true) };
    await offerConsent();
    await offerConsent();
    await offerConsent();
    out.onceOnly = policyCalls.length;

    // Already checking: nothing to ask for, so no request is spent.
    reset();
    fleet(['local']);
    INFO = { local: INFO_MODERN(true) };
    CHECK = { local: OK200({ state: 'current' }) };
    POLICY = { local: OK(true) };
    await offerConsent();
    out.alreadyOn = policyCalls.slice();

    // The operator's config names the key. Locked: not ours to change, and the
    // click must not even try.
    reset();
    fleet(['local']);
    INFO = { local: INFO_MODERN(false, { source: 'config', mutable: false }) };
    CHECK = { local: R503 };
    POLICY = { local: OK(true) };
    await offerConsent();
    out.locked = { posted: policyCalls.slice(),
                   mutable: policyMutableFor('local') };

    // A build too old to have the route publishes no capability at all.
    reset();
    fleet(['local']);
    INFO = { local: INFO_OLD };
    CHECK = { local: OK200({ state: 'current' }) };
    await offerConsent();
    out.tooOld = { posted: policyCalls.slice(), cap: updateCapFor('local') };

    // PEERS. A fleet where every other broker is switched off must produce
    // exactly one write, aimed at 'local', however many rows there are.
    reset();
    fleet(['local', 'peerA', 'peerB']);
    INFO = { local: INFO_MODERN(false), peerA: INFO_MODERN(false),
             peerB: INFO_MODERN(false) };
    CHECK = { local: OK200({ state: 'current' }), peerA: R503, peerB: R503 };
    POLICY = { local: OK(true), peerA: OK(true), peerB: OK(true) };
    await offerConsent();
    await pollTick();
    out.peers = { posted: policyCalls.slice(),
                  peerAState: peerState(checkStateFor('peerA')) };

    // Everything below starts from a page that has already polled once, which
    // is what the button's callers have actually done: /info is cached, so
    // these exercise the write against a known capability rather than against
    // an empty cache no real click could see.
    const primed = async (info, policy) => {
        reset();
        fleet(['local']);
        INFO = { local: info };
        CHECK = { local: R503 };
        POLICY = { local: policy };
        await pollTick();
        policyCalls.length = 0;
    };

    // The revoke, and the fact that nothing sends `false` on its own: a full
    // poll after it must not put the grant back.
    reset();
    fleet(['local']);
    INFO = { local: INFO_MODERN(true) };
    CHECK = { local: OK200({ state: 'current' }) };
    POLICY = { local: OK(false) };
    await pollTick();
    policyCalls.length = 0;
    await setChecking('local', false);
    const afterRevoke = policyCalls.slice();
    // The broker now genuinely refuses, and says so on /info too. refresh:true
    // because a plain tick reuses the cached /info and would never see it --
    // "Check now" is the deliberate re-read, and it is what retires the write.
    INFO = { local: INFO_MODERN(false) };
    CHECK = { local: R503 };
    await pollTick({ refresh: true });
    out.revoke = { posted: afterRevoke, postedAfterPoll: policyCalls.slice(),
                   cap: updateCapFor('local') };

    // A broker that 404s the route (it predates it) is reported as a fact
    // about that build, and the failure never claims the gate changed.
    await primed(INFO_MODERN(false), { status: 404, body: { ok: false } });
    const ok404 = await setChecking('local', true);
    out.notFound = { ok: ok404, note: (opFor('local')||{}).note,
                     phase: (opFor('local')||{}).phase,
                     cap: updateCapFor('local') };

    // And a 409 from a config-locked broker says whose decision it is.
    await primed(INFO_MODERN(false),
                 { status: 409, body: { ok: false, error: 'policy_locked' } });
    const ok409 = await setChecking('local', true);
    out.lockedWrite = { ok: ok409, phase: (opFor('local')||{}).phase,
                        note: (opFor('local')||{}).note };

    // A transport failure must not be reported as a changed gate either.
    await primed(INFO_MODERN(false), 'throw');
    const okDead = await setChecking('local', true);
    out.unreachable = { ok: okDead, phase: (opFor('local')||{}).phase,
                        cap: updateCapFor('local') };
    return out;
};

// --- #182: switching a PEER on, from another broker's desktop -------------
// The shape the local-only first cut could not do: an operator sits at one
// desktop and the broker that needs switching on is a different machine.
CASES.peers = async () => {
    const out = {};
    const OK = (on) => ({ status: 200, body: { ok: true, update: {
        check_enabled: on, apply_enabled: false, source: 'stored',
        mutable: true, remote_writable: true } } });

    // Who may be offered a switch at all.
    reset();
    fleet(['local', 'modern', 'rolling', 'locked', 'old', 'asleep']);
    INFO = {
        local: INFO_MODERN(false),
        modern: INFO_MODERN(false),
        // The build that shipped the route ORIGIN-GATED: mutable, but it will
        // refuse a cross-origin write. No remote_writable key.
        rolling: INFO_MODERN(false, { remote_writable: undefined }),
        locked: INFO_MODERN(false, { source: 'config', mutable: false }),
        old: INFO_OLD,
        asleep: INFO_DOWN,
    };
    CHECK = { local: R503, modern: R503, rolling: R503, locked: R503,
              old: R503, asleep: 'throw' };
    await pollTick();
    out.offered = {};
    for (const id of ['local', 'modern', 'rolling', 'locked', 'old', 'asleep']) {
        out.offered[id] = policyMutableFor(id);
    }

    // Switching a peer on really posts to THAT peer.
    reset();
    fleet(['local', 'peer']);
    INFO = { local: INFO_MODERN(true), peer: INFO_MODERN(false) };
    CHECK = { local: OK200({ state: 'current' }), peer: R503 };
    POLICY = { local: OK(true), peer: OK(true) };
    await pollTick();
    policyCalls.length = 0;
    CHECK.peer = OK200({ state: 'current' });
    const ok = await setChecking('peer', true);
    out.peerWrite = { ok: ok, posted: policyCalls.slice(),
                      peerCap: updateCapFor('peer'),
                      localCap: updateCapFor('local'),
                      peerState: peerState(checkStateFor('peer')) };

    // The rolling-upgrade refusal must not read as a password problem.
    reset();
    fleet(['local', 'rolling']);
    INFO = { local: INFO_MODERN(true),
             rolling: INFO_MODERN(false, { remote_writable: undefined }) };
    CHECK = { local: OK200({ state: 'current' }), rolling: R503 };
    POLICY = { local: OK(true),
               rolling: { status: 403,
                          body: { ok: false, error: 'forbidden_origin' } } };
    await pollTick();
    const rolled = await setChecking('rolling', true);
    out.rolling = { ok: rolled, note: (opFor('rolling') || {}).note,
                    cap: updateCapFor('rolling') };

    // A genuinely wrong password is still a wrong password.
    reset();
    fleet(['local', 'nopw']);
    INFO = { local: INFO_MODERN(true), nopw: INFO_MODERN(false) };
    CHECK = { local: OK200({ state: 'current' }), nopw: R503 };
    POLICY = { local: OK(true), nopw: { status: 401, body: { ok: false } } };
    await pollTick();
    await setChecking('nopw', true);
    out.badPassword = (opFor('nopw') || {}).note;

    // ID REUSE. A write to the machine at one url must never be applied to a
    // different machine that inherited the id while it was in flight.
    reset();
    fleet(['local', 'b']);
    INFO = { local: INFO_MODERN(true), b: INFO_MODERN(false) };
    CHECK = { local: OK200({ state: 'current' }), b: R503 };
    POLICY = { local: OK(true), b: OK(true) };
    await pollTick();
    const p = setChecking('b', true);
    // B1 is replaced by a different machine under the same id, mid-flight.
    HOSTS = HOSTS.map((h) => (h.id === 'b'
        ? Object.assign({}, h, { url: 'https://SOMEONE-ELSE.example:4445' }) : h));
    await p;
    out.idReuse = { cap: updateCapFor('b'), op: opFor('b') || null };

    // A removed host must not leave its failure behind for the next holder of
    // that id.
    reset();
    fleet(['local', 'gone']);
    INFO = { local: INFO_MODERN(true), gone: INFO_MODERN(false) };
    CHECK = { local: OK200({ state: 'current' }), gone: R503 };
    POLICY = { local: OK(true), gone: { status: 500, body: { ok: false } } };
    await pollTick();
    await setChecking('gone', true);
    const before = !!opFor('gone');
    fleet(['local']);              // the operator removes it
    await pollTick();
    out.removal = { noteBefore: before, noteAfter: !!opFor('gone') };
    return out;
};

// --- #182 Part 2 (A5): one consent click, every grantable gate ------------
// The broker takes any non-empty subset of the three keys in one POST, so
// the click that used to grant checking alone now grants everything the
// broker will let this browser open — and ONLY that: a config-owned gate is
// its file's decision, and a view without the `policy` block degrades to
// the single-key write through policyKeysFor.
CASES.consent_three_gates = async () => {
    const out = {};
    const OKPOST = { status: 200, body: { ok: true, update: {
        check_enabled: true, apply_enabled: true,
        source: 'stored', mutable: true, remote_writable: true } } };
    const GATE = (enabled, source) => ({ enabled: enabled, source: source,
                                         mutable: source !== 'config' });
    const grantCase = async (policyBlock, flatOver) => {
        reset();
        fleet(['local']);
        INFO = { local: INFO_MODERN(false, Object.assign(
            { policy: policyBlock }, flatOver || {})) };
        CHECK = { local: R503 };
        POLICY = { local: OKPOST };
        await offerConsent();
        return { posted: policyCalls.slice(), bodies: policyBodies.slice() };
    };

    // All three default-off and mutable: ONE request carries all three.
    out.allThree = await grantCase({
        check: GATE(false, 'default'), apply: GATE(false, 'default'),
        restart: GATE(false, 'default') });

    // Ledger AS10: a stored "off" the sidecar synthesized for the check —
    // nobody ever clicked off — is grantable, not a standing revoke.
    out.storedFalse = await grantCase({
        check: GATE(false, 'stored'), apply: GATE(false, 'default'),
        restart: GATE(false, 'default') });

    // restart config-pinned: the body carries exactly the two mutable keys.
    out.degraded = await grantCase({
        check: GATE(false, 'default'), apply: GATE(false, 'default'),
        restart: GATE(false, 'config') });

    // The config owns everything: nothing grantable, zero requests spent.
    out.allConfig = await grantCase({
        check: GATE(false, 'config'), apply: GATE(false, 'config'),
        restart: GATE(false, 'config') },
        { source: 'config', mutable: false });

    // The helpers themselves, over the shapes the fleet can serve.
    out.keysModern = policyKeysFor(INFO_MODERN(false).update);
    out.keysFlatOnly = policyKeysFor(INFO_MODERN(false,
        { policy: undefined }).update);
    out.keysNone = policyKeysFor(null);
    out.keysPlaceholder = policyKeysFor({ check_enabled: false });

    // A 409 that does come back names the file-owned keys, quoted.
    out.lockedNote = policyWriteOutcome(409, { ok: false,
        error: 'policy_locked', source: 'config',
        locked: ['apply_enabled', 'restart_enabled'] },
        ['check_enabled', 'apply_enabled', 'restart_enabled']).note;
    return out;
};

// --- #182 Part 2 (A29): the aftermath of an apply -------------------------
// The `last_deploy` object as the broker really serves it (supervise.py
// finalize_deploy / update.py cancel_pending_deploy): a validated journal
// record plus outcome/observedSha/detail. The strip model must survive junk
// in every optional field, so the helpers below build the honest shape and
// the cases mangle it.
const HEX40 = (ch) => ch.repeat(40);
const LD = (outcome, over) => Object.assign({
    version: 1,
    outcome: outcome,
    observedSha: HEX40('b'),
    detail: null,
    finalizedAt: 1000,
    record: { version: 1, operationId: 'op-1',
              oldSha: HEX40('a'), targetSha: HEX40('b'),
              expectedIdentity: 'x', createdAt: 999 },
}, over || {});
// A LOCAL record as poll() leaves it after a good check on the new build.
const stFor = (over) => Object.assign(
    { hostId: 'local',
      check: { state: 'current',
               local: { version: 'NEWBUILD', sha: HEX40('b') } },
      error: null, checkedAt: 1000, inFlight: false, lastDeploy: null },
    over || {});
// A 200 body that carries last_deploy BESIDE the check, like app.py does.
const OK200D = (check, ld) => ({ status: 200, ok: true,
    body: { ok: true, check: check, last_deploy: ld } });

CASES.deploy_strip = async () => {
    const out = {};
    // The session shapes /sessions really serves per host (registry.summary):
    // agents carry the build they were launched with; a plain terminal
    // reports none; junk rows are untrusted input to step over.
    const sess = [
        { kind: 'agent', version: 'NEWBUILD' },   // relaunched already
        { kind: 'agent', version: 'OLDBUILD' },   // survivor on old code
        { kind: 'agent' },                        // pre-#22 agent: no version
        { kind: 'term', version: '' },            // plain terminal: never flagged
        null, 'junk', 7,                          // stepped over, never thrown on
    ];
    out.success = deployStrip(
        stFor({ lastDeploy: LD('came-up-ready-on-target') }), sess);
    out.successNone = deployStrip(
        stFor({ lastDeploy: LD('came-up-ready-on-target') }),
        [{ kind: 'agent', version: 'NEWBUILD' }]);
    // The broker's own build unreadable -> the count must be "unknown",
    // never rounded down to a clean-sounding zero.
    out.successNoVersion = deployStrip(stFor({
        lastDeploy: LD('came-up-ready-on-target'),
        check: { state: 'current' } }), sess);
    out.successNoList = deployStrip(
        stFor({ lastDeploy: LD('came-up-ready-on-target') }), null);
    for (const oc of ['rolled-back', 'rollback-failed',
                      'rollback-impossible', 'came-up-on-wrong-sha',
                      'cancelled-before-restart']) {
        out[oc] = deployStrip(stFor({ lastDeploy: LD(oc,
            { detail: 'why-' + oc, observedSha: HEX40('c') }) }), sess);
    }
    out.cancelledTreeMoved = deployStrip(stFor({ lastDeploy:
        LD('cancelled-before-restart', { observedSha: HEX40('c'),
            detail: 'restart refused' }) }), sess);
    out.cancelledTreeSame = deployStrip(stFor({ lastDeploy:
        LD('cancelled-before-restart',
           { observedSha: HEX40('a') }) }), sess);
    out.unknownOutcome = deployStrip(
        stFor({ lastDeploy: LD('some-future-verdict') }), sess);
    out.absent = deployStrip(stFor({}), sess);
    out.junk = [
        deployStrip(stFor({ lastDeploy: 'nope' }), sess),
        deployStrip(stFor({ lastDeploy: {} }), sess),
        deployStrip(stFor({ lastDeploy: { outcome: 7 } }), sess),
        deployStrip(null, sess),
    ];
    out.survivors = {
        noVersion: staleSurvivors(null, sess),
        noList: staleSurvivors('NEWBUILD', null),
        empty: staleSurvivors('NEWBUILD', []),
        counted: staleSurvivors('NEWBUILD', sess),
    };
    return out;
};

// --- last_deploy rides the poll, and dies with the answer -----------------
CASES.deploy_rides_the_poll = async () => {
    const out = {};
    fleet(['local', 'peer']);
    INFO = { local: INFO_MODERN(true), peer: INFO_MODERN(true) };
    CHECK = {
        local: OK200D(
            { state: 'current',
              local: { version: 'NEW', sha: HEX40('b') } },
            LD('rolled-back', { detail: 'worker-never-ready' })),
        peer: OK200({ state: 'current' }),   // no last_deploy key at all
    };
    await pollTick();
    const st1 = checkStateFor('local');
    out.afterOk = {
        hasLd: !!st1.lastDeploy,
        outcome: st1.lastDeploy && st1.lastDeploy.outcome,
        peerLd: checkStateFor('peer').lastDeploy,
        stripOutcome: (deployStrip(st1, []) || {}).outcome,
    };
    // The broker stops answering; the strip must die with the answer, the
    // same way a stored 'current' does.
    INFO = { local: INFO_DOWN, peer: INFO_MODERN(true) };
    CHECK = { peer: OK200({ state: 'current' }) };
    modCatalogCache.clear();
    await pollTick();
    out.afterDown = { ld: checkStateFor('local').lastDeploy,
                      strip: deployStrip(checkStateFor('local'), []) };
    // …and a 503 (the gate closed meanwhile) equally leaves none behind.
    INFO = { local: INFO_MODERN(true), peer: INFO_MODERN(true) };
    CHECK = { local: R503, peer: OK200({ state: 'current' }) };
    modCatalogCache.clear();
    await pollTick();
    out.after503 = { ld: checkStateFor('local').lastDeploy };
    return out;
};

// --- the fresh terminal targets the LOCAL broker, explicitly --------------
CASES.fresh_terminal_host = async () => {
    fleet(['local', 'peerA', 'peerB']);
    const h = freshTerminalHost();
    const out = { id: h && h.id, isNull: h === null };
    HOSTS = [];
    // No local broker configured: the resolver answers null, and the click
    // handler stops on null rather than letting the launcher fall through
    // to the serving origin.
    out.unconfigured = freshTerminalHost();
    return out;
};

// --- atom A30: can the Update button even be offered? ---------------------
// Pure over literal facts -- no host, no fleet, no network. The five unmet
// conditions the brief names, each read off its own distinct code.
CASES.apply_gate = async () => {
    const out = {};
    const SHA = 'b'.repeat(40);
    out.enabled = applyGateFromFacts('behind', SHA, true, true);
    out.gateOff = applyGateFromFacts('behind', SHA, false, true);
    out.restartUnavailable = applyGateFromFacts('behind', SHA, true, false);
    out.unknownState = applyGateFromFacts('unknown', null, true, true);
    out.notBehindCurrent = applyGateFromFacts('current', null, true, true);
    out.notBehindAhead = applyGateFromFacts(
        'ahead-or-diverged', null, true, true);
    out.noTarget = applyGateFromFacts('behind', null, true, true);
    out.wordsGateOff = applyGateWords(out.gateOff, 'unused');
    out.wordsRestart = applyGateWords(out.restartUnavailable,
        'this broker cannot restart itself on this install');
    out.wordsUnknown = applyGateWords(out.unknownState, 'unused');
    out.wordsNotBehind = applyGateWords(out.notBehindCurrent, 'unused');
    out.wordsNoTarget = applyGateWords(out.noTarget, 'unused');
    out.wordsEnabled = applyGateWords(out.enabled, 'unused');
    // atom A7: the disabled-here words derive from the gate's CURRENT
    // source, handed in as a third arg -- stored/default/corrupt (the
    // row can move these) get the row sentence; config (or no facts
    // at all, the call above) keeps the config-file sentence.
    out.wordsGateOffStored = applyGateWords(out.gateOff, 'unused',
        { enabled: false, source: 'stored', mutable: true });
    out.wordsGateOffDefault = applyGateWords(out.gateOff, 'unused',
        { enabled: false, source: 'default', mutable: true });
    out.wordsGateOffCorrupt = applyGateWords(out.gateOff, 'unused',
        { enabled: false, source: 'corrupt', mutable: true });
    out.wordsGateOffConfig = applyGateWords(out.gateOff, 'unused',
        { enabled: false, source: 'config', mutable: false });
    // The one thing an apply actually needs: an exact upstream sha, not a
    // version compare -- release mode names a tag and no sha at all.
    out.targetShaValid = applyTargetSha({ upstream: { sha: 'a'.repeat(40) } });
    out.targetShaReleaseMode = applyTargetSha(
        { upstream: { tag: 'v1.0.0', url: 'https://x' } });
    out.targetShaBadHex = applyTargetSha({ upstream: { sha: 'not-a-sha' } });
    out.targetShaNoCheck = applyTargetSha(null);
    return out;
};

// --- atom A30: every shape POST /update/apply can answer with -------------
CASES.apply_refusals = async () => {
    const out = {};
    out.transport = applyRefusalOutcome(null, null);
    out.success = applyRefusalOutcome(202,
        { ok: true, bootId: 'boot-2', operation_id: 'apply-1' });
    out.gate = applyRefusalOutcome(503,
        { ok: false, error: 'update_apply_disabled' });
    out.incomplete = applyRefusalOutcome(503, { ok: false,
        error: 'apply_incomplete', reason_code: 'drain_failed',
        tree_updated: true, old_sha: 'a'.repeat(40) });
    out.inProgress = applyRefusalOutcome(409, { ok: false,
        error: 'apply_in_progress', operation_id: 'apply-abc123' });
    out.restartInProgress = applyRefusalOutcome(409,
        { ok: false, error: 'restart_in_progress' });
    out.multiRefusal = applyRefusalOutcome(409, { ok: false,
        error: 'apply_refused',
        reason_codes: ['dirty-tree', 'ahead-or-diverged'],
        refusals: [
            { reason: 'dirty-tree', message: 'Tracked files carry local '
                + 'modifications.' },
            { reason: 'ahead-or-diverged', message: 'This checkout carries '
                + '2 local commits upstream does not have.' },
        ] });
    out.treeSuspect = applyRefusalOutcome(409, { ok: false,
        error: 'apply_failed',
        refusals: [{ reason: 'not-fast-forward',
                     message: 'The merge did not fast-forward.' }],
        tree_suspect: true });
    out.noRefusalsListed = applyRefusalOutcome(409,
        { ok: false, error: 'apply_refused', refusals: [] });
    out.garbageBody = applyRefusalOutcome(500, null);
    // A 200 (not 202) is never read as success either.
    out.wrongStatus = applyRefusalOutcome(200, { ok: true });
    return out;
};

// --- atom A7: the restart-disabled reason points at the row too -----------
CASES.restart_disabled_words = async () => {
    const out = {};
    out.words = restartReasonWords('restart-disabled');
    return out;
};

// --- #183 R6: the cooldown reason's words ---------------------------------
// Pure over the shipped RESTART_REASONS table -- no host, no fleet.
CASES.restart_cooldown_words = async () => {
    const out = {};
    out.known = Object.prototype.hasOwnProperty.call(
        RESTART_REASONS, 'cooldown');
    out.plain = restartReasonWords('cooldown');
    out.withRetry = restartReasonWords('cooldown', 42);
    out.badRetry = restartReasonWords('cooldown', 'soon');
    out.otherReasonIgnoresRetry = restartReasonWords('restart-disabled', 42);
    out.unknownCode = restartReasonWords('never-a-code', 42);
    // The Update... button's gate: a cooldown flows through as the ordinary
    // restart-unavailable wording, no special-casing anywhere.
    out.applyGateCode = applyGateFromFacts('behind', 'b'.repeat(40),
        true, false);
    out.applyGateWords = applyGateWords(out.applyGateCode,
        restartReasonWords('cooldown', 30));
    return out;
};

CASES.refresh_refused_words = async () => {
    const out = {};
    out.rateLimited = refreshRefusedWords(
        { reason: 'rate-limited', retry_after_s: 1800 });
    out.tooSoon = refreshRefusedWords(
        { reason: 'too-soon', retry_after_s: 42 });
    out.budget = refreshRefusedWords(
        { reason: 'hourly-budget', retry_after_s: 900 });
    out.unknownReason = refreshRefusedWords(
        { reason: 'not-a-reason-this-build-knows', retry_after_s: 5 });
    out.noRetry = refreshRefusedWords({ reason: 'too-soon' });
    out.junkRetry = refreshRefusedWords(
        { reason: 'too-soon', retry_after_s: 'soon' });
    out.empty = refreshRefusedWords(null);
    return out;
};

// --- #182 Part 2 (A6): the self-update row --------------------------------
// "Allow this broker to update itself" — apply_enabled + restart_enabled as
// ONE switch, never check_enabled. The DOM row and its dialog are not in the
// sliced range; what runs here is the shipped model (selfUpdateRowModel,
// update-policy.js), the shipped wiring that feeds it (selfUpdateModelFor /
// selfUpdateRowNeeded) and the shipped writers (setPolicy under the 'self'
// op kind, commitRemoteSelfUpdate) — the exact code paths a click
// exercises, minus the button itself.
const SGATE = (enabled, source) => ({ enabled: enabled, source: source,
                                      mutable: source !== 'config' });
const SCONF = (enabled) => ({ enabled: enabled, source: 'config',
                              mutable: false });
const SPOL = (apply, restart) => ({
    check: SGATE(true, 'stored'), apply: apply, restart: restart });
const SVIEW = (apply, restart) => INFO_MODERN(true,
    { policy: SPOL(apply, restart) });

CASES.self_update_model = async () => {
    const out = {};
    out.bothOffMutable = selfUpdateRowModel(
        SPOL(SGATE(false, 'default'), SGATE(false, 'default')), null);
    out.applyOffRestartConfigTrue = selfUpdateRowModel(
        SPOL(SGATE(false, 'stored'), SCONF(true)), null);
    out.restartConfigFalse = selfUpdateRowModel(
        SPOL(SGATE(false, 'stored'), SCONF(false)), null);
    out.restartConfigFalseApplyGranted = selfUpdateRowModel(
        SPOL(SGATE(true, 'stored'), SCONF(false)), null);
    out.bothConfigFalse = selfUpdateRowModel(
        SPOL(SCONF(false), SCONF(false)), null);
    out.onBothMutable = selfUpdateRowModel(
        SPOL(SGATE(true, 'stored'), SGATE(true, 'stored')), null);
    out.onRestartConfigTrue = selfUpdateRowModel(
        SPOL(SGATE(true, 'stored'), SCONF(true)), null);
    out.onBothConfigTrue = selfUpdateRowModel(
        SPOL(SCONF(true), SCONF(true)), null);
    out.flatBuild = selfUpdateRowModel(null, null);
    // Present-but-malformed `policy` blocks: fail closed, and NEVER the
    // flat-build degradation (those words belong to a block that is
    // genuinely absent).
    out.malformed = [
        selfUpdateRowModel('nope', null),
        selfUpdateRowModel({}, null),
        selfUpdateRowModel({ apply: SGATE(false, 'default') }, null),
        selfUpdateRowModel({ apply: { enabled: 'yes', mutable: true },
                             restart: SGATE(false, 'default') }, null),
        selfUpdateRowModel({ apply: SGATE(false, 'default'),
                             restart: { enabled: false } }, null),
    ];
    out.busyOn = selfUpdateRowModel(
        SPOL(SGATE(false, 'default'), SGATE(false, 'default')),
        { phase: 'busy', note: selfUpdateBusyNote(true), want: true });
    out.busyOff = selfUpdateRowModel(
        SPOL(SGATE(true, 'stored'), SGATE(true, 'stored')),
        { phase: 'busy', note: selfUpdateBusyNote(false), want: false });
    out.failedOp = selfUpdateRowModel(
        SPOL(SGATE(false, 'default'), SGATE(false, 'default')),
        { phase: 'locked', note: 'locked words', want: true });
    return out;
};

CASES.self_update_writes = async () => {
    const out = {};
    const prime = async (pol, policyAnswer) => {
        reset();
        fleet(['local']);
        INFO = { local: INFO_MODERN(true, { policy: pol }) };
        CHECK = { local: OK200({ state: 'current' }) };
        POLICY = { local: policyAnswer };
        await pollTick();
        policyCalls.length = 0; policyBodies.length = 0;
    };
    // Exactly what renderSelfUpdateRow's local click does: build the
    // body from the model's postKeys with the direction variable, then
    // write under the 'self' op kind.
    const click = async () => {
        const m = selfUpdateModelFor('local');
        const grant = !m.on;
        const ok = await setPolicy('local',
            policyChangesFor(m.postKeys, grant),
            { kind: 'self', want: grant,
              busyNote: selfUpdateBusyNote(grant) });
        return { before: m, ok: ok };
    };

    // Both gates default-off + mutable: ONE post carries both keys.
    await prime(SPOL(SGATE(false, 'default'), SGATE(false, 'default')),
        { status: 200, body: { ok: true,
            update: SVIEW(SGATE(true, 'stored'),
                          SGATE(true, 'stored')).update } });
    let c = await click();
    out.grant = { before: c.before, ok: c.ok,
                  calls: policyCalls.slice(),
                  bodies: policyBodies.slice(),
                  after: selfUpdateModelFor('local'),
                  checkOp: policyOps.get('local|check') || null,
                  selfOp: policyOps.get('local|self') || null };

    // restart config-TRUE + apply stored-false: the body carries only
    // the key the config does not own, and granting it turns the row ON.
    await prime(SPOL(SGATE(false, 'stored'), SCONF(true)),
        { status: 200, body: { ok: true,
            update: SVIEW(SGATE(true, 'stored'), SCONF(true)).update } });
    c = await click();
    out.partial = { before: c.before, ok: c.ok,
                    bodies: policyBodies.slice(),
                    after: selfUpdateModelFor('local') };

    // Stop: both mutable and on -> false for both, never check_enabled.
    await prime(SPOL(SGATE(true, 'stored'), SGATE(true, 'stored')),
        { status: 200, body: { ok: true,
            update: SVIEW(SGATE(false, 'stored'),
                          SGATE(false, 'stored')).update } });
    c = await click();
    out.stop = { before: c.before, ok: c.ok,
                 bodies: policyBodies.slice(),
                 after: selfUpdateModelFor('local') };

    // A 409 policy_locked: the operator's file changed under us. The
    // note names the locked key, the refusal's authoritative view is
    // installed, and no residual POST follows.
    const view409 = SVIEW(SGATE(false, 'default'), SCONF(false)).update;
    await prime(SPOL(SGATE(false, 'default'), SGATE(false, 'default')),
        { status: 409, body: { ok: false, error: 'policy_locked',
            source: 'config', locked: ['restart_enabled'],
            update: view409 } });
    c = await click();
    out.locked = { before: c.before, ok: c.ok,
                   op: policyOps.get('local|self') || null,
                   calls: policyCalls.length,
                   bodies: policyBodies.slice(),
                   after: selfUpdateModelFor('local'),
                   cap: updateCapFor('local') };
    return out;
};

CASES.self_update_old_peers = async () => {
    reset();
    fleet(['local', 'flat', 'ancient']);
    INFO = { local: INFO_MODERN(true),
             // the flat single-key build: real `mutable`, no `policy`
             flat: INFO_MODERN(false, { policy: undefined }),
             ancient: INFO_OLD };
    CHECK = { local: OK200({ state: 'current' }) };
    await pollTick();
    return {
        localNeeded: selfUpdateRowNeeded('local'),
        flatNeeded: selfUpdateRowNeeded('flat'),
        flatModel: selfUpdateModelFor('flat'),
        ancientNeeded: selfUpdateRowNeeded('ancient'),
        keysFlat: policyKeysFor(updateCapFor('flat')),
        keysAncient: policyKeysFor(updateCapFor('ancient')),
    };
};

CASES.self_update_remote = async () => {
    const out = {};
    const setup = async () => {
        reset();
        fleet(['local', 'peer']);
        INFO = { local: INFO_MODERN(true),
                 peer: SVIEW(SGATE(false, 'default'),
                             SGATE(false, 'default')) };
        CHECK = { local: OK200({ state: 'current' }),
                  peer: OK200({ state: 'current' }) };
        POLICY = { peer: { status: 200, body: { ok: true,
            update: SVIEW(SGATE(true, 'stored'),
                          SGATE(true, 'stored')).update } } };
        await pollTick();
        policyCalls.length = 0; policyBodies.length = 0;
        return hostById('peer').url;
    };

    // Nothing posts until the dialog's OK reaches the commit: the
    // remote-enable click only opens the confirm and returns.
    let url = await setup();
    out.beforeConfirm = policyCalls.slice();
    const ok = await commitRemoteSelfUpdate('peer', url, 'peer');
    out.confirmed = { ok: ok, calls: policyCalls.slice(),
                      bodies: policyBodies.slice(),
                      after: selfUpdateModelFor('peer') };

    // The id was re-pointed at a different machine while the dialog was
    // open: abort with a note, nothing sent.
    url = await setup();
    HOSTS = HOSTS.map((h) => (h.id === 'peer'
        ? Object.assign({}, h, { url: 'https://SOMEONE-ELSE.example:1' })
        : h));
    out.moved = { ok: await commitRemoteSelfUpdate('peer', url, 'peer'),
                  calls: policyCalls.slice(),
                  op: policyOps.get('peer|self') || null };

    // Its label changed: the dialog named a machine, so same abort.
    url = await setup();
    HOSTS = HOSTS.map((h) => (h.id === 'peer'
        ? Object.assign({}, h, { label: 'someone else' }) : h));
    out.relabelled = { ok: await commitRemoteSelfUpdate('peer', url,
                                                        'peer'),
                       calls: policyCalls.slice() };

    // It vanished outright.
    url = await setup();
    fleet(['local']);
    out.vanished = { ok: await commitRemoteSelfUpdate('peer', url,
                                                      'peer'),
                     calls: policyCalls.slice(),
                     op: policyOps.get('peer|self') || null };

    // The row turned ON while the dialog was open (another browser
    // granted it): an enable confirmation finishes with NO post — it
    // must never become a Stop.
    url = await setup();
    lastWrite.set('peer', { update: SVIEW(SGATE(true, 'stored'),
        SGATE(true, 'stored')).update, seq: ++opSeq,
        fp: hostFingerprint('peer') });
    out.meanwhileOn = { ok: await commitRemoteSelfUpdate('peer', url,
                                                         'peer'),
                        calls: policyCalls.slice() };
    return out;
};

const want = process.argv[2];
if (!CASES[want]) { console.error('no such case: ' + want); process.exit(2); }
Promise.resolve(CASES[want]()).then((r) => {
    console.log(JSON.stringify(r));
}).catch((e) => { console.error(e && e.stack || String(e)); process.exit(3); });
"""


@pytest.fixture(scope="module")
def harness(tmp_path_factory):
    path = tmp_path_factory.mktemp("update-fleet") / "harness.js"
    path.write_text(_HARNESS.replace("__MODEL__", _model_source()),
                    encoding="utf-8")
    return path


def run(harness, case):
    # encoding is pinned: node writes UTF-8 and several of these strings carry
    # an ellipsis or an em dash, which the Windows console codepage mangles.
    proc = subprocess.run([NODE, str(harness), case],
                          capture_output=True, text=True, encoding="utf-8",
                          timeout=120)
    assert proc.returncode == 0, (
        f"case {case} failed (rc={proc.returncode})\n"
        f"stdout: {proc.stdout}\nstderr: {proc.stderr}")
    return json.loads(proc.stdout.strip().splitlines()[-1])


# ---- the five peer states are five states ---------------------------------

def test_each_peer_failure_is_its_own_state(harness):
    # One lumped 'unknown' would ask the same thing of everyone who read it.
    # These four ask four different things: update that broker, switch checking
    # on over there, re-enter its password, go find out why it is not answering.
    r = run(harness, "peer_state_table")
    assert r["routeAbsent"]["ps"] == "route-absent"
    assert r["notOptedIn"]["ps"] == "not-opted-in"
    assert r["unauthorized"]["ps"] == "unauthorized"
    assert r["unreachable"]["ps"] == "unreachable"
    # …and the fifth (#185), which asks a sixth thing: find out whether that
    # headless broker is running at all, and update it if it is.
    assert r["unreachableOrTooOld"]["ps"] == "unreachable-or-too-old"
    # The fifth: the check RAN, and failed carrying its reason code.
    assert r["unknownRateLimited"]["ps"] == "unknown"
    assert r["unknownRateLimited"]["reason"] == "rate-limited"
    assert r["unknownNoGit"]["ps"] == "unknown"
    assert r["unknownNoGit"]["reason"] == "no-git"
    # A broker-side reason and a browser-side one are both carried, unmerged.
    assert r["unknownOffline"]["reason"] == "offline"
    assert r["brokerError"]["ps"] == "unknown"
    assert r["brokerError"]["reason"] == "broker-error"
    assert r["noSuchHost"]["ps"] == "unknown"
    assert r["noSuchHost"]["reason"] == "no-such-host"
    # Nothing has come back yet — not a failure, and not an answer either.
    assert r["pending"]["ps"] == "pending"
    assert r["pendingBeatsPayload"]["ps"] == "pending"
    assert r["missing"]["ps"] == "unknown"


def test_every_state_reads_differently_and_has_words(harness):
    r = run(harness, "peer_state_table")
    words = {k: r[k]["words"] for k in
             ("routeAbsent", "notOptedIn", "unauthorized", "unreachable",
              "unreachableOrTooOld", "unknownRateLimited", "pending",
              "current", "behind3", "ahead")}
    assert len(set(words.values())) == len(words), (
        f"two states read the same: {words}")
    assert words["current"] == "up to date"
    assert words["behind3"] == "3 commits behind"
    assert r["behind1"]["words"] == "1 commit behind"
    assert r["behindNoCount"]["words"] == "behind upstream"
    assert words["pending"].startswith("checking")
    # …and every non-answer has a long-form sentence to show beside it.
    for code in ("route-absent", "not-opted-in", "unauthorized", "unreachable",
                 "broker-error", "no-such-host", "offline", "rate-limited"):
        assert r["reasons"][code], f"{code} has no words in REASONS"


def test_no_unanswered_state_is_ever_current(harness):
    # The single rule the mod exists to keep.
    r = run(harness, "peer_state_table")
    for key in ("routeAbsent", "notOptedIn", "unauthorized", "unreachable",
                "unreachableOrTooOld", "unknownRateLimited", "unknownOffline",
                "brokerError", "noSuchHost", "pending", "pendingBeatsPayload",
                "missing", "garbage"):
        row = r[key]
        assert row["ps"] != "current", f"{key} derived 'current'"
        assert row["words"] != "up to date", f"{key} said 'up to date'"
        assert row["answered"] is False, f"{key} counted as an answer"
        assert row["band"] != "green", f"{key} painted green"
    # Only a parsed 200 body gets there.
    assert r["current"]["ps"] == "current" and r["current"]["answered"] is True
    assert r["behind3"]["answered"] is True and r["behind3"]["band"] == "amber"
    assert r["ahead"]["answered"] is True and r["ahead"]["band"] == "grey"


# ---- a 503 is an answer ----------------------------------------------------

def test_a_503_is_not_opted_in_and_never_a_transport_failure(harness):
    # THE regression to pin. A broker that answers 503 was reached and it
    # answered; landing that on 'offline'/'unreachable' reports a healthy
    # machine as a network fault and sends its operator hunting a fault that
    # does not exist.
    r = run(harness, "gate_503")
    assert r["ps"] == "not-opted-in"
    assert r["error"] == "not-opted-in"
    assert r["reason"] == "not-opted-in"
    assert r["check"] is None
    assert r["ps"] not in ("unreachable", "offline", "route-absent", "unknown")
    assert r["answered"] is False and r["band"] != "green"
    assert r["words"] != "up to date"
    # The words a reader gets must not blame GitHub or the network.
    assert "could not reach GitHub" == r["offlineReason"]
    assert "could not reach GitHub" not in r["notOptedInReason"]
    assert "update_check_enabled" in r["notOptedInReason"]
    assert "did not answer" in r["unreachableReason"]
    assert "could not reach GitHub" not in r["title"]
    # The other way to learn the same thing — off /info, with no request spent.
    assert r["viaCapability"] == "not-opted-in"
    # It genuinely went over the wire in this case (the probe could not know).
    assert r["checkCalls"] == ["local"]


# ---- the probe decides who is asked ----------------------------------------

def test_the_probe_has_an_outcome_for_every_peer(harness):
    r = run(harness, "capability_table")
    assert r["keyOn"] == "ready"
    assert r["keyOff"] == "not-opted-in"      # answered from /info, not asked
    assert r["catalogOnly"] == "ready"        # the mod ships with the route
    assert r["old"] == "route-absent"
    assert r["pre157"] == "route-absent"      # older than #157 ⇒ older than #182
    # A headless peer with no capability key is the one genuinely ambiguous
    # case, and the probe says so rather than picking a side: 'unproven' asks
    # like 'ready' does, but tagged so poll() can report the ambiguity if the
    # request dies. It must NOT be 'ready' (which would report "did not answer"
    # for a broker whose real problem is that it is too old) and must NOT be a
    # refusal (which would never ask a broker that may well be there).
    assert r["headless"] == "unproven"
    assert r["headlessKeyOff"] == "not-opted-in"
    assert r["headlessKeyOn"] == "ready"      # a published key ends the doubt
    assert r["unauthorized"] == "unauthorized"
    assert r["unreachable"] == "unreachable"
    assert r["noRecord"] == "unreachable"
    # Untrusted input: one junk row must not take the whole probe down.
    assert r["junkRows"] == "ready"
    assert r["modsNotArray"] == "route-absent"
    # A capability read off our OWN placeholder record is not evidence.
    assert r["unauthorizedWithKey"] == "unauthorized"


def test_a_peer_that_predates_the_route_is_never_asked(harness):
    # The request would die in that broker's preflight and come back as an
    # opaque TypeError — indistinguishable from a machine that is asleep. So it
    # is not sent at all.
    r = run(harness, "route_absent_never_asked")
    assert r["first"]["check"].count("old") == 0, "asked a pre-#182 peer"
    assert r["first"]["check"].count("pre157") == 0, "asked a pre-#157 peer"
    assert r["first"]["check"] == ["local"]
    assert r["first"]["states"] == ["current", "route-absent", "route-absent"]
    # Every host IS probed once, and the cached verdict is not re-probed.
    assert sorted(r["first"]["info"]) == ["local", "old", "pre157"]
    assert r["second"]["info"] == r["first"]["info"], "re-probed a cached peer"
    assert r["second"]["check"] == ["local", "local"]
    # "Check now" is the deliberate retry, and it re-reads /info fleet-wide.
    assert sorted(r["third"]["info"][3:]) == ["local", "old", "pre157"]
    assert r["third"]["check"].count("old") == 0
    assert r["third"]["check"].count("pre157") == 0


# ---- the fleet, end to end -------------------------------------------------

def test_one_poll_gives_every_host_its_own_state(harness):
    r = run(harness, "fleet_poll")
    assert r["rows"] == [
        "local|current|up to date|-",
        "old|route-absent|too old to check|route-absent",
        "gated|not-opted-in|checking not enabled there|not-opted-in",
        "capoff|not-opted-in|checking not enabled there|not-opted-in",
        "nopw|unauthorized|password refused|unauthorized",
        "asleep|unreachable|did not answer|unreachable",
        "garbled|unknown|could not be checked|broker-error",
        "ratelimited|unknown|could not be checked|rate-limited",
        # a successful check carries no reason code — there is nothing to explain
        "stale|behind|4 commits behind|-",
    ]
    # Only the peers that could answer were asked; the other four were not.
    assert sorted(r["checkCalls"]) == ["garbled", "gated", "local",
                                       "ratelimited", "stale"]
    assert sorted(r["infoCalls"]) == ["asleep", "capoff", "garbled", "gated",
                                      "local", "nopw", "old", "ratelimited",
                                      "stale"]
    # One host that is behind cannot mask the ones that never answered.
    assert r["worst"] == "behind"
    assert r["allCurrent"] is False
    # 7 unchecked: the four the probe stopped, plus the two whose answer was
    # unusable, plus none pending — only 'local' and 'stale' actually answered.
    assert r["text"] == "9 brokers · 1 behind, 7 unchecked"
    assert r["lines"][1] == "old — too old to check"


def test_an_answer_does_not_outlive_the_broker_that_gave_it(harness):
    r = run(harness, "no_current_without_an_answer")
    assert r["before"] == {"ps": "current", "agg": True}
    # It went away; the stored 'current' must go with it.
    assert r["after"]["ps"] == "unreachable"
    assert r["after"]["check"] is None
    assert r["after"]["agg"] is False
    assert r["refused"]["ps"] == "unauthorized"
    assert r["refused"]["check"] is None
    # A host removed between scheduling and firing asks nobody at all — a null
    # host would hit the SERVING origin and report this broker's version under
    # the missing host's name.
    assert r["ghost"]["error"] == "no-such-host"
    assert r["ghost"]["ps"] == "unknown"
    assert r["ghost"]["check"] is None
    assert "ghost" not in r["checkCalls"]


# ---- a headless peer's ambiguity is reported, not resolved by a guess ------

AMBIGUOUS_WORDS = "no answer — asleep, or too old to check"


def test_a_silent_headless_peer_is_not_reported_as_merely_unreachable(harness):
    # THE #185 regression, graded where it matters: on the state that reaches
    # the screen. A headless broker publishes an empty `mods` list whatever
    # routes it has, so one that also publishes no `update` key is either too
    # old to have /update/check or new enough to have it and asleep — and a
    # missing route dies in the preflight looking EXACTLY like a dead machine.
    # Calling that 'unreachable' ("did not answer") sends its operator hunting a
    # network fault when the fix is "update that broker".
    r = run(harness, "headless_ambiguity")
    h = r["headless"]
    assert h["ps"] == "unreachable-or-too-old"
    assert h["error"] == "unreachable-or-too-old"
    assert h["reason"] == "unreachable-or-too-old"
    assert h["check"] is None
    assert h["words"] == AMBIGUOUS_WORDS
    # The words it must NOT wear, and the state it must not collapse into.
    assert h["words"] != "did not answer"
    assert h["ps"] != "unreachable"
    assert h["ps"] != "route-absent"
    assert h["ps"] != "unknown", "the new state must be pulled up out of unknown"
    # It is still emphatically a non-answer.
    assert h["answered"] is False
    assert h["band"] != "green"
    assert h["coarse"] == "unknown"      # the single-broker vocabulary is intact
    # …and it is DISTINCT from the two neighbouring failures in the same fleet:
    # a broker that answered /info and then went quiet really is unreachable,
    # and one that serves a UI without the route really is too old.
    assert r["ghosted"]["ps"] == "unreachable"
    assert r["ghosted"]["words"] == "did not answer"
    assert r["down"]["ps"] == "unreachable"
    assert r["old"]["ps"] == "route-absent"
    assert r["old"]["words"] == "too old to check"
    assert len({h["words"], r["ghosted"]["words"], r["old"]["words"]}) == 3
    # The long-form sentence names BOTH possibilities rather than picking one,
    # and tells the reader what to do about either.
    reason = r["reasons"]["ambiguous"]
    assert reason and reason != r["reasons"]["unreachable"]
    assert reason != r["reasons"]["routeAbsent"]
    assert "asleep" in reason
    assert "before update checking existed" in reason
    assert "update it" in reason
    # The tooltip carries it — never the generic fallback.
    assert h["title"] == "could not check: " + reason
    assert h["title"] != "could not check: reason unavailable"
    # The ambiguity is only claimed for a peer that was actually ASKED: 'down'
    # was never asked (its /info failed) and 'old' was never asked (the route is
    # known absent), so neither may wear these words.
    assert sorted(r["checkCalls"]) == ["ghosted", "headless", "local"]
    assert r["down"]["words"] != AMBIGUOUS_WORDS
    assert r["old"]["words"] != AMBIGUOUS_WORDS


def test_a_headless_peer_that_answers_is_never_left_ambiguous(harness):
    # 'unproven' is ready-that-asks. If it ever refused to ask, every headless
    # broker in the fleet would report a failure it never had — so the pre-set
    # reason must be overwritten by whatever actually comes back, in every
    # direction a broker can answer.
    r = run(harness, "headless_answers")
    assert r["rows"] == [
        "local|current|up to date|-",
        "hcur|current|up to date|-",
        "hbehind|behind|2 commits behind|-",
        "hgate|not-opted-in|checking not enabled there|not-opted-in",
        "hnopw|unauthorized|password refused|unauthorized",
    ]
    # Every headless peer was asked; none was written off unasked.
    assert sorted(r["checkCalls"]) == ["hbehind", "hcur", "hgate", "hnopw",
                                       "local"]
    assert AMBIGUOUS_WORDS not in r["rows"][1]
    assert "unreachable-or-too-old" not in r["states"]
    assert r["worst"] == "behind"        # WORST_FIRST leads with the actionable
    assert r["text"] == "5 brokers · 1 behind, 2 unchecked"
    assert r["allCurrent"] is False


# ---- a 401 on the wire is a refused password ------------------------------

def test_a_401_from_the_check_is_a_refused_password_not_an_unreadable_answer(
        harness):
    # THE #185 regression on the other side. `unauthorized` is a named peer
    # state with words already written, and before the fix nothing on the wire
    # path could reach it: poll() set 'broker-error' the moment hostFetch
    # resolved and special-cased only 503, so a 401 fell through to
    # `throw new Error('HTTP ' + r.status)` and rendered as "this broker
    # answered, but not with a version check that could be read" — a lock on a
    # door, described as a garbled reply.
    r = run(harness, "wire_unauthorized")
    for key in ("stale401", "forbidden403"):
        row = r[key]
        assert row["ps"] == "unauthorized", key
        assert row["error"] == "unauthorized", key
        assert row["reason"] == "unauthorized", key
        assert row["check"] is None, key
        assert row["words"] == "password refused", key
        # The two wrong answers it used to give.
        assert row["ps"] != "unknown", key
        assert row["reason"] != "broker-error", key
        assert row["words"] != "could not be checked", key
        assert row["answered"] is False and row["band"] != "green", key
        assert row["title"] == "could not check: " + r["reasons"][
            "unauthorized"], key
        assert "password" in r["reasons"]["unauthorized"]
    # …and the mapping stays NARROW. Any other unreadable answer is still the
    # broker's answer being unusable, not a locked door.
    assert r["broken500"]["ps"] == "unknown"
    assert r["broken500"]["reason"] == "broker-error"
    assert r["broken500"]["words"] == "could not be checked"
    # This is only reachable on the wire because the /info cache has no TTL: the
    # probe still says 'ready' for all three, so nothing but poll() can catch it.
    assert r["cachedCapability"] == ["ready", "ready", "ready"]
    assert sorted(r["checkCalls"]) == ["broken500", "forbidden403", "local",
                                       "stale401"]
    # One refused broker cannot be rounded up to a clean fleet.
    assert r["snap"]["allCurrent"] is False
    assert r["snap"]["worst"] == "unauthorized"
    assert r["snap"]["text"] == "4 brokers · 3 unchecked"


# ---- the aggregate ---------------------------------------------------------

def test_up_to_date_needs_every_host_to_have_answered_current(harness):
    r = run(harness, "aggregate_rules")
    ok = r["all_current"]
    assert ok["allCurrent"] is True
    assert ok["worst"] == "current"
    assert ok["text"] == "3 brokers · up to date"
    # One host that has not come back is enough to take the phrase off.
    for name, worst in (("one_pending", "pending"),
                        ("one_route_absent", "route-absent"),
                        ("one_not_opted_in", "not-opted-in"),
                        ("one_unauthorized", "unauthorized"),
                        ("one_unreachable", "unreachable"),
                        ("one_unknown", "unknown")):
        case = r[name]
        assert case["allCurrent"] is False, f"{name} still read as up to date"
        assert "up to date" not in case["text"], f"{name}: {case['text']}"
        assert case["text"] == "3 brokers · 1 unchecked", f"{name}"
        assert case["worst"] == worst, f"{name}"
    # An answer that is not 'current' also takes it off, and is counted apart
    # from the silent ones.
    assert r["one_behind"]["allCurrent"] is False
    assert r["one_behind"]["text"] == "3 brokers · 1 behind"
    assert r["one_behind"]["worst"] == "behind"
    assert r["one_ahead"]["allCurrent"] is False
    assert r["one_ahead"]["text"] == "3 brokers · 1 ahead"
    assert r["one_ahead"]["worst"] == "ahead-or-diverged"


def test_an_ambiguous_headless_peer_counts_as_unchecked(harness):
    # A state added to the model is worth nothing if the aggregate quietly
    # ignores it: answered() would leave it out of the silent count, and a
    # WORST_FIRST that never lists it would fall through to 'current' and paint
    # the whole fleet GREEN while one broker's build is unknown.
    r = run(harness, "aggregate_rules")
    case = r["one_unreachable_or_too_old"]
    assert case["allCurrent"] is False
    assert "up to date" not in case["text"]
    assert case["text"] == "3 brokers · 1 unchecked"
    assert case["worst"] == "unreachable-or-too-old"
    assert case["worst"] != "current"
    assert case["states"] == ["current", "current", "unreachable-or-too-old"]
    assert case["rows"][2] == ("c|unreachable-or-too-old|" + AMBIGUOUS_WORDS
                               + "|unreachable-or-too-old")
    assert case["lines"][2] == "c — " + AMBIGUOUS_WORDS
    # It is ranked, but below the one state with something to DO about it.
    mixed = r["ambiguous_vs_behind"]
    assert mixed["worst"] == "behind"
    assert mixed["text"] == "3 brokers · 1 behind, 1 unchecked"
    assert mixed["allCurrent"] is False


def test_the_worst_state_colours_it_but_the_text_counts_them_all(harness):
    r = run(harness, "aggregate_rules")
    worst = r["worst_wins"]
    assert worst["worst"] == "behind"          # the one with something to DO
    assert worst["text"] == "4 brokers · 1 behind, 2 unchecked"
    assert worst["allCurrent"] is False
    assert worst["lines"] == [
        "this broker — up to date",
        "b — 1 commit behind",
        "c — did not answer",
        "d — too old to check",
    ]


def test_a_hidden_broker_is_still_reported(harness):
    # #178: hiding a broker parks it on the desktop, it does not make its build
    # any less stale.
    r = run(harness, "aggregate_rules")
    hidden = r["hidden_still_counted"]
    assert hidden["allCurrent"] is False
    assert hidden["text"] == "2 brokers · 1 unchecked"
    assert hidden["lines"][1] == "b — did not answer — hidden"
    assert r["all_current_hidden"]["allCurrent"] is True


def test_an_empty_fleet_is_not_vacuously_up_to_date(harness):
    r = run(harness, "aggregate_rules")
    assert r["empty"]["allCurrent"] is False
    assert "up to date" not in r["empty"]["text"]
    assert "none configured" in r["empty"]["text"]


# ---- one broker still reads as it always did -------------------------------

def test_a_single_broker_install_is_unchanged(harness):
    r = run(harness, "single_host")
    assert r["current"]["chip"] == "up to date"
    assert r["current"]["title"] == "this build is current with upstream"
    assert r["current"]["band"] == "green"
    assert r["behind2"]["chip"] == "2 behind"
    assert r["behind2"]["title"] == "a newer build is available"
    assert r["behind2"]["band"] == "amber"
    assert r["behind0"]["chip"] == "update"
    assert r["ahead"]["chip"] == "ahead"
    assert r["pending"]["coarse"] == "unknown"
    assert r["pending"]["chip"] == "version ?"
    # The coarse vocabulary is untouched — every non-answer is still 'unknown'
    # to state(), while the row beside it carries the fine-grained reason.
    for name, ps in (("notOptedIn", "not-opted-in"),
                     ("unreachable", "unreachable"),
                     ("routeAbsent", "route-absent"),
                     ("unauthorized", "unauthorized"),
                     ("unknown", "unknown")):
        row = r[name]
        assert row["coarse"] == "unknown", name
        assert row["ps"] == ps, name
        assert row["chip"] == "version ?", name
        assert row["title"].startswith("could not check: "), name
        assert row["title"] != "could not check: reason unavailable", name


def test_fanning_out_did_not_change_when_one_broker_goes_quiet(harness):
    # renderChip() hides the chip on `state() === 'current'` with one host and
    # on aggregate().allCurrent with several. If those two ever disagreed for a
    # single host, adding the fleet path would have changed what a one-broker
    # install shows.
    r = run(harness, "single_host")
    for name in ("current", "behind2", "behind0", "ahead", "notOptedIn",
                 "unreachable", "routeAbsent", "unauthorized", "unknown",
                 "pending"):
        row = r[name]
        assert row["count"] == 1
        assert row["quietOne"] == row["quietMany"], (
            f"{name}: one-host chip and aggregate disagree")
    assert r["current"]["quietOne"] is True
    assert r["current"]["aggText"] == "1 broker · up to date"
    assert r["unreachable"]["aggText"] == "1 broker · 1 unchecked"


# ---- #182: what is allowed to open this broker's gate ----------------------

def test_a_click_is_what_opens_the_gate_and_only_a_click(harness):
    """The consent rule, both directions, run against the shipped mod.

    A page load, a preference synced in from another browser, a restored
    session and a broker-side mod pin all reach init() identically — none of
    them is somebody deciding to let this machine contact GitHub, and the mod
    shipped promising exactly that. Only ctx.enabledByUser (the Control Panel
    checkbox) leads here."""
    out = run(harness, "consent")

    posted = out["click"]["posted"]
    assert len(posted) == 1, f"a click must post exactly once, got {posted}"
    assert posted[0] == {"id": "local", "method": "POST", "want": True}
    # And the answer arrives in the same beat, not at the next half-hourly tick.
    assert out["click"]["checked"] == ["local"]
    assert out["click"]["state"] == "current"
    assert out["click"]["cap"]["check_enabled"] is True

    assert out["noClick"]["posted"] == [], (
        "a poll with no human behind it asked a broker to open its gate")
    assert out["noClick"]["state"] == "not-opted-in", (
        "and it must still report the refusal honestly")

    assert out["onceOnly"] == 1, "the attempt must not repeat within a page load"


def test_consent_is_not_spent_where_it_would_do_nothing(harness):
    out = run(harness, "consent")
    assert out["alreadyOn"] == [], "a broker already checking was asked anyway"
    assert out["locked"]["posted"] == [], (
        "a config-owned gate must not even be attempted")
    assert out["locked"]["mutable"] is False
    assert out["tooOld"]["posted"] == [], (
        "a build with no route was sent a request for it")
    assert out["tooOld"]["cap"] is None


def test_only_the_local_broker_is_ever_asked(harness):
    """Peers stay read-only about their own egress. Nobody has decided that
    reaching into another machine's policy from here is acceptable, and the far
    end origin-gates the route regardless."""
    out = run(harness, "consent")
    posted = out["peers"]["posted"]
    assert [p["id"] for p in posted] == ["local"], (
        f"a peer's gate was written to: {posted}")
    assert out["peers"]["peerAState"] == "not-opted-in"


def test_nothing_hands_the_grant_back_on_its_own(harness):
    """The gate is per-BROKER and the mod's switch is per-BROWSER, so an
    automatic `false` would let one browser revoke what another is relying on,
    and the two would fight on every load. Revoking is a deliberate act."""
    out = run(harness, "consent")
    assert out["revoke"]["posted"] == [
        {"id": "local", "method": "POST", "want": False}]
    assert out["revoke"]["postedAfterPoll"] == out["revoke"]["posted"], (
        "a poll after a revoke put the grant back")
    assert out["revoke"]["cap"]["check_enabled"] is False


def test_a_failed_write_never_reads_as_a_changed_gate(harness):
    """Three ways it can fail, three distinguishable outcomes — and in none of
    them may the cached capability start claiming the gate is open."""
    out = run(harness, "consent")

    assert out["notFound"]["ok"] is False
    assert out["notFound"]["phase"] == "failed"
    assert "predates" in out["notFound"]["note"]
    assert "update_check_enabled" in out["notFound"]["note"], (
        "on a build with no route, the config key IS the answer — say it")
    assert out["notFound"]["cap"]["check_enabled"] is False

    assert out["lockedWrite"]["ok"] is False
    assert out["lockedWrite"]["phase"] == "locked", (
        "a config-owned gate is not a failure, it is somebody else's decision")
    assert "config" in out["lockedWrite"]["note"]

    assert out["unreachable"]["ok"] is False
    assert out["unreachable"]["phase"] == "failed"
    assert out["unreachable"]["cap"]["check_enabled"] is False


# ---- #182: switching a PEER on, from another broker's desktop --------------

def test_a_peer_is_only_offered_a_switch_it_can_actually_accept(harness):
    """`mutable` is NOT a remote-write capability, and conflating the two breaks
    exactly during a rolling upgrade: the build that first shipped this route
    origin-gated it, so it reports mutable:true and then refuses the write. A
    button there could only ever produce a refusal described wrongly."""
    out = run(harness, "peers")
    o = out["offered"]
    assert o["local"] is True, "the serving broker ships with this page"
    assert o["modern"] is True
    assert o["rolling"] is False, (
        "a broker that reports mutable but not remote_writable must not be "
        "offered a switch — it will 403 the write")
    assert o["locked"] is False, "its config owns the setting"
    assert o["old"] is False, "no route at all"
    assert o["asleep"] is False, "it never answered"


def test_switching_a_peer_on_posts_to_that_peer(harness):
    out = run(harness, "peers")
    w = out["peerWrite"]
    assert w["ok"] is True
    assert w["posted"] == [{"id": "peer", "method": "POST", "want": True}], (
        "the write must land on the peer, never on the serving broker")
    assert w["peerCap"]["check_enabled"] is True
    assert w["localCap"]["check_enabled"] is True, "local was not touched"
    assert w["peerState"] == "current", (
        "the answer must arrive in the same beat as the click")


def test_a_refusal_is_diagnosed_as_the_thing_it_actually_is(harness):
    out = run(harness, "peers")
    note = out["rolling"]["note"]
    assert out["rolling"]["ok"] is False
    assert "update it" in note, f"got {note!r}"
    assert "password" not in note, (
        "an origin refusal from an older build is not a credentials problem — "
        "saying so sends someone to re-enter a password that is fine")
    assert out["rolling"]["cap"]["check_enabled"] is False, (
        "a refused write must never leave the cache claiming the gate opened")
    assert "password" in out["badPassword"], (
        "a real 401 must still read as a password problem")


def test_an_answer_is_never_applied_to_a_machine_that_did_not_give_it(harness):
    """Host ids are reusable. Remove a broker, add a different one, and the id
    can be handed out again — so a write still in flight would otherwise land
    its result on a row describing a machine that never answered it."""
    out = run(harness, "peers")
    assert out["idReuse"]["cap"] is None or \
        out["idReuse"]["cap"].get("check_enabled") is not True, (
            "the old machine's answer was applied to the new one's row")
    assert out["idReuse"]["op"] is None, (
        "and no status from that write may be shown against the new machine")


def test_a_removed_broker_leaves_no_note_for_the_next_holder_of_its_id(harness):
    out = run(harness, "peers")
    assert out["removal"]["noteBefore"] is True, "the failure was recorded"
    assert out["removal"]["noteAfter"] is False, (
        "it must be pruned with the host, or the next broker to get that id "
        "inherits an error about a machine it is not")


# ---- #182 Part 2 (A5): one consent click, every grantable gate -------------

def test_a_click_now_grants_all_three_gates_in_one_request(harness):
    """The click that consents to checking consents to the broker keeping
    itself up to date, so every gate the broker will let this browser open
    rides ONE POST — never three requests, and never a value but true."""
    out = run(harness, "consent_three_gates")
    posted = out["allThree"]["posted"]
    assert len(posted) == 1, f"one request, got {posted}"
    assert posted[0]["id"] == "local" and posted[0]["method"] == "POST"
    assert out["allThree"]["bodies"] == [
        {"check_enabled": True, "apply_enabled": True,
         "restart_enabled": True}]
    # Ledger AS10: a stored "off" the sidecar synthesized for the check —
    # nobody ever clicked off — is grantable, not a standing revoke, so it
    # reads exactly like a default here.
    assert out["storedFalse"]["bodies"] == [
        {"check_enabled": True, "apply_enabled": True,
         "restart_enabled": True}]
    # The capability ladder underneath: three keys on a `policy` block, one
    # on the flat-only build, none on anything older.
    assert out["keysModern"] == ["check_enabled", "apply_enabled",
                                 "restart_enabled"]
    assert out["keysFlatOnly"] == ["check_enabled"]
    assert out["keysNone"] == []
    assert out["keysPlaceholder"] == [], (
        "a placeholder update key without `mutable` predates ANY policy "
        "write and must not be offered one")


def test_consent_degrades_to_the_keys_the_config_does_not_own(harness):
    out = run(harness, "consent_three_gates")
    assert out["degraded"]["bodies"] == [
        {"check_enabled": True, "apply_enabled": True}], (
        "a config-pinned restart is its file's decision — the other two "
        "gates must still be granted, and the pinned one never asked for")
    # A 409 that does come back names the file-owned keys, quoted, so the
    # words point at the operator's config rather than at a dead switch.
    assert '"update_apply_enabled"' in out["lockedNote"]
    assert '"restart_enabled"' in out["lockedNote"]
    assert '"update_check_enabled"' not in out["lockedNote"], (
        "a key the broker did not name as locked must not be blamed")


def test_consent_still_posts_nothing_when_config_owns_everything(harness):
    out = run(harness, "consent_three_gates")
    assert out["allConfig"]["posted"] == [], (
        "all three gates config-owned: the click has nothing to grant and "
        "must not spend a request learning so")
    assert out["allConfig"]["bodies"] == []


# ---- #182 Part 2 (A29): the aftermath of an apply --------------------------

SUCCESS_LINE = "This broker was updated and came back on the build the apply"


def test_the_post_apply_strip_renders_success_with_survivors(harness):
    # #182 trap 9: agents survive a broker restart by design and reconnect
    # still running the old code, so a successful self-update leaves a new
    # broker talking to old agents. The strip must SAY so — new build id,
    # survivor count derived by #22's own rule — and offer a fresh terminal,
    # never a replay of an existing session.
    r = run(harness, "deploy_strip")
    ok = r["success"]
    assert ok is not None
    assert ok["outcome"] == "came-up-ready-on-target"
    assert ok["cls"] == "app-upd-deploy-ok"
    assert ok["newTerminal"] is True
    # OLDBUILD agent + the version-less pre-#22 agent; the relaunched agent
    # and the plain terminal are not counted, junk rows are stepped over.
    assert ok["survivors"] == 2
    assert ok["lines"][0].startswith(SUCCESS_LINE)
    assert "bbbbbbbbbb" in ok["lines"][0], "the new build's short sha"
    assert "2 surviving agent sessions are" in ok["lines"][1]
    assert "relaunched by hand" in ok["lines"][1]
    # A clean broker still says so, rather than silently dropping the line.
    assert r["successNone"]["survivors"] == 0
    assert "No surviving agent session" in r["successNone"]["lines"][1]
    # …but an UNREADABLE count is null, never rounded down to zero: "could
    # not count" rendered as "none are stale" is the mod's one forbidden
    # sentence wearing a different hat.
    for key in ("successNoVersion", "successNoList"):
        assert r[key]["survivors"] is None, key
        assert "could not be determined" in r[key]["lines"][1], key
    assert r["survivors"] == {"noVersion": None, "noList": None,
                              "empty": 0, "counted": 2}


def test_every_deploy_failure_renders_distinctly_and_never_as_success(harness):
    r = run(harness, "deploy_strip")
    keys = ["rolled-back", "rollback-failed", "rollback-impossible",
            "came-up-on-wrong-sha", "cancelled-before-restart"]
    firsts = {}
    for k in keys:
        row = r[k]
        assert row is not None, k
        assert row["outcome"] == k
        assert row["cls"] != "app-upd-deploy-ok", f"{k} banded as success"
        assert row["newTerminal"] is False, (
            f"{k}: the fresh-terminal offer belongs to a deploy that "
            "actually came up on its target")
        assert not row["lines"][0].startswith(SUCCESS_LINE), k
        firsts[k] = row["lines"][0]
        # The broker's own account of what happened rides along.
        assert any(("why-" + k) in ln for ln in row["lines"]), (
            f"{k} dropped the broker's detail sentence")
    assert len(set(firsts.values())) == len(firsts), (
        f"two outcomes read the same: {firsts}")
    # The rollback that failed or could not happen is louder than the one
    # that worked: it names the human the tree now needs.
    assert "human" in firsts["rollback-failed"]
    assert "human" in firsts["rollback-impossible"]
    assert "human" not in firsts["rolled-back"]
    assert "rolled back" in firsts["rolled-back"]
    assert "aaaaaaaaaa" in firsts["rolled-back"], "the sha it went back to"
    # Wrong-sha is odd but alive, and names both commits.
    assert "cccccccccc" in firsts["came-up-on-wrong-sha"]
    assert "bbbbbbbbbb" in firsts["came-up-on-wrong-sha"]


def test_a_cancelled_apply_says_where_the_tree_stands(harness):
    # cancelled-before-restart can leave the checkout already updated while
    # the process keeps running the old build — the one state where the code
    # on disk and the code in memory are known to differ without a restart
    # having happened. The strip says which of the two it is.
    r = run(harness, "deploy_strip")
    moved = r["cancelledTreeMoved"]["lines"]
    assert any("cccccccccc" in ln for ln in moved)
    assert any("already moved" in ln for ln in moved)
    assert any("restart refused" in ln for ln in moved)
    same = r["cancelledTreeSame"]["lines"]
    assert any("not changed" in ln for ln in same)


def test_no_deploy_history_renders_no_strip(harness):
    r = run(harness, "deploy_strip")
    assert r["absent"] is None
    assert r["junk"] == [None, None, None, None], (
        "a junk last_deploy must read as absent, never render half a strip")
    # A verdict this build does not recognise is NOT success either.
    unknown = r["unknownOutcome"]
    assert unknown["cls"] == "app-upd-deploy-bad"
    assert unknown["newTerminal"] is False
    assert "must not be read as a success" in unknown["lines"][0]


def test_last_deploy_rides_the_local_poll_and_dies_with_the_answer(harness):
    r = run(harness, "deploy_rides_the_poll")
    assert r["afterOk"]["hasLd"] is True
    assert r["afterOk"]["outcome"] == "rolled-back"
    assert r["afterOk"]["stripOutcome"] == "rolled-back"
    # A peer that sent no last_deploy holds none — and a peer that DID send
    # one is still never rendered: the strip reads the local record only.
    assert r["afterOk"]["peerLd"] is None
    # The answer went away; the outcome that rode it goes too, exactly like
    # the stored 'current' does.
    assert r["afterDown"]["ld"] is None
    assert r["afterDown"]["strip"] is None
    assert r["after503"]["ld"] is None


def test_the_fresh_terminal_targets_the_local_broker_explicitly(harness):
    # Inherited refutation R7: no durable session identity survives a
    # restart, so the ONLY affordance is a fresh terminal — and it goes to
    # the LOCAL broker, resolved from the literal id at click time, never a
    # null host that launchProfile would default to the serving origin.
    r = run(harness, "fresh_terminal_host")
    assert r["id"] == "local"
    assert r["isNull"] is False
    assert r["unconfigured"] is None
    # The wiring: resolved at click time, guarded on null, through the
    # core's own (+) quick-launch path.
    src = MOD_JS.read_text(encoding="utf-8")
    seg = src[src.index("function renderDeployStrip"):
              src.index("function renderChecked")]
    assert "const h = freshTerminalHost();" in seg
    assert "if (!h) return;" in seg
    assert "launchProfile(h, hostDefaultProfile(h))" in seg


def test_the_restart_confirm_names_the_old_code_cost():
    # Pre-flight honesty: the confirm's continuity counts already exist; the
    # sentence beside them must say that surviving is not updating.
    src = MOD_JS.read_text(encoding="utf-8")
    body = src[src.index("function restartConfirmBody"):
               src.index("function onRestartClick")]
    assert "keeps running " in body
    assert "the code it was started with" in body
    assert "relaunched by hand" in body


# ---- atom A30: the Update... UI action -------------------------------------

def test_apply_enablement_disables_on_each_unmet_condition_distinctly(harness):
    """The four conditions from the design (gate, restart, and the check
    being behind with a known target) surface as FIVE distinct codes, because
    "the check is not behind" and "the check never established anything" ask
    two different things of whoever reads the reason."""
    r = run(harness, "apply_gate")
    assert r["enabled"] is None, "every condition met must not disable"
    codes = {"gateOff": r["gateOff"], "restartUnavailable":
             r["restartUnavailable"], "unknownState": r["unknownState"],
             "notBehindCurrent": r["notBehindCurrent"], "noTarget":
             r["noTarget"]}
    assert len(set(codes.values())) == 5, f"two reasons collapsed: {codes}"
    assert r["notBehindAhead"] == r["notBehindCurrent"], (
        "current and ahead-or-diverged both read as 'nothing to apply'")
    assert r["gateOff"] == "apply-disabled-here"
    assert r["restartUnavailable"] == "restart-unavailable-here"
    assert r["unknownState"] == "unknown-state"
    assert r["notBehindCurrent"] == "not-behind"
    assert r["noTarget"] == "no-target-sha"


def test_apply_enablement_reasons_have_distinct_words(harness):
    r = run(harness, "apply_gate")
    assert r["wordsEnabled"] is None, "an enabled gate has no reason to show"
    words = [r["wordsGateOff"], r["wordsRestart"], r["wordsUnknown"],
             r["wordsNotBehind"], r["wordsNoTarget"]]
    assert all(words), "every disabled code must have words"
    assert len(set(words)) == 5, f"two reasons read the same: {words}"
    # The config gate says which key, and that it is config-file-only.
    assert "update_apply_enabled" in r["wordsGateOff"]
    assert "config" in r["wordsGateOff"]
    # The restart-unavailable reason reuses the restart control's OWN words
    # rather than inventing a second vocabulary for the same fact.
    assert "this broker cannot restart itself on this install" in \
        r["wordsRestart"]


def test_apply_disabled_words_point_at_the_row_when_it_is_writable(harness):
    """atom A7: the disabled-apply words derive from the gate's CURRENT
    source at render time, not a cached sentence. stored/default/corrupt
    (the second row can move all three) get the row's own sentence; a
    config-owned gate -- or a call with no facts at all, the historic
    signature -- keeps today's config-file sentence byte-for-byte."""
    r = run(harness, "apply_gate")
    row_label = 'Allow this broker to update itself'
    for key in ("wordsGateOffStored", "wordsGateOffDefault",
                "wordsGateOffCorrupt"):
        assert row_label in r[key], (key, r[key])
        assert "update_apply_enabled" not in r[key], (key, r[key])
    # config-owned reads exactly like the facts-absent (two-arg) call --
    # the byte-for-byte rule the brief pins.
    assert r["wordsGateOffConfig"] == r["wordsGateOff"]
    assert "update_apply_enabled" in r["wordsGateOffConfig"]
    assert "config" in r["wordsGateOffConfig"]


def test_apply_target_sha_needs_an_exact_upstream_commit(harness):
    """#182's release-mode branch names a tag and never a sha at all -- a
    'behind' state there is real, but there is nothing exact to apply."""
    r = run(harness, "apply_gate")
    assert r["targetShaValid"] == "a" * 40
    assert r["targetShaReleaseMode"] is None
    assert r["targetShaBadHex"] is None
    assert r["targetShaNoCheck"] is None


def test_every_apply_refusal_shape_is_rendered_distinctly(harness):
    """The server is the source of truth; this pins that the parser never
    collapses two different refusals into the same words, and never a guess
    at success for anything short of the one clean 202."""
    r = run(harness, "apply_refusals")
    assert r["transport"]["kind"] == "transport"
    assert r["success"] is None, (
        "a clean 202/ok:true must never be read as a refusal here -- "
        "waitForApplyBootId owns proving it, not this parser")
    assert r["gate"]["kind"] == "gate"
    # atom A7: source-neutral at refusal time -- names BOTH paths a
    # human could use, never claims the config file is the only one.
    assert 'Allow this broker to update itself' in r["gate"]["lines"][0]
    assert "config" in r["gate"]["lines"][0]
    assert r["incomplete"]["kind"] == "incomplete"
    assert r["incomplete"]["reasonCode"] == "drain_failed"
    assert "STILL RUNNING THE OLD CODE" in r["incomplete"]["lines"][0]
    assert "manual" in r["incomplete"]["lines"][0].lower()
    assert "apply-abc123" in r["inProgress"]["lines"][0]
    assert r["inProgress"]["kind"] == "in-progress"
    assert r["restartInProgress"]["kind"] == "in-progress"
    # every refusal message, not just the first
    assert r["multiRefusal"]["lines"] == [
        "Tracked files carry local modifications.",
        "This checkout carries 2 local commits upstream does not have.",
    ]
    assert r["multiRefusal"]["kind"] == "refused"
    assert any("human on the machine itself" in ln
               for ln in r["treeSuspect"]["lines"]), (
        "a merge that failed partway through must say the tree may need "
        "a human")
    assert r["treeSuspect"]["kind"] == "failed"
    assert r["noRefusalsListed"]["lines"] == [
        "the broker refused the update but did not say why."]
    assert r["garbageBody"]["kind"] == "unknown"
    assert r["wrongStatus"]["kind"] == "unknown"
    assert "must not be read as a success" in r["wrongStatus"]["lines"][0]
    # Every one of these shapes reads differently.
    kinds = [r[k]["kind"] for k in (
        "transport", "gate", "incomplete", "inProgress", "multiRefusal",
        "treeSuspect", "garbageBody")]
    assert len(set(kinds)) == len(set(kinds)), "sanity: kinds are hashable"


def test_the_update_button_is_local_only_and_beside_restart(harness):
    """apply never touches a remote host: renderApplyRow reads only the
    LOCAL_HOST_ID facts, and is rendered once, above the per-broker loop --
    never inside it, where a peer row would pick it up."""
    src = MOD_JS.read_text(encoding="utf-8")
    seg = src[src.index("function renderApplyRow"):
              src.index("// ---- detail window")]
    assert "checkStateFor(LOCAL_HOST_ID)" in seg
    assert "updateCapFor(LOCAL_HOST_ID)" in seg
    assert "restartInfo()" in seg
    window_seg = src[src.index("function renderWindow(win)"):
                     src.index("function renderAll()")]
    assert window_seg.count("renderApplyRow(body)") == 1
    assert window_seg.index("renderApplyRow(body)") < \
        window_seg.index("for (const r of rows)"), (
        "the apply row must sit above the per-broker loop, never inside it")
    # A6: the self-update row is the opposite shape — per broker, so it
    # rides INSIDE the loop, exactly once, right after the checking row.
    # It must not migrate up beside the local-only apply/restart controls.
    assert window_seg.count("renderSelfUpdateRow(body, r.id, r.label)") == 1
    assert window_seg.index("for (const r of rows)") < \
        window_seg.index("renderSelfUpdateRow(")
    assert window_seg.index("renderPolicyRow(body, r.id, r.label)") < \
        window_seg.index("renderSelfUpdateRow(body, r.id, r.label)")


def test_only_a_confirmed_click_can_post_and_the_previewed_sha_is_exact(
        harness):
    """The confirm dialog's values are captured once, at click time, from
    the SAME paint that disabled/enabled the button, and performApply is
    reachable from nowhere else."""
    src = MOD_JS.read_text(encoding="utf-8")
    assert src.count("performApply(") == 2, (
        "exactly one definition and one call site")
    assert "return performApply(target);" in src
    assert "if (!res || !res.value) return;" in src
    perform_seg = src[src.index("async function performApply"):
                      src.index("function applyConfirmBody")]
    # The previewed sha is used exactly as given -- never re-derived from
    # live state inside the POST path itself.
    assert "checkStateFor(" not in perform_seg
    assert "applyTargetSha(" not in perform_seg
    assert "body: JSON.stringify({ target_sha: targetSha })" in perform_seg
    # No timer or poll tick reaches it.
    poll_seg = src[src.index("async function poll(hostId, opts)"):
                   src.index("function pollTick(opts)")]
    assert "performApply" not in poll_seg
    assert "setInterval" not in src[src.index("async function performApply"):
                                    src.index("// ---- detail window")]


def test_apply_confirm_shows_range_count_compare_and_session_cost(harness):
    src = MOD_JS.read_text(encoding="utf-8")
    seg = src[src.index("function applyConfirmBody"):
              src.index("function renderApplyRow")]
    assert "shortSha(oldSha)" in seg and "shortSha(targetSha)" in seg
    assert "'..'" in seg
    assert "behindBy" in seg
    assert "compareUrl" in seg
    # The SAME live-session cost block the restart confirm renders (#183),
    # not a second copy of its wording that could drift from it.
    assert "restartConfirmBody(cont)" in seg


def test_202_never_renders_success_here_and_hands_off_to_the_boot_watch(
        harness):
    """The 202 is accepted-and-stopping, not done; only a proven boot id
    change may say so, and even that hands off to a recheck rather than
    declaring victory itself (#182 Part 2, A29 owns the real verdict)."""
    src = MOD_JS.read_text(encoding="utf-8")
    perform_seg = src[src.index("async function performApply"):
                      src.index("function applyConfirmBody")]
    assert "phase: 'done'" not in perform_seg, (
        "the 202 handler itself must never claim success")
    assert "await waitForApplyBootId(body.bootId);" in perform_seg
    wait_seg = src[src.index("async function waitForApplyBootId"):
                  src.index("// The ONLY caller of POST /update/apply")]
    assert "phase: 'done'" in wait_seg
    assert "recheck()" in wait_seg, (
        "a proven restart must still hand off to a recheck, never assume "
        "the target was reached")


# ---- atom A7: restart-disabled points at the row too -----------------------

def test_restart_disabled_words_carry_the_qualified_row_and_config_phrasing(
        harness):
    """RESTART_REASONS is static/factless -- it cannot see the restart
    gate's current source the way applyGateWords can -- so its
    'restart-disabled' entry is qualified rather than a flat claim: it
    names the row AND the config file, never just one."""
    r = run(harness, "restart_disabled_words")
    assert 'Allow this broker to update itself' in r["words"]
    assert "config" in r["words"]


# ---- #183 R6: the cooldown reason has honest words -------------------------

def test_the_cooldown_reason_has_honest_words(harness):
    """The broker's new "cooldown" reason_code must render as a sentence that
    says it clears by itself — never the raw token, and never the generic
    "did not say why" — and the retry_after_s the broker pairs with it is
    shown only when it is a genuine positive number."""
    r = run(harness, "restart_cooldown_words")
    assert r["known"] is True, "RESTART_REASONS has no 'cooldown' entry"
    assert r["plain"] != "this broker did not say why", (
        "the cooldown fell through to the generic sentence")
    assert "clears by itself" in r["plain"], r["plain"]
    # The number is additive: same sentence, plus WHEN.
    assert r["withRetry"].startswith(r["plain"]), r["withRetry"]
    assert "42" in r["withRetry"]
    # ...and only when it really is a positive number, only for the cooldown.
    assert r["badRetry"] == r["plain"], (
        "a non-numeric retry_after_s leaked into the rendered words")
    assert "42" not in r["otherReasonIgnoresRetry"]
    assert r["unknownCode"] == "this broker did not say why"
    # The Update… button's gate needs NO extra work for a cooldown: restart
    # unavailable is restart unavailable, and the reason words flow through.
    assert r["applyGateCode"] == "restart-unavailable-here"
    assert "applying needs a restart to take effect" in r["applyGateWords"]
    assert r["plain"] in r["applyGateWords"], (
        "the cooldown words did not flow through the apply gate wording")


def test_a_refused_refresh_never_reads_as_just_checked(harness):
    """The broker floors and budgets forced refreshes, and answers a refused
    one with a 200 carrying the answer it already had. These sentences are the
    only thing standing between that and a page claiming it just checked."""
    out = run(harness, "refresh_refused_words")
    # Each refusal names its own cause, and none of them claim freshness.
    assert "rate-limiting" in out["rateLimited"]
    assert "moments ago" in out["tooSoon"]
    assert "hour" in out["budget"]
    for k in ("rateLimited", "tooSoon", "budget", "unknownReason", "empty"):
        low = out[k].lower()
        assert "just checked" not in low and "up to date" not in low, k
        assert ("kept" in low or "did not re-ask" in low
                or "already had" in low), k
    # Distinct, so an operator can tell a self-clearing floor from a real
    # upstream limit -- one is a moment, the other is GitHub's word.
    said = [out["rateLimited"], out["tooSoon"], out["budget"]]
    assert len(set(said)) == 3
    # A retry hint when there is one, silence when there is not, and never a
    # fabricated number from junk.
    assert "42s" in out["tooSoon"]
    assert "30 min" in out["rateLimited"], "long waits read in minutes"
    assert "try again" not in out["noRetry"]
    assert "try again" not in out["junkRetry"]
    # An unrecognised reason still refuses honestly rather than inventing one.
    assert out["unknownReason"] and "did not re-ask" in out["unknownReason"]


# ---- #182 Part 2 (A6): the self-update row ---------------------------------

def test_the_second_row_grants_apply_and_restart_together(harness):
    """One click, one POST, both grants — and never the check key, which
    has a row of its own."""
    r = run(harness, "self_update_writes")
    g = r["grant"]
    assert g["before"]["on"] is False
    assert g["before"]["disabled"] is False
    assert g["before"]["postKeys"] == ["apply_enabled", "restart_enabled"]
    assert g["before"]["labelWords"] == "Allow this broker to update itself"
    assert g["ok"] is True
    assert len(g["calls"]) == 1
    assert g["bodies"] == [{"apply_enabled": True, "restart_enabled": True}]
    assert "check_enabled" not in g["bodies"][0]
    assert g["after"]["on"] is True
    # the write reported into its own 'self' lane and cleared on success;
    # the checking row's lane was never touched
    assert g["selfOp"] is None
    assert g["checkOp"] is None


def test_the_second_row_posts_only_the_keys_the_config_does_not_own(harness):
    """A config-owned TRUE gate never blocks enabling the other one: the
    row stays live, the body carries exactly the stored-false key, and
    granting it turns the row ON."""
    r = run(harness, "self_update_writes")
    p = r["partial"]
    assert p["before"]["on"] is False
    assert p["before"]["disabled"] is False
    assert p["before"]["postKeys"] == ["apply_enabled"]
    assert p["ok"] is True
    assert p["bodies"] == [{"apply_enabled": True}]
    assert p["after"]["on"] is True


def test_stop_posts_false_for_every_mutable_gate_and_never_check(harness):
    """The values are computed from the direction the human asked for —
    the ui_assets no-auto-revoke guard forbids a `*_enabled: false`
    literal anywhere in the mod, so this proves the built body still
    revokes both grants and leaves checking alone."""
    r = run(harness, "self_update_writes")
    s = r["stop"]
    assert s["before"]["on"] is True
    assert s["before"]["postKeys"] == ["apply_enabled", "restart_enabled"]
    assert s["ok"] is True
    assert s["bodies"] == [{"apply_enabled": False,
                            "restart_enabled": False}]
    assert all("check_enabled" not in b for b in s["bodies"])
    assert s["after"]["on"] is False


def test_a_locked_self_update_write_names_the_locked_key(harness):
    """The 409 names ONLY the key the broker said its file owns, the
    refusal's authoritative `update` view repaints the row, and nothing
    retries."""
    r = run(harness, "self_update_writes")
    lk = r["locked"]
    assert lk["ok"] is False
    assert lk["op"] is not None and lk["op"]["phase"] == "locked"
    assert '"restart_enabled"' in lk["op"]["note"]
    assert "update_apply_enabled" not in lk["op"]["note"]
    assert "update_check_enabled" not in lk["op"]["note"]
    assert lk["calls"] == 1, "a refusal must not be followed by another POST"
    # the 409's `update` view was installed: the row now reads the
    # config-owned restart off what the broker just said
    assert lk["cap"]["policy"]["restart"]["source"] == "config"
    assert lk["after"]["on"] is False
    assert lk["after"]["disabled"] is True
    assert lk["after"]["postKeys"] == []


def test_a_peer_that_predates_per_key_grants_gets_told_to_update_not_a_dead_switch(
        harness):
    """The flat single-key build can take a check write but knows nothing
    of per-gate grants: it gets a row that is present but dead, wearing
    words that say what fixes it — never a switch that would 400."""
    r = run(harness, "self_update_old_peers")
    assert r["flatNeeded"] is True
    m = r["flatModel"]
    assert m["on"] is False
    assert m["disabled"] is True
    assert m["postKeys"] == []
    assert "predates self-update grants" in m["note"]
    assert "update that broker" in m["note"]
    # …and the two neighbours: a modern build gets a row, a build with no
    # update view at all gets NO row.
    assert r["localNeeded"] is True
    assert r["ancientNeeded"] is False
    assert r["keysFlat"] == ["check_enabled"]
    assert r["keysAncient"] == []


def test_a_remote_self_update_grant_lands_on_that_peer_and_only_after_the_confirm(
        harness):
    r = run(harness, "self_update_remote")
    assert r["beforeConfirm"] == [], "no POST before the confirm resolves"
    c = r["confirmed"]
    assert c["ok"] is True
    assert [x["id"] for x in c["calls"]] == ["peer"], (
        "the grant must land on THAT peer, never the serving broker")
    assert c["bodies"] == [{"apply_enabled": True, "restart_enabled": True}]
    assert c["after"]["on"] is True
    # The dialog named a machine; a changed url/label or a vanished host
    # aborts silently-with-note, and an already-ON row posts nothing.
    assert r["moved"]["ok"] is False and r["moved"]["calls"] == []
    assert r["moved"]["op"] is not None
    assert "nothing was sent" in r["moved"]["op"]["note"]
    assert r["relabelled"]["ok"] is False and r["relabelled"]["calls"] == []
    assert r["vanished"]["ok"] is False and r["vanished"]["calls"] == []
    assert "nothing was sent" in r["vanished"]["op"]["note"]
    assert r["meanwhileOn"]["ok"] is False
    assert r["meanwhileOn"]["calls"] == [], (
        "an enable confirmation must never turn into a Stop")
    # The wiring: the remote-enable click reaches setPolicy only through
    # the confirm, whose OK button hands off to commitRemoteSelfUpdate.
    src = MOD_JS.read_text(encoding="utf-8")
    seg = src[src.index("function renderSelfUpdateRow"):
              src.index("function renderRestartRow")]
    branch = seg[seg.index("if (!local && !m.on)"):]
    branch = branch[:branch.index("const grant")]
    assert "confirmRemoteSelfUpdate(hostId, label)" in branch
    assert "setPolicy(" not in branch
    cseg = src[src.index("function confirmRemoteSelfUpdate"):
               src.index("// One broker's switch plus its inline reason")]
    assert "commitRemoteSelfUpdate(hostId, url, label)" in cseg
    assert "if (!res || !res.value) return false;" in cseg
    assert "selfConfirms.has(hostId)" in cseg, "one pending confirm per host"


def test_the_row_surfaces_config_owners_and_standing_grants(harness):
    """The mixed-owner matrix, over the pure model: a config-owned FALSE
    gate kills the row and is NAMED; a config-owned TRUE gate never
    blocks the other; a standing grant behind an OFF aggregate is said
    out loud; and a malformed block fails closed source-neutrally."""
    r = run(harness, "self_update_model")
    m = r["bothOffMutable"]
    assert (m["on"], m["disabled"]) == (False, False)
    assert m["postKeys"] == ["apply_enabled", "restart_enabled"]
    assert m["note"] == ""
    live = r["applyOffRestartConfigTrue"]
    assert live["disabled"] is False
    assert live["postKeys"] == ["apply_enabled"]
    assert "restarting itself is still granted" in live["note"]
    dead = r["restartConfigFalse"]
    assert dead["disabled"] is True and dead["postKeys"] == []
    assert 'its config names "restart_enabled", so that file decides' in \
        dead["note"]
    granted = r["restartConfigFalseApplyGranted"]
    assert granted["disabled"] is True and granted["postKeys"] == []
    assert '"restart_enabled"' in granted["note"]
    assert "applying updates is still granted" in granted["note"], (
        "a standing grant must be surfaced, never hidden behind an "
        "aggregate that reads OFF")
    both = r["bothConfigFalse"]
    assert '"update_apply_enabled"' in both["note"]
    assert '"restart_enabled"' in both["note"]
    on = r["onBothMutable"]
    assert on["on"] is True and on["disabled"] is False
    assert on["postKeys"] == ["apply_enabled", "restart_enabled"]
    on_part = r["onRestartConfigTrue"]
    assert on_part["on"] is True and on_part["postKeys"] == ["apply_enabled"]
    dead_on = r["onBothConfigTrue"]
    assert dead_on["on"] is True and dead_on["disabled"] is True
    assert dead_on["postKeys"] == []
    assert '"update_apply_enabled"' in dead_on["note"]
    assert '"restart_enabled"' in dead_on["note"]
    # Fail closed on shape: never ON, never live, source-neutral words,
    # and never the flat-build words while a block was present.
    for i, bad in enumerate(r["malformed"]):
        assert bad["on"] is False, i
        assert bad["disabled"] is True, i
        assert bad["postKeys"] == [], i
        assert "config" not in bad["note"], i
        assert "update that broker" not in bad["note"], i
    assert "update that broker" in r["flatBuild"]["note"]
    # An in-flight op wears the asked-for direction, not the cached state.
    assert r["busyOn"]["labelWords"] == "Allowing…"
    assert r["busyOn"]["disabled"] is True
    assert r["busyOff"]["labelWords"] == "Stopping…"
    assert r["failedOp"]["note"] == "locked words"
