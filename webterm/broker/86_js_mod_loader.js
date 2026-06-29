        // ---- frontend mod loader (#71) ------------------------------------
        // Phase-1 extension surface. A "mod" is in-repo, reviewed, first-party
        // JS that registers itself with registerMod({id, ctxVersion, init}) and
        // is handed a scoped `ctx` at init. This is conflict-avoidance + review
        // hygiene for TRUSTED code, NOT a security sandbox: a same-origin mod
        // already holds the auth token and could call /file, /launch, /state and
        // MCP directly regardless of what ctx hands it. The only real control is
        // "don't merge a mod that does X" — hence in-repo + reviewed + the
        // mods_enabled master switch (default on; the broker can force it off).
        //
        // Assembly: ui.py concatenates the loader, then every mod script (which
        // call registerMod during page-script eval), then 90_js_mod_boot.js's
        // loadMods(), into the one <script>. So registration is synchronous at
        // parse time and loadMods() runs once, last, after all of it.
        //
        // TDZ NOTE: localInfo()/notifyModSettings() are CALLED from earlier
        // fragments (83 broker-identity, 65 theming -> 85 startup) which execute
        // BEFORE this fragment's top-level `window.__mods = ...` assignment runs.
        // They are `function` declarations (hoisted) and their bodies touch only
        // globals / function-properties / `window.__mods` (a plain property =>
        // `undefined`, never a TDZ ReferenceError) before guarding — keep it so.
        window.__mods = window.__mods || {
            ctxVersion: 1,            // bump when the ctx/win contract changes
            registered: [],           // [{id, version, ctxVersion, init}] in decl order
            active: new Map(),        // id -> { id, version, unloads:[] }  (the "slot")
            settingToggles: [],       // [{modId, key, read, onChange, last, checkbox, section}]
            booted: false,
        };

        // A duplicate id / slot claim is a hard error (no silent last-wins).
        function ModConflictError(message) {
            const e = new Error(message);
            e.name = 'ModConflictError';
            return e;
        }

        // Called by each mod script during page-script eval. Records the
        // declaration; init happens later in loadMods()/initMod() so a throw
        // here can't be triggered by normal first-party mods (the conflict +
        // version + init-throw isolation all live in initMod). A genuinely
        // malformed/duplicate declaration throws synchronously so it surfaces in
        // review; real shipped mods never hit it.
        function registerMod(decl) {
            if (!decl || typeof decl !== 'object') {
                throw new Error('registerMod: declaration must be an object');
            }
            const id = decl.id;
            if (typeof id !== 'string' || !id) {
                throw new Error('registerMod: a non-empty string id is required');
            }
            if (typeof decl.init !== 'function') {
                throw new Error('registerMod[' + id + ']: init(ctx) must be a function');
            }
            const reg = window.__mods.registered;
            if (reg.some((m) => m.id === id)) {
                throw ModConflictError('registerMod: duplicate mod id "' + id + '"');
            }
            reg.push({
                id: id,
                version: (typeof decl.version === 'string') ? decl.version : '0',
                // null = "pin nothing" (still init); a number must match exactly.
                ctxVersion: (typeof decl.ctxVersion === 'number') ? decl.ctxVersion : null,
                init: decl.init,
            });
        }

        // LIFO teardown: run a mod's onUnload callbacks newest-first, each
        // isolated, mirroring win.cleanups (73_js_window_runtime.js:439). Drains
        // the list so a re-run is a no-op.
        function _runUnloads(rec) {
            const fns = rec.unloads.splice(0).reverse();
            for (const fn of fns) { try { fn(); } catch (_) {} }
        }

        // Build the per-mod ctx (contract v1). Organizational scoping only — see
        // the trust note above. `rec` is the active-mod record initMod created.
        function makeCtx(modId, rec) {
            const ns = 'webterm:mod:' + modId + ':';
            return {
                id: modId,
                ctxVersion: window.__mods.ctxVersion,
                // Register a teardown fn (reverses init); LIFO, isolated.
                onUnload: function (fn) {
                    if (typeof fn === 'function') rec.unloads.push(fn);
                },
                // Namespaced localStorage (webterm:mod:<id>:<key>) for per-mod
                // prefs a mod does NOT want synced to /state.
                storage: {
                    get: function (key) {
                        try { return localStorage.getItem(ns + key); }
                        catch (_) { return null; }
                    },
                    set: function (key, value) {
                        try { localStorage.setItem(ns + key, value); } catch (_) {}
                    },
                    remove: function (key) {
                        try { localStorage.removeItem(ns + key); } catch (_) {}
                    },
                },
                taskbar: {
                    // Mount a status node in the taskbar (before #help-chip, so a
                    // mod keeps the old clock slot). Auto-removed on teardown.
                    addStatusItem: function (node) {
                        return _modAddStatusItem(rec, node);
                    },
                },
                settings: {
                    // Read-through accessor onto the EXISTING shared /state
                    // settings blob (NOT namespaced localStorage) + a Control
                    // Panel checkbox in #set-mods. This is the one ctx primitive
                    // that touches the synced blob: it lets the clock keep its
                    // cross-browser /state sync via the existing `clock` key
                    // without a new schema field. get() never writes the default.
                    boolean: function (key, def, opts) {
                        return _modSettingBoolean(rec, key, def, opts);
                    },
                },
            };
        }

        function _modAddStatusItem(rec, node) {
            const bar = document.getElementById('taskbar');
            if (!bar || !node) {
                throw new Error('addStatusItem: taskbar or node missing');
            }
            const helpChip = document.getElementById('help-chip');
            bar.insertBefore(node, helpChip || null);   // null => append at end
            const handle = {
                remove: function () {
                    try { if (node.parentNode) node.parentNode.removeChild(node); }
                    catch (_) {}
                },
            };
            rec.unloads.push(handle.remove);   // torn down with the mod
            return handle;
        }

        function _modSettingBoolean(rec, key, def, opts) {
            opts = opts || {};
            const fallback = !!def;
            // Non-destructive read: returns the live value or the default, never
            // writes — so an upgrading user's existing `clock:true` is preserved
            // and a brand-new key is not forced into the synced blob by a read.
            const read = function () {
                const v = getSettings()[key];
                return (typeof v === 'boolean') ? v : fallback;
            };
            const entry = {
                modId: rec.id, key: key, read: read,
                onChange: null, last: read(),
                checkbox: null, section: null,
            };
            const accessor = {
                get: function () { return read(); },
                set: function (value) {
                    const v = !!value;
                    if (read() === v) return;        // no spurious savePrefs/push
                    getSettings()[key] = v;
                    savePrefs();                     // localStorage + /state push
                    entry.last = v;
                    if (entry.checkbox) entry.checkbox.checked = v;
                    if (entry.onChange) { try { entry.onChange(v); } catch (_) {} }
                },
                onChange: function (fn) {
                    entry.onChange = (typeof fn === 'function') ? fn : null;
                    return accessor;
                },
            };
            // Mount a Control Panel checkbox into #set-mods (browser-global; the
            // section is hidden on remote host tabs via .set-browser-global).
            const host = document.getElementById('set-mods');
            if (host) {
                const section = document.createElement('div');
                section.className = 'set-section set-mod-setting';
                section.dataset.modId = rec.id;
                if (opts.title) {
                    const t = document.createElement('div');
                    t.className = 'set-title';
                    t.textContent = opts.title;
                    section.appendChild(t);
                }
                const label = document.createElement('label');
                label.className = 'set-check';
                const checkbox = document.createElement('input');
                checkbox.type = 'checkbox';
                checkbox.checked = read();
                label.appendChild(checkbox);
                label.appendChild(
                    document.createTextNode(' ' + (opts.label || key)));
                section.appendChild(label);
                host.appendChild(section);
                checkbox.addEventListener('change', function () {
                    accessor.set(checkbox.checked);
                });
                entry.checkbox = checkbox;
                entry.section = section;
            }
            window.__mods.settingToggles.push(entry);
            // Teardown: drop the checkbox + forget the toggle.
            rec.unloads.push(function () {
                if (entry.section && entry.section.parentNode) {
                    entry.section.parentNode.removeChild(entry.section);
                }
                const list = window.__mods.settingToggles;
                const i = list.indexOf(entry);
                if (i !== -1) list.splice(i, 1);
            });
            return accessor;
        }

        // Fire mod settings onChange callbacks on convergence (boot + every
        // /state pull, via applyThemeSettings). Fire-on-change only: the clock's
        // apply() is idempotent, so an unchanged pull is a cheap no-op and the
        // running 1s interval is left alone. Guarded so the early-boot call (from
        // 85_js_startup.js, before this fragment ran) is a clean no-op.
        function notifyModSettings() {
            if (!window.__mods) return;
            const list = window.__mods.settingToggles;
            for (let i = 0; i < list.length; i++) {
                const t = list[i];
                let cur;
                try { cur = t.read(); } catch (_) { continue; }
                if (cur === t.last) continue;
                t.last = cur;
                if (t.checkbox) t.checkbox.checked = cur;
                if (t.onChange) {
                    try { t.onChange(cur); }
                    catch (e) {
                        console.error('[mods] settings onChange failed ("'
                            + t.modId + ':' + t.key + '"):', e);
                    }
                }
            }
        }

        // Reflect mod-setting checkboxes from the live settings when the Control
        // Panel (re)renders. Called from renderSettings (81_js_control_panel.js).
        // Reflect-only: the checkboxes are created by each mod's init (they
        // persist across renders and across panel open/close), so this never
        // rebuilds #set-mods or rebinds handlers. The section is browser-global,
        // hidden on remote tabs by applyBrowserGlobalVisibility; we still reflect
        // (harmless) so reopening on the local tab is always current.
        function renderModSettingsToggles(isLocal) {
            if (!window.__mods) return;
            const list = window.__mods.settingToggles;
            for (let i = 0; i < list.length; i++) {
                const t = list[i];
                if (t.checkbox) {
                    try { t.checkbox.checked = !!t.read(); } catch (_) {}
                }
            }
        }

        // Memoized single GET /info, fail-open / default-on. Shared by the
        // mods_enabled gate (loadMods) and learnLocalBrokerId (#64) so /info is
        // fetched once. The memo lives on the function (localInfo._p) so it is
        // safe to call before this fragment's top-level code runs (no TDZ).
        // Returns the parsed object, or {} on any failure (=> mods stay enabled,
        // broker_id stays unknown) — same best-effort posture probeBrokerId had.
        function localInfo() {
            if (!localInfo._p) {
                localInfo._p = (async function () {
                    try {
                        const r = await fetch(hostHttpUrl(localHost(), '/info'));
                        if (!r.ok) return {};
                        const j = await r.json();
                        return (j && typeof j === 'object') ? j : {};
                    } catch (_) { return {}; }
                })();
            }
            return localInfo._p;
        }

        // Init one mod through the full isolation path. Returns a structured
        // result (never throws) so loadMods() and the test API can both drive it:
        //   ctxVersion mismatch  -> refused, no init/mount     (reason 'ctxVersion')
        //   duplicate id/slot    -> ModConflictError, no last-wins (reason 'conflict')
        //   init() throws        -> rolled back via onUnload    (reason 'init-threw')
        // Siblings and core are unaffected in every case.
        function initMod(decl) {
            const id = decl && decl.id;
            if (typeof id !== 'string' || !id) {
                const e = new Error('initMod: a non-empty string id is required');
                console.error('[mods]', e.message);
                return { ok: false, reason: 'invalid', error: e };
            }
            const want = (typeof decl.ctxVersion === 'number') ? decl.ctxVersion : null;
            if (want !== null && want !== window.__mods.ctxVersion) {
                console.warn('[mods] refusing "' + id + '": ctxVersion ' + want
                    + ' != loader ' + window.__mods.ctxVersion);
                return { ok: false, reason: 'ctxVersion' };
            }
            if (window.__mods.active.has(id)) {
                const e = ModConflictError('mod "' + id + '" is already active');
                console.error('[mods]', e.message);
                return { ok: false, reason: 'conflict', error: e };
            }
            if (typeof decl.init !== 'function') {
                const e = new Error('mod "' + id + '" has no init(ctx) function');
                console.error('[mods]', e.message);
                return { ok: false, reason: 'invalid', error: e };
            }
            // Claim the slot (the id) BEFORE init so addStatusItem etc. can find
            // the record; on any init throw we roll back and release it.
            const rec = { id: id, version: (decl.version || '0'), unloads: [] };
            window.__mods.active.set(id, rec);
            let ctx;
            try {
                ctx = makeCtx(id, rec);
                decl.init(ctx);
            } catch (e) {
                console.error('[mods] init failed for "' + id
                    + '" — disabling it (core + other mods continue):', e);
                _runUnloads(rec);                  // reverse any partial init
                window.__mods.active.delete(id);   // release the slot
                return { ok: false, reason: 'init-threw', error: e };
            }
            return { ok: true, id: id };
        }

        // Fully reverse a mod's init: run its teardown (timers/listeners/DOM/
        // settings checkbox) and release its slot. Idempotent.
        function disableMod(id) {
            const rec = window.__mods.active.get(id);
            if (!rec) return false;
            _runUnloads(rec);
            window.__mods.active.delete(id);
            return true;
        }

        // The boot entry (called once by 90_js_mod_boot.js). Gates on the
        // broker's mods_enabled (runtime, via the memoized /info; fail-open) then
        // inits every registered mod, each isolated.
        async function loadMods() {
            if (window.__mods.booted) return;
            window.__mods.booted = true;
            let enabled = true;
            try {
                const info = await localInfo();
                if (info && info.mods_enabled === false) enabled = false;
            } catch (_) {}
            if (!enabled) {
                console.info('[mods] disabled by broker (mods_enabled=false)');
                return;
            }
            for (const decl of window.__mods.registered.slice()) {
                initMod(decl);
            }
        }

        // Test API (#71 acceptance: isolation / duplicate-conflict / version-
        // refusal / teardown). Drives the SAME initMod/disableMod paths the real
        // boot uses, so the Playwright checks exercise production code, not a
        // parallel harness.
        window.__mods.__test = {
            ctxVersion: function () { return window.__mods.ctxVersion; },
            run: function (decl) { return initMod(decl); },
            disable: function (id) { return disableMod(id); },
            isActive: function (id) { return window.__mods.active.has(id); },
            active: function () { return Array.from(window.__mods.active.keys()); },
            get: function (id) { return window.__mods.active.get(id) || null; },
        };
