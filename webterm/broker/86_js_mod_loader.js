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
            settingToggles: [],       // mod Control Panel controls: [{modId, kind, key, read, reflect, onChange, last, section}] (kind: boolean|radio|select|pane)
            helpCards: [],            // #78: mod-contributed Help cards (ctx.registerHelpCards), sanitized typed entries the Help mod merges with the core corpus
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
                // #82 (S9): host filesystem access — ONE reviewed wrapper over
                // the existing /file/* API (read/write/list/delete/upload) with
                // the same per-host routing core uses (fileApiPost + the editor's
                // fileHostId resolution).
                //
                // TRUST TIER — this is operator-granted REVIEW HYGIENE, NOT a
                // permission boundary or sandbox. A same-origin mod already holds
                // the page's auth token and can POST to /file/* directly, exactly
                // like core does; ctx.file grants NO new privilege and confines
                // nothing. Its only job is to route every mod's filesystem access
                // through one reviewed, host-aware choke point, so "don't merge a
                // mod that reads ~/.ssh" stays a code-review decision (the only
                // real control). Paths are host-wide ABSOLUTE native paths (or a
                // relative path / '' resolved against the broker's default dir);
                // there is NO editor_root confinement (app.py:_resolve_host_path,
                // #35). Every call resolves to a parsed result object — never a
                // rejected promise — {ok:true,...} or {ok:false,error}. opts.host
                // is a host *id* string (win.fileHostId semantics), NOT a host
                // object: a known id routes to that broker, ''/'local'/omitted ->
                // the local broker, and an UNKNOWN remote id FAILS CLOSED with
                // {ok:false,error:'host_not_found'} (no request made) rather than
                // silently writing to local. Feature-detect with `if (ctx.file)`
                // (ctxVersion stays 1 — this is an additive capability).
                file: {
                    // Read text, or {b64:true} for base64. -> {ok,path,content}
                    // (or {ok,path,content_b64} in b64 mode).
                    read: function (path, opts) {
                        const body = { path: path };
                        if (opts && opts.b64 === true) body.b64 = true;
                        return _modFileApi('/file/read', body, opts);
                    },
                    // UTF-8 text write (atomic, host-wide). -> {ok,path}
                    write: function (path, content, opts) {
                        return _modFileApi('/file/write',
                            { path: path, content: content }, opts);
                    },
                    // Directory listing (''/relative -> the broker default dir).
                    // -> {ok,root,cwd,parent,entries:[{name,type,size}]}
                    list: function (path, opts) {
                        return _modFileApi('/file/list', { path: path || '' }, opts);
                    },
                    // Single-file delete (refuses a directory). -> {ok,path}.
                    // Quoted key so the reserved word can never trip a minifier;
                    // ctx.file.delete(...) still calls it.
                    'delete': function (path, opts) {
                        return _modFileApi('/file/delete', { path: path }, opts);
                    },
                    // Binary-safe write via base64; opts.overwrite (default false)
                    // -> {ok,path,size} (or {ok:false,error:'exists'} on a clash).
                    upload: function (path, contentB64, opts) {
                        return _modFileApi('/file/upload',
                            { path: path, content_b64: contentB64,
                              overwrite: !!(opts && opts.overwrite) }, opts);
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
                    // Read-through accessors onto the EXISTING shared /state
                    // settings blob (NOT namespaced localStorage) + a Control
                    // Panel control in #set-mods. These are the ctx primitives
                    // that touch the synced blob: they let a mod own an existing
                    // settings key with cross-browser /state sync, without a new
                    // schema field. Every get()/read is non-destructive (never
                    // writes the default). All return {get,set,onChange}.
                    //   boolean(key, def, {label,title,isBrowserGlobal})  -> checkbox
                    //   radio(key, [{value,label}], {label,title,def,isBrowserGlobal})
                    //   select(key, [{value,label}], {label,title,def,isBrowserGlobal})
                    boolean: function (key, def, opts) {
                        return _modSettingBoolean(rec, key, def, opts);
                    },
                    radio: function (key, options, opts) {
                        return _modSettingChoice(rec, 'radio', key, options, opts);
                    },
                    select: function (key, options, opts) {
                        return _modSettingChoice(rec, 'select', key, options, opts);
                    },
                },
                // #74: a full custom Control Panel section, for controls richer
                // than a single boolean/radio/select. spec.render() builds the
                // widget DOM (returned node is appended); spec.reflect(settings)
                // syncs it on Control Panel open AND on every /state convergence,
                // so it must be idempotent and preserve any in-progress edit.
                // Browser-global by default (hidden on remote host tabs).
                registerSettingsPane: function (spec) {
                    return _modRegisterPane(rec, spec);
                },
                // #78 (S5): contribute typed Help cards. Each card is a DOM-safe
                // block/span schema (NEVER raw HTML) — same typed shape as the
                // wiki corpus:
                //   { slug, section, title, body:[block], keys?, search? }
                //   block = { t:'p'|'bullet'|'sub', spans:[span] }
                //   span  = { t:'text'|'strong'|'code'|'kbd', v:String }
                // Cards are sanitized here (unknown block/span types degrade to
                // text; values are coerced to String) and merged into the Help
                // window by the help mod. Removed on teardown; if Help is open
                // when a mod (un)registers, it re-renders.
                registerHelpCards: function (cards) {
                    return _modRegisterHelpCards(rec, cards);
                },
                // #80 (S7): register a brand-new app window kind in one call —
                // factory (open), serialize (persist), restore?, retainOnClose
                // (× keep-vs-discard), and a (+) launch-menu entry. Built on the
                // shared S6 chrome factory (buildAppChrome/addResizeHandles/
                // wireAppChrome). The same registry the six core built-ins use, so
                // a mod kind is a first-class window everywhere the registry is
                // consulted. A duplicate appKind throws (initMod rolls the mod
                // back); the kind is removed from the registry on teardown.
                registerWindowKind: function (spec) {
                    return _modRegisterWindowKind(rec, spec);
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

        // ---- Control Panel settings extension (#71 boolean; #74 radio/select/
        // pane) --------------------------------------------------------------
        // Shared scaffold: a titled .set-section mounted into #set-mods. It is
        // browser-global (hidden on remote host tabs by applyBrowserGlobalVisi-
        // bility, 81_js_control_panel.js) unless opts.isBrowserGlobal === false,
        // so a non-global mod control can stay visible on a remote tab. The
        // section-removal teardown is registered IMMEDIATELY so a throw while a
        // caller is still building its widget still rolls the section back. No
        // applyBrowserGlobalVisibility pass runs on append, so we match the live
        // tab's visibility now to avoid a flash if a section is mounted while the
        // panel is open on a remote tab (#74); renderSettings re-syncs later.
        function _controlSection(rec, opts) {
            const host = document.getElementById('set-mods');
            const section = document.createElement('div');
            section.className = 'set-section set-mod-setting';
            section.dataset.modId = rec.id;
            if (opts.isBrowserGlobal !== false) {
                section.classList.add('set-browser-global');
                try {
                    if (currentSettingsTab !== 'local') section.style.display = 'none';
                } catch (_) {}
            }
            if (opts.title) {
                const t = document.createElement('div');
                t.className = 'set-title';
                t.textContent = opts.title;
                section.appendChild(t);
            }
            if (host) host.appendChild(section);
            rec.unloads.push(function () {
                if (section.parentNode) section.parentNode.removeChild(section);
            });
            return section;
        }

        // Record a control entry on the shared list + wire its forget-on-teardown
        // (the DOM is removed by _controlSection's own onUnload). The list drives
        // reflect-on-open (renderModSettingsToggles) and /state convergence
        // (notifyModSettings).
        function _trackControl(rec, entry) {
            window.__mods.settingToggles.push(entry);
            rec.unloads.push(function () {
                const list = window.__mods.settingToggles;
                const i = list.indexOf(entry);
                if (i !== -1) list.splice(i, 1);
            });
        }

        // Shared {get,set,onChange} for a value control (boolean/radio/select).
        // set() is the ONLY ctx path that writes the synced blob: it coerces +
        // validates, no-ops on an unchanged value (no spurious savePrefs/push),
        // then writes getSettings()[key] + savePrefs(), updates `last` BEFORE
        // anything else can observe it (so the /state convergence pass won't
        // re-fire), reflects the widget, and calls the mod's onChange.
        function _valueAccessor(entry, key, read, coerce, valid) {
            const accessor = {
                get: function () { return read(); },
                set: function (value) {
                    value = coerce(value);
                    if (!valid(value)) return;
                    if (read() === value) return;
                    getSettings()[key] = value;
                    savePrefs();                 // localStorage + /state push
                    entry.last = value;
                    entry.reflect();
                    if (entry.onChange) { try { entry.onChange(value); } catch (_) {} }
                },
                onChange: function (fn) {
                    entry.onChange = (typeof fn === 'function') ? fn : null;
                    return accessor;
                },
            };
            return accessor;
        }

        function _modSettingBoolean(rec, key, def, opts) {
            opts = opts || {};
            const fallback = !!def;
            // Non-destructive read: live value or the default, never written — an
            // upgrading user's existing `clock:true` is preserved and a brand-new
            // key is not forced into the synced blob by a read / a panel open.
            const read = function () {
                const v = getSettings()[key];
                return (typeof v === 'boolean') ? v : fallback;
            };
            const section = _controlSection(rec, opts);
            const label = document.createElement('label');
            label.className = 'set-check';
            const checkbox = document.createElement('input');
            checkbox.type = 'checkbox';
            label.appendChild(checkbox);
            label.appendChild(document.createTextNode(' ' + (opts.label || key)));
            section.appendChild(label);
            const entry = {
                modId: rec.id, kind: 'boolean', key: key, read: read,
                onChange: null, last: read(), section: section,
                reflect: function () { checkbox.checked = read(); },
            };
            const accessor = _valueAccessor(entry, key, read,
                function (v) { return !!v; },           // coerce
                function () { return true; });          // valid (any boolean)
            checkbox.addEventListener('change', function () {
                accessor.set(checkbox.checked);
            });
            entry.reflect();                            // sync the box now
            _trackControl(rec, entry);
            return accessor;
        }

        // Validate + normalize a radio/select options list to [{value,label}]
        // with STRING values (DOM widget values are strings; mixing in numbers/
        // booleans would silently fall back to the default — reject loudly).
        // Duplicate/empty/invalid lists throw, which disables just this mod
        // (initMod rolls it back); core + other mods continue.
        function _normChoiceOptions(options) {
            if (!Array.isArray(options) || !options.length) {
                throw new Error('settings radio/select: options must be a '
                    + 'non-empty array of {value,label}');
            }
            const seen = {};
            const out = [];
            for (const o of options) {
                if (!o || typeof o.value !== 'string') {
                    throw new Error('settings radio/select: each option needs a '
                        + 'string value');
                }
                if (seen[o.value]) {
                    throw new Error('settings radio/select: duplicate option '
                        + 'value "' + o.value + '"');
                }
                seen[o.value] = true;
                out.push({ value: o.value,
                           label: (typeof o.label === 'string') ? o.label : o.value });
            }
            return out;
        }

        // A radio group or <select> bound to an existing synced settings key.
        // Same read-through, non-destructive, savePrefs-on-change contract as
        // boolean — only the widget differs. read() returns the live value when
        // it is a known option, else the (non-written) default.
        function _modSettingChoice(rec, kind, key, options, opts) {
            opts = opts || {};
            options = _normChoiceOptions(options);
            const valid = {};
            for (const o of options) valid[o.value] = true;
            const fallback = (typeof opts.def === 'string' && valid[opts.def])
                ? opts.def : options[0].value;
            const read = function () {
                const v = getSettings()[key];
                return (typeof v === 'string' && valid[v]) ? v : fallback;
            };
            const section = _controlSection(rec, opts);
            let reflect, bindChange;
            if (kind === 'radio') {
                const group = document.createElement('div');
                group.className = 'set-row set-mod-radio';
                const name = 'set-mod-' + rec.id + '-' + key;   // unique per control
                const inputs = [];
                for (const o of options) {
                    const lab = document.createElement('label');
                    const rb = document.createElement('input');
                    rb.type = 'radio'; rb.name = name; rb.value = o.value;
                    const span = document.createElement('span');
                    span.textContent = o.label;
                    lab.appendChild(rb); lab.appendChild(span);
                    group.appendChild(lab);
                    inputs.push(rb);
                }
                section.appendChild(group);
                reflect = function () {
                    const cur = read();
                    for (const rb of inputs) rb.checked = (rb.value === cur);
                };
                bindChange = function (accessor) {
                    for (const rb of inputs) {
                        rb.addEventListener('change', function () {
                            if (rb.checked) accessor.set(rb.value);
                        });
                    }
                };
            } else {
                const row = document.createElement('div');
                row.className = 'set-row';
                if (opts.label) {
                    const lab = document.createElement('label');
                    lab.textContent = opts.label;
                    row.appendChild(lab);
                }
                const sel = document.createElement('select');
                for (const o of options) {
                    const op = document.createElement('option');
                    op.value = o.value; op.textContent = o.label;
                    sel.appendChild(op);
                }
                row.appendChild(sel);
                section.appendChild(row);
                reflect = function () { sel.value = read(); };
                bindChange = function (accessor) {
                    sel.addEventListener('change', function () {
                        accessor.set(sel.value);
                    });
                };
            }
            const entry = {
                modId: rec.id, kind: kind, key: key, read: read,
                onChange: null, last: read(), section: section, reflect: reflect,
            };
            const accessor = _valueAccessor(entry, key, read,
                function (v) { return v; },                 // strings, no coercion
                function (v) { return valid[v] === true; });
            bindChange(accessor);            // listeners AFTER the accessor exists
            entry.reflect();                 // sync widget to the current value
            _trackControl(rec, entry);
            return accessor;
        }

        // A full custom Control Panel section (#74). render() builds the widget
        // DOM (returned node is appended); reflect(settings) syncs it on open and
        // on every /state convergence — it MUST be idempotent and preserve any
        // in-progress edit (e.g. skip a focused field), since it runs on every
        // pull. render() throwing disables just this mod (the section, already
        // mounted with its rollback wired, is removed); core + siblings continue.
        function _modRegisterPane(rec, spec) {
            spec = spec || {};
            if (typeof spec.render !== 'function') {
                throw new Error('registerSettingsPane[' + rec.id
                    + ']: render() must be a function');
            }
            const section = _controlSection(rec,
                { title: spec.title, isBrowserGlobal: spec.isBrowserGlobal });
            if (spec.id) section.dataset.paneId = spec.id;
            const node = spec.render();      // throw => initMod rolls back section
            if (node && node.nodeType) section.appendChild(node);
            const reflect = function () {
                if (typeof spec.reflect !== 'function') return;
                try { spec.reflect(getSettings()); }
                catch (e) {
                    console.error('[mods] settings pane reflect failed ("'
                        + rec.id + ':' + (spec.id || '') + '"):', e);
                }
            };
            const entry = {
                modId: rec.id, kind: 'pane', key: null, read: null,
                onChange: null, last: null, section: section, reflect: reflect,
            };
            entry.reflect();                 // initial sync
            _trackControl(rec, entry);
            return { id: spec.id || null, section: section };
        }

        // ---- Help-card contribution (#78 / S5) ------------------------------
        // ctx.registerHelpCards sanitizes mod-supplied cards to the SAME typed
        // block/span schema the wiki corpus uses, so a contributed card can only
        // ever be rendered as text nodes (the help renderer is textContent-only)
        // — never raw HTML. Unknown block/span types degrade to the nearest safe
        // type; every value is coerced to String. The sanitized entries live on
        // window.__mods.helpCards (the Help mod merges them with the core
        // corpus) and are removed on the contributing mod's teardown.
        const _HELP_BLOCK_TYPES = { p: 1, bullet: 1, sub: 1 };
        const _HELP_SPAN_TYPES = { text: 1, strong: 1, code: 1, kbd: 1 };
        function _sanitizeHelpSpan(sp) {
            if (!sp || typeof sp !== 'object') return null;
            const t = _HELP_SPAN_TYPES[sp.t] ? sp.t : 'text';
            return { t: t, v: sp.v == null ? '' : String(sp.v) };
        }
        function _sanitizeHelpBlock(blk) {
            if (!blk || typeof blk !== 'object') return null;
            const t = _HELP_BLOCK_TYPES[blk.t] ? blk.t : 'p';
            const spans = [];
            const raw = Array.isArray(blk.spans) ? blk.spans : [];
            for (let i = 0; i < raw.length; i++) {
                const s = _sanitizeHelpSpan(raw[i]);
                if (s) spans.push(s);
            }
            return { t: t, spans: spans };
        }
        function _sanitizeHelpBlocks(body) {
            const out = [];
            const raw = Array.isArray(body) ? body : [];
            for (let i = 0; i < raw.length; i++) {
                const b = _sanitizeHelpBlock(raw[i]);
                if (b) out.push(b);
            }
            return out;
        }
        // One card -> a normalized Help entry, or null when it lacks a title (the
        // minimum to render). `search` defaults to the title/section/keys + all
        // sanitized body text, lower-cased, so a contributed card is discoverable
        // by its body even when the mod omits an explicit search string.
        function _sanitizeHelpCard(card, modId) {
            if (!card || typeof card !== 'object') return null;
            const title = card.title == null ? '' : String(card.title);
            if (!title) return null;
            const slug = card.slug == null ? ('mod-' + modId) : String(card.slug);
            const section = card.section == null ? (slug || modId) : String(card.section);
            const keys = card.keys == null ? '' : String(card.keys);
            const bodyFrags = _sanitizeHelpBlocks(
                card.body != null ? card.body : card.bodyFrags);
            let search = card.search == null ? '' : String(card.search);
            if (!search) {
                const parts = [title, section, keys];
                for (const b of bodyFrags) for (const s of b.spans) parts.push(s.v);
                search = parts.join(' ');
            }
            return { modId: modId, slug: slug, section: section, title: title,
                     bodyFrags: bodyFrags, keys: keys, search: search.toLowerCase() };
        }
        // Re-render the live Help window (if any) so newly (un)registered cards
        // appear without a reopen. findHelpWindow/refreshHelpCorpus are hoisted
        // from the help mod; typeof-guarded so an absent/disabled help mod is a
        // clean no-op.
        function _refreshHelpIfOpen() {
            try {
                if (typeof findHelpWindow === 'function'
                    && typeof refreshHelpCorpus === 'function') {
                    const w = findHelpWindow();
                    if (w) refreshHelpCorpus(w);
                }
            } catch (_) {}
        }
        function _modRegisterHelpCards(rec, cards) {
            if (!Array.isArray(window.__mods.helpCards)) window.__mods.helpCards = [];
            const list = Array.isArray(cards) ? cards : [cards];
            const added = [];
            for (let i = 0; i < list.length; i++) {
                const norm = _sanitizeHelpCard(list[i], rec.id);
                if (norm) { window.__mods.helpCards.push(norm); added.push(norm); }
            }
            // Forget exactly these entries on teardown (the DOM is re-rendered by
            // _refreshHelpIfOpen), then refresh the open Help window.
            rec.unloads.push(function () {
                const reg = window.__mods.helpCards || [];
                for (const e of added) {
                    const idx = reg.indexOf(e);
                    if (idx !== -1) reg.splice(idx, 1);
                }
                _refreshHelpIfOpen();
            });
            _refreshHelpIfOpen();
            return added.length;
        }

        // ---- window-kind contribution (#80 / S7) ----------------------------
        // ctx.registerWindowKind hands a mod the SAME core registry the six built-
        // ins use (registerWindowKind in 54_js_app_windows_store.js). The core call
        // validates the spec and throws ModConflictError on a duplicate appKind —
        // that throw propagates out of init() so initMod rolls the whole mod back
        // (no half-registered kind). On success we wire teardown IMMEDIATELY (before
        // the mod's init does anything else) so a LATER init throw, or a normal
        // disable, removes exactly THIS registration — guarded by entry identity so
        // a re-register by someone else is never clobbered. Live windows of the kind
        // are the mod's own responsibility to close in onUnload; the registry just
        // forgets the kind, so closing one afterwards no longer serializes/retains.
        function _modRegisterWindowKind(rec, spec) {
            const entry = registerWindowKind(spec);   // throws => initMod rolls back
            rec.unloads.push(function () {
                deleteWindowKind(entry.appKind, entry);
            });
            return entry;
        }

        // ---- ctx.file host routing (#82 / S9) -------------------------------
        // Resolve a mod-supplied host *id* (win.fileHostId semantics) to the host
        // OBJECT fileApiPost wants, FAIL CLOSED — identical to the editor's
        // fileHost() (70_js_editor_app.js): a known id -> that host; ''/'local'/
        // undefined -> the local broker; an UNKNOWN remote id -> null so the op
        // aborts rather than silently hitting local (a remote absolute path can
        // also exist under the local root, so a fallback could clobber a LOCAL
        // file).
        function _modFileHost(hostId) {
            const h = hostById(hostId);
            if (h) return h;
            if (!hostId || hostId === 'local') return localHost();
            return null;
        }
        // POST a /file/* route through the resolved host. fileApiPost already
        // resolves to a parsed object on every transport/HTTP error (never
        // rejects), so the only synthetic result is the fail-closed host: a
        // resolved {ok:false,error:'host_not_found'} with NO request sent.
        function _modFileApi(route, body, opts) {
            const host = _modFileHost(opts && opts.host);
            if (!host) {
                return Promise.resolve({ ok: false, error: 'host_not_found' });
            }
            return fileApiPost(route, body, host);
        }

        // Fire mod controls on convergence (boot + every /state pull, via
        // applyThemeSettings). Value controls (boolean/radio/select) are change-
        // detected — the mod's apply is meant to be idempotent, so an unchanged
        // pull is a cheap no-op (e.g. the clock's running 1s interval is left
        // alone). Panes reflect(settings) on every pull (idempotent by contract).
        // Each entry is isolated so one bad control can't break the rest or core.
        // Guarded so the early-boot call (from 85_js_startup.js, before this
        // fragment ran) is a clean no-op.
        function notifyModSettings() {
            if (!window.__mods) return;
            const list = window.__mods.settingToggles;
            for (let i = 0; i < list.length; i++) {
                const t = list[i];
                if (t.kind === 'pane') {
                    try { t.reflect(); } catch (_) {}   // reflect logs its own errors
                    continue;
                }
                let cur;
                try { cur = t.read(); } catch (_) { continue; }
                if (cur === t.last) continue;
                t.last = cur;
                try { t.reflect(); } catch (_) {}
                if (t.onChange) {
                    try { t.onChange(cur); }
                    catch (e) {
                        console.error('[mods] settings onChange failed ("'
                            + t.modId + ':' + t.key + '"):', e);
                    }
                }
            }
        }

        // Reflect every mod control's widget from the live settings when the
        // Control Panel (re)renders. Called from renderSettings (81_js_control_
        // panel.js). Reflect-only: the widgets are created once by each mod's
        // init and persist across panel open/close, so this never rebuilds
        // #set-mods or rebinds handlers. Visibility (hide on remote host tabs) is
        // handled separately by applyBrowserGlobalVisibility, which renderSettings
        // already called before us; reflecting a hidden widget is harmless. Each
        // entry isolated so one throw can't abort the core settings render.
        function renderModSettingsToggles(isLocal) {
            if (!window.__mods) return;
            const list = window.__mods.settingToggles;
            for (let i = 0; i < list.length; i++) {
                try { list[i].reflect(); } catch (_) {}
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
            // #80 (S7): the registered window kinds, in registration order — the
            // Playwright acceptance inspects this to prove the six built-ins ride
            // the registry and a fixture mod's kind appears/disappears with it.
            windowKinds: function () {
                return windowKindMenuList().map(function (k) { return k.appKind; });
            },
        };
