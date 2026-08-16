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
    # #182 Part 2 (A3): the apply flow itself -- POST /update/apply plus the
    # boot-id wait -- moved out of update.js's closure into update-apply.js
    # as a dependency-injected factory, host-parameterized. Read whole with
    # the companion; update.js's init only builds the deps object.
    "function makeApplyFlow", "async function performApply",
    "async function pollBootId", "async function waitForApplyBootId",
    # #182 Part 2 (A4): the remote rows' Update button. The row's whole
    # decision and the confirm dialog's decidable half ship in the
    # update-apply.js companion (read whole); the confirm-time re-verify
    # and its refusal-note map ride update.js's sliced opt-in range, the
    # commitRemoteSelfUpdate pattern.
    "function remoteApplyRowModel", "function remoteApplyConfirmModel",
    "function noteRefusal", "function swapDeixis",
    "async function commitRemoteApply",
    # atom A5: the manual-pull how-to note is now residual -- it renders
    # only for the brokers that are behind and have no live path, and
    # names them. Pure over update.js's own per-row verdict assembly;
    # ships in update-apply.js (read whole, above).
    "function manualPullNote",
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
        // #188 item 1 (A3): the real broker COMMITS a policy write before
        // answering, so any /info read that starts after the response
        // carries the post-write view. A static INFO map would instead
        // hand the targeted re-read the PRE-write record -- a lie no real
        // server tells -- so a 2xx answer writes its view through.
        if (spec.status >= 200 && spec.status < 300
                && spec.body && spec.body.update
                && INFO[host.id] && typeof INFO[host.id] === 'object') {
            INFO[host.id] = Object.assign({}, INFO[host.id],
                                          { update: spec.body.update });
        }
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

    // A consent body WITHOUT the check key — the check is already on and
    // config-owned, so consentBody rightly omits it — must still read as a
    // GRANT: the op a failed write leaves behind carries want=true and
    // never the checking-off words (the critic's stuck-amber-row finding).
    reset();
    fleet(['local']);
    INFO = { local: INFO_MODERN(true, Object.assign(
        { source: 'config', mutable: false },
        { policy: { check: GATE(true, 'config'),
                    apply: GATE(false, 'default'),
                    restart: GATE(false, 'default') } })) };
    CHECK = { local: R503 };
    POLICY = { local: { status: 500, body: { ok: false } } };
    await offerConsent();
    out.noCheckKey = { bodies: policyBodies.slice(),
                       op: opFor('local') || null };

    // Ledger AS12: a mutable gate whose `enabled` is missing or junk-falsy
    // reads as closed and GRANTABLE — and the body may only ever carry
    // true, whatever the junk was.
    out.junkEnabled = consentBody({
        check: GATE(true, 'stored'),
        apply: { mutable: true },
        restart: { enabled: 0, source: 'stored', mutable: true } });

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

// --- #188 item 1 (atom A3): restart-gate staleness repair ------------------
// The pure predicate (restartGateMoved, update-policy.js) deciding whether a
// policy write owes a targeted /info re-read, and the setPolicy wiring that
// spends it: one GET, the written host only, never on a write that left the
// restart gate where it stood, never on a refusal.
CASES.restart_gate_moved = async () => {
    const out = {};
    const V = (restartOn) => SVIEW(SGATE(true, 'stored'),
                                   SGATE(restartOn, 'stored')).update;
    // The predicate proper. Direction must be NAMED in the write body --
    // absence is never read as a direction (#187's lesson).
    out.movedOn = restartGateMoved({ restart_enabled: true },
                                   V(false), V(true));
    out.movedOff = restartGateMoved({ restart_enabled: false },
                                    V(true), V(false));
    out.namedButStill = restartGateMoved({ restart_enabled: true },
                                         V(true), V(true));
    out.unnamed = restartGateMoved({ apply_enabled: true },
                                   V(false), V(true));
    out.emptyChanges = restartGateMoved({}, V(false), V(true));
    out.nullChanges = restartGateMoved(null, V(false), V(true));
    out.stringDirection = restartGateMoved({ restart_enabled: 'true' },
                                           V(false), V(true));
    // AFTER missing (response carried no update view): fall back to the
    // direction the write named.
    out.noAfterMoved = restartGateMoved({ restart_enabled: true },
                                        V(false), null);
    out.noAfterStill = restartGateMoved({ restart_enabled: true },
                                        V(true), null);
    // BEFORE unreadable: fail toward freshness.
    out.noBefore = restartGateMoved({ restart_enabled: true },
                                    null, V(true));
    out.junkBefore = restartGateMoved({ restart_enabled: true },
                                      'nope', V(true));

    // The wiring, over the shipped setPolicy.
    const prime = async (pol, policyAnswer) => {
        reset();
        fleet(['local']);
        INFO = { local: INFO_MODERN(true, { policy: pol }) };
        CHECK = { local: OK200({ state: 'current' }) };
        POLICY = { local: policyAnswer };
        await pollTick();
        policyCalls.length = 0; policyBodies.length = 0;
        infoCalls.length = 0;
    };
    const click = async () => {
        const m = selfUpdateModelFor('local');
        const grant = !m.on;
        return setPolicy('local', policyChangesFor(m.postKeys, grant),
            { kind: 'self', want: grant,
              busyNote: selfUpdateBusyNote(grant) });
    };
    // Grant flips restart_enabled: exactly one /info re-read, this host,
    // and the row model repaints from the POST-write facts.
    await prime(SPOL(SGATE(false, 'default'), SGATE(false, 'default')),
        { status: 200, body: { ok: true,
            update: SVIEW(SGATE(true, 'stored'),
                          SGATE(true, 'stored')).update } });
    out.grantOk = await click();
    out.grantRereads = infoCalls.slice();
    out.grantAfterOn = selfUpdateModelFor('local').on;
    // Apply-only write (restart config-owned): the body never names
    // restart_enabled, so no re-read is spent.
    await prime(SPOL(SGATE(false, 'stored'), SCONF(true)),
        { status: 200, body: { ok: true,
            update: SVIEW(SGATE(true, 'stored'), SCONF(true)).update } });
    out.applyOnlyOk = await click();
    out.applyOnlyRereads = infoCalls.slice();
    // A refusal commits nothing: no re-read.
    await prime(SPOL(SGATE(false, 'default'), SGATE(false, 'default')),
        { status: 409, body: { ok: false, error: 'policy_locked',
            source: 'config', locked: ['restart_enabled'],
            update: SVIEW(SGATE(false, 'default'),
                          SCONF(false)).update } });
    out.lockedOk = await click();
    out.lockedRereads = infoCalls.slice();
    return out;
};

// --- atom A4: the remote rows' Update button model -------------------------
// Pure over ONE broker's own facts -- check, update view, restart block,
// op -- so two rows in one paint can disagree honestly. RUPD is a modern
// remote-applyable view; RRST a healthy restart block; each case overrides
// exactly the facts it is about.
const RUPD = (over) => Object.assign({
    check_enabled: true, apply_enabled: true, source: 'stored',
    mutable: true, remote_writable: true, remote_applyable: true,
    policy: { check: SGATE(true, 'stored'), apply: SGATE(true, 'stored'),
              restart: SGATE(true, 'stored') } }, over || {});
const RRST = (over) => Object.assign({ known: true, available: true,
    reason: null, retryAfterS: null,
    continuity: { guaranteed: 2, at_risk: 1, unknown: 0 },
    bootId: 'boot-1' }, over || {});
CASES.remote_apply_row_model = async () => {
    const out = {};
    // TWO hosts, DIFFERENT facts, one paint: X live on its own target, Y
    // dead on ITS OWN policy -- neither row may read the other's facts.
    out.x = remoteApplyRowModel({ behind: true, coarseState: 'behind',
        check: { upstream: { sha: HEX40('a') }, behindBy: 3 }, op: null,
        upd: RUPD(), restart: RRST() });
    out.y = remoteApplyRowModel({ behind: true, coarseState: 'behind',
        check: { upstream: { sha: HEX40('b') } }, op: null,
        upd: RUPD({ apply_enabled: false,
            policy: { check: SGATE(true, 'stored'),
                      apply: SGATE(false, 'stored'),
                      restart: SGATE(true, 'stored') } }),
        restart: RRST() });
    // the gate words derive from THAT broker's own facts: its config-owned
    // apply gate, its own cooldown through the restart gate, and an honest
    // absence when it never reported a restart block at all.
    out.configGate = remoteApplyRowModel({ behind: true,
        coarseState: 'behind', check: { upstream: { sha: HEX40('c') } },
        op: null, upd: RUPD({ apply_enabled: false,
            policy: { check: SGATE(true, 'stored'), apply: SCONF(false),
                      restart: SGATE(true, 'stored') } }),
        restart: RRST() });
    out.restartGate = remoteApplyRowModel({ behind: true,
        coarseState: 'behind', check: { upstream: { sha: HEX40('c') } },
        op: null, upd: RUPD(),
        restart: RRST({ available: false, reason: 'cooldown',
                        retryAfterS: 30 }) });
    out.restartUnknown = remoteApplyRowModel({ behind: true,
        coarseState: 'behind', check: { upstream: { sha: HEX40('c') } },
        op: null, upd: RUPD(),
        restart: { known: false, available: false } });
    // the two missing-capability degradations, plus no view at all
    out.noView = remoteApplyRowModel({ behind: true, coarseState: 'behind',
        check: { upstream: { sha: HEX40('c') } }, op: null, upd: null,
        restart: RRST() });
    out.noViewWithOp = remoteApplyRowModel({ behind: false,
        coarseState: 'unknown', check: null,
        op: { phase: 'failed', note: ['went dark'] }, upd: null,
        restart: { known: false } });
    out.predates = remoteApplyRowModel({ behind: true,
        coarseState: 'behind', check: { upstream: { sha: HEX40('c') } },
        op: null, upd: { check_enabled: true, apply_enabled: true,
                         mutable: true },
        restart: RRST() });
    out.noRemoteApplyable = remoteApplyRowModel({ behind: true,
        coarseState: 'behind', check: { upstream: { sha: HEX40('c') } },
        op: null, upd: RUPD({ remote_applyable: undefined }),
        restart: RRST() });
    // the OR: an op keeps its row while ps flips off 'behind'; a quiet
    // current host renders nothing; and only 'waiting' blocks the button.
    out.opKeepsRow = remoteApplyRowModel({ behind: false,
        coarseState: 'unknown', check: null,
        op: { phase: 'waiting', note: ['applying…'] }, upd: RUPD(),
        restart: RRST() });
    out.hiddenWhenQuiet = remoteApplyRowModel({ behind: false,
        coarseState: 'current', check: { upstream: { sha: HEX40('c') } },
        op: null, upd: RUPD(), restart: RRST() });
    out.waiting = remoteApplyRowModel({ behind: true, coarseState: 'behind',
        check: { upstream: { sha: HEX40('a') } },
        op: { phase: 'waiting', note: ['sending…'] }, upd: RUPD(),
        restart: RRST() });
    out.settled = remoteApplyRowModel({ behind: true, coarseState: 'behind',
        check: { upstream: { sha: HEX40('a') } },
        op: { phase: 'failed', note: ['no'] }, upd: RUPD(),
        restart: RRST() });
    // the confirm dialog's decidable half: broker named, link scheme-
    // filtered, session cost from ITS restart block or an explicit unknown.
    out.confirm = remoteApplyConfirmModel('peer-label',
        { local: { sha: HEX40('a') }, behindBy: 3,
          upstream: { sha: HEX40('b'),
                      url: 'https://github.com/x/y/compare/a...b' } },
        RRST());
    out.confirmUnknown = remoteApplyConfirmModel('peer-label',
        { upstream: { sha: HEX40('b'), url: 'javascript:alert(1)' } },
        { known: false });
    return out;
};

// --- atom A5: the how-to note is residual and names its brokers -----------
// Pure over update.js's own per-row verdict assembly -- {label, behind,
// live} -- so every case here is about the DECISION and the WORDING, not
// about how `live` gets decided (that is remoteApplyRowModel's and the
// local apply row's own gate, both pinned elsewhere).
CASES.manual_pull_note = async () => {
    const out = {};
    // Both directions: a behind row with no live path renders the note...
    out.rendersWhenDead = manualPullNote(
        [{ label: 'peer-b', behind: true, live: false }]);
    // ...and the SAME row, live, renders nothing at all.
    out.silentWhenLive = manualPullNote(
        [{ label: 'peer-b', behind: true, live: true }]);
    // A row that is not behind is never named, live or not -- an
    // unreachable row already carries its own reason note above this one.
    out.silentWhenNotBehind = manualPullNote(
        [{ label: 'peer-c', behind: false, live: false }]);
    out.empty = manualPullNote([]);
    out.zeroBehind = manualPullNote([
        { label: 'local', behind: false, live: true },
        { label: 'peer-a', behind: false, live: false }]);
    // The R2 trap: the LOCAL broker, behind but with its own live apply
    // row, must not be counted -- only a dead local counts.
    out.localLive = manualPullNote(
        [{ label: 'this broker', behind: true, live: true }]);
    out.localDead = manualPullNote(
        [{ label: 'this broker', behind: true, live: false }]);
    // A mixed fleet: one behind+live, one behind+dead, one not behind at
    // all -- the note names only the dead one.
    out.mixed = manualPullNote([
        { label: 'this broker', behind: true, live: true },
        { label: 'peer-dead', behind: true, live: false },
        { label: 'peer-current', behind: false, live: false }]);
    // Three or more named brokers join the same way the per-broker reason
    // notes' own list-building precedent does ("X, Y and Z").
    out.multiNamed = manualPullNote([
        { label: 'peer-a', behind: true, live: false },
        { label: 'peer-b', behind: true, live: false },
        { label: 'peer-c', behind: true, live: false }]);
    // Untrusted input -- a verdict list is update.js's own assembly, but
    // this stays defensive the same way every other companion helper is.
    out.junkRows = manualPullNote([null, 'nope', 7,
        { label: 42, behind: true, live: false },
        { behind: true, live: false }]);
    return out;
};

// --- atom A4: refusals name the broker they describe -----------------------
CASES.remote_apply_refusal_words = async () => {
    const out = {};
    out.transportLocal = applyRefusalOutcome(null, null);
    out.transportRemote = applyRefusalOutcome(null, null, 'that broker');
    out.gateLocal = applyRefusalOutcome(503,
        { ok: false, error: 'update_apply_disabled' });
    out.gateRemote = applyRefusalOutcome(503,
        { ok: false, error: 'update_apply_disabled' }, 'that broker');
    out.busyLocal = applyRefusalOutcome(409,
        { ok: false, error: 'apply_in_progress' });
    out.busyRemote = applyRefusalOutcome(409,
        { ok: false, error: 'apply_in_progress' }, 'that broker');
    // the pre-CP1 peer: 403 forbidden_origin gets its OWN sentence, and a
    // malformed body never earns it.
    out.forbidden = applyRefusalOutcome(403,
        { ok: false, error: 'forbidden_origin' }, 'that broker');
    out.forbiddenDefault = applyRefusalOutcome(403,
        { ok: false, error: 'forbidden_origin' });
    out.forbiddenNoError = applyRefusalOutcome(403, { ok: false });
    out.forbiddenJunkError = applyRefusalOutcome(403,
        { ok: false, error: 42 });
    out.forbiddenNullBody = applyRefusalOutcome(403, null);
    // The deixis rewriter itself: unquoted 'this broker' prose swaps,
    // the LITERAL consent-row label survives verbatim, and the local
    // default is a byte-identical no-op.
    out.deixisSwap = swapDeixis(RESTART_REASONS['restart-disabled'],
        'that broker');
    out.deixisLocal = swapDeixis(RESTART_REASONS['cooldown'],
        'this broker');
    out.deixisRaw = RESTART_REASONS['cooldown'];
    return out;
};

// --- atom A4: the confirm-time re-verify (commitRemoteSelfUpdate's twin) ---
CASES.remote_apply_commit_guard = async () => {
    const out = {};
    const sent = [];
    const notes = [];
    // The refusal note goes through the FLOW's own surface since the
    // A4 remediation (a settled earlier op could shadow a side map);
    // the stub records what commitRemoteApply hands it.
    globalThis.applyFlow = {
        performApply: async (id, sha) => { sent.push([id, sha]); },
        opFor: () => null,
        noteRefusal: (id, lines) => { notes.push([id, lines]); return true; },
    };
    fleet(['local', 'peer']);
    const url = 'https://peer.example:4445';
    // clean pass-through: the captured sha rides unchanged
    out.ok = { r: await commitRemoteApply('peer', url, 'peer', HEX40('b')),
               sent: sent.slice(),
               notes: notes.slice() };
    // the url moved while the confirmation was open
    HOSTS = HOSTS.map((h) => (h.id === 'peer'
        ? Object.assign({}, h, { url: 'https://ELSEWHERE.example' }) : h));
    await commitRemoteApply('peer', url, 'peer', HEX40('b'));
    out.moved = { sent: sent.slice(),
                  notes: notes.slice() };
    // relabelled
    fleet(['local', 'peer']);
    HOSTS = HOSTS.map((h) => (h.id === 'peer'
        ? Object.assign({}, h, { label: 'someone else' }) : h));
    await commitRemoteApply('peer', url, 'peer', HEX40('b'));
    out.relabelled = { sent: sent.slice() };
    // vanished outright
    fleet(['local']);
    await commitRemoteApply('peer', url, 'peer', HEX40('b'));
    out.vanished = { sent: sent.slice(),
                     notes: notes.slice() };
    delete globalThis.applyFlow;
    return out;
};

// --- atom A3: the host-parameterized apply flow ----------------------------
// makeApplyFlow is driven here with every dependency stubbed: the fetch
// records every request WITH the host it was aimed at (and hard-fails on a
// null one, like the page-level stub above), sleep resolves immediately and
// advances a fake clock a quarter of the deadline per poll, so a full
// 90s-shaped wait runs in microtasks as exactly 4 /info polls. What runs is
// the SHIPPED flow -- POST target, poll target, per-host op state, the
// mid-restart transport grace -- not a copy of it.
const applyEnv = () => {
    const env = {
        hosts: {
            local: { id: 'local', url: '' },
            x: { id: 'x', url: 'https://x.example:4445' },
            y: { id: 'y', url: 'https://y.example:4445' },
        },
        calls: [],           // 'id METHOD path', one entry per wire trip
        rejected: 0,         // how many of them the stub rejected
        rechecked: [],
        cache: new Map([['local', 'rec-local'], ['x', 'rec-x'],
                        ['y', 'rec-y']]),
        clock: 0,
        dead: false,
        resp: {},            // hostId -> array of specs; the last is sticky
    };
    env.flow = makeApplyFlow({
        localHostId: 'local',
        updHost: (id) => env.hosts[id] || null,
        hostFingerprint: (id) => (env.hosts[id]
            ? String(env.hosts[id].url || '') : null),
        hostFetch: async (host, path, opts) => {
            if (!host) throw new Error('hostFetch got a null host');
            env.calls.push(host.id + ' ' + ((opts && opts.method) || 'GET')
                + ' ' + path);
            const q = env.resp[host.id];
            const spec = Array.isArray(q)
                ? (q.length > 1 ? q.shift() : q[0]) : q;
            if (typeof spec === 'function') return spec();
            if (!spec || spec === 'reject') {
                env.rejected += 1;
                throw new TypeError('Failed to fetch');
            }
            return { status: spec.status,
                     ok: spec.status >= 200 && spec.status < 300,
                     json: async () => spec.body };
        },
        renderAll: () => {},
        modCatalogCache: env.cache,
        recheckHost: async (id) => { env.rechecked.push(id); },
        isDead: () => env.dead,
        sleep: async () => { env.clock += 250; },
        now: () => env.clock,
        waitTimeoutMs: 1000,
        pollMs: 250,
    });
    env.infoPolls = (id) => env.calls.filter(
        (c) => c === id + ' GET /info').length;
    return env;
};
const A202 = (bootId) => ({ status: 202,
    body: bootId === undefined ? { ok: true, operation_id: 'op-1' }
        : { ok: true, bootId: bootId, operation_id: 'op-1' } });
const INFO_BOOT = (bootId) => ({ status: 200,
    body: { restart: { bootId: bootId } } });

CASES.apply_explicit_target = async () => {
    const out = {};
    // (a) an apply against host x POSTs to x, polls x's /info and, on the
    // proven restart, invalidates x's catalog entry and rechecks x -- and
    // nobody else's anything.
    let env = applyEnv();
    env.resp.x = [A202('boot-1'), INFO_BOOT('boot-1'), INFO_BOOT('boot-2')];
    await env.flow.performApply('x', HEX40('b'));
    out.success = { calls: env.calls.slice(),
                    op: env.flow.opFor('x'),
                    opY: env.flow.opFor('y'),
                    opLocal: env.flow.opFor('local'),
                    cache: Array.from(env.cache.keys()).sort(),
                    rechecked: env.rechecked.slice() };

    // (b) a null/undefined/absent host id returns BEFORE any fetch -- the
    // injected fetch (which throws on a null host) is never reached.
    env = applyEnv();
    await env.flow.performApply(null, HEX40('b'));
    await env.flow.performApply(undefined, HEX40('b'));
    await env.flow.performApply('', HEX40('b'));
    out.nullHost = { calls: env.calls.slice(),
                     ops: [env.flow.opFor(null) || null,
                           env.flow.opFor(undefined) || null,
                           env.flow.opFor('') || null] };

    // (c) an id nobody holds: refused before any fetch, with the
    // no-longer-configured words, never a request to the serving origin.
    env = applyEnv();
    await env.flow.performApply('ghost', HEX40('b'));
    out.ghost = { calls: env.calls.slice(), op: env.flow.opFor('ghost') };
    return out;
};

CASES.apply_per_host_ops = async () => {
    // Host x parks mid-poll on a gated /info; host y's WHOLE apply runs to
    // completion through x's busy window; then x's gate resolves. One flow
    // instance, one Map, two independent ops.
    const env = applyEnv();
    let openGate = null;
    const gate = new Promise((res) => { openGate = res; });
    env.resp.x = [A202('boot-x1'), () => gate];
    env.resp.y = [A202('boot-y1'), INFO_BOOT('boot-y2')];
    const px = env.flow.performApply('x', HEX40('b'));
    // Everything is microtask-driven (the stub sleep never sets a timer),
    // so spinning the microtask queue walks x to its parked /info.
    let spins = 0;
    while (env.infoPolls('x') === 0 && spins++ < 10000) {
        await Promise.resolve();
    }
    const during = { xOp: (env.flow.opFor('x') || {}).phase,
                     yOp: env.flow.opFor('y') };
    await env.flow.performApply('y', HEX40('b'));
    const afterY = { xOp: (env.flow.opFor('x') || {}).phase,
                     yOp: env.flow.opFor('y'),
                     cache: Array.from(env.cache.keys()).sort(),
                     rechecked: env.rechecked.slice() };
    // x itself IS busy-guarded: a second apply for x spends nothing.
    const callsBefore = env.calls.length;
    await env.flow.performApply('x', HEX40('b'));
    const guarded = env.calls.length === callsBefore;
    openGate({ status: 200, ok: true,
               json: async () => ({ restart: { bootId: 'boot-x2' } }) });
    await px;
    return { during: during, afterY: afterY, guarded: guarded,
             xFinal: env.flow.opFor('x'), yFinal: env.flow.opFor('y'),
             calls: env.calls.slice(), rechecked: env.rechecked.slice(),
             cache: Array.from(env.cache.keys()).sort() };
};

CASES.apply_poll_transport_grace = async () => {
    const out = {};
    // (a) the connection dies mid-restart -- rejected fetches -- then the
    // broker comes back on a new boot id: polling continued through the
    // deaths and the verdict is the proven restart, never 'failed'.
    let env = applyEnv();
    env.resp.x = [A202('boot-1'), 'reject', 'reject', INFO_BOOT('boot-2')];
    await env.flow.performApply('x', HEX40('b'));
    out.recovers = { phase: env.flow.opFor('x').phase,
                     rejected: env.rejected,
                     infoPolls: env.infoPolls('x') };

    // (b) it never comes back: every poll dies, and the verdict at the
    // deadline is the honest timeout.
    env = applyEnv();
    env.resp.x = [A202('boot-1'), 'reject'];
    await env.flow.performApply('x', HEX40('b'));
    out.allDead = { phase: env.flow.opFor('x').phase,
                    note: env.flow.opFor('x').note,
                    rejected: env.rejected,
                    infoPolls: env.infoPolls('x') };

    // (c) a !ok answer mid-restart gets the same grace as a rejection.
    env = applyEnv();
    env.resp.x = [A202('boot-1'), { status: 500, body: {} }, 'reject',
                  INFO_BOOT('boot-2')];
    await env.flow.performApply('x', HEX40('b'));
    out.badStatus = { phase: env.flow.opFor('x').phase,
                      infoPolls: env.infoPolls('x') };

    // (d) the old process still answering with the OLD boot id proves
    // nothing -- polling continues to the deadline.
    env = applyEnv();
    env.resp.x = [A202('boot-1'), INFO_BOOT('boot-1')];
    await env.flow.performApply('x', HEX40('b'));
    out.sameBoot = { phase: env.flow.opFor('x').phase,
                     infoPolls: env.infoPolls('x') };

    // (e) the POST itself dying is NOT the mid-restart grace: nothing was
    // accepted, so it is reported plainly and no poll follows.
    env = applyEnv();
    env.resp.x = ['reject'];
    await env.flow.performApply('x', HEX40('b'));
    out.postDied = { phase: env.flow.opFor('x').phase,
                     note: env.flow.opFor('x').note,
                     infoPolls: env.infoPolls('x') };

    // (f) the same POST death aimed at the LOCAL broker keeps the local
    // words -- brokerName flows through applyRefusalOutcome (A4/R6).
    env = applyEnv();
    env.resp.local = ['reject'];
    await env.flow.performApply('local', HEX40('b'));
    out.postDiedLocal = { note: env.flow.opFor('local').note };
    return out;
};

CASES.apply_host_identity = async () => {
    const out = {};
    // (a) the id is re-pointed at a different machine between the POST and
    // the first poll: the wait ends indeterminate -- never a success, and
    // never a poll of the machine that inherited the id.
    let env = applyEnv();
    env.resp.x = [A202('boot-1'), INFO_BOOT('boot-2')];
    const px = env.flow.performApply('x', HEX40('b'));
    env.hosts.x = { id: 'x', url: 'https://SOMEONE-ELSE.example:4445' };
    await px;
    out.moved = { phase: env.flow.opFor('x').phase,
                  note: env.flow.opFor('x').note,
                  infoPolls: env.infoPolls('x'),
                  rechecked: env.rechecked.slice(),
                  cacheHasX: env.cache.has('x') };

    // (b) the host is removed outright mid-wait.
    env = applyEnv();
    env.resp.x = [A202('boot-1'), INFO_BOOT('boot-2')];
    const pg = env.flow.performApply('x', HEX40('b'));
    delete env.hosts.x;
    await pg;
    out.gone = { phase: env.flow.opFor('x').phase,
                 note: env.flow.opFor('x').note };

    // (c) unload mid-wait: the dead flag stops the op without claiming any
    // verdict, even though a changed boot id was there for the taking.
    env = applyEnv();
    env.resp.x = [A202('boot-1'), INFO_BOOT('boot-2')];
    const pd = env.flow.performApply('x', HEX40('b'));
    env.dead = true;
    await pd;
    out.dead = { phase: env.flow.opFor('x').phase,
                 infoPolls: env.infoPolls('x'),
                 rechecked: env.rechecked.slice(),
                 cacheHasX: env.cache.has('x') };

    // (d) a null fingerprint skips the pin -- the #183 restart flow's
    // exact old shape, shared through the same loop.
    env = applyEnv();
    env.resp.x = [INFO_BOOT('boot-2')];
    env.hosts.x = { id: 'x', url: 'https://MOVED-MEANWHILE.example:4445' };
    out.nullFp = await env.flow.pollBootId('x', 'boot-1', null, () => false);
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


def test_a_consent_grant_never_reads_as_switching_checking_off(harness):
    """A broker whose check is already config-ON gets a consent body of
    apply+restart alone. That write is a GRANT: its direction and words are
    named at the call site, so a failure strands an honest granting note,
    never a 'switching checking off…' one on a row nobody touched."""
    out = run(harness, "consent_three_gates")
    nk = out["noCheckKey"]
    assert nk["bodies"] == [
        {"apply_enabled": True, "restart_enabled": True}], (
        "the config-owned check must not be asked for")
    assert nk["op"] is not None, "the failed write leaves its note behind"
    assert nk["op"]["want"] is True, (
        "a consent write is a grant whatever keys its body carries")
    assert "switching checking off" not in (nk["op"]["note"] or "")


def test_a_mutable_gate_with_junk_enabled_grants_true_never_false(harness):
    """Ledger AS12's pin: `mutable === true && !enabled` means a gate whose
    `enabled` is missing (or junk-falsy) is grantable — and the body carries
    true for it, never anything else."""
    out = run(harness, "consent_three_gates")
    assert out["junkEnabled"] == {
        "apply_enabled": True, "restart_enabled": True}
    assert all(v is True for v in out["junkEnabled"].values())


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
    """The LOCAL Update button stays where it was -- above the per-broker
    loop, in the Restart section, reading only the LOCAL_HOST_ID facts.
    A4 adds a per-broker button INSIDE the loop, guarded to remote rows
    only and reading only THAT row's facts -- the local row must never
    double up, and the remote one must never read the serving broker."""
    src = MOD_JS.read_text(encoding="utf-8")
    seg = src[src.index("function renderApplyRow"):
              src.index("function renderRemoteApplyRow")]
    assert "checkStateFor(LOCAL_HOST_ID)" in seg
    assert "updateCapFor(LOCAL_HOST_ID)" in seg
    assert "restartInfo()" in seg
    window_seg = src[src.index("function renderWindow(win)"):
                     src.index("function renderAll()")]
    assert window_seg.count("renderApplyRow(body)") == 1
    assert window_seg.index("renderApplyRow(body)") < \
        window_seg.index("for (const r of rows)"), (
        "the local apply row must sit above the per-broker loop")
    # A6: the self-update row is the opposite shape — per broker, so it
    # rides INSIDE the loop, exactly once, right after the checking row.
    # It must not migrate up beside the local-only apply/restart controls.
    assert window_seg.count("renderSelfUpdateRow(body, r.id, r.label)") == 1
    assert window_seg.index("for (const r of rows)") < \
        window_seg.index("renderSelfUpdateRow(")
    assert window_seg.index("renderPolicyRow(body, r.id, r.label)") < \
        window_seg.index("renderSelfUpdateRow(body, r.id, r.label)")
    # A4: the remote button, inside the loop, remote rows only, last.
    assert window_seg.count("renderRemoteApplyRow(body, r)") == 1
    loop_seg = window_seg[window_seg.index("for (const r of rows)"):]
    assert "if (r.id !== LOCAL_HOST_ID) {" in loop_seg
    assert loop_seg.index("if (r.id !== LOCAL_HOST_ID) {") < \
        loop_seg.index("renderRemoteApplyRow(body, r)")
    assert loop_seg.index("renderSelfUpdateRow(body, r.id, r.label)") < \
        loop_seg.index("renderRemoteApplyRow(body, r)")
    # ...and it reads THAT broker's facts, never the serving broker's.
    rseg = src[src.index("function renderRemoteApplyRow"):
               src.index("// ---- detail window")]
    assert "updateCapFor(r.id)" in rseg
    assert "restartInfoFor(r.id)" in rseg
    assert "remoteApplyRowModel(" in rseg
    assert "LOCAL_HOST_ID" not in rseg


def test_the_how_to_note_is_wired_to_the_same_gates_as_the_buttons(harness):
    """Atom A5: the note must never re-decide 'live' with a rule looser
    than the button beside it. The local verdict is renderApplyRow's own
    `code === null` (captured by return value, the SAME code its button's
    `disabled` reads); the remote verdict is renderRemoteApplyRow's own
    `m.live` (the SAME m.live its button's `disabled` reads). update.js
    assembles one verdict per row and makes exactly one render call; the
    decision and the sentence both live in manualPullNote (companion)."""
    src = MOD_JS.read_text(encoding="utf-8")
    apply_seg = src[src.index("function renderApplyRow"):
                    src.index("function renderRemoteApplyRow")]
    assert "return code === null;" in apply_seg
    assert "btn.disabled = busy || !!code;" in apply_seg
    remote_seg = src[src.index("function renderRemoteApplyRow"):
                     src.index("// ---- detail window")]
    # Both exits carry a verdict: the early "nothing to show" return and
    # the row's own final return -- never a bare `return;` that would
    # silently read as "not behind". The final exit returns m.livePath,
    # the model's own manual-note fact: a row mid-apply ('waiting') or
    # freshly proven restarted ('done', stale-behind until its recheck
    # lands) IS using its one-click path, so the note must not flash
    # for a broker whose own row says "Applying..." or "restarted".
    # (The early exit cannot carry an op -- no op means both false.)
    assert remote_seg.count("return m.livePath;") == 1
    assert "if (!m.show) return m.live;" in remote_seg
    assert "btn.disabled = !m.live;" in remote_seg
    window_seg = src[src.index("function renderWindow(win)"):
                     src.index("function renderAll()")]
    assert "const localLive = renderApplyRow(body);" in window_seg
    loop_seg = window_seg[window_seg.index("for (const r of rows)"):]
    assert "let live = localLive;" in loop_seg
    assert "live = renderRemoteApplyRow(body, r);" in loop_seg
    # The verdict assembly itself -- the one link between the row's own
    # gate result and the note's input that only these bytes carry.
    assert "verdicts.push({ label: r.label," in loop_seg
    assert "behind: r.ps === 'behind', live: live });" in loop_seg
    assert "const verdicts = [];" in window_seg
    assert window_seg.count("manualPullNote(verdicts)") == 1
    assert "if (howTo) addNote(body, howTo.text, howTo.cls);" in window_seg
    # The old blanket sentence, and its rendering-whenever-any-row-is-
    # behind test, are both gone -- the wording and the decision moved to
    # the companion in full.
    assert "To update a broker that is behind" not in src
    apply_src = MOD_APPLY_JS.read_text(encoding="utf-8")
    assert "'To update '" in apply_src


def test_only_a_confirmed_click_can_post_and_the_previewed_sha_is_exact(
        harness):
    """The confirm dialog's values are captured once, at click time, from
    the SAME paint that disabled/enabled the button, and performApply is
    reachable from nowhere else. A3 moved the flow into update-apply.js's
    makeApplyFlow; update.js keeps exactly TWO call sites (A4 added the
    remote commit beside the local row's), and each names its target host
    explicitly -- never a null that hostFetch would silently aim at the
    serving origin."""
    src = MOD_JS.read_text(encoding="utf-8")
    assert src.count("performApply(") == 2, (
        "two call sites -- the local row and the remote confirm-commit; "
        "the definition lives in update-apply.js")
    assert "return applyFlow.performApply(" in src
    assert "LOCAL_HOST_ID, target);" in src
    assert "await applyFlow.performApply(hostId, targetSha);" in src, (
        "the remote call site must ride commitRemoteApply's named host")
    assert "if (!res || !res.value) return;" in src
    apply_src = MOD_APPLY_JS.read_text(encoding="utf-8")
    perform_seg = apply_src[apply_src.index("async function performApply"):
                            apply_src.index("return { performApply")]
    # The previewed sha is used exactly as given -- never re-derived from
    # live state inside the POST path itself.
    assert "checkStateFor(" not in perform_seg
    assert "applyTargetSha(" not in perform_seg
    assert "body: JSON.stringify({ target_sha: targetSha })" in perform_seg
    # An absent host id returns before ANY fetch can be aimed anywhere.
    assert "if (typeof hostId !== 'string' || !hostId) return;" in perform_seg
    # No timer or poll tick reaches it.
    poll_seg = src[src.index("async function poll(hostId, opts)"):
                   src.index("function pollTick(opts)")]
    assert "performApply" not in poll_seg
    assert "setInterval" not in apply_src[
        apply_src.index("function makeApplyFlow"):]


def test_apply_confirm_shows_range_count_compare_and_session_cost(harness):
    src = MOD_JS.read_text(encoding="utf-8")
    seg = src[src.index("function applyConfirmBody"):
              src.index("function renderApplyRow")]
    assert "shortSha(oldSha)" in seg and "shortSha(targetSha)" in seg
    assert "'..'" in seg
    assert "behindBy" in seg
    assert "compareUrl" in seg
    # The SAME live-session cost block the restart confirm renders (#183),
    # not a second copy of its wording that could drift from it. A4 hands
    # optional remote words THROUGH it rather than forking the body.
    assert "restartConfirmBody(cont, w && w.restart)" in seg
    # A4: an unreadable restart block renders an explicit unknown line
    # INSTEAD of the continuity counts, and the remote compare link is
    # scheme-filtered in the companion model (broker-controlled input).
    body_seg = src[src.index("function restartConfirmBody"):
                   src.index("function onRestartClick")]
    assert "if (w.unknown)" in body_seg
    apply_src = MOD_APPLY_JS.read_text(encoding="utf-8")
    mseg = apply_src[apply_src.index("function remoteApplyConfirmModel"):]
    assert r"/^https?:\/\//i" in mseg


def test_202_never_renders_success_here_and_hands_off_to_the_boot_watch(
        harness):
    """The 202 is accepted-and-stopping, not done; only a proven boot id
    change may say so, and even that hands off to a recheck rather than
    declaring victory itself (#182 Part 2, A29 owns the real verdict)."""
    apply_src = MOD_APPLY_JS.read_text(encoding="utf-8")
    perform_seg = apply_src[apply_src.index("async function performApply"):
                            apply_src.index("return { performApply")]
    assert "'done'" not in perform_seg, (
        "the 202 handler itself must never claim success")
    assert "await waitForApplyBootId(hostId, op, body.bootId," in perform_seg
    wait_seg = apply_src[
        apply_src.index("async function waitForApplyBootId"):
        apply_src.index("// The ONLY caller of POST /update/apply")]
    assert "'done'" in wait_seg
    assert "deps.recheckHost(hostId)" in wait_seg, (
        "a proven restart must still hand off to a recheck, never assume "
        "the target was reached")
    # ...and update.js's deps wiring keeps the LOCAL success path on the
    # fleet-wide recheck() it always ran, while any other host gets a
    # targeted refresh poll of itself.
    src = MOD_JS.read_text(encoding="utf-8")
    assert "? recheck() : poll(hid, { refresh: true });" in src


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


# ---- atom A3: the apply flow targets an EXPLICIT host ----------------------

def test_apply_flow_posts_and_polls_the_explicit_target(harness):
    """Every wire trip the flow makes names the host it was given: the POST
    goes to that host, the boot-id poll asks that host, and success
    invalidates THAT host's catalog entry and rechecks it -- nobody else's.
    A null/absent host id never reaches the injected fetch at all (which,
    like the real hostFetch, would otherwise silently hit the serving
    origin)."""
    r = run(harness, "apply_explicit_target")
    s = r["success"]
    assert s["calls"][0] == "x POST /update/apply"
    assert "x GET /info" in s["calls"]
    assert all(c.startswith("x ") for c in s["calls"]), s["calls"]
    assert s["op"]["phase"] == "done"
    # Nothing about the other hosts moved: no op, no cache invalidation.
    assert s["opY"] is None and s["opLocal"] is None
    assert s["cache"] == ["local", "y"]
    assert s["rechecked"] == ["x"]
    # null / undefined / '' are rejected BEFORE any fetch.
    assert r["nullHost"]["calls"] == []
    assert r["nullHost"]["ops"] == [None, None, None]
    # An id nobody holds is a named failure, still with zero fetches.
    assert r["ghost"]["calls"] == []
    assert r["ghost"]["op"]["phase"] == "failed"
    assert "no longer configured" in r["ghost"]["op"]["note"][0]


def test_apply_op_state_is_keyed_per_host(harness):
    """Host x mid-apply never marks host y busy: y's whole apply runs to
    completion through x's wait, each op lands on its own host's entry, and
    only x itself is busy-guarded against a second x apply."""
    r = run(harness, "apply_per_host_ops")
    assert r["during"]["xOp"] == "waiting"
    assert r["during"]["yOp"] is None, "x's op marked y busy"
    assert r["afterY"]["yOp"]["phase"] == "done"
    assert r["afterY"]["xOp"] == "waiting", "y's completion touched x's op"
    assert r["afterY"]["cache"] == ["local", "x"]
    assert r["afterY"]["rechecked"] == ["y"]
    assert r["guarded"] is True, "a second x apply escaped the busy-guard"
    assert r["xFinal"]["phase"] == "done"
    assert r["yFinal"]["phase"] == "done"
    assert r["rechecked"] == ["y", "x"]
    assert r["cache"] == ["local"]
    assert all(c.split(" ")[0] in ("x", "y") for c in r["calls"]), r["calls"]


def test_apply_poll_survives_transport_errors_until_the_deadline(harness):
    """E4: the broker being polled is mid-restart, so rejected fetches (and
    !ok answers) are THE expected shape of the wait -- polling continues to
    the bounded deadline, and a mid-restart connection death never becomes
    a 'failed' verdict."""
    r = run(harness, "apply_poll_transport_grace")
    rec = r["recovers"]
    assert rec["phase"] == "done", "a transport death mid-poll read as final"
    assert rec["rejected"] == 2 and rec["infoPolls"] == 3
    dead = r["allDead"]
    assert dead["phase"] == "timeout", (
        "a broker that never came back must time out honestly, not 'fail'")
    assert dead["infoPolls"] == 4, "polling stopped before the deadline"
    assert "cannot tell whether it is still starting" in dead["note"][0]
    assert r["badStatus"]["phase"] == "done"
    assert r["badStatus"]["infoPolls"] == 3
    # The old process still answering with the OLD boot id proves nothing.
    assert r["sameBoot"]["phase"] == "timeout"
    assert r["sameBoot"]["infoPolls"] == 4
    # ...but the POST itself dying is NOT the mid-restart grace: nothing
    # was accepted yet, so it is reported plainly and no poll follows.
    # A4: host 'x' is REMOTE, so the sentence names THAT broker; the same
    # death aimed at the local broker keeps the local wording.
    post = r["postDied"]
    assert post["phase"] == "failed"
    assert post["infoPolls"] == 0
    assert "could not reach that broker" in post["note"][0]
    assert "could not reach this broker" in r["postDiedLocal"]["note"][0]


def test_apply_wait_is_pinned_to_the_machine_it_asked(harness):
    """The fingerprint captured with the POST is re-checked every poll
    iteration: an id re-pointed at a different machine (or removed) ends
    the op indeterminate -- never a success, never a poll of the machine
    that inherited the id -- and the unload flag stops an op without
    claiming any verdict."""
    r = run(harness, "apply_host_identity")
    moved = r["moved"]
    assert moved["phase"] == "failed"
    assert "different machine" in moved["note"][0]
    assert moved["infoPolls"] == 0, "polled the machine that inherited the id"
    assert moved["rechecked"] == [] and moved["cacheHasX"] is True
    gone = r["gone"]
    assert gone["phase"] == "failed"
    assert "no longer configured" in gone["note"][0]
    dead = r["dead"]
    assert dead["phase"] == "waiting", "unload invented a verdict"
    assert dead["infoPolls"] == 0
    assert dead["rechecked"] == [] and dead["cacheHasX"] is True
    # A null fingerprint skips the pin: the #183 restart flow's exact old
    # shape, shared through the same loop.
    assert r["nullFp"] == "changed"


# ---- atom A4: remote broker rows get the Update button ----------------------

def test_remote_apply_row_binds_each_brokers_own_facts(harness):
    """E5: two remote hosts with DIFFERENT facts in one paint -- X live on
    its own target while Y is dead on ITS OWN policy -- plus the OR that
    keeps an op's row visible after 'behind' goes away, and the busy rule
    (only a 'waiting' op blocks the button; a settled one does not)."""
    r = run(harness, "remote_apply_row_model")
    x, y = r["x"], r["y"]
    assert x["show"] is True and x["live"] is True and x["busy"] is False
    assert x["words"] is None and x["gate"] is None
    assert x["targetSha"] == "a" * 40
    assert y["show"] is True and y["live"] is False
    assert y["targetSha"] == "b" * 40, "Y's target leaked from X's facts"
    assert "applying updates is switched off on that broker" in y["words"]
    assert '"Allow this broker to update itself"' in y["words"], (
        "the consent row's LABEL stays quoted verbatim")
    assert "row above switches it on" in y["words"], (
        "the consent row renders ABOVE this row in the loop")
    # livePath, the manual-note fact: a live button is a path, a dead
    # gate is not.
    assert x["livePath"] is True and y["livePath"] is False
    # An active op keeps its row while ps flips off 'behind'...
    ok = r["opKeepsRow"]
    assert ok["show"] is True and ok["busy"] is True and ok["live"] is False
    # ...and counts as a live path while it runs; its gate words (the
    # check state went 'unknown' mid-flip) name THAT broker, never
    # 'here'.
    assert ok["livePath"] is True
    assert "on that broker yet" in ok["words"]
    assert "here" not in ok["words"]
    # ...but a quiet current host renders nothing at all.
    assert r["hiddenWhenQuiet"]["show"] is False
    assert r["waiting"]["live"] is False and r["waiting"]["busy"] is True
    assert r["waiting"]["livePath"] is True
    assert r["settled"]["live"] is True and r["settled"]["busy"] is False
    assert r["settled"]["livePath"] is True


def test_remote_apply_row_words_come_from_that_brokers_own_gates(harness):
    """E6: a dead button's words derive from THAT broker's own facts --
    its config-owned apply gate, its own cooldown flowing through the
    restart gate, and an honest absence sentence when it never reported a
    restart block at all."""
    r = run(harness, "remote_apply_row_model")
    cfg = r["configGate"]
    assert cfg["live"] is False
    assert "applying updates is switched off on that broker" in cfg["words"]
    assert '"update_apply_enabled"' in cfg["words"]
    assert "config-file decision" in cfg["words"]
    rg = r["restartGate"]
    assert rg["live"] is False
    assert "applying needs a restart to take effect" in rg["words"]
    assert "clears by itself" in rg["words"], (
        "that broker's own cooldown words did not flow through")
    # E6's deixis rule: #183 wrote the cooldown sentence for the LOCAL
    # control ('this broker came back up...'); on a remote row it must
    # name the row's broker instead -- swapDeixis in the model.
    assert "that broker came back up" in rg["words"]
    assert "this broker" not in rg["words"]
    ru = r["restartUnknown"]
    assert ru["live"] is False
    assert "has not reported a restart capability" in ru["words"]


def test_remote_apply_row_degradations_have_distinct_honest_words(harness):
    """E7: no update view -> no row (an op still keeps one); no `policy`
    block -> the predates wording; policy present but no remote_applyable
    -> the NON-CAUSAL wording (a missing key can be a stale or partial
    record, so those words must not claim age)."""
    r = run(harness, "remote_apply_row_model")
    assert r["noView"]["show"] is False
    nvo = r["noViewWithOp"]
    assert nvo["show"] is True and nvo["live"] is False
    pre = r["predates"]
    assert pre["live"] is False
    assert "predates remote updates" in pre["words"]
    assert "update that broker" in pre["words"]
    nra = r["noRemoteApplyable"]
    assert nra["live"] is False
    assert "has not reported support" in nra["words"]
    assert "another broker" in nra["words"]
    assert "on its own machine" in nra["words"]
    assert "predates" not in nra["words"]
    assert pre["words"] != nra["words"]


def test_remote_apply_confirm_names_the_broker_and_its_own_session_cost(
        harness):
    """The dialog's decidable half: title names the broker, the compare
    link is scheme-filtered (broker-controlled input), the continuity is
    THAT broker's own block, and an unreadable restart block becomes an
    explicit session-impact-unknown line -- never zeros, never silence."""
    r = run(harness, "remote_apply_row_model")
    c = r["confirm"]
    assert c["title"] == "Apply this update to peer-label?"
    assert c["restarts"] == "peer-label"
    assert c["oldSha"] == "a" * 40 and c["behindBy"] == 3
    assert c["compareUrl"] == "https://github.com/x/y/compare/a...b"
    assert c["continuity"] == {"guaranteed": 2, "at_risk": 1, "unknown": 0}
    assert c["restart"]["unknown"] is None
    assert "what that broker reports" in c["restart"]["intro"]
    u = r["confirmUnknown"]
    assert u["compareUrl"] is None, "a non-http url must never linkify"
    assert u["restart"]["unknown"].startswith("Session impact unknown")
    assert u["continuity"] == {"guaranteed": 0, "at_risk": 0, "unknown": 0}


# --- atom A5: the how-to note is residual and names its brokers -----------

def test_the_how_to_note_renders_only_when_a_behind_row_has_no_live_path(
        harness):
    """Both directions pinned on the SAME row: behind and dead renders
    the note, the identical row live renders nothing at all."""
    r = run(harness, "manual_pull_note")
    dead = r["rendersWhenDead"]
    assert dead is not None
    assert dead["cls"] == "app-upd-howto"
    assert "peer-b" in dead["text"]
    assert r["silentWhenLive"] is None
    # Not behind at all is never this note's business, live or not.
    assert r["silentWhenNotBehind"] is None
    assert r["empty"] is None
    assert r["zeroBehind"] is None


def test_the_how_to_note_keeps_its_instructions_and_names_the_machine(
        harness):
    """R3: the substance survives (stop it, pull --ff-only, reinstall
    deps, start it again, reload the page) but the sentence now names
    which broker it is for and says the commands run on ITS OWN
    machine -- not the machine the reader is looking at this page from."""
    r = run(harness, "manual_pull_note")
    text = r["rendersWhenDead"]["text"]
    assert "peer-b" in text
    assert 'git pull --ff-only' in text
    assert "its checkout" in text
    assert "pyproject.toml" in text
    assert "reload this page" in text
    assert "own machine" in text
    assert "start it again" in text


def test_the_how_to_note_joins_several_names_the_same_way_reasons_do(
        harness):
    """Precedent for naming is the per-broker reason notes just above it
    -- an unattributed sentence belongs to nobody among N rows."""
    r = run(harness, "manual_pull_note")
    text = r["multiNamed"]["text"]
    assert "peer-a" in text and "peer-b" in text and "peer-c" in text
    assert "peer-a, peer-b and peer-c" in text


def test_a_local_broker_with_its_own_live_apply_row_is_not_counted(
        harness):
    """The R2 trap: a LOCAL broker that is behind but whose own apply row
    is live must not be named -- only a dead local counts."""
    r = run(harness, "manual_pull_note")
    assert r["localLive"] is None
    dead = r["localDead"]
    assert dead is not None
    assert "this broker" in dead["text"]


def test_a_mixed_fleet_names_only_the_row_with_no_live_path(harness):
    """One behind+live (local), one behind+dead (a peer), one not behind
    at all -- the note names the dead peer and nobody else."""
    r = run(harness, "manual_pull_note")
    text = r["mixed"]["text"]
    assert "peer-dead" in text
    assert "this broker" not in text
    assert "peer-current" not in text


def test_the_how_to_note_never_throws_on_a_junk_verdict_list(harness):
    """`verdicts` is update.js's own assembly, not wire input, but this
    stays defensive like every other companion helper here."""
    r = run(harness, "manual_pull_note")
    junk = r["junkRows"]
    assert junk is not None
    assert "an unnamed broker" in junk["text"]


def test_apply_refusals_name_the_broker_they_describe(harness):
    """E10: applyRefusalOutcome's optional broker words -- the default
    keeps every local sentence byte-identical, and the remote flow's own
    brokerName rides through makeApplyFlow (the transport case is proven
    against the live flow in the transport-grace test above)."""
    r = run(harness, "remote_apply_refusal_words")
    assert r["transportLocal"]["lines"][0] == (
        "could not reach this broker to ask for the update.")
    assert r["transportRemote"]["lines"][0] == (
        "could not reach that broker to ask for the update.")
    # The local sentences are pinned by FULL equality: byte-identical
    # to the pre-A4 wording is the claim, so a substring cannot carry it.
    assert r["gateLocal"]["lines"][0] == (
        "applying updates is switched off on this broker — the \"Allow "
        "this broker to update itself\" row switches it on when it is "
        "writable; a config key naming it overrides the row.")
    assert r["busyLocal"]["lines"][0] == (
        "this broker already has an update or restart under way -- try "
        "again once it finishes.")
    assert "switched off on that broker" in r["gateRemote"]["lines"][0]
    assert '"Allow this broker to update itself"' in \
        r["gateRemote"]["lines"][0], "the row LABEL stays verbatim"
    assert r["busyRemote"]["lines"][0].startswith("that broker already has")
    # The deixis rewriter: prose swaps, the quoted LABEL survives, and
    # the local default is a no-op (byte-identical output).
    assert '"Allow this broker to update itself"' in r["deixisSwap"]
    assert "switched off on that broker" in r["deixisSwap"]
    assert r["deixisLocal"] == r["deixisRaw"]


def test_a_403_forbidden_origin_names_the_real_cause(harness):
    """E11: a peer whose build still origin-gates POST /update/apply
    answers 403 forbidden_origin; that earns its own sentence -- update it
    by hand once -- with precedence over the generic unknown-shape
    fallthrough, which a malformed body still lands on."""
    r = run(harness, "remote_apply_refusal_words")
    f = r["forbidden"]
    assert f["kind"] == "forbidden-origin"
    assert "still refuses applies driven from another broker" in f["lines"][0]
    assert "update it by hand this once" in f["lines"][0]
    assert "newer build will accept them" in f["lines"][0]
    assert "not in a shape this page" not in f["lines"][0]
    assert r["forbiddenDefault"]["kind"] == "forbidden-origin"
    # tolerance: an absent or non-string error key never earns the
    # sentence -- it lands on the honest never-read-as-success unknown.
    for k in ("forbiddenNoError", "forbiddenJunkError", "forbiddenNullBody"):
        assert r[k]["kind"] == "unknown", k
        assert "must not be read as a" in r[k]["lines"][0], k


def test_a_remote_apply_commit_re_verifies_the_machine_it_named(harness):
    """R5: the dialog captured url+label; a host that moved, was
    relabelled or vanished while it was open gets NOTHING sent and an
    honest note, and a clean confirm sends exactly the paint's sha."""
    r = run(harness, "remote_apply_commit_guard")
    assert r["ok"]["r"] is True
    assert r["ok"]["sent"] == [["peer", "b" * 40]]
    assert r["ok"]["notes"] == []
    assert r["moved"]["sent"] == [["peer", "b" * 40]], (
        "a moved url still reached performApply")
    assert r["moved"]["notes"][-1][0] == "peer"
    assert "nothing was sent" in r["moved"]["notes"][-1][1][0]
    assert r["relabelled"]["sent"] == [["peer", "b" * 40]]
    assert r["vanished"]["sent"] == [["peer", "b" * 40]]
    assert "nothing was sent" in r["vanished"]["notes"][-1][1][0]


# ---- #188 item 1 (atom A3): restart facts refresh on a gate-moving write --


def test_a_write_that_moves_the_restart_gate_refreshes_that_hosts_facts(
        harness):
    """The Restart button renders from /info's restart block, which a
    policy-write response does NOT carry -- so a write that moved the
    restart gate spends exactly one targeted /info re-read on the host
    it wrote to, and only then. Direction must be NAMED in the body
    (absence is never a direction), a write that landed on the value
    already shown spends nothing, and a refusal spends nothing."""
    r = run(harness, "restart_gate_moved")
    # The pure predicate (update-policy.js).
    assert r["movedOn"] is True
    assert r["movedOff"] is True
    assert r["namedButStill"] is False, (
        "a write that left the gate where it stood owes no re-read")
    assert r["unnamed"] is False, (
        "a body that never named restart_enabled did not ask to move it")
    assert r["emptyChanges"] is False
    assert r["nullChanges"] is False
    assert r["stringDirection"] is False, (
        "a non-boolean direction is not a named direction")
    # AFTER missing: fall back to the direction the write named.
    assert r["noAfterMoved"] is True
    assert r["noAfterStill"] is False
    # BEFORE unreadable: fail toward freshness.
    assert r["noBefore"] is True
    assert r["junkBefore"] is True
    # The wiring, over the shipped setPolicy: one GET, that host only.
    assert r["grantOk"] is True
    assert r["grantRereads"] == ["local"]
    assert r["grantAfterOn"] is True, (
        "the row must repaint from the post-write facts, not the "
        "pre-write cache")
    assert r["applyOnlyOk"] is True
    assert r["applyOnlyRereads"] == []
    assert r["lockedOk"] is False
    assert r["lockedRereads"] == []


def test_the_restart_refresh_wiring_is_pinned():
    """setPolicy captures the pre-write view BEFORE the response can
    land in lastWrite, asks the pure predicate, and spends the re-read
    through the shared fetcher (a GET, never a POST) on a re-resolved
    host."""
    src = MOD_JS.read_text(encoding="utf-8")
    seg = src[src.index("async function setPolicy"):
              src.index("async function setChecking")]
    assert "const updBefore = updateCapFor(hid);" in seg
    assert "restartGateMoved(changes, updBefore," in seg
    assert "body && body.update)" in seg
    assert "const fresh = updHost(hid);" in seg
    assert "await capabilityFor(fresh, true);" in seg
    policy = MOD_POLICY_JS.read_text(encoding="utf-8")
    assert "function restartGateMoved(changes, beforeUpd, afterUpd)" in policy
