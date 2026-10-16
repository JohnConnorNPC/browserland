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

The checks, by label (each is one pytest param; several run the same scenario
over four ways of leaving rx: a proxied tab, one in flight, a failed one, and
Browser, which keeps ``settingsTarget``):

* C6-C11 / 4j -- controls: the writes that SHOULD happen still do (profile
  Delete/Default/Edit Save, Detect suggestion, label up/down/drag, the Start
  path debounce) and the Local tab re-enables what a remote tab leaves off.
* L1-L3, G1, H4 -- the reason line, a mid-await switch, and no re-fetch loop.
* D0a-c, D5 -- the dialog handle; D1-D4 -- a switch closes the profile dialog.
* B1-B5, H7, P -- the blank on every switch (and not after a good repaint).
* S1-S4 -- a Start path typed before a switch lands on the host it was typed
  for, even when the commit throws.
* Q1a-Q1f, K -- the display-only fields (MCP, default profile, statics,
  keybindings) are blank; renderMcpConfig's own not-loaded path is neutral.
* 4a-4i -- profile writers and dialogs refuse a host that is not the open tab;
  5a-5c -- so do the label-order rows; U1 -- the new names are declared once.

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

EXPECTED_ASSERTS = 53

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
    if (!el) return false;
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
const buttonsOf = (row, text) => findAll(row || new El('div'), c => c.tag === 'button' && c.textContent === text);
const rowOf = (list, name) => list.children.find(r => r.textContent.indexOf(name) !== -1) || null;
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
        _profileDlgFin: null, _dlgFinish: null, _settingsTabGen: 0,
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
            if (w.hold && method === 'GET' && /^\/(profiles\/config|mcp\/config)$/.test(path)) await w.hold;
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
                if (w.holdDetect) await w.holdDetect;
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
            return (w.hold || tick()).then(() => {
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
            // like the real picker: a late OS answer after disposal is a no-op
            rec.pick = (sw) => { if (target.disposed) return false; onPick(sw); return true; };
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
    '_kbRecording', '_labelDragKey', '_startPathTimer', '_profileDlgFin', '_dlgFinish', '_settingsTabGen',
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
        const row0 = w.env.labelOrderEl.children[0];
        const cb = row0 ? row0.children[0] : null;
        const saves0 = w.saves.filter(s => s === 'rd').length;
        if (cb) { cb.checked = false; fire(cb, 'change'); }
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
        ok(worst <= 2 && w.fetches.filter(f => / GET \/profiles\/config/.test(f)).length <= 4
           && (w.fcount['cfg:rx'] || 0) >= 1 && (w.fcount['cfg:rd'] || 0) >= 1
           && (w.fcount['names:rx'] || 0) >= 1,
           'H4 painting three tabs fetches each section at most twice per visit (no re-entry loop)',
           { fcount: w.fcount, fetches: w.fetches });
    }

    // ---- D0: the profile dialog handle (atom 2) -----------------------------------
    const openEdit = (w, name) => {
        const row = rowOf(w.env.setProfilesListEl, name);
        buttonsOf(row, 'Edit').forEach(b => fire(b, 'click'));
        // no dialog opened: a stand-in that is 'done', so the check's armed clause fails
        return w.dialogs[w.dialogs.length - 1]
            || { done: true, result: undefined, finish: () => {}, spec: {}, body: new El('div') };
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

    // ---- atom 3: the blank on every switch -----------------------------------------
    // The "previous host" is rx. armed(w) proves the subject exists BEFORE the
    // switch, so a check can never pass because there was nothing to blank.
    const paintRx = async (w) => { w.seed(['rx']); await w.visit('rx'); };
    const armedRx = (w) => hasText(w.env.setProfilesListEl, 'rx-alpha')
        && hasText(w.env.setProfilesListEl, 'rx-beta')
        && w.env.labelOrderEl.children.length === 4
        && w.env.setProfileAddBtn.disabled === false
        && w.env.setProfileDetectBtn.disabled === false
        && w.pickers.length >= 2 && w.env.profilePickerTargets.length >= 2;
    const blankState = (w) => ({
        profileText: w.env.setProfilesListEl.textContent,
        labelRows: w.env.labelOrderEl.children.length,
        add: w.env.setProfileAddBtn.disabled, detect: w.env.setProfileDetectBtn.disabled,
        target: w.env.settingsTarget && w.env.settingsTarget.hostId,
    });
    const isBlank = (w) => w.env.setProfilesListEl.textContent === ''
        && w.env.setProfilesListEl.children.length === 0
        && w.env.labelOrderEl.children.length === 0 && w.env.labelOrderEl.textContent === ''
        && w.env.setProfileAddBtn.disabled === true && w.env.setProfileDetectBtn.disabled === true;
    {   // B1 proxied
        const w = mkWorld(); await paintRx(w);
        const armed = armedRx(w);
        await w.visit('rb');
        ok(armed && isBlank(w) && w.env.settingsTarget === null
           && !hasText(w.env.setProfilesListEl, 'rx-'),
           'B1 a proxied tab holds nothing of the previous host: lists empty, Add/Detect disabled, no target',
           Object.assign({ armed }, blankState(w)));
    }
    {   // B2 in flight, asserted right after the synchronous part
        const w = mkWorld(); await paintRx(w);
        const armed = armedRx(w);
        w.mode = 'gate';
        w.fn.selectSettingsTab('rd');          // never answered
        ok(armed && isBlank(w) && w.env.settingsTarget === null && w.gates.length === 1,
           'B2 while /state is in flight the previous host\u2019s rows are already gone',
           Object.assign({ armed, gates: w.gates.length }, blankState(w)));
    }
    {   // B3 failed
        const w = mkWorld(); await paintRx(w);
        const armed = armedRx(w);
        await w.visit('rf');
        ok(armed && isBlank(w) && w.env.settingsTarget === null,
           'B3 a failed direct tab holds nothing of the previous host',
           Object.assign({ armed }, blankState(w)));
    }
    {   // B4 the Browser tab keeps its target (ruling) but not the rows
        const w = mkWorld(); await paintRx(w);
        const armed = armedRx(w);
        await w.visit('browser');
        ok(armed && isBlank(w) && w.env.currentSettingsTab === 'browser'
           && w.env.settingsTarget && w.env.settingsTarget.hostId === 'rx',
           'B4 the Browser tab empties the rows but keeps settingsTarget (ruling: never nulled)',
           Object.assign({ armed }, blankState(w)));
    }
    {   // H7 the blank is not applied after a successful repaint
        const w = mkWorld(); w.seed(['rx', 'rd']);
        await w.visit('rx'); const armed = armedRx(w);
        await w.visit('rd');
        ok(armed && hasText(w.env.setProfilesListEl, 'rd-one') && !hasText(w.env.setProfilesListEl, 'rx-')
           && w.env.labelOrderEl.children.length === 4 && w.env.setProfileAddBtn.disabled === false
           && w.env.settingsTarget && w.env.settingsTarget.hostId === 'rd',
           'H7 a successful switch ends with the new host\u2019s rows (the blank is not applied after the repaint)',
           Object.assign({ armed }, blankState(w)));
    }

    // ---- P: pickers -----------------------------------------------------------------
    {
        const w = mkWorld(); await paintRx(w);
        const recs = w.pickers.slice();
        const targets = w.env.profilePickerTargets.slice();
        const armed = recs.length >= 2 && targets.length >= 2
            && recs.every(r => r.cleaned === false) && targets.every(t => t.disposed === false)
            && recs.some(r => r.target.color === '#ff0000');
        await w.visit('rb');                         // a tab that does not re-render
        const before = w.fetches.length;
        const late = recs.map(r => r.pick({ color: '#00ff00' }));   // the OS dialog answering late
        await settle();
        ok(armed && targets.every(t => t.disposed === true) && recs.every(r => r.cleaned === true)
           && late.every(v => v === false) && w.fetches.length === before
           && w.env.profilePickerCleanups.length === 0,
           'P every painted colour picker is disposed and its cleanup ran on the switch; a late pick does nothing',
           { armed, disposed: targets.map(t => t.disposed), cleaned: recs.map(r => r.cleaned),
             late, newFetches: w.fetches.slice(before) });
    }

    // ---- D: the open profile dialog is closed by a switch ---------------------------
    {
        const w = mkWorld(); await paintRx(w);
        const dlg = openEdit(w, 'rx-alpha');
        const armed = !!dlg && !dlg.done && w.env._profileDlgFin === dlg.finish;
        await w.visit('rb');
        ok(armed && dlg.done && dlg.result === null && w.env._profileDlgFin === null,
           'D1 an open Edit dialog is closed by a tab switch', { armed, done: dlg && dlg.done });
    }
    {
        const w = mkWorld(); await paintRx(w);
        fire(w.env.setProfileDetectBtn, 'click'); await settle();
        const dlg = w.dialogs[w.dialogs.length - 1];
        const armed = !!dlg && dlg.spec.title === 'Detected shells' && !dlg.done;
        await w.visit('browser');
        ok(armed && dlg.done && dlg.result === null,
           'D2 an open Detect list is closed by a tab switch', { armed, done: dlg && dlg.done });
    }
    {
        const w = mkWorld(); await paintRx(w);
        const mine = openEdit(w, 'rx-alpha');
        w.env.openDialog({ title: 'a mod dialog' });     // replaces ours before anything settles
        const foreign = w.dialogs[w.dialogs.length - 1];
        const armed = foreign !== mine && !foreign.done && w.env._dlgFinish === foreign.finish
            && w.env._profileDlgFin === mine.finish;      // our handle is still set, but stale
        await w.visit('rb');
        ok(armed && !foreign.done && w.env._dlgFinish === foreign.finish,
           'D3 a dialog that is not the panel\u2019s survives a tab switch', { armed, done: foreign.done });
    }
    {
        const w = mkWorld(); await paintRx(w);
        const dlg = openEdit(w, 'rx-alpha');
        const armed = !!dlg && !dlg.done;
        await w.visit('rx');                          // same tab again = retry
        ok(armed && dlg.done && dlg.result === null,
           'D4 re-selecting the same tab closes the profile dialog too', { armed, done: dlg && dlg.done });
    }

    // ---- S: the Start path debounce -------------------------------------------------
    const typeStartPath = (w, v) => {
        w.env.setStartPathEl.value = v;
        fire(w.env.setStartPathEl, 'input');
        return w.timers.filter(t => !t.cancelled && !t.ran).length === 1
            && w.env._startPathTimer !== null;
    };
    const runLiveTimers = (w) => { for (const t of w.timers) {
        if (!t.cancelled && !t.ran) { t.ran = true; t.fn(); } } };
    {   // S1
        const w = mkWorld(); await paintRx(w);
        const armed = typeStartPath(w, 'C:/typed');
        await w.visit('browser');
        runLiveTimers(w);
        const sp = w.entries.rx.settings.startPath;
        ok(armed && sp === 'C:/typed' && w.saves.filter(s => s === 'rx').length === 1,
           'S1 a Start path typed on rx lands on rx when the tab is left, and is never written as an empty string',
           { armed, startPath: sp, saves: w.saves });
    }
    {   // S2 the helper clears a live timer by itself
        const w = mkWorld(); await paintRx(w);
        const armed = typeStartPath(w, 'C:/typed');
        w.fn.blankHostSections();
        ok(armed && w.timers.filter(t => !t.cancelled).length === 0 && w.env._startPathTimer === null,
           'S2 blankHostSections cancels a pending Start path timer on its own (backstop)',
           { armed, timers: w.timers.map(t => t.cancelled), handle: w.env._startPathTimer });
    }
    {   // S3 closing the panel after the Browser switch writes nothing blank
        const w = mkWorld(); await paintRx(w);
        const armed = typeStartPath(w, 'C:/typed');
        await w.visit('browser');
        w.fn.flushSettingsEdits();
        runLiveTimers(w);
        ok(armed && w.entries.rx.settings.startPath === 'C:/typed'
           && w.saves.filter(s => s === 'rx').length === 1,
           'S3 closing the panel after switching to Browser leaves rx\u2019s Start path as typed',
           { armed, startPath: w.entries.rx.settings.startPath, saves: w.saves });
    }
    {   // S4 a throwing commit never blocks the switch
        const a = mkWorld(); await paintRx(a);
        const armedA = typeStartPath(a, 'C:/typed');
        a.env.putHostState = (id) => { a.saves.push(id); throw new Error('save boom'); };
        let rejA = false;
        await a.fn.selectSettingsTab('browser').catch(() => { rejA = true; });
        const b = mkWorld(); await paintRx(b);
        const armedB = typeStartPath(b, 'C:/typed');
        const st = b.entries.rx.settings;
        Object.defineProperty(st, 'startPath', { get() { return 'C:/rx-start'; },
                              set() { b.wrote = true; throw new Error('write boom'); } });
        let rejB = false;
        await b.fn.selectSettingsTab('browser').catch(() => { rejB = true; });
        ok(armedA && armedB && !rejA && !rejB
           && a.env.currentSettingsTab === 'browser' && isBlank(a)
           && b.env.currentSettingsTab === 'browser' && isBlank(b)
           && a.entries.rx.settings.startPath === 'C:/typed' && a.saves.length === 1
           && b.entries.rx.settings.startPath === 'C:/rx-start' && b.wrote === true,
           'S4 a throwing Start path commit never blocks the switch: the tab changes, the rows are gone, the value is never blank',
           { armedA, armedB, rejA, rejB, tabA: a.env.currentSettingsTab, tabB: b.env.currentSettingsTab,
             spA: a.entries.rx.settings.startPath, wroteB: b.wrote });
    }

    // ---- B5: sections still loading when the tab is left -----------------------------
    {
        const w = mkWorld();
        let release; w.hold = new Promise((r) => { release = r; });
        w.mode = 'auto';
        await w.fn.selectSettingsTab('rx'); await settle();      // sections stay pending
        const armed = hasText(w.env.setProfilesListEl, 'loading') && w.cfg.rx && !w.env.profilesConfigCache.has('rx');
        await w.visit('browser');
        release(); await settle(6);
        ok(armed && isBlank(w) && w.env.setDefaultProfile.children.length === 1
           && w.env.setMcpUrlEl.textContent === '—',
           'B5 a section fetch landing after the tab was left paints nothing',
           { armed, text: w.env.setProfilesListEl.textContent, rows: w.env.labelOrderEl.children.length });
    }

    // ---- atom 4: the inert fields ---------------------------------------------------
    // Four entries that leave rx's rows without repainting: a proxied tab, one in
    // flight, a failed one, and Browser (which keeps settingsTarget).
    const ARMS = ['rb', 'gate', 'rf', 'browser'];
    const leave = async (w, arm) => {
        if (arm === 'gate') { w.mode = 'gate'; w.fn.selectSettingsTab('rd'); await settle(2); }
        else await w.visit(arm);
    };
    // Paint rx (with `prep` applied first), prove the subject is armed, leave by
    // each arm, and return {armed, <arm>: check(w)} .
    const overArms = async (prep, armedFn, checkFn) => {
        const out = { armed: true };
        for (const arm of ARMS) {
            const w = mkWorld(); if (prep) prep(w);
            await paintRx(w);
            if (!armedFn(w)) out.armed = false;
            await leave(w, arm);
            out[arm] = checkFn(w);
        }
        return out;
    };
    const allArms = (r) => r.armed && ARMS.every(a => r[a] === true);

    {   // Q1a the MCP URL
        const r = await overArms(null,
            (w) => w.env.setMcpUrlEl.textContent === 'http://rx:1/mcp',
            (w) => w.env.setMcpUrlEl.textContent === '—');
        ok(allArms(r), 'Q1a the MCP URL no longer names the previous host after a switch', r);
    }
    {   // Q1a-ctl renderMcpConfig on an uncached host still shows ITS url
        const w = mkWorld(); await paintRx(w);
        w.env.currentSettingsTab = 'rd';
        w.env.settingsTarget = w.fn.makeRemoteTarget('rd', w.entries.rd);
        w.fn.renderMcpConfig();                       // no blank on this entry
        ok(w.env.setMcpUrlEl.textContent === 'http://rd:1/mcp' && !w.env.mcpConfigCache.has('rd'),
           'Q1a-ctl an uncached host\u2019s MCP section shows its own connect URL, not a dash',
           { url: w.env.setMcpUrlEl.textContent });
    }
    {   // Q1b the default-profile select
        const names = (w) => w.env.setDefaultProfile.children.map(o => o.value);
        const r = await overArms(null,
            (w) => names(w).join() === ',rx-alpha,rx-beta' && w.env.setDefaultProfile.value === 'rx-alpha',
            (w) => names(w).join() === '' && w.env.setDefaultProfile.children.length === 1
                && w.env.setDefaultProfile.children[0].textContent === '(broker default)'
                && w.env.setDefaultProfile.value === '');
        ok(allArms(r), 'Q1b the default-profile select is back to the lone "(broker default)" option', r);
    }
    {   // Q1c the neutral MCP set, from a pinned/loaded/ticked/errored mirror state
        const E = (w) => w.env;
        const prep = (w) => { w.mcp.rx.admin_token = 'rx-admin'; };
        const armedFn = (w) => {
            // user ticks Admin, and the copy row has shown an error
            E(w).setMcpCopyAdmin.checked = true;
            E(w).setMcpCopyErr.textContent = 'not a scope name';
            E(w).setMcpCopyErr.classList.add('show');
            return E(w).setMcpEnabled.checked === true && E(w).setMcpToken.value === 'rx-token'
                && E(w).setMcpAdminToken.value === 'rx-admin' && E(w).setMcpDefaultMode.value === 'rw'
                && E(w).setMcpAllowLaunch.checked === true && E(w).setMcpAllowDesktop.checked === true
                && E(w).setMcpToken.disabled === true && E(w).setMcpGenerate.disabled === true
                && E(w).setMcpAdminToken.disabled === true && E(w).setMcpAdminGenerate.disabled === true
                && E(w).setMcpAdminClear.disabled === true
                && E(w).setMcpToken.placeholder === 'set by WEB_TERMINAL_MCP_TOKEN (env)'
                && E(w).setMcpAdminToken.placeholder === 'set by WEB_TERMINAL_MCP_ADMIN_TOKEN (env)'
                && E(w).setMcpCopyJson.disabled === false && E(w).setMcpCopyAdmin.disabled === false
                && E(w).setMcpCopyAdmin.checked === true;
        };
        const checkFn = (w) => {
            const e = E(w);
            const parts = {
                values: e.setMcpEnabled.checked === false && e.setMcpToken.value === ''
                    && e.setMcpAdminToken.value === '' && e.setMcpDefaultMode.value === 'off'
                    && e.setMcpAllowLaunch.checked === false && e.setMcpAllowDesktop.checked === false,
                unpinned: e.setMcpToken.disabled === false && e.setMcpGenerate.disabled === false
                    && e.setMcpAdminToken.disabled === false && e.setMcpAdminGenerate.disabled === false
                    && e.setMcpAdminClear.disabled === false,
                placeholders: e.setMcpToken.placeholder === '(none — MCP disabled)'
                    && e.setMcpAdminToken.placeholder === '(none — no MCP admin)',
                copyJson: e.setMcpCopyJson.disabled === true,
                copyAdmin: e.setMcpCopyAdmin.disabled === true && e.setMcpCopyAdmin.checked === false,
                copyErr: e.setMcpCopyErr.textContent === '' && !e.setMcpCopyErr.classList.contains('show'),
            };
            return Object.values(parts).every(Boolean) ? true : parts;
        };
        const r = await overArms(prep, armedFn, checkFn);
        ok(allArms(r), 'Q1c the MCP section is back to its neutral state: values, pins, placeholders and the copy row', r);
    }
    {   // Q1d the static inputs
        const armedFn = (w) => {
            const e = w.env;
            return e.setColsEl.value === 111 && e.setRowsEl.value === 33 && e.setSnapHold.value === 1111
                && e.setSlideMs.value === 222 && e.setStartPathEl.value === 'C:/rx-start'
                && e.setTiling.checked === true && e.setStripScrollbar.checked === true
                && e.setCloseTerminates.checked === true && e.setConfirmTerminate.checked === true
                && e.setConfirmTerminate.disabled === false;
        };
        const checkFn = (w) => {
            const e = w.env;
            return e.setColsEl.value === '' && e.setRowsEl.value === '' && e.setSnapHold.value === ''
                && e.setSlideMs.value === '' && e.setStartPathEl.value === ''
                && e.setTiling.checked === false && e.setStripScrollbar.checked === false
                && e.setCloseTerminates.checked === false && e.setConfirmTerminate.checked === false
                && e.setConfirmTerminate.disabled === true;
        };
        const r = await overArms(null, armedFn, checkFn);
        ok(allArms(r), 'Q1d the static inputs (size, snap, slide, Start path, toggles, Confirm) are blank', r);
    }
    {   // Q1e renderMcpConfig's own not-loaded path resets the pinned state too
        const w = mkWorld();
        await paintRx(w);
        w.env.setMcpCopyAdmin.checked = true;
        w.env.setMcpCopyErr.textContent = 'not a scope name';
        w.env.setMcpCopyErr.classList.add('show');
        const armed = w.env.setMcpToken.disabled === true && w.env.setMcpCopyAdmin.checked === true
            && w.env.setMcpCopyErr.classList.contains('show');
        w.env.currentSettingsTab = 'rd';
        w.env.settingsTarget = w.fn.makeRemoteTarget('rd', w.entries.rd);
        w.fn.renderMcpConfig();                       // the no-blank entry, as openControlPanelWindow / the host fallback reach it
        const e = w.env;
        ok(armed && e.setMcpToken.disabled === false && e.setMcpToken.value === ''
           && e.setMcpToken.placeholder === '(none — MCP disabled)'
           && e.setMcpAdminToken.disabled === false && e.setMcpCopyAdmin.checked === false
           && e.setMcpCopyAdmin.disabled === true && e.setMcpCopyErr.textContent === ''
           && !e.setMcpCopyErr.classList.contains('show') && e.setMcpUrlEl.textContent === 'http://rd:1/mcp',
           'Q1e the not-loaded path of renderMcpConfig itself drops the previous host\u2019s pins, ticks and error line',
           { armed, tokenDisabled: e.setMcpToken.disabled, ph: e.setMcpToken.placeholder,
             copyAdmin: e.setMcpCopyAdmin.checked, err: e.setMcpCopyErr.textContent });
    }
    {   // Q1f success controls: rx -> rd repaints rd's own values
        const w = mkWorld();
        w.mcp.rd = { ok: true, enabled: false, token: 'rd-token', admin_token: '', default_mode: 'off',
                     allow_launch: false, allow_desktop: false, python: 'py', local_url: 'http://rd:1/mcp' };
        w.entries.rd.settings.terminalCloseTerminates = true;
        w.entries.rd.settings.snapHoldMs = 4444;
        w.seed(['rx', 'rd']);
        await w.visit('rx');
        const armed = armedRx(w);
        await w.visit('rd');
        const e = w.env;
        ok(armed && e.setMcpUrlEl.textContent === 'http://rd:1/mcp' && e.setMcpToken.value === 'rd-token'
           && e.setMcpToken.disabled === false && e.setMcpCopyJson.disabled === false
           && e.setDefaultProfile.children.map(o => o.value).join() === ',rd-one,rd-two'
           && e.setSnapHold.value === 4444 && e.setStartPathEl.value === 'C:/rd-start'
           && e.setConfirmTerminate.disabled === false && e.setTiling.checked === true,
           'Q1f a successful switch repaints the new host\u2019s MCP section, default profile and static inputs',
           { url: e.setMcpUrlEl.textContent, token: e.setMcpToken.value, snap: e.setSnapHold.value,
             opts: e.setDefaultProfile.children.map(o => o.value) });
    }
    {   // K keybindings are emptied on every arm, Browser included
        const r = await overArms(null,
            (w) => w.env.setKeybindingsEl.children.length === 2 && w.env.setKeybindingsEl.textContent.indexOf('Ctrl+X') !== -1,
            (w) => w.env.setKeybindingsEl.children.length === 0 && w.env.setKeybindingsEl.textContent === '');
        ok(allArms(r), 'K the keybinding rows of the previous host are gone on every arm, Browser included', r);
    }

    // ---- atom 5: the profile writers refuse a host that is not the open tab ----------
    // Rows are painted for rx; the tab is then left by each arm; the DETACHED
    // row's handler is fired (a late answer, an Edit dialog's Save, a Detect scan).
    // `capture` proves the subject exists before the switch ("armed").
    const profilesEmpty = (w) => w.env.setProfilesListEl.children.length === 0
        && w.env.setProfilesListEl.textContent === '';
    const stale = async (capture, act, arms) => {
        const out = { armed: true };
        for (const arm of (arms || ARMS)) {
            const w = mkWorld(); await paintRx(w);
            const subj = capture(w);
            if (!subj.armed) out.armed = false;
            await leave(w, arm);
            const f0 = w.fetches.length, d0 = w.dialogs.length;
            let threw = null;
            try { await act(w, subj); } catch (e) { threw = String(e); }
            await settle();
            const newFetches = w.fetches.slice(f0);
            out[arm] = (threw === null && newFetches.length === 0 && w.dialogs.length === d0
                        && profilesEmpty(w)) ? true : { threw, newFetches, dialogs: w.dialogs.length - d0,
                                                        empty: profilesEmpty(w) };
        }
        return out;
    };
    const rowBtn = (text) => (w) => {
        const row = rowOf(w.env.setProfilesListEl, 'rx-beta');
        const b = row ? buttonsOf(row, text)[0] : null;
        return { armed: !!b && !b.disabled, b };
    };
    const BROWSER = ['browser'];
    {   // 4a Delete
        const r = await stale(rowBtn('Delete'), (w, s) => fire(s.b, 'click'));
        ok(allArms(r), '4a a stale Delete posts nothing and leaves the list empty on every arm', r);
    }
    {   // 4b Default (a NON-default row, or the click would never fire)
        const r = await stale(rowBtn('Default'), (w, s) => fire(s.b, 'click'));
        ok(allArms(r), '4b a stale Default posts nothing and leaves the list empty on every arm', r);
    }
    {   // 4c colour pick (the raw handler: the picker itself is already disposed)
        const r = await stale((w) => {
            const rec = w.pickers.find(p => p.target.color === '#ff0000');
            return { armed: !!rec, rec };
        }, (w, s) => s.rec.onPick({ color: '#00ff00' }));
        ok(allArms(r), '4c a stale colour pick posts nothing, does not throw, and leaves the list empty', r);
    }
    {   // 4d colour clear (the coloured row has the x)
        const r = await stale(rowBtn('✕'), (w, s) => fire(s.b, 'click'));
        ok(allArms(r), '4d a stale colour clear posts nothing and leaves the list empty on every arm', r);
    }
    {   // 4e-open Edit on a stale row opens no dialog
        const r = await stale(rowBtn('Edit'), (w, s) => fire(s.b, 'click'));
        ok(allArms(r), '4e-open a stale Edit opens no dialog and posts nothing', r);
    }
    {   // 4e-save an Edit dialog's Save that resolves while the tab is being left
        const out = { armed: true };
        for (const arm of ARMS) {
            const w = mkWorld(); await paintRx(w);
            const dlg = openEdit(w, 'rx-beta');
            if (!dlg || dlg.done) out.armed = false;
            dlg.finish({ value: 'save' });             // resolved, its .then still queued
            const p = leave(w, arm);                   // the switch runs before that .then
            await p; await settle();
            const posts = w.posts();
            out[arm] = posts.length === 0 ? true : { posts };
        }
        ok(allArms(out), '4e-save a dialog Save that lands after the switch posts nothing', out);
    }
    {   // 4f a Detect answer after the switch opens no list
        const out = { armed: true };
        for (const arm of ARMS) {
            const w = mkWorld(); await paintRx(w);
            let release; w.holdDetect = new Promise((res) => { release = res; });
            fire(w.env.setProfileDetectBtn, 'click'); await settle(2);
            const d0 = w.dialogs.length;
            if (w.env.setProfileDetectBtn.disabled || !w.fetches.some(f => /profiles\/detect/.test(f))) out.armed = false;
            await leave(w, arm);
            release(); await settle(4);
            out[arm] = w.dialogs.length === d0 ? true : { dialogs: w.dialogs.length - d0 };
        }
        ok(allArms(out), '4f a Detect scan answering after the switch opens no list', out);
    }
    {   // 4g the Browser tab, where settingsTarget is still rx's
        const r = await stale(rowBtn('Delete'), (w, s) => fire(s.b, 'click'), BROWSER);
        const r2 = await stale(rowBtn('Default'), (w, s) => fire(s.b, 'click'), BROWSER);
        ok(r.armed && r2.armed && r.browser === true && r2.browser === true,
           '4g on the Browser tab a stale Delete or Default posts nothing (the guard follows the open tab, not settingsTarget)',
           { delete: r, def: r2 });
    }
    {   // 4h the refusal on Browser repaints EMPTY and requests nothing
        const w = mkWorld(); await paintRx(w);
        const s = rowBtn('Delete')(w);
        await w.visit('browser');
        const f0 = w.fetches.length;
        fire(s.b, 'click'); await settle();
        const cfgReqs = w.fetches.slice(f0).filter(f => /profiles\/config/.test(f));
        ok(s.armed && w.env.settingsTarget && w.env.settingsTarget.hostId === 'rx'
           && profilesEmpty(w) && cfgReqs.length === 0 && w.fetches.length === f0,
           '4h a refused write on Browser leaves the list empty and requests no /profiles/config',
           { armed: s.armed, cfgReqs, empty: profilesEmpty(w), text: w.env.setProfilesListEl.textContent });
    }
    {   // 4i a Detect suggestion clicked after the switch opens no dialog
        const out = { armed: true };
        for (const arm of ARMS) {
            const w = mkWorld(); await paintRx(w);
            fire(w.env.setProfileDetectBtn, 'click'); await settle(2);
            const list = w.dialogs[w.dialogs.length - 1];
            const sug = list ? findAll(list.body, c => c.className === 'set-detect-item')[0] : null;
            if (!sug) out.armed = false;
            await leave(w, arm);
            const d0 = w.dialogs.length;
            if (sug) fire(sug, 'click'); await settle();
            out[arm] = (sug && w.dialogs.length === d0) ? true : { dialogs: w.dialogs.length - d0, sug: !!sug };
        }
        ok(allArms(out), '4i a stale Detect suggestion opens no dialog', out);
    }
    {   // 4j control: the SAME host's writes still go through while its tab is open
        const w = mkWorld(); await paintRx(w);
        const row = rowOf(w.env.setProfilesListEl, 'rx-beta');
        const f0 = w.posts().length;
        buttonsOf(row, 'Default').forEach(b => fire(b, 'click')); await settle();
        const posts = w.posts().slice(f0);
        ok(posts.length === 1 && posts[0] === 'rx POST /profiles/config' && w.cfg.rx.default_profile === 'rx-beta',
           '4j control: on rx\u2019s own open tab Default POSTs to rx exactly once', { posts });
    }

    // ---- atom 6: the label-order editor refuses a target that is not the open tab ----
    const labelSubject = (w) => {
        const rows = w.env.labelOrderEl.children.slice();
        const sub = rows.length === 4 ? {
            cb: rows[1].children[0], up: rows[1].children[2], down: rows[1].children[3],
            dragFrom: rows[0], dropOn: rows[2],
        } : null;
        const t = w.env.settingsTarget;
        return { armed: !!sub && !sub.up.disabled && !sub.down.disabled && !!t
                 && w.env.labelOrderEl.children.length === 4, sub, show: clone(t && t.s.show) };
    };
    const fireLabelRows = (w, s) => {
        s.sub.cb.checked = false; fire(s.sub.cb, 'change');
        fire(s.sub.up, 'click');
        fire(s.sub.down, 'click');
        fire(s.sub.dragFrom, 'dragstart', { dataTransfer: null });
        fire(s.sub.dropOn, 'drop', { clientY: 9 });
    };
    const labelUntouched = (w, s) => w.saves.length === 0
        && JSON.stringify(w.entries.rx.settings.show) === JSON.stringify(s.show);
    {   // 5a the four mechanisms after leaving by each arm
        const out = { armed: true };
        for (const arm of ARMS) {
            const w = mkWorld(); await paintRx(w);
            const s = labelSubject(w);
            if (!s.armed) out.armed = false;
            await leave(w, arm);
            let threw = null;
            try { fireLabelRows(w, s); } catch (e) { threw = String(e); }
            await settle();
            out[arm] = (threw === null && labelUntouched(w, s)) ? true
                : { threw, saves: w.saves, show: w.entries.rx.settings.show };
        }
        ok(allArms(out), '5a stale label-order rows (checkbox, up, down, drop) save nothing and change nothing on every arm', out);
    }
    {   // 5b the Browser tab keeps settingsTarget: only the tab test refuses there
        const w = mkWorld(); await paintRx(w);
        const s = labelSubject(w);
        await w.visit('browser');
        const kept = w.env.settingsTarget && w.env.settingsTarget.hostId === 'rx';
        fireLabelRows(w, s); await settle();
        ok(s.armed && kept && labelUntouched(w, s),
           '5b on the Browser tab (settingsTarget still rx\u2019s) a stale label-order row saves nothing',
           { armed: s.armed, kept, saves: w.saves });
    }
    {   // 5c the same tab re-selected while its /state is in flight: an old row's target is gone
        const w = mkWorld(); await paintRx(w);
        const s = labelSubject(w);
        w.mode = 'gate';
        w.fn.selectSettingsTab('rx');                    // in flight: settingsTarget is null
        const inflight = w.env.currentSettingsTab === 'rx' && w.env.settingsTarget === null;
        fireLabelRows(w, s); await settle();
        ok(s.armed && inflight && labelUntouched(w, s),
           '5c re-selecting the same tab while it loads leaves the old label-order rows unable to save',
           { armed: s.armed, inflight, saves: w.saves });
    }

    // ---- controls the diff review asked for: the WRITES THAT SHOULD HAPPEN still do ---
    {   // C8 label order, the three mechanisms, on the open host
        const w = mkWorld(); w.seed(['rd']);
        await w.visit('rd');
        const order = () => w.entries.rd.settings.show.order.join();
        const start = order();
        const row = (i) => w.env.labelOrderEl.children[i];
        fire(row(2).children[2], 'click');                       // pid up
        const afterUp = order();
        fire(row(0).children[3], 'click');                       // first row down
        const afterDown = order();
        fire(row(0), 'dragstart', { dataTransfer: null });       // drag the first row ...
        fire(row(3), 'drop', { clientY: 9 });                    // ... to after the last
        const afterDrop = order();
        ok(start === 'id,host,pid,title' && afterUp === 'id,pid,host,title'
           && afterDown === 'pid,id,host,title' && afterDrop === 'id,host,title,pid'
           && w.saves.length === 3 && w.saves.every(s => s === 'rd'),
           'C8 control: on the open host the label-order up, down and drag/drop each reorder and save once',
           { start, afterUp, afterDown, afterDrop, saves: w.saves });
    }
    {   // C9 the Start path debounce on the open host
        const w = mkWorld(); w.seed(['rd']);
        await w.visit('rd');
        const armed = typeStartPath(w, 'C:/typed-on-rd');
        runLiveTimers(w);
        ok(armed && w.entries.rd.settings.startPath === 'C:/typed-on-rd'
           && w.saves.length === 1 && w.saves[0] === 'rd' && w.env._startPathTimer === null,
           'C9 control: a Start path typed on the open host is saved to it once when the debounce fires',
           { armed, startPath: w.entries.rd.settings.startPath, saves: w.saves });
    }
    {   // 4k an Edit dialog's Save on the open host
        const w = mkWorld(); await paintRx(w);
        const dlg = openEdit(w, 'rx-beta');
        const name = findAll(dlg.body, c => c.tag === 'input' && /^name/.test(c.placeholder))[0];
        if (name) name.value = 'rx-gamma';
        const p0 = w.posts().length;
        dlg.finish({ value: 'save' }); await settle();
        const posts = w.posts().slice(p0);
        ok(!!name && posts.length === 1 && posts[0] === 'rx POST /profiles/config'
           && !!w.cfg.rx.profiles['rx-gamma'] && !w.cfg.rx.profiles['rx-beta'],
           'C10 control: on the open host an Edit dialog Save POSTs the renamed set to it once',
           { posts, keys: Object.keys(w.cfg.rx.profiles) });
    }
    {   // 4l a Detect suggestion on the open host
        const w = mkWorld(); await paintRx(w);
        fire(w.env.setProfileDetectBtn, 'click'); await settle(2);
        const list = w.dialogs[w.dialogs.length - 1];
        const sug = list ? findAll(list.body, c => c.className === 'set-detect-item')[0] : null;
        const n0 = w.dialogs.length;
        if (sug) fire(sug, 'click'); await settle();
        const add = w.dialogs[w.dialogs.length - 1];
        ok(!!sug && w.dialogs.length === n0 + 1 && add.spec.title === 'Add profile',
           'C11 control: on the open host a Detect suggestion opens the Add dialog',
           { sug: !!sug, title: add && add.spec && add.spec.title });
    }

    // ---- G2: two live /state GETs for the SAME host (round 1) -------------------------
    {
        const w = mkWorld(); w.seed(['rx', 'rd']);
        const entA = clone(w.entries.rx); entA.settings.startPath = 'C:/old-answer';
        const entC = clone(w.entries.rx); entC.settings.startPath = 'C:/new-answer';
        w.mode = 'gate';
        const pA = w.fn.selectSettingsTab('rx');        // the older GET for rx
        const pB = w.fn.selectSettingsTab('rd');
        const pC = w.fn.selectSettingsTab('rx');        // the newer GET for rx
        const rxGates = w.gates.filter(g => g.id === 'rx');
        const rdGate = w.gates.find(g => g.id === 'rd');
        const armed = rxGates.length === 2 && !!rdGate && entA.settings !== entC.settings
            && w.env.currentSettingsTab === 'rx' && w.env.settingsTarget === null;
        rxGates[1].res(entC); await settle();             // the newer answer lands first
        const target = w.env.settingsTarget;
        const newerFirst = !!target && target.s === entC.settings
            && w.env.setStartPathEl.value === 'C:/new-answer';
        rxGates[0].res(entA); await settle();             // the older one lands last
        rdGate.res(w.entries.rd); await pA; await pB; await pC; await settle();
        ok(armed && newerFirst && w.env.settingsTarget === target && target.s === entC.settings
           && w.env.setStartPathEl.value === 'C:/new-answer',
           'G2 an older /state answer for the same host landing last does not replace the newer target',
           { armed, newerFirst, startPath: w.env.setStartPathEl.value,
             isNewer: w.env.settingsTarget && w.env.settingsTarget.s === entC.settings });
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
    "G2 an older /state answer for the same host landing last does not replace the newer target",
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


@pytest.mark.parametrize("label", [
    "B1 a proxied tab holds nothing of the previous host: lists empty, Add/Detect disabled, no target",
    "B2 while /state is in flight the previous host’s rows are already gone",
    "B3 a failed direct tab holds nothing of the previous host",
    "B4 the Browser tab empties the rows but keeps settingsTarget (ruling: never nulled)",
    "H7 a successful switch ends with the new host’s rows (the blank is not applied after the repaint)",
    "P every painted colour picker is disposed and its cleanup ran on the switch; a late pick does nothing",
    "D1 an open Edit dialog is closed by a tab switch",
    "D2 an open Detect list is closed by a tab switch",
    "D3 a dialog that is not the panel’s survives a tab switch",
    "D4 re-selecting the same tab closes the profile dialog too",
    "B5 a section fetch landing after the tab was left paints nothing",
])
def test_blank_on_every_switch(run, label):
    _ok(run, label)


@pytest.mark.parametrize("label", [
    "S1 a Start path typed on rx lands on rx when the tab is left, and is never written as an empty string",
    "S2 blankHostSections cancels a pending Start path timer on its own (backstop)",
    "S3 closing the panel after switching to Browser leaves rx’s Start path as typed",
    "S4 a throwing Start path commit never blocks the switch: the tab changes, the rows are gone, the value is never blank",
])
def test_start_path_debounce(run, label):
    _ok(run, label)


@pytest.mark.parametrize("label", [
    "Q1a the MCP URL no longer names the previous host after a switch",
    "Q1a-ctl an uncached host’s MCP section shows its own connect URL, not a dash",
    "Q1b the default-profile select is back to the lone \"(broker default)\" option",
    "Q1c the MCP section is back to its neutral state: values, pins, placeholders and the copy row",
    "Q1d the static inputs (size, snap, slide, Start path, toggles, Confirm) are blank",
    "Q1e the not-loaded path of renderMcpConfig itself drops the previous host’s pins, ticks and error line",
    "Q1f a successful switch repaints the new host’s MCP section, default profile and static inputs",
    "K the keybinding rows of the previous host are gone on every arm, Browser included",
])
def test_inert_fields_are_blanked(run, label):
    _ok(run, label)


@pytest.mark.parametrize("label", [
    "4a a stale Delete posts nothing and leaves the list empty on every arm",
    "4b a stale Default posts nothing and leaves the list empty on every arm",
    "4c a stale colour pick posts nothing, does not throw, and leaves the list empty",
    "4d a stale colour clear posts nothing and leaves the list empty on every arm",
    "4e-open a stale Edit opens no dialog and posts nothing",
    "4e-save a dialog Save that lands after the switch posts nothing",
    "4f a Detect scan answering after the switch opens no list",
    "4g on the Browser tab a stale Delete or Default posts nothing (the guard follows the open tab, not settingsTarget)",
    "4h a refused write on Browser leaves the list empty and requests no /profiles/config",
    "4i a stale Detect suggestion opens no dialog",
    "4j control: on rx’s own open tab Default POSTs to rx exactly once",
])
def test_profile_writers_refuse_a_stale_host(run, label):
    _ok(run, label)


@pytest.mark.parametrize("label", [
    "5a stale label-order rows (checkbox, up, down, drop) save nothing and change nothing on every arm",
    "5b on the Browser tab (settingsTarget still rx’s) a stale label-order row saves nothing",
    "5c re-selecting the same tab while it loads leaves the old label-order rows unable to save",
])
def test_label_order_refuses_a_stale_target(run, label):
    _ok(run, label)


@pytest.mark.parametrize("label", [
    "C8 control: on the open host the label-order up, down and drag/drop each reorder and save once",
    "C9 control: a Start path typed on the open host is saved to it once when the debounce fires",
    "C10 control: on the open host an Edit dialog Save POSTs the renamed set to it once",
    "C11 control: on the open host a Detect suggestion opens the Add dialog",
])
def test_the_writes_that_should_happen_still_do(run, label):
    _ok(run, label)


# U1 -- source-shape, and the only non-behavioural check here: a slice harness
# cannot see a duplicate top-level declaration in the assembled page (a parse
# error there would take the whole panel down while every node cell passed).
NEW_NAMES = ("blankHostSections", "blankInertHostFields", "mcpShowNeutral",
             "cancelProfileDialog", "profileHostIsCurrent",
             "refuseStaleProfileHost", "_profileDlgFin")


@pytest.mark.parametrize("name", NEW_NAMES)
def test_u1_new_top_level_names_are_declared_exactly_once(name):
    pat = re.compile(r"(?:\bfunction\s+|\b(?:let|const|var)\s+)" + re.escape(name) + r"\b")
    hits = {p.name: len(pat.findall(p.read_text(encoding="utf-8")))
            for p in sorted(BROKER_DIR.glob("*.js"))}
    hits = {k: v for k, v in hits.items() if v}
    assert sum(hits.values()) == 1, f"{name} is declared {hits}"


def test_every_scenario_passed_and_node_exited_clean(run):
    assert run["failed"] == 0, run["stdout"] + run["stderr"]
    assert run["passed"] == EXPECTED_ASSERTS, run["stdout"]
    assert len(run["results"]) == EXPECTED_ASSERTS
    assert run["rc"] == 0, run["stderr"]
    assert "HARNESS CRASH" not in run["stdout"], run["stdout"]
