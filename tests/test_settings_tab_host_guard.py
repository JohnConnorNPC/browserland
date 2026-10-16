"""Behavioural tests for #255: a Control Panel host tab never shows -- and can
never write through -- the PREVIOUS tab's per-host rows.

The cells execute the SHIPPED code in node: the real ``selectSettingsTab`` and
``renderSettings`` (81), the profiles editor and its writers, the label-order
editor, the MCP / default-profile / keybinding renderers, the start-path
debounce, and the 79 target makers + ``flushSettingsEdits``, cut from their
fragments at named boundaries and evaluated in one ``with (env)`` scope.

* Shared module state (``settingsTarget``, ``currentSettingsTab``,
  ``_startPathTimer``, the picker arrays, ``_dlgFinish`` ...) is a property of
  ``env``, never declared inside a slice, so the tests read back what the code
  wrote. The 79 element ``const`` block is never sliced: each element is a fake
  on ``env``.
* ``fetchHostState`` is a deferred-gate stub, so the in-flight and mid-await
  cases are driven by resolving (or never resolving) a promise on cue.
* Out of scope and stubbed, named so a reader does not assume otherwise: the
  OSC 52 box, the mod policy list, the browser-global sections, the tab strip
  and the host form (each paints itself or is covered elsewhere).

Atom 1 (this commit) holds the checks that are true of the CURRENT behaviour:

* C6 / C7 -- controls: a direct host's rows render and write to THAT host; the
  Local tab re-enables what a remote tab leaves off.
* L1-L3 -- the reason line: loading / could not load / proxied wording.
* G1 -- a tab switch during the /state await is still honoured.
* H4 -- the re-render stubs settle: no section re-fetches in a loop.

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
# The cuts START AFTER every shared ``let`` (they live on env), so no slice
# declares module state of its own.
SLICES = {
    "tab": ("81_js_control_panel.js",
            "        // Retire the remote-load placeholder",
            "        // #123: taskbar/title label editor."),
    "labels": ("81_js_control_panel.js",
               "        function renderLabelOrder(t) {",
               '        // Task 8: populate the "Default terminal profile"'),
    "defprof": ("81_js_control_panel.js",
                '        // Task 8: populate the "Default terminal profile"',
                "        // ---- MCP access section"),
    "mcp": ("81_js_control_panel.js",
            "        function mcpConnectUrl(host) {",
            "        // POST a patch to /mcp/config"),
    "ped": ("81_js_control_panel.js",
            "        // The host the editor acts on",
            "        // #115: per-profile color pickers reuse"),
    "pickers": ("81_js_control_panel.js",
                "        function flushProfilePickers() {",
                '        // ---- "Mods on this broker" section (#157)'),
    "editor": ("81_js_control_panel.js",
               "        const PROFILES_TERMINALS_OFF_NOTE",
               "        if (setProfileAddBtn) {"),
    "addbtns": ("81_js_control_panel.js",
                "        if (setProfileAddBtn) {",
                "        // The mode of a remote tab's cached layout"),
    "startpath": ("81_js_control_panel.js",
                  "        const commitStartPath = () => {",
                  "        setStartPathEl.addEventListener('change', commitStartPath);"),
    "keys": ("82_js_settings_keys_hosts.js",
             "        function renderKeybindings() {",
             "        // ---- settings: hosts"),
    "targets": ("79_js_settings_modal.js",
                "        function makeLocalTarget() {",
                "        function openSettings() {"),
    "flush": ("79_js_settings_modal.js",
              "        function flushSettingsEdits() {",
              "        function closeSettings() {"),
}
# Source order, one string: cross-fragment siblings (flushSettingsEdits with
# commitStartPath, the profile writers with each other) must share a scope.
SLICE_ORDER = ["tab", "labels", "defprof", "mcp", "ped", "pickers", "editor",
               "addbtns", "startpath", "keys", "targets", "flush"]

EXPECTED_ASSERTS = 11

_HARNESS = r"""
'use strict';
const S = JSON.parse(require('fs').readFileSync(__SLICES_PATH__, 'utf8'));
const CODE = __ORDER__.map(n => S[n]).join('\n');

let pass = 0, fail = 0;
function ok(cond, label, detail) {
    if (cond) { pass++; console.log('PASS: ' + label); }
    else {
        fail++; console.log('FAIL: ' + label);
        if (detail !== undefined) console.log('  detail: ' + JSON.stringify(detail));
    }
}
const tick = () => new Promise(r => setTimeout(r, 0));
async function settle(n) { for (let i = 0; i < (n || 4); i++) await tick(); }

// Evaluate `code` (sloppy, so `with` works) with env's names in scope and
// return the named functions it declares (absent ones come back undefined).
function load(code, env, names) {
    const ret = '{' + names.map(n => n + ': typeof ' + n + " === 'function' ? " + n
        + ' : undefined').join(', ') + '}';
    return (new Function('env', 'with (env) { return (function () {\n' + code
        + '\nreturn ' + ret + ';\n})(); }'))(env);
}

// ---- a tiny fake DOM --------------------------------------------------------
class El {
    constructor(tag, id) {
        this.tag = tag || 'div'; this.id = id || '';
        this.children = []; this.listeners = {}; this._text = '';
        this.dataset = {}; this.disabled = false; this.checked = false;
        this.value = ''; this.placeholder = ''; this.title = ''; this.type = '';
        this.className = ''; this.draggable = false; this.rows = 0;
        this.style = { display: '', props: {},
                       setProperty(k, v) { this.props[k] = v; } };
        const cls = new Set();
        this.classList = {
            add: (...a) => a.forEach(x => cls.add(x)),
            remove: (...a) => a.forEach(x => cls.delete(x)),
            toggle: (x, on) => { if (on === undefined) on = !cls.has(x);
                                 if (on) cls.add(x); else cls.delete(x); return on; },
            contains: (x) => cls.has(x),
        };
    }
    get textContent() { return this._text + this.children.map(c => c.textContent).join(''); }
    set textContent(v) { this.children = []; this._text = String(v); }
    set innerHTML(v) { this.children = []; this._text = ''; }
    get innerHTML() { return ''; }
    appendChild(c) { this.children.push(c); c.parentNode = this; return c; }
    addEventListener(t, f) { (this.listeners[t] = this.listeners[t] || []).push(f); }
    getBoundingClientRect() { return { top: 0, height: 10 }; }
}
// Like a browser, a disabled control dispatches no click/change.
function fire(el, type, ev) {
    if (el.disabled && (type === 'click' || type === 'change')) return false;
    const e = Object.assign({ preventDefault() {}, target: el }, ev || {});
    for (const f of (el.listeners[type] || []).slice()) f(e);
    return true;
}
function findAll(el, pred, out) {
    out = out || [];
    for (const c of el.children) { if (pred(c)) out.push(c); findAll(c, pred, out); }
    return out;
}
const buttonsOf = (row, text) => findAll(row, c => c.tag === 'button' && c.textContent === text);
const rowOf = (list, name) => list.children.find(r => r.textContent.indexOf(name) !== -1);
const hasText = (el, s) => el.textContent.indexOf(s) !== -1;

const clone = (o) => JSON.parse(JSON.stringify(o));
const mkSettings = (o) => Object.assign({
    show: { id: true, host: true, pid: false, title: true,
            order: ['id', 'host', 'pid', 'title'] },
    size: null, startPath: '', defaultProfile: '', keybindings: {},
    stripScrollbar: false, terminalCloseTerminates: false, terminalCloseConfirm: false,
    snapHoldMs: 3000, slideScreenMs: 350,
}, o || {});
const mkProfiles = (names, def, colors) => {
    const profiles = {};
    for (const n of names) {
        profiles[n] = { command: [n + '.exe'], title: null, cwd: null,
                        color: (colors && colors[n]) || null };
    }
    return { ok: true, profiles, default_profile: def, exists: {} };
};

// One world: hosts, the caches, every recorder, and the env the slices run in.
function mkWorld() {
    const w = { fetches: [], saves: [], notices: [], dialogs: [], pickers: [],
                gates: [], stateFetches: [], timers: [], fcount: {}, mode: 'auto' };
    const bump = (k) => { w.fcount[k] = (w.fcount[k] || 0) + 1; };
    const hosts = [
        { id: 'local', label: 'this broker' },
        { id: 'rd', label: 'rd', url: 'http://rd:1' },
        { id: 'rb', label: 'rb', url: 'http://rb:1', via: 'server' },
        { id: 'rx', label: 'rx', url: 'http://rx:1' },
        { id: 'rf', label: 'rf', url: 'http://rf:1' },
    ];
    w.hosts = hosts;
    w.localSettings = mkSettings({ terminalCloseTerminates: true, startPath: 'C:/local',
                                   defaultProfile: 'local-one' });
    // What each host would answer. rx is the "previous" host: every value is
    // distinctive so a leak is visible by name.
    w.entries = {
        rx: { rev: 1, layout: {}, settings: mkSettings({
            show: { id: true, host: true, pid: false, title: true,
                    order: ['title', 'id', 'host', 'pid'] },
            size: { cols: 111, rows: 33 }, startPath: 'C:/rx-start',
            defaultProfile: 'rx-alpha', terminalCloseTerminates: true,
            terminalCloseConfirm: true, snapHoldMs: 1111, slideScreenMs: 222,
            stripScrollbar: true, keybindings: { launch: 'Ctrl+X' } }) },
        rd: { rev: 1, layout: {}, settings: mkSettings({ startPath: 'C:/rd-start' }) },
        rb: null, rf: null,
    };
    w.cfg = {   // /profiles/config answers
        local: mkProfiles(['local-one', 'local-two'], 'local-one'),
        rx: mkProfiles(['rx-alpha', 'rx-beta'], 'rx-alpha', { 'rx-beta': '#ff0000' }),
        rd: mkProfiles(['rd-one', 'rd-two'], 'rd-one'),
    };
    w.mcp = {   // /mcp/config answers (rd is absent on purpose: "not loaded yet")
        local: { ok: true, enabled: true, token: 'local-token', admin_token: '',
                 default_mode: 'off', allow_launch: false, allow_desktop: false,
                 python: 'py', local_url: 'http://local:1/mcp' },
        rx: { ok: true, enabled: true, token: 'rx-token', admin_token: 'rx-admin',
              default_mode: 'rw', allow_launch: true, allow_desktop: true,
              token_env_pinned: true, admin_token_env_pinned: true, python: 'py',
              local_url: 'http://rx:1/mcp' },
    };
    const E = {};
    const el = (name, tag) => (E[name] = new El(tag || 'div', name));
    const ids = {};   // document.getElementById map
    for (const [name, id] of [
        ['setPaneHost', 'set-pane-host'], ['setHostLoadingEl', 'set-host-loading'],
        ['setColsEl', 'set-cols'], ['setRowsEl', 'set-rows'], ['setTiling', 'set-tiling'],
        ['setStripScrollbar', 'set-strip-scrollbar'], ['setCloseTerminates', 'set-close-terminates'],
        ['setConfirmTerminate', 'set-confirm-terminate'], ['setSnapHold', 'set-snap-hold'],
        ['setSlideMs', 'set-slide-ms'], ['setOsc52', 'set-osc52'], ['setRestore', 'set-restore'],
        ['setDefaultProfile', 'set-default-profile'], ['setKeybindingsEl', 'set-keybindings'],
        ['setStartLabelEl', 'set-start-label'], ['setSwapLaunchEl', 'set-swap-launch'],
        ['setHostStatusChipEl', 'set-host-status-chip'], ['setStartPathEl', 'set-start-path'],
        ['setMcpEnabled', 'set-mcp-enabled'], ['setMcpToken', 'set-mcp-token'],
        ['setMcpGenerate', 'set-mcp-generate'], ['setMcpDefaultMode', 'set-mcp-default-mode'],
        ['setMcpAllowLaunch', 'set-mcp-allow-launch'], ['setMcpAllowDesktop', 'set-mcp-allow-desktop'],
        ['setMcpAdminToken', 'set-mcp-admin-token'], ['setMcpAdminGenerate', 'set-mcp-admin-generate'],
        ['setMcpAdminClear', 'set-mcp-admin-clear'], ['setMcpCopyAdmin', 'set-mcp-copy-admin'],
        ['setMcpUrlEl', 'set-mcp-url'], ['setProfilesListEl', 'set-profiles-list'],
        ['setProfileAddBtn', 'set-profile-add'], ['setProfileDetectBtn', 'set-profile-detect'],
        ['setMcpScopeEl', 'set-mcp-scope'], ['setMcpCopyJson', 'set-mcp-copy-json'],
        ['setMcpCopyErr', 'set-mcp-copy-err'], ['labelOrderEl', 'set-label-order'],
    ]) { ids[id] = el(name); }
    E.setHostLoadingEl.style.display = 'none';
    E.setHostLoadingEl._text = 'loading settings…';
    E.setMcpUrlEl._text = '—';
    E.setMcpCopyJson.disabled = true;
    E.setMcpToken.placeholder = '(none — MCP disabled)';
    E.setMcpAdminToken.placeholder = '(none — no MCP admin)';
    E.setOsc52.type = 'checkbox';
    w.E = E;

    const hostById = (id) => hosts.find(h => h.id === id) || null;
    const env = Object.assign({}, E, {
        // ---- shared module state: OWN properties, never in a slice ----------
        settingsTarget: null, currentSettingsTab: 'local', settingsOpenHostId: null,
        _kbRecording: null, _labelDragKey: null, _startPathTimer: null,
        _profileDlgFin: null, _dlgFinish: null,
        profilePickerCleanups: [], profilePickerTargets: [],
        LABEL_ORDER_NAMES: { id: 'window id', host: 'host', pid: 'pid', title: 'title' },
        PROFILES_TERMINALS_OFF_NOTE_UNUSED: null,
        mcpConfigCache: new Map(), mcpConfigFetching: new Set(),
        profilesConfigCache: new Map(), profilesConfigFetching: new Set(),
        profilesConfigFailed: new Set(), profilesCache: new Map(),
        // ---- plain helpers ---------------------------------------------------
        hostById, hostIsProxied: (h) => !!h && h.via === 'server',
        hostTerminalsOff: () => false, hostDefaultColor: () => '',
        getSettings: () => w.localSettings, isTilingMode: () => false,
        remoteTilingMode: () => 'tiling', commandPreview: (c) => (c || []).join(' '),
        normalizeHex: (v) => v, PALETTE: ['#ff0000', '#00ff00'],
        normalizeLabelOrder: (a) => (Array.isArray(a) && a.length) ? a.slice()
            : ['id', 'host', 'pid', 'title'],
        startPathForDisplay: (s) => s.startPath || '',
        keyActions: () => [{ id: 'launch', label: 'Launch' }, { id: 'close', label: 'Close' }],
        window: { location: { origin: 'http://local:1' } },
        showNotice: (t) => { w.notices.push(t); },
        refreshTaskbar: () => {}, applyDisplaySettings: () => {},
        savePrefs: () => { w.saves.push('local'); },
        putHostState: (id) => { w.saves.push(id); },
        isOsc52Allowed: () => false,
        // ---- out of scope, stubbed (see the docstring) -----------------------
        renderSettingsTabs: () => {}, showSettingsPane: () => {},
        renderModPolicy: () => {}, dropUnreachableCatalog: () => {},
        applyBrowserGlobalVisibility: (show) => { w.browserGlobal = show; },
        renderModSettingsToggles: () => {}, resetHostForm: () => {},
        renderHostsList: () => {},
        // ---- the network, as recorders ---------------------------------------
        hostFetch: async (h, path, init) => {
            const method = (init && init.method) || 'GET';
            w.fetches.push(h.id + ' ' + method + ' ' + path);
            const respond = (body, okv) => ({ ok: okv !== false, status: okv === false ? 404 : 200,
                                              json: async () => body });
            if (path === '/profiles/config' && method === 'POST') {
                const b = JSON.parse(init.body);
                const nb = { ok: true, profiles: b.profiles, default_profile: b.default_profile,
                             exists: {} };
                w.cfg[h.id] = nb;
                return respond(nb);
            }
            if (path === '/profiles/config') return respond(w.cfg[h.id] || { ok: false }, !!w.cfg[h.id]);
            if (path === '/mcp/config' && method === 'GET') {
                return respond(w.mcp[h.id] || { ok: false }, !!w.mcp[h.id]);
            }
            if (path === '/profiles/detect') {
                return respond({ ok: true, suggestions: [
                    { name: 'sug', title: 'Sug', command: ['sug.exe'] }] });
            }
            return respond({ ok: false }, false);
        },
        fetchProfilesConfig: (host) => {
            bump('cfg:' + host.id);
            return env.hostFetch(host, '/profiles/config')
                .then(r => (r.ok ? r.json() : null))
                .then(j => { if (j && j.ok) env.profilesConfigCache.set(host.id, j); })
                .catch(() => {});
        },
        fetchProfiles: (host) => {
            bump('names:' + host.id);
            const c = w.cfg[host.id];
            return tick().then(() => {
                if (c) env.profilesCache.set(host.id, { profiles: Object.keys(c.profiles),
                                                        default: c.default_profile });
            });
        },
        fetchHostState: (id) => {
            w.stateFetches.push(id);
            if (w.mode === 'auto') return Promise.resolve(w.entries[id] || null);
            return new Promise((res) => { w.gates.push({ id, res }); });
        },
        attachColorPicker: (target, row, palette, onPick) => {
            const b = new El('button'); b.type = 'button';
            const rec = { target, onPick, cleaned: false };
            target.cleanups.push(() => { rec.cleaned = true; });
            w.pickers.push(rec);
            return b;
        },
        openDialog: (spec) => {
            if (env._dlgFinish) env._dlgFinish(null);
            let resolve;
            const p = new Promise((r) => { resolve = r; });
            const rec = { spec, result: undefined, done: false, body: new El('div') };
            const finish = (v) => {
                if (env._dlgFinish === finish) env._dlgFinish = null;
                if (!rec.done) { rec.done = true; rec.result = v; }
                resolve(v);
            };
            rec.finish = finish;
            env._dlgFinish = finish;
            w.dialogs.push(rec);
            if (spec.body) spec.body(rec.body);
            return p;
        },
        setTimeout: (fn, ms) => { const t = { fn, ms, cancelled: false, ran: false };
                                  w.timers.push(t); return t; },
        clearTimeout: (t) => { if (t) t.cancelled = true; },
        document: {
            getElementById: (id) => ids[id] || null,
            createElement: (tag) => new El(tag),
            querySelectorAll: () => [],
            activeElement: null,
        },
        Object, Array, Map, Set, JSON, Promise, String, Number, Math, parseInt, Number,
        Date, Error, console,
    });
    w.env = env;
    const fns = load(CODE, env, [
        'selectSettingsTab', 'renderSettings', 'renderLabelOrder', 'renderProfilesEditor',
        'renderMcpConfig', 'renderDefaultProfile', 'renderKeybindings', 'flushSettingsEdits',
        'makeLocalTarget', 'makeRemoteTarget', 'profilesEditorHost', 'flushProfilePickers',
        'applyProfileColor', 'deleteProfile', 'saveProfilesConfig', 'openProfileDialog',
        'detectProfiles', 'hostSettingsLoadError', 'clearHostLoading', 'commitStartPath',
        'blankHostSections', 'blankInertHostFields', 'cancelProfileDialog', 'mcpShowNeutral',
        'profileHostIsCurrent', 'refuseStaleProfileHost']);
    w.fn = fns;
    w.cache = () => {
        env.profilesConfigCache.clear(); env.profilesCache.clear();
        env.mcpConfigCache.clear();
    };
    // Pre-seed the caches the way a visit to the tab would have (so a paint
    // does not depend on a fetch landing first) for the named hosts.
    w.seed = (ids2) => {
        for (const id of ids2) {
            if (w.cfg[id]) env.profilesConfigCache.set(id, clone(w.cfg[id]));
            if (w.cfg[id]) env.profilesCache.set(id, {
                profiles: Object.keys(w.cfg[id].profiles), default: w.cfg[id].default_profile });
            if (w.mcp[id]) env.mcpConfigCache.set(id, clone(w.mcp[id]));
        }
    };
    // Select a tab to completion (auto mode: the /state answer is immediate).
    w.visit = async (id) => { w.mode = 'auto'; await fns.selectSettingsTab(id); await settle(); };
    w.posts = () => w.fetches.filter(f => / POST /.test(f));
    w.listNames = () => env.setProfilesListEl.children.map(r => r.textContent);
    return w;
}
const REQUIRED_ENV = ['settingsTarget', 'currentSettingsTab', 'settingsOpenHostId',
    '_kbRecording', '_labelDragKey', '_startPathTimer', '_profileDlgFin', '_dlgFinish',
    'profilePickerCleanups', 'profilePickerTargets'];

(async function () {
    {   // the harness itself: every shared let lives on env before the first run
        const w = mkWorld();
        const missing = REQUIRED_ENV.filter(k => !Object.prototype.hasOwnProperty.call(w.env, k));
        if (missing.length) { console.log('HARNESS CRASH: env lacks ' + missing.join(',')); return; }
    }

    // ---- C6: control, a direct host with a good fetch --------------------------
    {
        const w = mkWorld(); w.seed(['rd']);
        await w.visit('rd');
        const names = w.listNames();
        const before = w.posts().length;
        const row = rowOf(w.env.setProfilesListEl, 'rd-two');
        buttonsOf(row, 'Delete').forEach(b => fire(b, 'click'));
        await settle();
        const posts = w.posts().slice(before);
        const cb = w.env.labelOrderEl.children[0].children[0];
        const saves0 = w.saves.filter(s => s === 'rd').length;
        cb.checked = false; fire(cb, 'change');
        const saves1 = w.saves.filter(s => s === 'rd').length;
        ok(names.length === 2 && names[0].indexOf('rd-one') !== -1
           && posts.length === 1 && posts[0] === 'rd POST /profiles/config'
           && saves1 - saves0 === 1 && w.saves.filter(s => s !== 'rd').length === 0,
           'C6 control: rd renders its own rows, Delete POSTs /profiles/config to rd exactly once, a label checkbox saves rd once',
           { names, posts, saves: w.saves });
    }

    // ---- C7: control, the Local tab after a remote one -------------------------
    {
        const w = mkWorld(); w.seed(['rx', 'local']);
        await w.visit('rx');
        await w.visit('local');
        const E = w.env;
        const row = rowOf(E.setProfilesListEl, 'local-two');
        const before = w.posts().length;
        if (row) buttonsOf(row, 'Delete').forEach(b => fire(b, 'click'));
        await settle();
        const posts = w.posts().slice(before);
        ok(!E.setProfileAddBtn.disabled && !E.setProfileDetectBtn.disabled
           && !hasText(E.setProfilesListEl, 'rx-') && !!row
           && E.setConfirmTerminate.disabled === false && E.setConfirmTerminate.checked === false
           && E.setMcpToken.value === 'local-token' && E.setMcpUrlEl.textContent === 'http://local:1/mcp'
           && E.setDefaultProfile.value === 'local-one'
           && E.setStartPathEl.value === 'C:/local'
           && posts.length === 1 && posts[0] === 'local POST /profiles/config',
           'C7 control: the Local tab repaints its own rows, enables Add/Detect and Confirm, and Delete POSTs to local',
           { add: E.setProfileAddBtn.disabled, confirmDisabled: E.setConfirmTerminate.disabled,
             token: E.setMcpToken.value, url: E.setMcpUrlEl.textContent,
             def: E.setDefaultProfile.value, posts });
    }

    // ---- L1-L3: the reason line --------------------------------------------------
    {
        const w = mkWorld(); w.seed(['rx']);
        await w.visit('rx');
        w.mode = 'gate';
        w.fn.selectSettingsTab('rd');      // not awaited: held in flight
        ok(w.env.setHostLoadingEl.textContent === 'loading settings…'
           && w.env.setHostLoadingEl.style.display === '',
           'L1 in flight the reason line reads "loading settings…" and is shown',
           { text: w.env.setHostLoadingEl.textContent, display: w.env.setHostLoadingEl.style.display });
    }
    {
        const w = mkWorld(); w.seed(['rx']);
        await w.visit('rx');
        w.mode = 'gate';
        const p = w.fn.selectSettingsTab('rf');
        w.gates.pop().res(null);
        await p; await settle();
        ok(w.env.setHostLoadingEl.textContent === 'could not load this broker’s settings'
           && w.env.setHostLoadingEl.style.display === '',
           'L2 a failed direct host shows "could not load this broker’s settings"',
           { text: w.env.setHostLoadingEl.textContent });
    }
    {
        const w = mkWorld(); w.seed(['rx']);
        await w.visit('rx');
        await w.visit('rb');
        ok(w.env.setHostLoadingEl.textContent
           === 'settings are not available for a host reached through this broker'
           && w.env.setHostLoadingEl.style.display === '',
           'L3 a proxied host says its settings are not available through this broker',
           { text: w.env.setHostLoadingEl.textContent });
    }

    // ---- G1: a tab switch during the /state await is honoured -------------------
    {
        const w = mkWorld(); w.seed(['rx', 'local']);
        w.mode = 'gate';
        const p = w.fn.selectSettingsTab('rx');
        const gate = w.gates.pop();
        await w.fn.selectSettingsTab('local');
        gate.res(w.entries.rx);
        await p; await settle();
        const E = w.env;
        ok(E.currentSettingsTab === 'local' && E.settingsTarget && E.settingsTarget.hostId === 'local'
           && !hasText(E.setProfilesListEl, 'rx-') && !hasText(E.labelOrderEl, 'rx'),
           'G1 an old /state answer landing after a switch to Local paints nothing of rx',
           { tab: E.currentSettingsTab, target: E.settingsTarget && E.settingsTarget.hostId,
             rows: w.listNames() });
    }

    // ---- H4: the re-render stubs settle, nothing re-fetches in a loop ------------
    {
        const w = mkWorld();
        await w.visit('rx'); await w.visit('rd'); await w.visit('rx');
        const worst = Math.max(...Object.values(w.fcount));
        ok(worst <= 2 && w.fetches.filter(f => / GET \/profiles\/config/.test(f)).length <= 4,
           'H4 painting three tabs fetches each section at most twice per visit (no re-entry loop)',
           { fcount: w.fcount, fetches: w.fetches });
    }

    // ---- D0: the profile dialog handle (atom 2) -----------------------------------
    const openEdit = (w, name) => {
        const row = rowOf(w.env.setProfilesListEl, name);
        buttonsOf(row, 'Edit').forEach(b => fire(b, 'click'));
        return w.dialogs[w.dialogs.length - 1];
    };
    {
        const w = mkWorld(); w.seed(['rx']);
        await w.visit('rx');
        const dlg = openEdit(w, 'rx-alpha');
        const armed = !!dlg && !dlg.done && w.env._dlgFinish === dlg.finish
            && w.env._profileDlgFin === dlg.finish;
        w.fn.cancelProfileDialog();
        ok(armed && dlg.done && dlg.result === null && w.env._profileDlgFin === null
           && w.env._dlgFinish === null,
           'D0a cancelProfileDialog finishes an open Edit dialog with null and drops the handle',
           { armed, done: dlg && dlg.done, result: dlg && dlg.result });
    }
    {
        const w = mkWorld(); w.seed(['rx']);
        await w.visit('rx');
        fire(w.env.setProfileDetectBtn, 'click');
        await settle();
        const dlg = w.dialogs[w.dialogs.length - 1];
        const armed = !!dlg && dlg.spec.title === 'Detected shells' && !dlg.done
            && w.env._profileDlgFin === dlg.finish;
        w.fn.cancelProfileDialog();
        ok(armed && dlg.done && dlg.result === null && w.env._profileDlgFin === null,
           'D0b cancelProfileDialog finishes an open Detect list too',
           { armed, done: dlg && dlg.done, title: dlg && dlg.spec.title, hand: w.env._profileDlgFin === (dlg && dlg.finish), n: w.dialogs.length, f: w.fetches });
    }
    {
        const w = mkWorld(); w.seed(['rx']);
        await w.visit('rx');
        const mine = openEdit(w, 'rx-alpha');
        // a dialog that is not ours replaces ours before anything settles
        w.env.openDialog({ title: 'a mod dialog' });
        const foreign = w.dialogs[w.dialogs.length - 1];
        const armed = foreign !== mine && !foreign.done && w.env._dlgFinish === foreign.finish
            && w.env._profileDlgFin === mine.finish;
        w.fn.cancelProfileDialog();
        ok(armed && !foreign.done && w.env._dlgFinish === foreign.finish
           && w.env._profileDlgFin === null,
           'D0c a dialog that is not ours is never finished from here',
           { armed, foreignDone: foreign.done });
    }
    {
        const w = mkWorld(); w.seed(['rx']);
        await w.visit('rx');
        const a = openEdit(w, 'rx-alpha');
        const b = openEdit(w, 'rx-beta');     // replaces (cancels) a
        await settle();                        // a's .then runs now
        const armed = a.done && !b.done && w.env._profileDlgFin === b.finish;
        w.fn.cancelProfileDialog();
        ok(armed && b.done && b.result === null,
           'D5 an older dialog settling late does not drop the newer one’s handle',
           { armed, bDone: b.done, handleIsB: w.env._profileDlgFin === b.finish });
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
    d = tmp_path_factory.mktemp("settings-tab-guard")
    slices = d / "slices.json"
    slices.write_text(json.dumps({n: _cut(n) for n in SLICES}), encoding="utf-8")
    harness = d / "harness.js"
    harness.write_text(
        _HARNESS.replace("__SLICES_PATH__", json.dumps(str(slices)))
        .replace("__ORDER__", json.dumps(SLICE_ORDER)), encoding="utf-8")
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
    "C6 control: rd renders its own rows, Delete POSTs /profiles/config to rd exactly once, a label checkbox saves rd once",
    "C7 control: the Local tab repaints its own rows, enables Add/Detect and Confirm, and Delete POSTs to local",
    "L1 in flight the reason line reads \"loading settings…\" and is shown",
    "L2 a failed direct host shows \"could not load this broker’s settings\"",
    "L3 a proxied host says its settings are not available through this broker",
    "G1 an old /state answer landing after a switch to Local paints nothing of rx",
    "H4 painting three tabs fetches each section at most twice per visit (no re-entry loop)",
])
def test_controls_and_reason_lines(run, label):
    _ok(run, label)


@pytest.mark.parametrize("label", [
    "D0a cancelProfileDialog finishes an open Edit dialog with null and drops the handle",
    "D0b cancelProfileDialog finishes an open Detect list too",
    "D0c a dialog that is not ours is never finished from here",
    "D5 an older dialog settling late does not drop the newer one’s handle",
])
def test_profile_dialog_handle(run, label):
    _ok(run, label)


def test_every_scenario_passed_and_node_exited_clean(run):
    assert run["failed"] == 0, run["stdout"] + run["stderr"]
    assert run["passed"] == EXPECTED_ASSERTS, run["stdout"]
    assert len(run["results"]) == EXPECTED_ASSERTS
    assert run["rc"] == 0, run["stderr"]
    assert "HARNESS CRASH" not in run["stdout"], run["stdout"]
