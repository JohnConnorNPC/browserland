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
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="node not installed")

# (start, end) anchors for each declaration-only range, in file order. Every
# chunk begins on a declaration and ends immediately before the next thing that
# is not one, so the concatenation runs at module scope with no side effects.
_CHUNKS = (
    # REASONS + the per-host record map (hostChecks/checkStateFor) + updHost
    ("const REASONS = {", "let timer = null;"),
    # state/reasonCode/bandFor/peerState/answered/stateWords/hostRows/aggregate
    ("function state(st) {", "// ONE chip for the whole fleet."),
    ("function chipTitle(st) {", "// ---- can this broker even be asked? (#182) ----"),
    # the capability probe
    ("function servesUpdateMod(mods) {", "// ---- the poll ----"),
    ("async function poll(hostId, opts) {", "// The driver. EVERY configured host"),
    ("function pollTick(opts) {", "function start() {"),
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
)


def _model_source() -> str:
    src = MOD_JS.read_text(encoding="utf-8")
    out = []
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
const INFO_MODERN = (on) => ({
    state: 'ok', mods: [{ id: 'update' }], modsEnabled: true, policy: {},
    update: { check_enabled: on !== false, apply_enabled: false },
});

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
globalThis.hostFetch = async (host, path, opts) => {
    // A null host would silently hit the SERVING origin — the exact lie the
    // mod is written to prevent, so it is a hard failure here.
    if (!host) throw new Error('hostFetch got a null host');
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
function reset() { hostChecks.clear(); modCatalogCache.clear(); HOSTS = []; }
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
