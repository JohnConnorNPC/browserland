"""Behavioural tests for #253's page half: a broker whose ``/info`` says
``terminals: false`` is never launched on by the desktop.

The cells execute the SHIPPED code in node, cut from the fragments at named
boundaries and evaluated inside a ``with (env)`` scope whose outer names
(``localInfo``, ``hostFetch``, ``openInfoModal``, the caches) are stubs:

* P1-P4 -- 56 ``hostTerminalsOff``: the paint-only predicate. The local answer
  comes from the ``localInfo()`` memo (unknown on the first sync read), a peer's
  from the Control Panel record; only a literal ``false`` counts, and a call
  before 81 declared its cache reads "unknown" instead of throwing.
* D1-D5 -- 56 ``defaultLaunchHost``: never picks a host the user did not
  choose. A chosen default or local fallback that hosts no terminals is null.
* L1-L6 -- 76 click time: quick-launch and ``launchProfile`` decide on the
  AWAITED local ``/info``, a null host is the explained modal, and a broker's
  own 403 ``terminals_disabled`` maps to the same modal.
* I1, M1-M4 -- 76 launch menu: a terminals-off host gets one disabled reason
  row and no ``/profiles`` fetch; a host whose answer is not known yet is asked
  once and the open menu repaints when the answer lands.
* E1, E2 -- 81 profiles editor: a terminals-off target shows the note.
* C1, C2 -- 81 ``fetchModCatalog`` records ``terminalsOff`` from a peer's
  ``/info``; ``primeModCatalog`` hands back that read so the menu can repaint.
* B1 -- 83 Settings -> Hosts badge survives a null default.

One PASS/FAIL line per assert plus a machine-readable tail; the fixture FAILS
if a boundary vanishes. Skipped when node is absent.
"""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from webterm.broker import ui

BROKER_DIR = Path(ui.__file__).resolve().parent
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="node not installed")

# name -> (fragment, start marker (unique), end marker (first one after start))
SLICES = {
    "pred": ("56_js_hosts.js",
             "        // #253: does this host's broker host NO terminals",
             "        // Optional per-host DEFAULT accent (#103)"),
    "launch": ("76_js_launch_fullscreen.js",
               "        // #253: the reason a terminals-off broker gives",
               "        // /profiles is static per broker run"),
    "items": ("76_js_launch_fullscreen.js",
              "        const LAUNCH_ROW_TERMINALS_OFF",
              "        function buildLaunchMenuItems(hosts) {"),
    "menu": ("76_js_launch_fullscreen.js",
             "        // #253: a host whose terminals answer is not known yet",
             "        if (launchBtn) {"),
    "quick": ("76_js_launch_fullscreen.js",
              "            const quickLaunch = async () => {",
              "            launchBtn.addEventListener('click'"),
    "editor": ("81_js_control_panel.js",
               "        const PROFILES_TERMINALS_OFF_NOTE",
               "        function deleteProfile(host, name) {"),
    "catalog": ("81_js_control_panel.js",
                "        async function fetchModCatalog(host) {",
                "        // One /info read of `host` into modCatalogCache"),
    "prime": ("81_js_control_panel.js",
              "        // Returns the read's promise (undefined when one is already in",
              "        // The well-shaped {modId: bool} subset"),
    "badge": ("83_js_broker_identity.js",
              "            const curHost = defaultLaunchHost();",
              "            const storedDefault = getSettings().defaultHost;"),
}

EXPECTED_ASSERTS = 36

_HARNESS = r"""
'use strict';
const S = JSON.parse(require('fs').readFileSync(__SLICES_PATH__, 'utf8'));

let pass = 0, fail = 0;
function ok(cond, label) {
    if (cond) { pass++; console.log('PASS: ' + label); }
    else { fail++; console.log('FAIL: ' + label); }
}
const tick = () => new Promise(r => setTimeout(r, 0));

// Evaluate `code` (sloppy, so `with` works) with env's names in scope and
// return the named functions it declares.
function load(code, env, names) {
    const ret = '{' + names.map(n => n + ': typeof ' + n + " === 'function' ? " + n
        + ' : undefined').join(', ') + '}';
    return (new Function('env', 'with (env) { return (function () {\n' + code
        + '\nreturn ' + ret + ';\n})(); }'))(env);
}

// One world: hosts, the local /info memo, peer records, and recorders.
function mkWorld(o) {
    o = o || {};
    const w = { fetches: [], modals: [], notices: [], profileFetches: [] };
    const local = { id: 'local', label: 'this broker', hidden: false };
    w.hosts = [local].concat(o.peers || []);
    const li = function () { return li._p; };
    li._p = ('info' in o) ? Promise.resolve(o.info) : null;
    w.records = new Map(Object.entries(o.records || {}));
    w.settings = { defaultHost: o.defaultHost || '' };
    w.launchStatus = o.launchStatus || 200;
    w.launchBody = o.launchBody || { ok: true, id: 7 };
    const env = {
        localInfo: li,
        getHosts: () => w.hosts,
        allHosts: () => w.hosts,
        localHost: () => w.hosts[0],
        hostById: (id) => w.hosts.find(h => h.id === id) || null,
        getSettings: () => w.settings,
        hostMenuState: (st) => st || 'ok',
        pollStateFor: (id) => (o.states || {})[id] || 'ok',
        openInfoModal: (m) => { w.modals.push(m); },
        showNotice: (t) => { w.notices.push(t); },
        showAuthOverlay: () => {},
        resolveStartPath: async () => '',
        hostDefaultProfile: () => null,
        launchBtn: null,
        hostFetch: async (h, path, init) => {
            w.fetches.push(h.id + ' ' + path);
            const body = w.launchBody;
            return { status: w.launchStatus, ok: w.launchStatus < 300,
                     json: async () => body };
        },
        pendingOpens: new Map(),
        AUTO_OPEN_TIMEOUT_MS: 1000,
        startFastPoll: () => {},
        refreshTaskbar: () => {},
        setTimeout: setTimeout,
        Date: Date,
        String: String,
        Promise: Promise,
        Map: Map,
        Array: Array,
        JSON: JSON,
    };
    if (o.tdz) {
        Object.defineProperty(env, 'modCatalogCache', {
            get() { throw new ReferenceError('modCatalogCache before init'); },
        });
    } else {
        env.modCatalogCache = w.records;
    }
    w.env = env;
    const code = S.pred + '\n' + S.launch + '\n' + S.quick;
    Object.assign(w, load(code, env, ['hostTerminalsOff', 'defaultLaunchHost',
        'launchProfile', 'showNoLaunchHost', 'hostRefusesLaunch', 'quickLaunch']));
    return w;
}
const peer = (id, extra) => Object.assign({ id, label: 'peer-' + id, hidden: false }, extra || {});
const launched = (w) => w.fetches.filter(f => f.endsWith(' /launch'));

(async function () {
    // ---- P: the paint predicate ------------------------------------------
    {
        const w = mkWorld({ info: { terminals: false } });
        const first = w.hostTerminalsOff('local');
        await tick();
        ok(first === null && w.hostTerminalsOff('local') === true,
           'P1 local terminals:false reads unknown on the first sync read, then true');
    }
    {
        const on = mkWorld({ info: { terminals: true } });
        on.hostTerminalsOff('local'); await tick();
        const old = mkWorld({ info: {} });
        old.hostTerminalsOff('local'); await tick();
        const none = mkWorld({});
        ok(on.hostTerminalsOff('local') === false && old.hostTerminalsOff('local') === false
           && none.hostTerminalsOff('local') === null,
           'P2 local terminals:true and a key-less (older) /info read false; no memo reads null');
    }
    {
        const w = mkWorld({ peers: [peer('a'), peer('b'), peer('c')],
                            records: { a: { terminalsOff: true }, b: { terminalsOff: false } } });
        ok(w.hostTerminalsOff('a') === true && w.hostTerminalsOff('b') === false
           && w.hostTerminalsOff('c') === null,
           'P3 a peer reads its Control Panel record; no record is unknown');
    }
    {
        let threw = false, got;
        try { const w = mkWorld({ tdz: true, peers: [peer('a')] }); got = w.hostTerminalsOff('a'); }
        catch (e) { threw = true; }
        ok(!threw && got === null, 'P4 a peer read before 81 declared its cache is unknown, not a throw');
    }

    // ---- D: defaultLaunchHost ----------------------------------------------
    {
        const w = mkWorld({ info: { terminals: false }, peers: [peer('a')],
                            records: { a: { terminalsOff: false } } });
        w.hostTerminalsOff('local'); await tick();
        ok(w.defaultLaunchHost() === null,
           'D1 no chosen default + local off + a visible launching peer -> null (no peer picked)');
    }
    {
        const w = mkWorld({ info: { terminals: true }, peers: [peer('a')] });
        w.hostTerminalsOff('local'); await tick();
        ok(w.defaultLaunchHost() === w.hosts[0], 'D2 no chosen default + local on -> local (unchanged)');
    }
    {
        const on = mkWorld({ peers: [peer('a')], records: { a: { terminalsOff: false } },
                             defaultHost: 'a' });
        const off = mkWorld({ peers: [peer('a')], records: { a: { terminalsOff: true } },
                              defaultHost: 'a' });
        const down = mkWorld({ peers: [peer('a')], defaultHost: 'a', states: { a: 'down' } });
        ok(on.defaultLaunchHost() === on.hosts[1] && off.defaultLaunchHost() === null
           && down.defaultLaunchHost() === down.hosts[1],
           'D3 a chosen default is returned, null only when it hosts no terminals (down stays with launchProfile)');
    }
    {
        const w = mkWorld({ info: { terminals: false }, defaultHost: 'local' });
        w.hostTerminalsOff('local'); await tick();
        const gone = mkWorld({ info: { terminals: true }, defaultHost: 'removed-id' });
        gone.hostTerminalsOff('local'); await tick();
        ok(w.defaultLaunchHost() === null && gone.defaultLaunchHost() === gone.hosts[0],
           'D4 chosen local + local off -> null; a removed id falls back to a launching local');
    }
    {
        const w = mkWorld({ info: { terminals: false } });     // memo never read: sync = unknown
        ok(w.defaultLaunchHost(true) === null && w.defaultLaunchHost(false) === w.hosts[0],
           'D5 a click-time localOff overrides the unknown paint answer');
    }

    // ---- L: click time -----------------------------------------------------
    {
        // The FIRST click on a fresh page: the memo exists but the sync
        // predicate has never been read, so it would say unknown -> launch.
        const w = mkWorld({ info: { terminals: false } });
        await w.quickLaunch();
        await tick();
        ok(launched(w).length === 0 && w.modals.length === 1
           && w.modals[0].title === 'No host can launch here',
           'L1 the first-ever quick-launch with local off posts nothing and explains');
    }
    {
        const w = mkWorld({ info: { terminals: false } });
        await w.launchProfile(w.hosts[0], null);
        ok(launched(w).length === 0 && w.modals.length === 1
           && w.modals[0].title === 'Cannot launch'
           && /hosts no terminals \(terminals: false\)/.test(w.modals[0].rows[0].v),
           'L2 launchProfile on a terminals-off local refuses at click time, no /launch');
    }
    {
        const w = mkWorld({ info: { terminals: true } });
        let threw = false;
        try { await w.launchProfile(null, null); } catch (e) { threw = true; }
        ok(!threw && launched(w).length === 0 && w.modals.length === 1
           && w.modals[0].title === 'No host can launch here',
           'L3 launchProfile(null) is the explained modal, never a silent local launch');
    }
    {
        // Booted before login: /info answered 401, the memo is {}.
        const w = mkWorld({ info: {}, launchStatus: 403,
                            launchBody: { ok: false, error: 'terminals_disabled' } });
        await w.launchProfile(w.hosts[0], null);
        ok(launched(w).length === 1 && w.modals.length === 1
           && w.modals[0].title === 'Cannot launch' && w.notices.length === 0,
           'L4 a 403 terminals_disabled from /launch is the same modal, not "launch failed"');
    }
    {
        const w = mkWorld({ info: { terminals: true } });
        await w.quickLaunch();
        await tick();
        ok(launched(w).length === 1 && w.fetches[0] === 'local /launch' && w.modals.length === 0,
           'L5 default config: quick-launch posts /launch to local exactly as before');
    }
    {
        const unset = mkWorld({ info: { terminals: false } });
        unset.showNoLaunchHost();
        const chosen = mkWorld({ peers: [peer('a')], records: { a: { terminalsOff: true } },
                                 defaultHost: 'a' });
        chosen.showNoLaunchHost();
        ok(/This broker is a Hub with terminals off\. Pick a host in the launch menu, or set a default in Settings → Hosts\./
               .test(unset.modals[0].rows[0].v)
           && /^peer-a \(the default host\) hosts no terminals\./.test(chosen.modals[0].rows[0].v),
           'L6 the null modal names the reason: this Hub, or the chosen default');
    }
    {
        const w = mkWorld({ info: { terminals: true }, peers: [peer('a')],
                            records: { a: { terminalsOff: true } } });
        await w.launchProfile(w.hosts[1], null);
        ok(launched(w).length === 0 && w.modals.length === 1 && w.modals[0].rows[0].k === 'peer-a',
           'L7 a peer whose record says terminals off is refused at click time too');
    }

    // ---- I / M: launch menu --------------------------------------------------
    function mkMenu(o) {
        const w = mkWorld(o);
        const cached = new Map(Object.entries(o.profiles || {}));
        Object.assign(w.env, {
            hostMenuItems: (h) => [{ label: h.label, enabled: true }],
            profilesCache: cached,
            profilesFailedRecently: () => false,
            profileMenuItems: (h, d) => d.profiles.map(n => ({ label: n })),
            launchMenuGen: 0, launchMenuNode: null, launchMenuPaint: null,
            launchMenuStillOurs: () => true,
            // Content-based like 76's, so paint()'s unchanged-signature early
            // return runs here too and a repaint that changes nothing stops.
            launchMenuSig: (items) => JSON.stringify(items.map(i => [i.label, i.enabled])),
            renderMenu: (items) => {
                w.renders = (w.renders || []).concat([items.map(i => i.label)]);
            },
            ctxMenu: { firstChild: null },
            // Like 76's: count it, then re-run the open menu's paint.
            repaintLaunchMenu: () => {
                w.repaints = (w.repaints || 0) + 1;
                if (w.env.launchMenuPaint) w.env.launchMenuPaint();
            },
            // Like 76's: the answer lands in profilesCache, so a repaint
            // does not dial again.
            fetchProfiles: (h) => {
                w.profileFetches.push(h.id);
                cached.set(h.id, { profiles: ['p-' + h.id] });
                return Promise.resolve();
            },
            appMenuItems: () => [],
            // The Control Panel's /info read of a peer: records what o.peerInfo
            // says it answered, like 81's fetchModCatalog does.
            primeModCatalog: (h) => {
                w.primes = (w.primes || []).concat([h.id]);
                return Promise.resolve().then(() => {
                    const off = (o.peerInfo || {})[h.id];
                    w.records.set(h.id, { terminalsOff: off === true });
                });
            },
        });
        Object.assign(w, load(S.items + '\nfunction buildLaunchMenuItems(hosts) { const items = []; '
            + 'hosts.forEach(h => items.push(...launchHostItems(h))); return items; }\n' + S.menu,
            w.env, ['launchHostItems', 'openLaunchMenu']));
        return w;
    }
    {
        const off = mkMenu({ info: { terminals: false }, profiles: { local: { profiles: ['cmd'] } } });
        off.hostTerminalsOff('local'); await tick();
        // launchHostItems reads the env predicate; the loaded one shares the memo.
        off.env.hostTerminalsOff = off.hostTerminalsOff;
        const rows = off.launchHostItems(off.hosts[0]);
        const on = mkMenu({ info: { terminals: true }, profiles: { local: { profiles: ['cmd'] } } });
        on.hostTerminalsOff('local'); await tick();
        on.env.hostTerminalsOff = on.hostTerminalsOff;
        const onRows = on.launchHostItems(on.hosts[0]);
        ok(rows.length === 2 && rows[1].enabled === false
           && rows[1].label === 'terminals off — launches nothing here'
           && onRows.length === 2 && onRows[1].label === 'cmd',
           'I1 a terminals-off host gets one disabled reason row instead of its profiles');
    }
    {
        const w = mkMenu({ info: { terminals: false }, peers: [peer('a')] });
        w.hostTerminalsOff('local'); await tick();
        w.env.hostTerminalsOff = w.hostTerminalsOff;
        w.openLaunchMenu(0, 0);
        ok(w.profileFetches.length === 1 && w.profileFetches[0] === 'a',
           'M1 the open menu fetches /profiles for a launching peer, never for a terminals-off host');
    }
    {
        // A terminals-off peer the Control Panel never probed: no record yet.
        // Its profiles are cached, so the /info read is the only thing that
        // can repaint the open menu.
        const w = mkMenu({ info: { terminals: true }, peers: [peer('a')],
                           peerInfo: { a: true },
                           profiles: { local: { profiles: ['cmd'] },
                                       a: { profiles: ['sh'] } } });
        w.hostTerminalsOff('local'); await tick();
        w.env.hostTerminalsOff = w.hostTerminalsOff;
        w.openLaunchMenu(0, 0);
        const first = w.renders[0];
        await tick();
        const last = w.renders[w.renders.length - 1];
        ok(JSON.stringify(w.primes) === '["a"]' && w.repaints >= 1
           && first.indexOf('sh') !== -1
           && last.indexOf('sh') === -1 && last.indexOf('terminals off — launches nothing here') !== -1,
           'M2 the open menu asks a cold peer for its /info once and repaints into the reason row');
    }
    {
        // The menu opens before the serving broker's boot /info has settled.
        const w = mkMenu({ info: { terminals: false },
                           profiles: { local: { profiles: ['cmd'] } } });
        w.env.hostTerminalsOff = w.hostTerminalsOff;
        w.openLaunchMenu(0, 0);
        const first = w.renders[0];
        await tick();
        const last = w.renders[w.renders.length - 1];
        ok(first.indexOf('cmd') !== -1 && w.repaints >= 1
           && last.indexOf('terminals off — launches nothing here') !== -1,
           'M3 a menu opened before the local /info settled repaints when it lands');
    }
    {
        // Opened before anything asked for the local /info at all: the
        // memo does not exist yet, so the menu's own ask creates it.
        const w = mkMenu({ profiles: { local: { profiles: ['cmd'] } } });
        w.env.hostTerminalsOff = w.hostTerminalsOff;
        w.env.localInfo = function lazy() {
            if (!lazy._p) lazy._p = Promise.resolve({ terminals: false });
            return lazy._p;
        };
        // The predicate reads localInfo._p through env too.
        const f = load(S.pred, w.env, ['hostTerminalsOff']);
        w.env.hostTerminalsOff = f.hostTerminalsOff;
        w.openLaunchMenu(0, 0);
        await tick();
        const last = w.renders[w.renders.length - 1];
        ok(last.indexOf('terminals off — launches nothing here') !== -1,
           'M5 a menu opened before the local /info memo existed still repaints into the reason row');
    }
    {
        // Both hosts' profiles are cached, so any repaint would be ours.
        const w = mkMenu({ info: { terminals: true }, peers: [peer('a')],
                           records: { a: { terminalsOff: false } },
                           profiles: { local: { profiles: ['cmd'] },
                                       a: { profiles: ['sh'] } } });
        w.hostTerminalsOff('local'); await tick();
        w.env.hostTerminalsOff = w.hostTerminalsOff;
        w.openLaunchMenu(0, 0);
        await tick();
        ok(!w.primes && !w.repaints,
           'M4 a host whose answer is known is never re-asked and nothing repaints for it');
    }

    // ---- E: profiles editor ------------------------------------------------
    function mkEditor(off) {
        const list = { innerHTML: '', textContent: '', children: [],
                       appendChild(c) { this.children.push(c); } };
        const add = { disabled: false }, detect = { disabled: false };
        const fetched = [];
        const env = {
            flushProfilePickers: () => {},
            settingsTarget: { hostId: 'local' },
            setProfilesListEl: list, setProfileAddBtn: add, setProfileDetectBtn: detect,
            hostById: (id) => ({ id, label: 'this broker' }),
            hostTerminalsOff: () => off,
            profilesConfigCache: new Map(),
            profilesConfigFetching: new Set(),
            profilesConfigFailed: new Set(),
            fetchProfilesConfig: (h) => { fetched.push(h.id); return new Promise(() => {}); },
            currentSettingsTab: 'local',
            Set: Set, Map: Map, Object: Object, document: {},
        };
        const f = load(S.editor, env, ['renderProfilesEditor']);
        f.renderProfilesEditor();
        return { list, add, detect, fetched };
    }
    {
        const e = mkEditor(true);
        ok(/^This broker hosts no terminals \(terminals: false\)/.test(e.list.textContent)
           && e.add.disabled && e.detect.disabled && e.fetched.length === 0,
           'E1 a terminals-off target shows the note, disables Add/Detect and fetches nothing');
    }
    {
        const e = mkEditor(false);
        ok(e.list.textContent === 'loading…' && e.fetched.length === 1,
           'E2 a launching target loads its profiles as before');
    }

    // ---- C: the Control Panel record ---------------------------------------
    async function catalogRec(info) {
        const cache = new Map();
        const env = {
            hostFetch: async () => ({ status: 200, ok: true, json: async () => info }),
            modCatalogCache: cache,
            sanitizeModPolicy: () => ({}),
            Array: Array,
        };
        const f = load(S.catalog, env, ['fetchModCatalog']);
        await f.fetchModCatalog({ id: 'a' });
        return cache.get('a');
    }
    {
        const off = await catalogRec({ ok: true, mods: [], terminals: false });
        const on = await catalogRec({ ok: true, mods: [], terminals: true });
        const old = await catalogRec({ ok: true, mods: [] });
        const noMods = await catalogRec({ ok: true, terminals: false });
        ok(off.terminalsOff === true && on.terminalsOff === false && old.terminalsOff === false
           && noMods.terminalsOff === true,
           'C1 fetchModCatalog records terminalsOff from a literal false only, with or without mods');
    }

    {
        const fetching = new Set();
        let release;
        const env = {
            modCatalogFetching: fetching,
            fetchModCatalog: () => new Promise(r => { release = r; }),
            currentSettingsTab: null,
            renderModPolicy: () => {},
        };
        const f = load(S.prime, env, ['primeModCatalog']);
        const p1 = f.primeModCatalog({ id: 'a' });
        const p2 = f.primeModCatalog({ id: 'a' });
        let landed = false;
        if (p1 && p1.then) p1.then(() => { landed = true; });
        release();
        await tick();
        ok(!!(p1 && typeof p1.then === 'function') && p2 === undefined && landed
           && !fetching.has('a'),
           'C2 primeModCatalog hands back its read (undefined while one is in flight)');
    }

    // ---- B: the Settings -> Hosts badge ------------------------------------
    {
        let threw = false, got;
        try {
            const f = load('function probe() {\n' + S.badge + '\nreturn curId;\n}',
                           { defaultLaunchHost: () => null }, ['probe']);
            got = f.probe();
        } catch (e) { threw = true; }
        const g = load('function probe() {\n' + S.badge + '\nreturn curId;\n}',
                       { defaultLaunchHost: () => ({ id: 'a' }) }, ['probe']);
        ok(!threw && got === null && g.probe() === 'a',
           'B1 a null START default leaves no badge instead of throwing');
    }

    // ---- extra coverage for the defaults ---------------------------------
    {
        const w = mkWorld({ info: { terminals: true }, peers: [peer('a')], defaultHost: 'a',
                            states: { a: 'down' } });
        await w.quickLaunch();
        await tick();
        ok(launched(w).length === 0 && w.modals.length === 1
           && w.modals[0].rows[0].v === 'unreachable (broker down)',
           'L8 a down chosen default still gets launchProfile\'s own reason (unchanged)');
    }
    {
        const w = mkWorld({ info: { terminals: true } });
        ok((await w.hostRefusesLaunch(w.hosts[0])) === false,
           'L9 hostRefusesLaunch: a launching local is not refused');
    }
    {
        const w = mkWorld({});
        w.env.localInfo = function () { return Promise.reject(new Error('x')); };
        ok((await w.hostRefusesLaunch(w.hosts[0])) === false,
           'L10 hostRefusesLaunch: a failed /info read launches (older behaviour), never throws');
    }
    {
        const w = mkWorld({ info: { terminals: false } });
        await w.launchProfile(w.hosts[0], null);
        ok(w.notices.length === 0 && w.fetches.length === 0,
           'L11 the click-time refusal dials nothing at all');
    }
    {
        const w = mkWorld({ info: { terminals: true }, launchStatus: 403,
                            launchBody: { ok: false, error: 'launch_disabled' } });
        await w.launchProfile(w.hosts[0], null);
        ok(w.modals.length === 0 && w.notices.length === 1
           && w.notices[0] === 'launch failed: launch_disabled',
           'L12 any other refusal keeps the existing "launch failed" notice');
    }
    {
        const w = mkWorld({ info: { terminals: false }, peers: [peer('a')],
                            records: { a: { terminalsOff: false } } });
        await w.launchProfile(w.hosts[1], null);
        ok(launched(w).length === 1 && w.fetches[0] === 'a /launch',
           'L13 a launching peer still launches when only local hosts no terminals');
    }
    {
        const w = mkWorld({ info: { terminals: false }, peers: [peer('a')],
                            records: { a: { terminalsOff: false } }, defaultHost: 'a' });
        await w.quickLaunch();
        await tick();
        ok(launched(w).length === 1 && w.fetches[0] === 'a /launch',
           'L14 quick-launch honours a launching chosen default when local is off');
    }
    {
        const w = mkWorld({ info: { terminals: false } });
        w.hostTerminalsOff('local'); await tick();
        ok(w.hostTerminalsOff('local') === true && w.defaultLaunchHost() === null,
           'D6 after the memo settles the paint answer agrees with the click-time one');
    }
    {
        const w = mkWorld({ info: { terminals: false } });
        w.hostTerminalsOff('local'); await tick();
        w.env.localInfo._p = Promise.resolve({ terminals: true });   // a refetch
        const before = w.hostTerminalsOff('local');
        await tick();
        ok(before === null && w.hostTerminalsOff('local') === false,
           'P5 a localInfo(true) refetch is read again (keyed on the memo promise)');
    }

    console.log(pass + ' passed, ' + fail + ' failed');
})().catch(e => { console.log('HARNESS CRASH: ' + (e && e.stack || e)); });
"""


def _cut(name):
    frag, start, end = SLICES[name]
    src = (BROKER_DIR / frag).read_text(encoding="utf-8")
    # FAIL, never skip: a moved boundary would grade a stale cut.
    assert src.count(start) == 1, (
        f"{start!r} occurs {src.count(start)}x in {frag}; retarget this test")
    a = src.index(start)
    b = src.find(end, a + len(start))
    assert b != -1, f"{end!r} missing after {start!r} in {frag}"
    return src[a:b]


@pytest.fixture(scope="module")
def run(tmp_path_factory):
    d = tmp_path_factory.mktemp("launch-roles")
    slices = d / "slices.json"
    slices.write_text(json.dumps({n: _cut(n) for n in SLICES}), encoding="utf-8")
    harness = d / "harness.js"
    harness.write_text(_HARNESS.replace("__SLICES_PATH__", json.dumps(str(slices))),
                       encoding="utf-8")
    proc = subprocess.run([NODE, str(harness)], capture_output=True, text=True,
                          encoding="utf-8", timeout=120)
    lines = (proc.stdout or "").strip().splitlines()
    tail = re.match(r"^(\d+) passed, (\d+) failed$", lines[-1] if lines else "")
    assert tail is not None, (
        "harness produced no machine-readable tail (node crashed?)\n"
        f"rc={proc.returncode}\nstdout={proc.stdout}\nstderr={proc.stderr}")
    results = {}
    for line in lines[:-1]:
        if line.startswith("PASS: "):
            results[line[len("PASS: "):]] = True
        elif line.startswith("FAIL: "):
            results[line[len("FAIL: "):]] = False
    return {"rc": proc.returncode, "stdout": proc.stdout, "stderr": proc.stderr,
            "passed": int(tail.group(1)), "failed": int(tail.group(2)),
            "results": results}


def _ok(run, label):
    got = run["results"].get(label)
    verdict = "failed" if got is False else "never ran"
    assert got is True, (
        f"{label!r} {verdict}\nstdout: {run['stdout']}\nstderr: {run['stderr']}")


@pytest.mark.parametrize("label", [
    "P1 local terminals:false reads unknown on the first sync read, then true",
    "P2 local terminals:true and a key-less (older) /info read false; no memo reads null",
    "P3 a peer reads its Control Panel record; no record is unknown",
    "P4 a peer read before 81 declared its cache is unknown, not a throw",
    "P5 a localInfo(true) refetch is read again (keyed on the memo promise)",
])
def test_253_paint_predicate(run, label):
    _ok(run, label)


@pytest.mark.parametrize("label", [
    "D1 no chosen default + local off + a visible launching peer -> null (no peer picked)",
    "D2 no chosen default + local on -> local (unchanged)",
    "D3 a chosen default is returned, null only when it hosts no terminals (down stays with launchProfile)",
    "D4 chosen local + local off -> null; a removed id falls back to a launching local",
    "D5 a click-time localOff overrides the unknown paint answer",
    "D6 after the memo settles the paint answer agrees with the click-time one",
])
def test_253_default_launch_host(run, label):
    _ok(run, label)


@pytest.mark.parametrize("label", [
    "L1 the first-ever quick-launch with local off posts nothing and explains",
    "L2 launchProfile on a terminals-off local refuses at click time, no /launch",
    "L3 launchProfile(null) is the explained modal, never a silent local launch",
    "L4 a 403 terminals_disabled from /launch is the same modal, not \"launch failed\"",
    "L5 default config: quick-launch posts /launch to local exactly as before",
    "L6 the null modal names the reason: this Hub, or the chosen default",
    "L7 a peer whose record says terminals off is refused at click time too",
    "L8 a down chosen default still gets launchProfile's own reason (unchanged)",
    "L9 hostRefusesLaunch: a launching local is not refused",
    "L10 hostRefusesLaunch: a failed /info read launches (older behaviour), never throws",
    "L11 the click-time refusal dials nothing at all",
    "L12 any other refusal keeps the existing \"launch failed\" notice",
    "L13 a launching peer still launches when only local hosts no terminals",
    "L14 quick-launch honours a launching chosen default when local is off",
])
def test_253_click_time_launch(run, label):
    _ok(run, label)


@pytest.mark.parametrize("label", [
    "I1 a terminals-off host gets one disabled reason row instead of its profiles",
    "M1 the open menu fetches /profiles for a launching peer, never for a terminals-off host",
    "M2 the open menu asks a cold peer for its /info once and repaints into the reason row",
    "M3 a menu opened before the local /info settled repaints when it lands",
    "M4 a host whose answer is known is never re-asked and nothing repaints for it",
    "M5 a menu opened before the local /info memo existed still repaints into the reason row",
    "E1 a terminals-off target shows the note, disables Add/Detect and fetches nothing",
    "E2 a launching target loads its profiles as before",
    "C1 fetchModCatalog records terminalsOff from a literal false only, with or without mods",
    "C2 primeModCatalog hands back its read (undefined while one is in flight)",
    "B1 a null START default leaves no badge instead of throwing",
])
def test_253_menus_editor_badge(run, label):
    _ok(run, label)


def test_every_scenario_passed_and_node_exited_clean(run):
    assert run["failed"] == 0, run["stdout"] + run["stderr"]
    assert run["passed"] == EXPECTED_ASSERTS, run["stdout"]
    assert len(run["results"]) == EXPECTED_ASSERTS
    assert run["rc"] == 0, run["stderr"]
    assert "HARNESS CRASH" not in run["stdout"], run["stdout"]


def test_the_page_posts_launch_from_launch_profile_only():
    """Every launch affordance (START, the menu rows, the keybinding, the
    update mod's fresh terminal) reaches the broker through launchProfile, so
    its click-time refusal covers them all. Pin that: the assembled page --
    shipped mods included -- names the /launch route exactly once, inside
    launchProfile."""
    from webterm.broker.ui import INDEX_HTML
    hits = [m.start() for m in re.finditer(r"""['"`]/launch['"`]""", INDEX_HTML)]
    assert len(hits) == 1, f"/launch named {len(hits)}x in the page"
    start = INDEX_HTML.index("async function launchProfile(host, name, cwd) {")
    end = INDEX_HTML.index("// /profiles is static per broker run", start)
    assert start < hits[0] < end, "the one /launch POST is not in launchProfile"
