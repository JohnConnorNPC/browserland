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
            settingToggles: [],       // mod Control Panel controls: [{modId, kind, key, read, reflect, onChange, last, section}] (kind: boolean|radio|select|combo|text|pane; text also carries maxLength)
            // #169: ctx.theme.onChange subscribers, [{fn}] boxes. Lives on the
            // loader object (NOT a fragment-level `const`) for the same TDZ
            // reason the note above gives: notifyModTheme() rides notifyModSettings,
            // which earlier fragments call before this assignment runs — a plain
            // property read of a not-yet-created window.__mods is `undefined`,
            // which notifyModTheme guards, where a TDZ `const` would throw.
            themeSubs: [],
            helpCards: [],            // #78: mod-contributed Help cards (ctx.registerHelpCards), sanitized typed entries the Help mod merges with the core corpus
            booted: false,
            // #157: this broker's MOD POLICY as resolved at boot — {id: bool},
            // the pins GET /mods reported (plus the dependencies they imply).
            // A frozen SNAPSHOT on purpose: it is read at page load and never
            // re-read from a /state pull, for exactly the reason the per-browser
            // set is not synced (a pull must not tear down a mod in use).
            // `{}` until loadMods resolves it, so every reader sees "no pins"
            // rather than a TDZ/undefined during page-script eval.
            policy: {},
            // #163: the RAW pin map /info reported, stashed unresolved. Pins are
            // resolved only AFTER the installed packages have registered (see
            // loadMods), because _resolvePins drops any pin whose id is not yet
            // in `registered`; keeping the raw map lets _lateRegister re-resolve.
            policyRaw: null,
            // Did the boot /mods actually answer? false after a 401 (no token
            // stored yet) / 404 (older broker) / network failure, which is what
            // lets a successful login re-ask instead of silently running the
            // page unpinned until the next reload.
            policyAuthoritative: false,
            // #163: /info's `mods` — shipped rows first, then installed rows
            // topologically sorted, every row carrying `source`. The boot
            // snapshot the package loader, the prune and the status model read.
            catalog: [],
            // #163: id -> per-package LOAD record (installed mods only); id ->
            // true for every package this page asked for (the _lateRegister
            // gate); url -> in-flight load promise (the (id, gen, file) dedup);
            // id -> 'cycle'|'blocked-by-cycle'; id -> deps outside shipped ∪
            // installed. See 86b_js_mod_packages.js.
            //
            // Object.create(null), not {}: these are keyed by MOD ID, and a
            // truthy inherited `constructor`/`toString` would make a lookup say
            // "yes" for an id nothing ever put there — which for `requested` is
            // the gate _lateRegister trusts. Installed ids are regex-constrained
            // server-side, so this is belt and braces, but the bag is the wrong
            // place to rely on that.
            packages: Object.create(null),
            requested: Object.create(null),
            assetLoads: Object.create(null),
            cycleState: Object.create(null),
            missingRequires: Object.create(null),
            // _topoSortRegistered() has run, so a registerMod arriving now is a
            // LATE one and routes through _lateRegister (which re-sorts). It is
            // NOT "boot finished": a mod that registers another from its own
            // init() lands DURING the boot loop and must still be sorted.
            sorted: false,
            // The forward boot loop has finished. Observable only — the
            // late-registration gate is `sorted` above.
            bootComplete: false,
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
            // #163: bind the declaration to the PACKAGE whose script is running.
            // A CORRECTNESS CONVENTION, NOT A BOUNDARY — fork-trust means the
            // code can bypass it. What it buys: an accepted "x-wrapper" package
            // whose script registers `clock` cannot silently collide with the
            // shipped registration, show the wrong provenance, and make the
            // broker's dependency analysis disagree with the client's. A shipped
            // mod runs inside the one inline <script>, which carries no package
            // stamp, so it is unaffected.
            //
            // Two limits, both inherent and both stated rather than papered
            // over: (a) document.currentScript is null in a callback, so a
            // package registering from a promise/timeout is UNATTRIBUTABLE and
            // this check simply does not apply to it — the duplicate-id
            // ModConflictError below is then the only backstop; (b) the check
            // runs BEFORE that duplicate check, so a wrong-id collision reports
            // as wrong-id rather than as a duplicate. That ordering is
            // deliberate: it names the package at fault.
            const pkgId = _currentPackageId();
            if (pkgId && pkgId !== id) {
                const pkg = window.__mods.packages && window.__mods.packages[pkgId];
                if (pkg) { pkg.state = 'wrong-id'; pkg.wrongId = id; }
                throw ModConflictError('registerMod: package "' + pkgId
                    + '" declared mod id "' + id + '"');
            }
            const reg = window.__mods.registered;
            if (reg.some((m) => m.id === id)) {
                throw ModConflictError('registerMod: duplicate mod id "' + id + '"');
            }
            const entry = {
                id: id,
                version: (typeof decl.version === 'string') ? decl.version : '0',
                // null = "pin nothing" (still init); a number must match exactly.
                ctxVersion: (typeof decl.ctxVersion === 'number') ? decl.ctxVersion : null,
                // #86 (S13): declared trust tiers, the ctx-capability families a
                // reviewed mod uses (settings / taskbar / file / session / window).
                // Display + review only — the "Mods" pane shows them so the operator
                // can review what each mod touches BEFORE enabling it. Like every
                // tier in this system they are DECLARED, not enforced (ctx grants no
                // runtime boundary, #71): a reviewer cross-checks the declaration
                // against the mod's actual `ctx.` usage. Non-strings dropped; a mod
                // that declares none renders as "unspecified".
                tiers: Array.isArray(decl.tiers)
                    ? decl.tiers.filter(function (t) { return typeof t === 'string'; })
                    : [],
                // #116 (S14): declared boot default. Omitted => true (every
                // existing mod is default-on, so their meaning is unchanged);
                // `defaultEnabled:false` ships a mod OFF until the operator opts
                // in via the Mods pane. The persisted enable set stores ids
                // TOGGLED AWAY from this default (see _modsDisabled below), so a
                // default-off mod is absent from the set until enabled.
                defaultEnabled: (decl.defaultEnabled !== false),
                // #121 (S15): declared mod dependencies — ids of OTHER mods that
                // must be ACTIVE for this mod to init. Omitted => [] (every existing
                // mod is dependency-free, so its meaning is unchanged).
                // #163: ordering is now established at RUNTIME by
                // _topoSortRegistered() (86b), which sorts `registered` in place
                // over the shipped ∪ installed graph and splits its residual into
                // `cycle` / `blocked-by-cycle`. It used to be established only by
                // the static ordering guard over ui._MODS, which made cycles,
                // self-require and missing ids unrepresentable — that guard still
                // holds for the SHIPPED set (it is a style rule now), but a
                // runtime-installed set has no such list, so the sort is real and
                // not merely defensive. Non-strings (and empty strings) dropped,
                // mirroring `tiers`.
                requires: Array.isArray(decl.requires)
                    ? decl.requires.filter(function (t) { return typeof t === 'string' && t; })
                    : [],
                init: decl.init,
            };
            reg.push(entry);
            // #163: a registration that lands after the topological sort has
            // already run — a package that overran the load deadline, one a
            // 401'd boot never fetched, or a mod calling registerMod from its
            // own init(). _lateRegister re-sorts, re-resolves the pins, repaints
            // the Mods pane and brings the mod up, but only if this page asked
            // for that package.
            //
            // Gated on `sorted`, NOT on "boot finished": a declaration appended
            // after the sort but during the boot loop would otherwise never be
            // sorted at all, permanently breaking the dependency-precedes-
            // dependent invariant every cascade assumes.
            if (window.__mods.sorted) _lateRegister(entry);
        }

        // LIFO teardown: run a mod's onUnload callbacks newest-first, each
        // isolated, mirroring win.cleanups (73_js_window_runtime.js:439). Drains
        // the list so a re-run is a no-op.
        function _runUnloads(rec) {
            // #169: mark the record DEAD for the whole drain, and leave it dead.
            // A teardown callback is allowed to change a setting, which reaches
            // notifyModTheme through _valueAccessor.set — and because the drain
            // is LIFO, a LATER-registered unload runs while an EARLIER-registered
            // ctx.theme subscriber is still on the list. Without this flag that
            // mod's own theme callback would run mid-teardown, after resources it
            // registered later have already been released.
            rec.unloading = true;
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
                visibility: makeModVisibilityApi(rec),
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
                    // Text write (atomic, host-wide). opts.encoding (#97) is an
                    // optional source-encoding label the server round-trips so a
                    // UTF-16/cp1252 file saves in its own encoding; omitted ->
                    // server defaults utf-8 (so other callers are unaffected).
                    // -> {ok,path}.
                    write: function (path, content, opts) {
                        const body = { path: path, content: content };
                        if (opts && opts.encoding) body.encoding = opts.encoding;
                        return _modFileApi('/file/write', body, opts);
                    },
                    // Directory listing (''/relative -> the broker default dir).
                    // -> {ok,root,cwd,parent,entries:[{name,type,size}]}
                    list: function (path, opts) {
                        return _modFileApi('/file/list', { path: path || '' }, opts);
                    },
                    // Delete a file, a symlink/junction (entry only), or — with
                    // opts.recursive — a directory tree. -> {ok,path}. Quoted key
                    // so the reserved word can never trip a minifier;
                    // ctx.file.delete(...) still calls it.
                    'delete': function (path, opts) {
                        return _modFileApi('/file/delete',
                            { path: path,
                              recursive: !!(opts && opts.recursive) }, opts);
                    },
                    // Binary-safe write via base64; opts.overwrite (default false)
                    // -> {ok,path,size} (or {ok:false,error:'exists'} on a clash).
                    upload: function (path, contentB64, opts) {
                        return _modFileApi('/file/upload',
                            { path: path, content_b64: contentB64,
                              overwrite: !!(opts && opts.overwrite) }, opts);
                    },
                    // #108 chunked transfer (additive — ctxVersion stays 1). Thin
                    // wrappers over the streaming /file/* endpoints; the mod
                    // orchestrates the loop (all fetch/token stays in core). Each
                    // is one _modFileApi call, host-routed + fail-closed like the
                    // rest. readChunk: ranged read -> {ok,content_b64,length,size,
                    // offset,eof}. The upload trio is an append-and-atomic-replace
                    // session: begin -> {ok,upload_id}; chunk -> {ok,received};
                    // commit -> {ok,path,size}; abort -> {ok} (best-effort).
                    readChunk: function (path, opts) {
                        // Omit length when unset so the server clamps to its own
                        // MAX_CHUNK_BYTES (0 would trip the length>=1 guard).
                        const body = { path: path,
                                       offset: (opts && opts.offset) || 0 };
                        if (opts && opts.length) body.length = opts.length;
                        return _modFileApi('/file/read_chunk', body, opts);
                    },
                    // #110: SHA-256 of a file, streamed server-side (bounded, NOT
                    // capped at 5 MiB). A cross-host MOVE hashes the SOURCE with
                    // this, then hands the digest to uploadCommit as expected_sha256
                    // to gate the source delete. -> {ok,path,sha256,size}.
                    hash: function (path, opts) {
                        return _modFileApi('/file/hash', { path: path }, opts);
                    },
                    uploadBegin: function (path, opts) {
                        return _modFileApi('/file/upload_begin',
                            { path: path,
                              overwrite: !!(opts && opts.overwrite) }, opts);
                    },
                    uploadChunk: function (uploadId, contentB64, opts) {
                        return _modFileApi('/file/upload_chunk',
                            { upload_id: uploadId, content_b64: contentB64,
                              offset: (opts && opts.offset) || 0 }, opts);
                    },
                    uploadCommit: function (uploadId, opts) {
                        const body = { upload_id: uploadId };
                        // #110: a cross-host MOVE passes the source digest so the
                        // server verifies the dest BEFORE its atomic replace and
                        // refuses on mismatch. Absent/null (copy) -> field omitted
                        // -> server skips verification (unchanged fast path).
                        if (opts && opts.expected_sha256) {
                            body.expected_sha256 = opts.expected_sha256;
                        }
                        return _modFileApi('/file/upload_commit', body, opts);
                    },
                    uploadAbort: function (uploadId, opts) {
                        return _modFileApi('/file/upload_abort',
                            { upload_id: uploadId }, opts);
                    },
                    // #72 richer ops (additive — ctxVersion stays 1). Paths are
                    // host-wide native absolutes, same per-host routing.
                    // Create ONE directory (parent must exist). -> {ok,path}.
                    mkdir: function (path, opts) {
                        return _modFileApi('/file/mkdir', { path: path }, opts);
                    },
                    // Server-side copy of a file or tree; opts.overwrite. ->{ok,path}
                    copy: function (src, dst, opts) {
                        return _modFileApi('/file/copy',
                            { src: src, dst: dst,
                              overwrite: !!(opts && opts.overwrite) }, opts);
                    },
                    // Server-side move/rename of a file or tree; opts.overwrite.
                    move: function (src, dst, opts) {
                        return _modFileApi('/file/move',
                            { src: src, dst: dst,
                              overwrite: !!(opts && opts.overwrite) }, opts);
                    },
                    // Zip a file/tree into dest archive; opts.overwrite. ->{ok,path}
                    zip: function (src, dest, opts) {
                        return _modFileApi('/file/zip',
                            { src: src, dest: dest,
                              overwrite: !!(opts && opts.overwrite) }, opts);
                    },
                    // Extract a .zip into a FRESH dest dir. -> {ok,path}.
                    unzip: function (path, dest, opts) {
                        return _modFileApi('/file/unzip',
                            { path: path, dest: dest }, opts);
                    },
                    // Properties: type/size/mtime/mode (+children for a dir).
                    stat: function (path, opts) {
                        return _modFileApi('/file/stat', { path: path }, opts);
                    },
                    // #96: editable Properties. attrs = {mode} (POSIX rwx, low 9
                    // bits; server preserves special bits) OR {attributes:{read-
                    // only,hidden,archive}} (Windows). -> {ok,path}.
                    setattr: function (path, attrs, opts) {
                        const body = { path: path };
                        if (attrs && attrs.mode != null) body.mode = attrs.mode;
                        if (attrs && attrs.attributes) body.attributes = attrs.attributes;
                        return _modFileApi('/file/setattr', body, opts);
                    },
                },
                // #124: ctx.serverStore — a generic, DURABLE, cross-browser per-mod
                // key/value store backed by the server (/mod-store/<modId>), the
                // persistent twin of the per-browser ctx.storage (localStorage).
                // A value saved here survives a reload / cache clear and reads from
                // every browser on THIS broker (cross-host is out of scope — a
                // server store is per-broker). SCOPED to this mod's id: the mod
                // never names the store key, so one mod can neither read nor write
                // another's. Writes reuse the /state single-active-client lease
                // (set() auto-attaches the core CLIENT_ID), so a NON-ACTIVE browser
                // can get()/getRevision() but its set() resolves 409
                // {error:'not_active'} — it must become the active browser to
                // write. Every call resolves to a parsed result object — never a
                // rejected promise. Like ctx.file this is operator-granted REVIEW
                // HYGIENE, NOT a permission boundary: a same-origin mod already
                // holds the page token and could POST /mod-store/* itself; the seam
                // just funnels durable server storage through one reviewed, mod-
                // scoped choke point. Additive — ctxVersion stays 1; feature-detect
                // `if (ctx.serverStore)`.
                // opts.host (#65): a server store is per-broker, so every call
                // takes an optional host *id* (win.fileHostId semantics) routing
                // the request to THAT broker — the seam publish-to-all needs to
                // write one registry to each broker. Omitted / ''/'local' -> the
                // local broker (so every existing caller is unchanged); an unknown
                // remote id FAILS CLOSED (no request, synthetic no-host result),
                // never a silent write to the local store. Additive — ctxVersion
                // stays 1; a mod feature-detects the whole family with
                // `if (ctx.serverStore)` (host routing is a superset of the old
                // local-only contract, so an omitted opts behaves exactly as v1).
                serverStore: {
                    // Current value + revision METADATA (rev + ts, NO bodies — the
                    // ring can be large). -> {status, rev, value,
                    // revisions:[{rev,ts}]} (rev 0 / value null when the mod has
                    // never written). Ungated by the lease, so a non-active /
                    // reactivating browser always reads.
                    //
                    // #174: `status` is the HTTP status, the same field set()
                    // has always carried, because a mod reading ANOTHER broker's
                    // store has to tell "it refused our password" (401) from "no
                    // registry there yet" (200, value null) from "unreachable"
                    // (0) — and the body alone cannot say. It is applied LAST so
                    // a `status` in the body can never overwrite the transport's
                    // (a remote broker's response body is untrusted input).
                    get: function (opts) {
                        return _modStoreApi('GET', modId, undefined, '', opts)
                            .then(r => _withStatus(r));
                    },
                    // Optimistic write: PUT {baseRev, value, clientId}. clientId is
                    // the core lease id (CLIENT_ID), so the ACTIVE browser's write
                    // passes the lease. Resolves {status, ok, rev, error?, value?}:
                    // 200 {ok:true, rev}; 409 {error:'conflict'|'not_active', rev,
                    // value} with the LIVE value inlined (rebase in one round trip);
                    // 400/413/500 {ok:false, error}. opts.purgeRevisions (#65) adds
                    // the strict-bool "write value AND forget history" flag — the
                    // only way to un-publish a value that scrolled into the ring.
                    // opts.noHistory (#192) is a strict tri-state passthrough: an
                    // explicit true/false rides the body verbatim (stickiness is a
                    // SERVER decision — un-flagging also needs purgeRevisions:true
                    // in the same call, or the server leaves the record flagged);
                    // omitted/null is left OFF the wire so a caller that forgets
                    // the option can never accidentally un-stick a flagged record.
                    set: function (value, baseRev, opts) {
                        const body = {
                            baseRev: baseRev, value: value, clientId: CLIENT_ID,
                        };
                        if (opts && opts.purgeRevisions === true) {
                            body.purgeRevisions = true;
                        }
                        if (opts && typeof opts.noHistory === 'boolean') {
                            body.noHistory = opts.noHistory;
                        }
                        return _modStoreApi('PUT', modId, body, '', opts)
                            .then(r => _withStatus(r));
                    },
                    // Fetch ONE past (or current) revision's FULL value. -> {status,
                    // ok, rev, value} or {ok:false, error:'no_such_rev'} (404 — it
                    // scrolled off the ring) / 'bad_rev'. Drives History preview +
                    // restore.
                    getRevision: function (n, opts) {
                        return _modStoreApi('GET', modId, undefined,
                            '?rev=' + encodeURIComponent(n), opts)
                            .then(r => _withStatus(r));
                    },
                },
                // #85 (S12): host session RPC — ONE reviewed wrapper over the
                // existing /session/procs (enumerate a session's process tree) and
                // the DESTRUCTIVE /session/kill (end a pid; killing the shell pid
                // destroys the whole session). Host-aware, mirroring the task
                // manager's old inline sessionPost (72_js_task_manager.js).
                //
                // TRUST TIER — HIGH. Like ctx.file this is operator-granted REVIEW
                // HYGIENE, NOT a permission boundary or sandbox: a same-origin mod
                // already holds the page's auth token and can POST to /session/*
                // directly, exactly like core does; ctx.session grants NO new
                // privilege and confines nothing. Its only job is to funnel every
                // mod's session RPC — including the destructive process kill /
                // session destroy — through one reviewed, host-aware choke point, so
                // "don't merge a mod that kills sessions" stays a code-review
                // decision (the only real control). opts.host is a host *id* string
                // (sess.hostId / win.fileHostId semantics): a known id routes to that
                // broker, ''/'local'/omitted -> the local broker, and an UNKNOWN
                // remote id resolves to a synthetic no-host result (NO request made)
                // rather than silently hitting local. Every call resolves to
                // {status, json} — never a rejected promise: `status` is the HTTP
                // status (0 for a transport / no-host failure) and `json` the parsed
                // body, so a caller honors the broker's status contract verbatim — a
                // 409 + {error:'session_gone'} is the session-destroy SUCCESS path,
                // NOT an error. Feature-detect with `if (ctx.session)` (ctxVersion
                // stays 1 — this is an additive capability).
                session: {
                    // Enumerate one session's process tree.
                    // -> {status, json:{ok,procs}}
                    procs: function (id, opts) {
                        return _modSessionApi('/session/procs', { id: id }, opts);
                    },
                    // DESTRUCTIVE: end `pid` in a session (the shell pid destroys
                    // it). -> {status, json}: 200 {ok:true}, or 409 {error:
                    // 'session_gone'} when killing the shell tore the agent down
                    // before it could reply — that 409 is the destroy SUCCESS path.
                    kill: function (id, pid, opts) {
                        return _modSessionApi('/session/kill',
                            { id: id, pid: pid }, opts);
                    },
                    // #116 (S14): per-terminal git status. POST /session/git
                    // {id:<wireId>} — the agent runs git in its OWN live cwd, so
                    // only the bare wire id is sent. Same host-resolution + fail-
                    // open {status, json} envelope as procs/kill, byte-identical to
                    // the git widget's old inline gitPost: a non-repo, a 404 on an
                    // old broker, or a transport failure resolves (never rejects)
                    // so a routine terminal never toasts. -> {status, json:{ok,
                    // branch,detached,ahead,behind,staged,unstaged,untracked,
                    // conflicts,dirty,dirty_count}} on a repo. opts.host is a host
                    // id (win.hostId semantics), routed like every other session op.
                    git: function (id, opts) {
                        return _modSessionApi('/session/git', { id: id }, opts);
                    },
                },
                // #106: an explicit, reviewable clipboard observer seam. observe(fn)
                // registers fn(dir, text) — dir 'out' (copied out of a terminal) |
                // 'in' (pasted in) — called on every copy/paste core captures at its
                // clipboard seams (copyTextToClipboard + the terminal paste listeners,
                // 63/67). Returns an unsubscribe fn; ALSO auto-removed on the mod's
                // teardown (rec.unloads), so capturing STOPS the moment the mod is
                // disabled — the whole reason the #106 history mod ships default-off
                // (clipboards carry secrets). Additive — ctxVersion stays 1; feature-
                // detect `if (ctx.clipboard)`. Like every ctx family this is REVIEW
                // HYGIENE, not a boundary: a same-origin mod could already observe the
                // clipboard directly; the seam just funnels it through one reviewed
                // choke point that a disable can switch off.
                clipboard: {
                    observe: function (fn) { return _modClipboardObserve(rec, fn); },
                },
                taskbar: {
                    // Mount a status node in the taskbar (before #help-chip, so a
                    // mod keeps the old clock slot). Auto-removed on teardown.
                    addStatusItem: function (node) {
                        return _modAddStatusItem(rec, node);
                    },
                    // #148: fires after EVERY chip rebuild (the 2 s poll and every
                    // manual refresh), so a mod can badge / dim / reorder the bar
                    // on the same tick the chips appear.
                    onItemsRendered: function (cb) {
                        return _modTrack(rec, registerTaskbarItemsRendered(cb));
                    },
                    // #148: fn(key) runs BEFORE core resolves a taskbar
                    // activation and returns true if it changed what the desktop
                    // shows. It takes a KEY, not a window: a chip for a CLOSED
                    // session has no window, which is exactly the case
                    // ctx.desktop.onReveal cannot cover.
                    interceptActivate: function (fn) {
                        return _modTrack(rec, registerTaskbarActivateIntercept(fn));
                    },
                },
                // #148 (workspaces): the desktop model seams. Core owns ONE
                // desktop whose columns all live in prefs._layout.columns; these
                // let a mod change what is DRAWN without moving any of it.
                //   columnFilter(fn)   fn(col, layout) -> show this column?
                //                      Registering/dropping it relayouts, so a
                //                      disable puts every column back on screen.
                //   onColumnCreated(fn) a column was just spliced in — claim it
                //                      HERE, before anyone reads a visible index
                //                      back, or it is born invisible.
                //   onPlaced(fn)       a window was just placed as a FLOAT.
                //   onForgotten(fn)    fn(key) -- a window was CLOSED and its
                //                      key is gone for good; prune anything
                //                      keyed off it. A reload is NOT this.
                //   onReveal(fn)       first refusal before revealAndFocusWindow
                //                      focuses an existing window.
                //   onLayoutRender(fn) the strip was re-laid-out; repaint your
                //                      desktop chrome. Every path that changes
                //                      placement converges here.
                // Each family is ONE slot: a second registration throws
                // ModConflictError so two mods can never silently fight over the
                // desktop. Additive — ctxVersion stays 1; feature-detect
                // `if (ctx.desktop)`.
                desktop: {
                    columnFilter: function (fn) {
                        return _modTrack(rec, registerColumnFilter(fn));
                    },
                    onColumnCreated: function (fn) {
                        return _modTrack(rec, registerColumnCreated(fn));
                    },
                    onPlaced: function (fn) {
                        return _modTrack(rec, registerPlacementHooks({ placed: fn }));
                    },
                    onForgotten: function (fn) {
                        return _modTrack(rec, registerPlacementHooks({ forgotten: fn }));
                    },
                    onReveal: function (fn) {
                        return _modTrack(rec, registerPlacementHooks({ reveal: fn }));
                    },
                    onLayoutRender: function (fn) {
                        return _modTrack(rec, registerLayoutRendered(fn));
                    },
                },
                // #148: contribute keyboard actions and menu items.
                // registerKeyActions([{id,label,run}]) merges into the live action
                // list every reader now goes through (the dispatcher, the Keyboard
                // Shortcuts pane, the help corpus), and re-renders the open pane on
                // register AND on teardown. A duplicate id throws rather than
                // last-wins, so one mod can never steal another's binding.
                // registerWindowMenuItems(fn) / registerDesktopMenuItems(fn) each
                // return an ARRAY of renderMenu items (separators included, so the
                // contributor owns its own grouping) from a marked insertion point;
                // with no contributor the menus are byte-identical to before.
                // #162: the desktop one is called in BOTH window modes and gets
                // { tiling } saying which — it used to run only in tiling mode, so
                // a contributor that offers tiling-only actions must check the flag
                // rather than assume. Core owns the separator above the block.
                registerKeyActions: function (actions) {
                    return _modTrack(rec, registerKeyActions(actions));
                },
                registerWindowMenuItems: function (fn) {
                    return _modTrack(rec, registerWindowMenuItems(fn));
                },
                registerDesktopMenuItems: function (fn) {
                    return _modTrack(rec, registerDesktopMenuItems(fn));
                },
                // #116 (S14): per-terminal-window lifecycle. onTerminalCreate(cb)
                // subscribes a mod to EVERY terminal window — REPLAYED over those
                // already open and fired for every future one — so a per-window
                // title-bar control (the git status widget) rides the SAME core
                // hook (registerTerminalCreate, 67_js_window_lifecycle.js) core
                // uses, not a re-scope to a taskbar chip. cb receives
                //   { win, titleBar, host, wireId, addTitleBarItem, onDispose }
                // where addTitleBarItem(node) inserts the node before the window's
                // min button (left of min/close) and onDispose(fn) registers a
                // per-window teardown on win.cleanups (drained by closeWindow (73)
                // and the active-view rebuild (84) — no new teardown plumbing).
                // Returns an unsubscribe fn; ALSO auto-unsubscribed on the mod's
                // teardown (rec.unloads), so a disabled mod stops decorating new
                // windows (the mod tears down live widgets via its own onDispose
                // set). Additive — ctxVersion stays 1; feature-detect
                // `if (ctx.windows)` (same convention as `if (ctx.file)`).
                windows: {
                    onTerminalCreate: function (cb) {
                        const off = registerTerminalCreate(cb);
                        rec.unloads.push(off);
                        return off;
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
                    //   combo(key,  [{value,label}], {label,title,def,isBrowserGlobal})
                    //     -> searchable <input list>+<datalist> (type-to-filter a
                    //        long list; the empty-value option becomes placeholder)
                    //   text(key, {label,title,def,isBrowserGlobal,options,
                    //              placeholder,maxLength,validate})
                    //     -> #168: FREE text, optionally with a suggestion
                    //        datalist (a superset of combo, whose list is a
                    //        domain where this one is only a shortcut). The only
                    //        primitive that is not choice-constrained: read-
                    //        through is structural so a stored value never
                    //        evaporates, validate() is write-only and its
                    //        rejection is visible, writes are debounced 400 ms
                    //        and flushed on teardown. See _modSettingText.
                    boolean: function (key, def, opts) {
                        return _modSettingBoolean(rec, key, def, opts);
                    },
                    radio: function (key, options, opts) {
                        return _modSettingChoice(rec, 'radio', key, options, opts);
                    },
                    select: function (key, options, opts) {
                        return _modSettingChoice(rec, 'select', key, options, opts);
                    },
                    combo: function (key, options, opts) {
                        return _modSettingChoice(rec, 'combo', key, options, opts);
                    },
                    text: function (key, opts) {
                        return _modSettingText(rec, key, opts);
                    },
                },
                // #169: the LIVE theme, and a subscription to changes. See the
                // "#169: the live theme" block below for the derivation rule and
                // the public CSS-variable contract vars() resolves.
                //   get()         -> {name, dark}. Derived from the DOM, so it is
                //                    right with the theme mod disabled, absent or
                //                    replaced. `dark` is the honest half; `name`
                //                    is a hint (see below).
                //   vars()        -> {'--bg': '#1e1e1e', …}. A SEPARATE call on
                //                    purpose: it costs a getPropertyValue per
                //                    public var plus an object, which a
                //                    subscriber that only branches on `dark`
                //                    should never pay.
                //   onChange(fn)  -> unsubscribe fn. fn({name, dark}) runs AFTER
                //                    the new vars are on screen, only on a real
                //                    change, and is NOT replayed on register
                //                    (call get() in your init). ALSO auto-dropped
                //                    on this mod's teardown, like
                //                    ctx.clipboard.observe.
                // Additive — ctxVersion stays 1; feature-detect `if (ctx.theme)`.
                theme: {
                    get: function () { return _themeState(); },
                    vars: function () { return _themeVars(); },
                    onChange: function (fn) { return _modThemeObserve(rec, fn); },
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
                //   block = { t:'p'|'bullet'|'sub'|'pre', spans:[span] }
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
                // #170 — the launch-menu icon: `menu.iconKey` names an entry in
                // core's CLOSED APP_ICON_SVG table (65), so a mod id that isn't
                // shipped there resolves to nothing; `menu.iconGlyph` is the
                // mod-owned alternative — a SHORT text/emoji glyph (controls,
                // bidi overrides + whitespace stripped, capped at APP_GLYPH_MAX
                // code points) that renderMenu paints with textContent. There is
                // deliberately
                // no ctx.registerAppIcon(svg): a mod's icon must never be able to
                // put markup into the shared menu renderer, not least because the
                // value a mod passes need not be its own (a /mod-store blob, a
                // peer's /state). Declare neither and the launcher is a bare
                // label row — core synthesizes no placeholder, because the same
                // renderer paints the layout/host/profile menus, whose rows are
                // all icon-less by design.
                // Two traps worth knowing: (a) an iconKey is NOT owned by
                // anyone, so a mod may name a SHIPPED key and get that icon —
                // only the LABEL identifies a launcher; and (b) iconKey WINS, so
                // a kind that declares an as-yet-unknown iconKey plus a glyph
                // shows the glyph today and would silently switch to the SVG if
                // core ever ships that key. Declare one or the other.
                // An optional `restore` is called directly, so it does NOT get
                // openAppWindow's dedup-by-id: it must check windows.get(rec.id)
                // itself, because a lease-loss rebuild re-runs the restore (#167).
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

        // #106: register a clipboard observer for a mod, auto-removed on teardown
        // (mirrors _modAddStatusItem's rec.unloads.push wiring). addClipboardObserver
        // (63_js_clipboard_auth.js) is the hoisted core registry the copy/paste seams
        // notify; here we push its remover onto the mod's unloads so the observer is
        // dropped — and capturing stops — when the mod tears down (disable or master
        // gate off). A non-function is a clean no-op returning a no-op remover.
        function _modClipboardObserve(rec, fn) {
            if (typeof fn !== 'function') return function () {};
            const off = addClipboardObserver(fn);
            rec.unloads.push(off);   // observer removed when the mod tears down
            return off;
        }

        // ---- #169: the live theme, and a channel that announces changes ------
        // A mod that must know whether the desktop is light or dark (an iframe
        // hint through the frame URL, a canvas it paints itself) has no honest
        // source today. Core stopped owning `theme` when #75 extracted the engine,
        // so the synced key is the theme MOD's private key and says nothing when
        // that mod is disabled, absent, or replaced; a duplicate
        // ctx.settings.radio('theme', …) silently mounts a SECOND Control Panel
        // widget for the same key (there is no duplicate-key guard); and there is
        // no DOM event to listen to (this app dispatches none, by design — a
        // document-level listener is not removable by a mod's teardown in
        // practice).
        //
        // So derive from the LIVE DOM, which dissolves the "who owns the key"
        // problem entirely:
        //
        //   dark   a YIQ test (core's hoisted isDarkAccent, 65) on the COMPUTED
        //          --bg. Whatever painted it — the theme mod, a replacement, or
        //          the :root defaults — that is what the user is looking at.
        //   name   the synced `theme` key, but ONLY when an INLINE --bg on
        //          documentElement proves a theme mod actually applied one
        //          (mods/theme/theme.js writes the six chrome vars there and
        //          nowhere else). With no inline var the :root defaults are live,
        //          and by the #75 contract they reproduce `night` EXACTLY — so
        //          reporting a stored `theme` there would name a scheme that is
        //          NOT on screen (the theme mod disabled with `day` saved is
        //          precisely that case). `name` is therefore a HINT, only as
        //          trustworthy as whichever mod claims the key; `dark` is the
        //          half to branch on. Three ways `name` can be imprecise, all of
        //          them the same missing fact — nothing in the DOM records WHICH
        //          theme wrote those vars, so no derivation off the DOM can do
        //          better, and `dark` stays honest through all of them:
        //            - disable the theme mod and THEN let a /state pull move the
        //              key (verified live): `name` reports the pulled scheme
        //              while the screen still shows the last applied one;
        //            - the stored value is one this build's theme mod does not
        //              know. Its radio read() is NON-destructive, so the widget
        //              shows night and applies night while the blob keeps the
        //              unknown string — which is what `name` reports;
        //            - a replacement theme mod keys off something OTHER than
        //              `theme`, or another mod writes an inline --bg of its own.
        //          A mod that needs certainty should key off `dark`.
        //
        // Two limits worth stating plainly, both deliberate:
        //   - The channel is change-detected on {name, dark} ONLY. A change that
        //     moves a public var without moving either (a hypothetical accent-
        //     only setting) does NOT call subscribers, so a mod refreshing
        //     ctx.theme.vars() from onChange would miss it. Signing the full var
        //     set would make every announcement pay the read that vars() exists
        //     to keep optional; no shipped setting does this.
        //   - A theme mod must apply its vars SYNCHRONOUSLY from its settings
        //     onChange (as mods/theme/theme.js does). Every mover announces on
        //     the same tick it fires, so a mod that deferred the write to a
        //     rAF/timeout would be announced BEFORE it painted. That mirrors the
        //     synchronous-init contract initMod already relies on.
        //
        // Fired from the four movers that can change what is on screen —
        // _valueAccessor.set (a LOCAL pick, which never routes through
        // applyThemeSettings), notifyModSettings (/state convergence),
        // setModEnabled and _applyPolicyLive (a theme mod coming up or going
        // down) — plus once at the end of boot, so a subscriber whose mod inits
        // BEFORE the theme mod is not left holding the pre-theme answer.
        //
        // Hooked at those CALLERS rather than inside initMod/disableMod, for the
        // reason #167 gives for restoreAppWindowsAfterMods: _bringUp/_takeDown
        // run in loops, and one announcement after the whole cascade settles
        // beats N interleaved with mid-flight inits — and it keeps a subscriber's
        // handler off the stack in the middle of a dependency cascade. The cost
        // is that the __test.run/__test.disable inspectors, which bypass
        // setModEnabled by design, do not announce; that is deliberate (they
        // exist to drive initMod/disableMod in isolation) — an acceptance test
        // that wants the channel should drive __test.setEnabled or a real pick.

        // The PUBLIC CSS-variable contract, resolved off documentElement:
        //
        //   --bg / --bg-2 / --bg-3     surfaces, back to front
        //   --fg / --fg-dim            ink, primary + de-emphasized
        //   --accent-default           the accent to use when there is no window one
        //   --sel-bg                   selected / active rows, tabs, buttons
        //   --ok / --warn / --danger   the #173 semantic status trio
        //
        // Deliberately NOT here:
        //   --accent   is per-WINDOW, not global — there is no document-level
        //              value to resolve. Inside a window's DOM, write
        //              `var(--accent, var(--accent-default))` in CSS.
        //   --taskbar-h / --title-h / --handle-thick / --corner-size are
        //              GEOMETRY, not theme; no theme mod writes them.
        // Everything else in the stylesheets is private core and may change
        // without notice. Mods should consume these instead of the hardcoded
        // semantic hexes several of them still carry.
        //
        // Values come back as the CSSOM reports a CUSTOM PROPERTY, which is a
        // substituted token stream and NOT a resolved color: the six a theme
        // writes are hexes, but the derived four read back as e.g.
        // 'color-mix(in srgb, #4aa3ff 28%, #1e1e1e)' (verified live). That is a
        // valid CSS color string — assign it to a style property and it paints —
        // but do NOT expect to parse a number out of it; branch on `dark` from
        // get() instead. A var the page somehow lacks is simply absent.
        function _themeVars() {
            // Declared INSIDE the function on purpose: a fragment-level `const`
            // read by a hoisted function is the TDZ hazard the header note warns
            // about, and this list is small enough that rebuilding it per call
            // (vars() is the deliberately-rare path) costs nothing.
            const PUBLIC_VARS = ['--bg', '--bg-2', '--bg-3', '--fg', '--fg-dim',
                                 '--accent-default', '--sel-bg',
                                 '--ok', '--warn', '--danger'];
            const out = {};
            try {
                const cs = getComputedStyle(document.documentElement);
                for (const n of PUBLIC_VARS) {
                    const v = String(cs.getPropertyValue(n) || '').trim();
                    if (v) out[n] = v;
                }
            } catch (_) {}
            return out;
        }

        // {name, dark} from the live DOM — see the derivation above. Never
        // throws: a missing --bg, a color syntax we cannot read, or a document
        // mid-teardown all fall back to the shipped default (night / dark), which
        // is exactly what :root paints when nothing else has.
        function _themeState() {
            let name = 'night';
            let dark = true;
            try {
                const root = document.documentElement;
                const inline = String(
                    root.style.getPropertyValue('--bg') || '').trim();
                if (inline) {
                    const v = getSettings().theme;
                    if (typeof v === 'string' && v) name = v;
                }
                const hex = _themeBgHex(root);
                if (hex) dark = isDarkAccent(hex);
            } catch (_) {}
            return { name: name, dark: dark };
        }

        // The live background as a hex isDarkAccent can read, or '' if it cannot
        // be determined. Reads --bg FIRST: it is what a theme mod writes, and no
        // CSS transition can make it stale — an UNREGISTERED custom property (this
        // app declares no @property, which is what would make one interpolate) is
        // a token substitution, so a mid-transition read already reports the
        // destination. Falls back to the RESOLVED html background, which
        // 10_css_root.css pins to var(--bg), so a theme that writes --bg in a
        // syntax we cannot read still has a second chance at a number.
        //
        // normalizeHex answers PALETTE[0] for anything it cannot parse — a
        // plausible-looking WRONG color — so nothing unparsed is handed to it: a
        // theme in a color space the CSSOM serializes as-is (oklch(), color(),
        // lab()) reads as '' here and `dark` keeps its documented default rather
        // than being invented. Every shipped palette is plain hex.
        function _themeBgHex(root) {
            const cs = getComputedStyle(root);
            const fromVar = _themeAsHex(
                String(cs.getPropertyValue('--bg') || '').trim());
            if (fromVar) return fromVar;
            return _themeAsHex(String(cs.backgroundColor || '').trim());
        }
        // '#abc' / '#aabbcc' / 'rgb(r,g,b)' / 'rgba(r,g,b,a)' -> a hex string
        // (3-digit passes through; normalizeHex expands it). '' for anything
        // else, including an unresolved color-mix()/var() token, a modern
        // color-space form, and a FULLY TRANSPARENT color: alpha 0 is what an
        // invalid --bg computes `background: var(--bg)` down to, and calling that
        // opaque black would report `dark` off a surface nobody can see.
        function _themeAsHex(v) {
            if (/^#[0-9a-fA-F]{3}$/.test(v) || /^#[0-9a-fA-F]{6}$/.test(v)) return v;
            const m = v.match(
                /^rgba?\(\s*([\d.]+)[\s,]+([\d.]+)[\s,]+([\d.]+)(?:[\s,/]+([\d.]+))?/);
            if (!m) return '';
            if (m[4] !== undefined && parseFloat(m[4]) === 0) return '';
            let s = '#';
            for (let i = 1; i <= 3; i++) {
                const n = Math.max(0, Math.min(255, Math.round(parseFloat(m[i]))));
                s += (n < 16 ? '0' : '') + n.toString(16);
            }
            return s;
        }
        function _themeSig(state) {
            return state.name + '|' + (state.dark ? 'd' : 'l');
        }

        // Subscribe a mod to theme changes (ctx.theme.onChange). Returns an
        // unsubscribe fn AND pushes it onto rec.unloads, mirroring
        // _modAddStatusItem / _modClipboardObserve: a disable, a dependency
        // cascade, or an initMod rollback drops the subscriber, so a torn-down
        // mod is never called again. Both paths are idempotent (indexOf === -1),
        // so unsubscribing by hand and then tearing down is safe. A non-function
        // is a clean no-op returning a no-op remover.
        //
        // The entry is a BOX ({fn}), not the bare fn, so identity survives a mod
        // registering the SAME function twice — one unsubscribe then removes one
        // registration rather than the wrong one.
        function _modThemeObserve(rec, fn) {
            if (typeof fn !== 'function') return function () {};
            const subs = window.__mods.themeSubs;
            // Priming on the 0 -> 1 transition is what makes "not replayed on
            // register" safe: the change detector starts at what is on screen
            // NOW, so the next mover calls fn only if the theme really moved, and
            // a subscribe that follows the last unsubscribe cannot inherit a
            // signature that went stale while nobody was listening.
            if (!subs.length) {
                try { notifyModTheme._last = _themeSig(_themeState()); }
                catch (_) { notifyModTheme._last = null; }
            }
            // `rec` rides along so a fire can skip a mod that is mid-teardown
            // (see _runUnloads), not just one already unsubscribed.
            const sub = { fn: fn, rec: rec };
            subs.push(sub);
            const off = function () {
                const i = subs.indexOf(sub);
                if (i !== -1) subs.splice(i, 1);
            };
            rec.unloads.push(off);   // subscriber dropped when the mod tears down
            return off;
        }

        // Announce a possible theme change to every subscriber. Guarded so the
        // early-boot notifyModSettings call (from 85_js_startup.js, before this
        // fragment ran) is a clean no-op, exactly like notifyModSettings itself.
        //
        // With no subscriber this returns BEFORE touching any style, so the whole
        // feature costs nothing until a mod opts in — which matters because one
        // of its call sites (notifyModSettings) runs on every /state convergence.
        // Deliveries are change-detected on {name, dark}, so a mover that did not
        // move the theme (any other setting converging, any other mod toggling)
        // calls nobody.
        function notifyModTheme() {
            if (!window.__mods) return;
            const subs = window.__mods.themeSubs;
            if (!subs || !subs.length) return;
            // Re-entrancy: a subscriber is explicitly allowed to change a setting
            // from its handler, which re-enters here through _valueAccessor.set.
            // The nested call only raises _pending; the pass in flight aborts as
            // soon as it sees that (so nobody is handed a state that is no longer
            // on screen) and this loop re-runs it against the new one, so a
            // handler that picks a theme settles in two passes.
            //
            // BOUNDED, so two subscribers fighting over the theme cannot livelock
            // the UI thread. Exhausting the bound is a deliberate abandonment, not
            // a lost update: _last still holds the last state that was DELIVERED,
            // so whatever the runaway settled on differs from it and the next
            // mover announces it (and if it settled back, subscribers already have
            // that value). Cheap to reason about; the warning is the real signal.
            //
            // State lives on the function (not a fragment `const`) for the TDZ
            // reason in the header note.
            if (notifyModTheme._firing) { notifyModTheme._pending = true; return; }
            notifyModTheme._firing = true;
            try {
                for (let pass = 0; pass < 4; pass++) {
                    notifyModTheme._pending = false;
                    _fireThemeSubs(subs);
                    if (!notifyModTheme._pending) break;
                }
                if (notifyModTheme._pending) {
                    console.warn('[mods] theme subscribers keep moving the theme;'
                        + ' stopped after 4 passes — the next theme change will'
                        + ' announce whatever it settled on');
                }
            } finally {
                notifyModTheme._firing = false;
                notifyModTheme._pending = false;
            }
        }

        // One delivery pass. `_last` is updated BEFORE the loop so a re-entrant
        // notify compares against what is being announced, not the previous
        // announcement. Iterates a SNAPSHOT (a handler may subscribe, unsubscribe
        // or tear a mod down mid-fire — never skip or revisit). Each subscriber is
        // isolated — a throw is logged and the others still run, and it can never
        // break the mover that fired us — and gets its OWN object, so one cannot
        // mutate what the next sees.
        //
        // Three reasons a snapshot entry is skipped rather than called:
        //   - it was unsubscribed since the snapshot (an earlier subscriber tore
        //     its mod down);
        //   - its mod is mid-teardown (_runUnloads set rec.unloading; see there);
        //   - a subscriber already moved the theme, so the state in hand is NO
        //     LONGER on screen. Handing the rest of the list an obsolete payload
        //     would break the one guarantee this channel makes — that a callback
        //     sees the theme that is live — so the pass ABORTS and notifyModTheme
        //     re-runs it against the new state. The subscribers already called
        //     with the superseded state get the new one on that replay.
        function _fireThemeSubs(subs) {
            let state;
            try { state = _themeState(); } catch (_) { return; }
            const sig = _themeSig(state);
            if (sig === notifyModTheme._last) return;
            notifyModTheme._last = sig;
            const list = subs.slice();
            for (let i = 0; i < list.length; i++) {
                if (notifyModTheme._pending) return;          // state moved: replay
                const sub = list[i];
                if (subs.indexOf(sub) === -1) continue;       // gone mid-fire
                if (sub.rec && sub.rec.unloading) continue;   // mod mid-teardown
                try { sub.fn({ name: state.name, dark: state.dark }); }
                catch (e) {
                    console.error('[mods] theme subscriber failed:', e);
                }
            }
        }

        // ---- Control Panel settings extension (#71 boolean; #74 radio/select/
        // pane; #104 combo; #168 text) ----------------------------------------
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
            // #65: opts.mount ('mods' default | 'browser') picks the target pane.
            // 'browser' mounts into #set-browser-mods (in the Browser settings
            // pane, beside the Hosts list) instead of the per-host #set-mods, so a
            // browser-local mod can sit with the config it manages. Unknown / omit
            // -> the historical #set-mods, so every existing control is unchanged.
            const browserMount = (opts.mount === 'browser');
            const mountId = browserMount ? 'set-browser-mods' : 'set-mods';
            const host = document.getElementById(mountId);
            const section = document.createElement('div');
            section.className = 'set-section set-mod-setting';
            section.dataset.modId = rec.id;
            // #181: opts.mount ALSO names a Control Panel applet, so a mod can
            // say which topic its control belongs to instead of everything
            // mod-owned landing in one bucket. Deliberately this existing seam
            // rather than a new one: it is already a per-CALL-SITE placement
            // hint resolved to a DOM host (one mod may want two placements — a
            // color-scheme radio belongs in Desktop, a recording pane with the
            // terminals), every ctx.settings.* primitive and registerSettingsPane
            // already funnel through here, and its documented contract for an
            // unknown/omitted value is already "the historical shared bucket".
            //
            // The applet id space is CLOSED and core-owned (cpAppletFor, 81a):
            // an unrecognised string degrades to the Mods applet, it never mints
            // an applet. That is also what keeps `if (host)` below unreachable —
            // the DOM host stays #set-mods whatever the applet is, so an applet
            // id can never resolve to null and drop a mod's control silently.
            //
            // A 'browser' mount is resolved FIRST and gets no applet: it lands
            // in #set-pane-browser, a different pane with no grid.
            if (!browserMount) section.dataset.applet = cpAppletFor(opts.mount);
            if (opts.isBrowserGlobal !== false) {
                section.classList.add('set-browser-global');
                try {
                    if (currentSettingsTab !== 'local') section.style.display = 'none';
                } catch (_) {}
            }
            if (opts.title) {
                const t = document.createElement('div');
                t.className = 'set-title';
                // #181: placed inside a CORE applet, the section sits among core
                // controls, so it carries its mod's badge — placement is a hint,
                // not a claim, and an installed mod (#163) must not read as ours
                // by virtue of where it asked to sit. In the Mods applet
                // everything is mod-owned, so the badge would be noise.
                if (!browserMount && section.dataset.applet !== 'mods') {
                    t.appendChild(cpModBadge(rec.id));
                }
                t.appendChild(document.createTextNode(opts.title));
                section.appendChild(t);
            }
            if (host) host.appendChild(section);
            // The grid is built from what is actually MOUNTED, and mods mount and
            // leave LIVE (a toggle in the Mods pane, an initMod rollback), so
            // both edges have to tell it. Without the teardown call an applet
            // whose only section belonged to a just-disabled mod would keep an
            // icon that opens onto nothing.
            reconcileControlPanel();
            rec.unloads.push(function () {
                if (section.parentNode) section.parentNode.removeChild(section);
                reconcileControlPanel();
            });
            return section;
        }

        // Record a control entry on the shared list + wire its forget-on-teardown
        // (the DOM is removed by _controlSection's own onUnload). The list drives
        // reflect-on-open (renderModSettingsToggles) and /state convergence
        // (notifyModSettings).
        // #148: register-and-remember. Every one-slot desktop/menu/keys seam
        // returns a disposer; push it onto rec.unloads so a disable, or an
        // initMod rollback after a later throw, releases the slot exactly once.
        function _modTrack(rec, off) {
            if (typeof off === 'function') rec.unloads.push(off);
            return off;
        }
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
        //
        // #168: `unchanged` is an OPTIONAL override for the no-op test only.
        // Omitted (every caller but text) it is read()-equality, byte-for-byte
        // today's behaviour. text needs its own because its read() answers with
        // the fallback for a structurally broken stored value, which would make
        // an explicit "clear this" indistinguishable from a no-op and strand the
        // junk in the synced blob — see _modSettingText.
        function _valueAccessor(entry, key, read, coerce, valid, unchanged) {
            const accessor = {
                get: function () { return read(); },
                set: function (value) {
                    value = coerce(value);
                    if (!valid(value)) return;
                    if (unchanged ? unchanged(value) : (read() === value)) return;
                    getSettings()[key] = value;
                    savePrefs();                 // localStorage + /state push
                    entry.last = value;
                    entry.reflect();
                    if (entry.onChange) { try { entry.onChange(value); } catch (_) {} }
                    // #169: mover 1 of 5. A LOCAL pick never routes through
                    // applyThemeSettings, so hooking only the /state convergence
                    // would leave it unannounced until the next pull. AFTER
                    // entry.onChange on purpose — that is where the theme mod
                    // writes the vars notifyModTheme reads back, so a subscriber
                    // can never see the pre-change DOM. Cheap: it returns before
                    // reading any style unless a mod has subscribed.
                    notifyModTheme();
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
            } else if (kind === 'combo') {
                // Searchable variant of select: an <input list> + <datalist> the
                // user can TYPE into to substring-filter a long option list (a
                // native <select> only prefix-matches). Same _valueAccessor
                // persistence / validation / convergence as select — only the
                // widget DOM differs. The empty-value option (if any) becomes the
                // input placeholder, so a blank input reads as that "(default)"
                // label instead of a bare, blank datalist row.
                const row = document.createElement('div');
                row.className = 'set-row';
                if (opts.label) {
                    const lab = document.createElement('label');
                    lab.textContent = opts.label;
                    row.appendChild(lab);
                }
                const uid = 'set-mod-' + rec.id + '-' + key + '-list';
                const input = document.createElement('input');
                input.type = 'text';
                input.setAttribute('list', uid);
                const datalist = document.createElement('datalist');
                datalist.id = uid;
                for (const o of options) {
                    if (o.value === '') {
                        input.placeholder = o.label;   // blank input => this label
                        continue;                      // no empty datalist row
                    }
                    const op = document.createElement('option');
                    op.value = o.value;
                    if (o.label !== o.value) op.label = o.label;
                    datalist.appendChild(op);
                }
                row.appendChild(input);
                row.appendChild(datalist);
                section.appendChild(row);
                // Focus guard: a /state convergence must not clobber an in-progress
                // edit (the pane contract, applied to a text input).
                reflect = function () {
                    if (document.activeElement === input) return;
                    input.value = read();
                };
                bindChange = function (accessor) {
                    input.addEventListener('change', function () {
                        accessor.set(input.value);   // no-op for an unknown value
                        input.value = read();        // snap back garbage / show stored
                    });
                    // reflect() skips a /state convergence that lands while this
                    // input is focused (the focus guard). onChange still fires, so
                    // the mod repaints — but the widget can stay stale if the edit
                    // ends with no local change event. Reconcile the display on
                    // blur so a remote change made during an edit is never left
                    // visually stale.
                    input.addEventListener('blur', function () {
                        input.value = read();
                    });
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
            // #65: spec.mount ('mods' default | 'browser') picks the target pane.
            // A 'browser' mount lands in #set-pane-browser, which is .active ONLY
            // on the Browser tab — so the section is already hidden on every other
            // tab by the pane itself and must NOT also be .set-browser-global:
            // applyBrowserGlobalVisibility() display:none's those whenever the tab
            // isn't 'local', which would hide it exactly when the Browser tab
            // (its own tab) is open. So a 'browser' mount is forced non-browser-
            // global regardless of spec.isBrowserGlobal.
            const browserMount = spec.mount === 'browser';
            const section = _controlSection(rec, {
                title: spec.title,
                // #181: forward the hint VERBATIM for a non-browser mount rather
                // than flattening it to 'mods' — that flattening predates applet
                // ids and would silently drop a pane's applet placement, which is
                // exactly the case #181 wants (a pane-registering mod is the kind
                // that most deserves to sit with the topic it administers).
                // _controlSection closes the id space, so an unknown value still
                // lands in the Mods applet.
                mount: browserMount ? 'browser' : spec.mount,
                isBrowserGlobal: browserMount ? false : spec.isBrowserGlobal,
            });
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
        const _HELP_BLOCK_TYPES = { p: 1, bullet: 1, sub: 1, pre: 1 };
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
        //
        // The transport controls ride along too: fileApiPost(route, body, host)
        // already picks a per-route deadline, and a mod may override it with
        // opts.timeoutMs (0 = none) and/or hand in opts.signal so a long
        // transfer stays cancellable — the file manager's progress dialog
        // Cancel is exactly that. They are forwarded EXPLICITLY (never the whole
        // opts object) so a mod's own bookkeeping fields — host, offset,
        // recursive, overwrite, expected_sha256 — can never leak into the fetch
        // init and change how the request is made.
        function _modFileApi(route, body, opts) {
            const host = _modFileHost(opts && opts.host);
            if (!host) {
                return Promise.resolve({ ok: false, error: 'host_not_found' });
            }
            return fileApiPost(route, body, host, {
                timeoutMs: opts && opts.timeoutMs,
                signal: opts && opts.signal,
            });
        }

        // ---- ctx.session host routing (#85 / S12) ---------------------------
        // The session-RPC twin of _modFileHost/_modFileApi (#82): resolve a mod-
        // supplied host *id* to the host object, FAIL CLOSED (a known id -> that
        // host; ''/'local'/undefined -> the local broker; an UNKNOWN remote id ->
        // null so the op aborts as a synthetic no-host rather than silently hitting
        // local — a removed host must never quietly fall through to a kill on the
        // wrong broker), then POST a /session/* route. Returns {status, json} on
        // EVERY outcome (never rejects): the parsed body with its HTTP status, an
        // HTTP-<status> stub when the body isn't JSON (status PRESERVED — a non-JSON
        // 409 must still read as 409, so the session-destroy success path survives),
        // a status-0 no_host when the host won't resolve, or a status-0
        // {error:String(e)} on a transport failure. Byte-identical to the task
        // manager's old inline sessionPost (the local host id is literally 'local',
        // which hostById resolves, so a real session never reaches the ''/'local'
        // fallback branch).
        function _modSessionHost(hostId) {
            const h = hostById(hostId);
            if (h) return h;
            if (!hostId || hostId === 'local') return localHost();
            return null;
        }
        // A /session/* call is an RPC onto the owning broker's session loop,
        // which caps its own wait at RPC_TIMEOUT = 10s (app.py). Sit just above
        // that: long enough that the SERVER's timeout is what a mod sees (a real
        // 504-shaped answer it can report), short enough that a dead broker
        // doesn't hang the task manager forever. opts.timeoutMs / opts.signal
        // override + cancel, forwarded explicitly like _modFileApi.
        const SESSION_API_TIMEOUT_MS = 12000;
        function _modSessionApi(route, body, opts) {
            const host = _modSessionHost(opts && opts.host);
            if (!host) {
                return Promise.resolve(
                    { status: 0, json: { ok: false, error: 'no_host' } });
            }
            return hostFetch(host, route, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(body),
                timeoutMs: (opts && opts.timeoutMs !== undefined
                            && opts.timeoutMs !== null)
                    ? opts.timeoutMs : SESSION_API_TIMEOUT_MS,
                signal: opts && opts.signal,
            }).then(r => r.json()
                .then(j => ({ status: r.status, json: j }))
                .catch(() => ({ status: r.status,
                                json: { ok: false, error: 'HTTP ' + r.status } })))
             .catch(e => ({ status: 0, json: { ok: false, error: String(e) } }));
        }

        // ---- ctx.serverStore transport (#124, host routing #65) ------------
        // Resolve a mod-supplied host *id* to the host object, FAIL CLOSED —
        // the /mod-store twin of _modFileHost/_modSessionHost: a known id -> that
        // host; ''/'local'/undefined -> the local broker; an UNKNOWN remote id ->
        // null so the op aborts (no request) instead of silently writing the
        // wrong broker's registry. A server store is per-broker, so publish-to-
        // all (#65) routes each write to a specific host through here.
        function _modStoreHost(hostId) {
            const h = hostById(hostId);
            if (h) return h;
            if (!hostId || hostId === 'local') return localHost();
            return null;
        }
        // Fetch the generic per-mod server KV (GET current+metadata / one
        // revision, PUT an optimistic write) on the resolved host. Resolves to
        // {status, json} on EVERY outcome (never rejects): the parsed body with
        // its HTTP status, an HTTP-<status> stub when the body isn't JSON, a
        // status-0 no_host when the host won't resolve (fail closed, no request),
        // or a status-0 {error:String(e)} on a transport failure — so a caller
        // honors the broker's status contract verbatim (a 409 not_active /
        // conflict is data to rebase on, NOT an exception). `query` is an optional
        // pre-built querystring ('?rev=3'); `body` omitted -> a bodyless GET;
        // opts.host picks the target broker (omitted -> local, so #124 callers
        // are unchanged).
        // One shape for every /mod-store answer: the parsed body plus the HTTP
        // `status`. The status is written LAST, not first — a body carrying its
        // own `status` (a hostile or simply odd broker; the store can be read
        // across brokers since #174) must not be able to overwrite the transport
        // status a caller is about to branch on. A non-object body degrades to
        // just the status rather than throwing.
        function _withStatus(r) {
            const j = (r && r.json && typeof r.json === 'object'
                       && !Array.isArray(r.json)) ? r.json : {};
            return Object.assign({}, j, { status: r.status });
        }
        function _modStoreApi(method, modId, body, query, opts) {
            const host = _modStoreHost(opts && opts.host);
            if (!host) {
                return Promise.resolve(
                    { status: 0, json: { ok: false, error: 'no_host' } });
            }
            const path = '/mod-store/' + encodeURIComponent(modId) + (query || '');
            const o = { method: method };
            if (body !== undefined) {
                o.headers = { 'Content-Type': 'application/json' };
                o.body = JSON.stringify(body);
            }
            return hostFetch(host, path, o).then(r => r.json()
                .then(j => ({ status: r.status, json: j }))
                .catch(() => ({ status: r.status,
                                json: { ok: false, error: 'HTTP ' + r.status } })))
             .catch(e => ({ status: 0, json: { ok: false, error: String(e) } }));
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
            // Snapshot: a control's reflect/onChange could, in principle, mutate the
            // list (e.g. the #86 Mods pane toggling a mod whose teardown splices its
            // own entries out), so iterate a copy to never skip or revisit (#86).
            const list = window.__mods.settingToggles.slice();
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
            // #169: mover 2 of 5 — a cross-browser /state convergence that
            // carried a new `theme`. Once, AFTER the whole loop, not per entry:
            // every control has converged by here, so the DOM the subscribers
            // read is final, and one pull can only ever produce one
            // announcement.
            notifyModTheme();
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
            // Snapshot for the same reason as notifyModSettings: a reflect must never
            // be able to skip a sibling by mutating the live list mid-iteration (#86).
            const list = window.__mods.settingToggles.slice();
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
        //
        // #157: `localInfo._ok` records whether a real payload ever arrived. The
        // {} of a failure and the {} of a broker that reports no pins are the
        // same value to every caller, but only the FIRST may be retried after a
        // login, so the boot policy needs the two told apart. `refetch` drops the
        // memo for exactly that retry (see notifyModsHostAuth); learnLocalBrokerId
        // has long since read its copy, so re-asking cannot disturb it.
        function localInfo(refetch) {
            if (refetch) localInfo._p = null;
            if (!localInfo._p) {
                localInfo._p = (async function () {
                    try {
                        const r = await hostFetch(localHost(), '/info');
                        if (!r.ok) return {};
                        const j = await r.json();
                        if (j && typeof j === 'object') {
                            localInfo._ok = true;
                            return j;
                        }
                        return {};
                    } catch (_) { return {}; }
                })();
            }
            return localInfo._p;
        }

        // Resolve a reported policy map into the pins this build will ENFORCE.
        //
        // Two things happen here. (1) Only well-shaped entries survive — a real
        // boolean for an id this build actually registered; a pin naming a mod
        // this broker does not ship is dropped (it is still shown, as "not
        // installed here", by the editor, which reads the SERVER's catalog).
        // (2) A pinned-ON mod's `requires` are pinned ON too, transitively:
        // without that, "pinned on" is a lie the moment a browser has the
        // dependency locally disabled (#121's precondition leaves the dependent
        // blocked) or disables it later (the disable cascade at _takeDown would
        // tear the pinned mod down). An EXPLICIT pin always wins over an implied
        // one, so pinning `editor:false` and `scratchpad:true` leaves editor off
        // and scratchpad visibly blocked ("needs: editor") rather than silently
        // overriding the operator's own off.
        //
        // The reverse walk works because a dependency is always registered
        // EARLIER than its dependents, so walking registration order BACKWARDS
        // visits every dependent before its dependencies and one pass closes a
        // whole chain. #163: that used to be guaranteed by the static ordering
        // guard over ui._MODS; it is now guaranteed at RUNTIME by
        // _topoSortRegistered(), which loadMods calls BEFORE this. Resolving
        // pins any earlier would silently drop every pin naming an installed
        // mod (they are not in `registered` until their package has run).
        function _resolvePins(raw) {
            const pins = {};
            if (!raw || typeof raw !== 'object' || Array.isArray(raw)) return pins;
            const known = new Set(window.__mods.registered.map(
                function (m) { return m.id; }));
            const explicit = new Set();
            for (const id of Object.keys(raw)) {
                if (typeof raw[id] !== 'boolean' || !known.has(id)) continue;
                pins[id] = raw[id];
                explicit.add(id);
            }
            const regs = window.__mods.registered;
            for (let i = regs.length - 1; i >= 0; i--) {
                const m = regs[i];
                if (pins[m.id] !== true) continue;
                for (const dep of (m.requires || [])) {
                    if (known.has(dep) && !explicit.has(dep)) pins[dep] = true;
                }
            }
            return pins;
        }
        // The pin in force for one mod: true (pinned on), false (pinned off), or
        // null (unpinned — the browser's own choice governs).
        function _pin(id) {
            const p = window.__mods.policy;
            return (p && typeof p[id] === 'boolean') ? p[id] : null;
        }

        // Init one mod through the full isolation path. Returns a structured
        // result (never throws) so loadMods() and the test API can both drive it:
        //   ctxVersion mismatch  -> refused, no init/mount     (reason 'ctxVersion')
        //   duplicate id/slot    -> ModConflictError, no last-wins (reason 'conflict')
        //   init() throws        -> rolled back via onUnload    (reason 'init-threw')
        // Siblings and core are unaffected in every case.
        function initMod(decl, opts) {
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
            // #121: dependency precondition. A mod may declare requires:[ids] —
            // other mods that must be ACTIVE for it to init. With the static
            // ordering guard (every required id is a KNOWN mod EARLIER in _MODS),
            // boot's deps-first loop never trips this; it only fires for a direct
            // __test.run()/live-enable of a dependent whose dependency is off,
            // leaving the mod safely blocked (no slot claimed, no partial init).
            // Default defensively — a raw test decl may skip registerMod's
            // normalization — so the documented never-throws contract still holds.
            //
            // #163: a mod _topoSortRegistered put IN a cycle can never satisfy
            // its own precondition, so refuse it with its own structured reason
            // rather than letting it read as an ordinary dependency block. A
            // mod merely blocked BY a cycle falls through to the `requires`
            // check below and reports as blocked, which is exactly what it is.
            if (window.__mods.cycleState
                    && window.__mods.cycleState[id] === 'cycle') {
                console.error('[mods] refusing "' + id
                    + '": it is in a dependency cycle');
                return { ok: false, reason: 'cycle' };
            }
            const reqs = Array.isArray(decl.requires) ? decl.requires : [];
            const missing = reqs.filter(function (dep) { return !window.__mods.active.has(dep); });
            if (missing.length) {
                console.info('[mods] "' + id + '" needs inactive mod(s): ' + missing.join(', '));
                return { ok: false, reason: 'requires', missing: missing };
            }
            // Claim the slot (the id) BEFORE init so addStatusItem etc. can find
            // the record; on any init throw we roll back and release it.
            const rec = { id: id, version: (decl.version || '0'), unloads: [] };
            window.__mods.active.set(id, rec);
            let ctx;
            try {
                ctx = makeCtx(id, rec);
                // #182: did a HUMAN just turn this mod on, in this browser, in
                // this gesture? True ONLY on the setModEnabled path — the
                // Control Panel checkbox — and false for every other way a mod
                // comes up: boot, a restored or synced preference, a broker-side
                // pin applied after login (_applyPolicyLive), a dependency
                // cascade, __test.run().
                //
                // The distinction exists because a mod may take a real-world
                // action on being enabled that it must NOT take merely on being
                // initialized. The update mod opts its broker in to reaching
                // github.com; doing that from a plain init would reinterpret a
                // preference stored months ago — under documentation promising
                // enabling caused no egress — as fresh consent, and would let a
                // remote operator's mod pin cause a broker to make outbound
                // requests. A click is consent. An init is not.
                ctx.enabledByUser = !!(opts && opts.byUser);
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

        // ---- per-mod enable, on top of the master gate (#86 / S13) ----------
        // Operator enable/disable PER MOD, layered on the broker's mods_enabled
        // master gate (master off => loadMods returns before any of this, so every
        // mod is off regardless of per-mod state — the gate stays absolute). State
        // is stored loader-private in localStorage, NOT in the synced /state
        // settings blob: a /state pull from another browser would otherwise
        // live-tear-down a mod you are actively using (e.g. an open file manager).
        // So this is deliberately PER-BROWSER.
        //
        // #116 (S14): the set holds "ids TOGGLED AWAY from their declared
        // default" (registerMod's defaultEnabled). Absent-from-set => the mod is
        // at its default. For every default-ON mod this is byte-for-byte the old
        // "disabled list" (present => off), so there is ZERO migration; a
        // default-OFF mod (git, #116) is the mirror image — present in the set
        // means it has been ENABLED, absent means off. The key name stays
        // `webterm:mods:disabled` for that same zero-migration reason (a legacy
        // set of default-on ids keeps meaning "off"). localStorage failures are
        // swallowed: the toggle then has no durable effect but never throws, and
        // reflect() re-reads the real state.
        const MODS_DISABLED_KEY = 'webterm:mods:disabled';
        function _modsDisabled() {
            let raw = null;
            try { raw = localStorage.getItem(MODS_DISABLED_KEY); } catch (_) {}
            const set = new Set();
            if (raw) {
                try {
                    const arr = JSON.parse(raw);
                    if (Array.isArray(arr)) {
                        for (const id of arr) if (typeof id === 'string') set.add(id);
                    }
                } catch (_) {}
            }
            return set;
        }
        function _writeModsDisabled(set) {
            try {
                localStorage.setItem(MODS_DISABLED_KEY,
                    JSON.stringify(Array.from(set)));
            } catch (_) {}
        }
        // A mod's DECLARED default (registerMod defaultEnabled). Unknown id ->
        // true (treated like a default-on mod so a stale id is inert, not stuck).
        //
        // #163: an INSTALLED mod's default comes from the CATALOG, not from its
        // own registerMod call, and the broker reports it unconditionally false.
        // Installing a mod on one broker must not silently switch it on for
        // every browser that loads that broker's page — install is two steps
        // (install, then enable), and a package is free to declare
        // `defaultEnabled: true` (or omit it, which means true) in its own JS.
        function _modDefault(id) {
            const row = _modCatalogRow(id);
            if (row && row.source === 'installed') return row.default_enabled === true;
            const rec = window.__mods.registered.find(
                function (m) { return m.id === id; });
            return rec ? rec.defaultEnabled !== false : true;
        }
        // Enabled = default XOR overridden. The set lists ids toggled AWAY from
        // their default (#116): present flips the default, absent keeps it. For a
        // default-on mod this is the old `!disabled.has(id)`; for a default-off
        // mod it is the mirror (present => enabled).
        //
        // #157: a PIN from this broker's mod policy outranks all of it. The
        // per-browser set is not consulted at all for a pinned mod — it is left
        // untouched in localStorage, so unpinning restores whatever that browser
        // had chosen before. Layering the policy UNDER the set instead (as a
        // replacement default) would invert on a policy change: declared-on +
        // browser-toggled-off + operator pins off => the set's "away from
        // default" entry flips it back ON, which is the opposite of both wishes.
        function isModEnabled(id) {
            const pin = _pin(id);
            if (pin !== null) return pin;
            const def = _modDefault(id);
            return _modsDisabled().has(id) ? !def : def;
        }
        // #121 enable cascade, extracted (#157) so the operator toggle and the
        // post-login policy apply drive the SAME sequencing. Bring `decl` up, then
        // a single FORWARD pass over the mods registered AFTER it: any that is
        // itself enabled, not yet active, and whose every `requires` is now active
        // gets init'd. The ordering guard (a dependency is always registered
        // EARLIER than its dependents) makes this deps-first, so one pass suffices
        // even for a chain — no fixpoint loop.
        //
        // #182: `opts` describes THIS call only and is deliberately not passed
        // down the cascade below. Turning on a mod that drags a dependency up
        // with it is a click aimed at the one mod named in the checkbox; the
        // dependency was enabled by implication, and implication is not consent.
        function _bringUp(decl, opts) {
            const regs = window.__mods.registered;
            if (!window.__mods.active.has(decl.id)) initMod(decl, opts);
            for (let i = regs.indexOf(decl) + 1; i < regs.length; i++) {
                const m = regs[i];
                if (window.__mods.active.has(m.id) || !isModEnabled(m.id)) continue;
                // Only mods that DECLARE a dependency participate — an inactive
                // mod with no `requires` is a boot init-FAILURE, not a dependency
                // block, so enabling something else must not silently retry it
                // (deps.every is vacuously true for []).
                const deps = m.requires || [];
                if (deps.length && deps.every(function (d) { return window.__mods.active.has(d); })) {
                    initMod(m);
                }
            }
        }
        // #121 disable cascade, extracted (#157) alongside _bringUp. Nothing
        // still-active may be left requiring `id`. First compute the transitive
        // closure of active mods that depend on it via a FORWARD pass (dependencies
        // precede dependents, so `id` and every already-doomed dep is seen before
        // the mods that require it — a single pass gets the full chain). Then tear
        // them down dependents-FIRST (reverse index order) and `id` itself LAST, so
        // no mod is unloaded while an active mod still requires it. Each mod's own
        // teardown stays per-mod LIFO inside disableMod; sequencing the disableMod
        // calls is what makes the cross-mod teardown correct.
        function _takeDown(id) {
            const regs = window.__mods.registered;
            const doomed = new Set([id]);
            for (const m of regs) {
                if (m.id === id || !window.__mods.active.has(m.id)) continue;
                const deps = m.requires || [];
                if (deps.some(function (d) { return doomed.has(d); })) doomed.add(m.id);
            }
            doomed.delete(id);
            for (let i = regs.length - 1; i >= 0; i--) {
                if (doomed.has(regs[i].id)) disableMod(regs[i].id);
            }
            disableMod(id);
        }
        // Persist the per-mod choice AND apply it live, reusing the SAME isolated
        // initMod/disableMod paths boot uses (so a live toggle exercises production
        // code, not a parallel harness). Only a KNOWN (registered) id is accepted
        // (returns null otherwise, so a stale/renamed id can't accrete junk). The
        // live init/teardown is itself gated on the master switch: with master off
        // we persist the preference but never init (the gate is absolute). Returns
        // the new enabled state.
        //
        // #157: a PINNED mod refuses the write outright — no persist, no live
        // init/teardown — and reports the pin. The Mods pane disables its
        // checkbox, so this is belt-and-braces for a programmatic caller (the
        // __test API, a keybinding) that would otherwise write a preference the
        // resolver ignores and leave the pane showing a value that isn't in force.
        function setModEnabled(id, on) {
            const decl = window.__mods.registered.find(
                function (m) { return m.id === id; });
            if (!decl) return null;
            const pin = _pin(id);
            if (pin !== null) return pin;
            on = !!on;
            const set = _modsDisabled();
            // Store an id only while it is toggled AWAY from its default (#116);
            // at-default means absent, so a default-off mod that gets disabled
            // and a default-on mod that gets enabled both leave NO junk entry.
            if (on === _modDefault(id)) set.delete(id); else set.add(id);
            _writeModsDisabled(set);
            if (window.__mods.masterEnabled !== false) {
                if (on) {
                    // byUser: this function IS the Control Panel checkbox's
                    // handler, so reaching here with on=true means somebody
                    // clicked. #182 uses it as the consent signal; see initMod.
                    _bringUp(decl, { byUser: true });
                    // #167: the cascade just registered this mod's window kinds,
                    // so a persisted record that boot had to skip (its kind was
                    // unregistered then, so buildAppWindow returned null and left
                    // the record intact) can finally be built — which is exactly
                    // the "re-enabling its mod restores it faithfully" promise
                    // that null return is documented on (54_js_app_windows_store).
                    // Placed at the CALLER, not inside _bringUp: _applyPolicyLive
                    // calls _bringUp in a loop, and one pass after the whole
                    // cascade beats N passes interleaved with mid-flight inits.
                    // Sound only because init is SYNCHRONOUS by contract (initMod
                    // calls init(ctx) and ignores any returned promise), so every
                    // kind the cascade brings up is registered by the time we get
                    // here — an async init would re-open this very race.
                    restoreAppWindowsAfterMods();
                } else {
                    _takeDown(id);
                }
            }
            // #113: a mod that ships help.md but registers no help CARDS (e.g.
            // clock) has no teardown that refreshes Help, so toggling it wouldn't
            // live-update an open Help window's enabled-filter. Nudge it here (a
            // typeof-guarded no-op when Help is closed or the help mod is absent);
            // harmless double-refresh for card-registering mods.
            _refreshHelpIfOpen();
            // #169: mover 3 of 5. Enabling a theme mod applies its vars inside
            // _bringUp; disabling one puts :root back if (unlike today's theme
            // mod) it unloads them, and can take a theme mod down as a #121
            // dependency cascade. Change-detected, so toggling any OTHER mod
            // calls nobody.
            notifyModTheme();
            return on;
        }

        // The boot entry (called once by 90_js_mod_boot.js). Gates on the
        // broker's mods_enabled (runtime, via the memoized /info; fail-open); when
        // enabled it mounts the Mods pane (#86), prunes any stale disabled ids, then
        // inits every per-mod-ENABLED registered mod, each isolated.
        //
        // #163 resequenced this, and the order is LOAD-BEARING, not cosmetic:
        //   1. await localInfo() -> stash policyRaw + catalog. Do NOT resolve
        //      pins yet.
        //   2. master off -> return before a single script is fetched (the gate
        //      stays absolute).
        //   3. await _loadInstalledPackages(catalog) — the installed mods'
        //      registerMod calls land here.
        //   4. _topoSortRegistered() — re-establish dependency order over
        //      shipped ∪ installed.
        //   5. policy = _resolvePins(policyRaw). It MUST follow 3-4:
        //      _resolvePins drops any pin whose id is not already in
        //      `registered`, so resolving at the old position would silently
        //      discard every pin naming an installed mod and mis-resolve the
        //      implied set (its backward pass assumes dependency order).
        //   6. _mountModsManagerPane() — also after 3, because _modRegisterPane
        //      calls spec.render() exactly ONCE, so mounting before the
        //      installed mods registered would freeze a stale row set.
        //   7. prune -> the existing forward boot loop, unchanged.
        async function loadMods() {
            if (window.__mods.booted) return;
            window.__mods.booted = true;
            // #163 rescue hatch. Before the /info fetch, before any package, and
            // before the pane: an installed mod that bricks the desktop must not
            // also be able to make the Control Panel unreachable. The alternative
            // is a curl POST /mods/uninstall or mods_enabled:false in the config,
            // and the latter needs the restart this issue exists to avoid.
            if (_nomodsRequested()) {
                window.__mods.masterEnabled = false;
                window.__mods.policyResolved = true;
                console.warn('[mods] ?nomods=1 — no mod was loaded, initialized'
                    + ' or fetched on this page load');
                return;
            }
            let enabled = true;
            try {
                const info = await localInfo();
                if (info && info.mods_enabled === false) enabled = false;
                // #157: the broker's pins — but only STASHED here. See step 5.
                // An older broker has no mod_policy key and _resolvePins({}) is
                // {}, i.e. byte-for-byte today's behaviour.
                window.__mods.policyRaw = (info && info.mod_policy) || null;
                window.__mods.catalog = (info && Array.isArray(info.mods))
                    ? info.mods : [];
                window.__mods.policyAuthoritative = !!localInfo._ok;
            } catch (_) {}
            window.__mods.masterEnabled = enabled;
            if (!enabled) {
                window.__mods.policyResolved = true;
                console.info('[mods] disabled by broker (mods_enabled=false)');
                return;
            }
            // Isolated: a defect anywhere in the installed-package machinery
            // must not cost the SHIPPED mods their init. loadMods is
            // fire-and-forget (90_js_mod_boot.js), so a throw escaping here
            // would skip the boot loop entirely and leave the desktop with no
            // mods at all — a strictly worse outcome than "the installed ones
            // are missing". _loadInstalledPackages already never rejects; this
            // covers the rest.
            try {
                await _loadInstalledPackages(window.__mods.catalog);
                _topoSortRegistered();
            } catch (e) {
                console.error('[mods] loading installed packages failed'
                    + ' — continuing with the shipped mods:', e);
            }
            // From here every registration routes through _lateRegister, which
            // re-runs the sort. Set even if the block above threw: an unsorted
            // late arrival is worse than a refused one.
            window.__mods.sorted = true;
            window.__mods.policy = _resolvePins(window.__mods.policyRaw);
            window.__mods.policyResolved = true;
            try { _mountModsManagerPane(); }
            catch (e) { console.error('[mods] the Mods pane failed to mount:', e); }
            // Prune ids for mods that no longer exist (renamed/removed) so the set
            // can never grow unbounded junk; write back only if it actually changed.
            //
            // #163 widened the keep-set to registered ∪ CATALOG ids. The set
            // holds ids TOGGLED AWAY from their declared default (#116), so
            // dropping an entry means "put this mod back to its default" —
            // which for an installed mod (default OFF) is off, and for a shipped
            // one is usually on. Either way it silently DISCARDS a choice the
            // operator made. Pruning against `registered` alone would do exactly
            // that to every installed mod whose script 404s, times out, or fails
            // SRI: the one page load on which the mod cannot speak for itself is
            // the one that would forget what you asked for.
            //
            // Residual, stated rather than hidden: an id survives an uninstall
            // and REINSTALL that both happen before this browser sees a catalog
            // without it, so the new package inherits the old one's toggle.
            // localStorage is browser-local and cannot be inspected server-side,
            // which is why the install dialog reports adopts_existing_state for
            // the halves that CAN be (the /mod-store value and the pin) and says
            // so about the half that cannot.
            //
            // ...and the whole pass is skipped unless the boot /info actually
            // ANSWERED. A 401 (no token stored yet) / network failure gives an
            // EMPTY catalog, and pruning against that would delete every
            // installed mod's choice on exactly the page load that could not see
            // that they exist. Pruning is pure hygiene; it must never run from a
            // view it knows is incomplete.
            if (window.__mods.policyAuthoritative) {
                const known = new Set(window.__mods.registered.map(
                    function (m) { return m.id; }));
                for (const row of window.__mods.catalog) {
                    if (row && typeof row.id === 'string' && row.id) known.add(row.id);
                }
                const disabled = _modsDisabled();
                let pruned = false;
                for (const id of Array.from(disabled)) {
                    if (!known.has(id)) { disabled.delete(id); pruned = true; }
                }
                if (pruned) _writeModsDisabled(disabled);
            }
            for (const decl of window.__mods.registered.slice()) {
                // #116: gate on the resolved enable state (default XOR override),
                // so a default-off mod (git) stays off until the operator opts in.
                if (!isModEnabled(decl.id)) {
                    console.info('[mods] "' + decl.id + '" disabled by operator');
                    continue;
                }
                // #163: a mod that registered another one from its own init()
                // reaches _lateRegister, which may already have brought this one
                // up. Skipping is not cosmetic — initMod on an active id is a
                // ModConflictError, which would report a real conflict where
                // there is none.
                if (window.__mods.active.has(decl.id)) continue;
                initMod(decl);
            }
            // Observable "the boot loop is done" marker (the acceptance reads
            // it). The LATE-registration gate is `sorted`, set earlier — see
            // registerMod.
            window.__mods.bootComplete = true;
            _renderManagerRows();
            // #169: mover 5 of 5 — boot itself. The theme mod is FIRST in
            // ui._MODS, so today every subscriber inits after the vars are
            // already applied and ctx.theme.get() in its init is right; this one
            // line makes that independent of _MODS ORDER, which is otherwise a
            // silent trap (a subscriber ordered ahead of the theme mod would read
            // the pre-theme :root defaults and never be told otherwise). A no-op
            // in the ordered case: the change detector was primed at subscribe
            // time with the same state.
            notifyModTheme();
        }

        // Reconcile every registered mod with the CURRENT resolved policy, using
        // the same isolated cascades a live operator toggle uses. Teardowns run
        // first, in reverse registration order (so _takeDown never unloads a mod
        // an active dependent still requires), then the bring-ups run forward
        // (deps precede dependents, so a chain comes up in one pass).
        function _applyPolicyLive() {
            if (window.__mods.masterEnabled === false) return;   // gate absolute
            const regs = window.__mods.registered;
            for (let i = regs.length - 1; i >= 0; i--) {
                const m = regs[i];
                if (window.__mods.active.has(m.id) && !isModEnabled(m.id)) {
                    _takeDown(m.id);
                }
            }
            let broughtUp = false;
            for (const m of regs.slice()) {
                if (!window.__mods.active.has(m.id) && isModEnabled(m.id)) {
                    _bringUp(m);
                    broughtUp = true;
                }
            }
            // #167: this path (a post-login pin apply, #157) is a deferred boot in
            // all but name — mods come up with no reload, so without this a
            // browser that booted with file-manager/scratchpad off would run
            // without the windows their records describe until the user happens to
            // refresh. Same self-guarded, idempotent pass boot uses; only after a
            // bring-up, since a teardown-only reconcile has nothing new to build.
            if (broughtUp) restoreAppWindowsAfterMods();
            if (window.__mods._reflectManager) {
                try { window.__mods._reflectManager(); } catch (_) {}
            }
            _refreshHelpIfOpen();
            // #169: mover 4 of 5 — a post-login pin apply (#157) that brought a
            // theme mod up or took one down. Once, after the whole reconcile.
            notifyModTheme();
        }

        // #157: a host just authenticated (called from the login overlay, 63).
        // This closes the hole in "the broker pins it for every browser that
        // loads its page": a browser arriving with no stored token 401s on the
        // boot /info, so the pins are unknown and every mod comes up at this
        // browser's own default — and entering the token heals sockets and
        // windows in place, it does NOT reload, so without this the page would
        // run unpinned until the user happens to refresh.
        //
        // Deliberately narrow. It fires only for the HOME broker (a remote host's
        // token says nothing about our pins), only while the boot snapshot was
        // NEVER authoritative, and only once — so an ordinary later /state or
        // /info round trip can never re-resolve the policy and tear down a mod
        // being used, which is the whole reason the snapshot is frozen.
        async function notifyModsHostAuth(hostId) {
            if (!window.__mods || !window.__mods.policyResolved) return;
            if (window.__mods.policyAuthoritative) return;
            // #163: this page has no mods and is not going to get any — the
            // broker's master gate said so, or ?nomods=1 did. Without this the
            // rescue hatch would still FETCH every installed package the moment
            // the user logged in, which is exactly the thing it exists to stop.
            if (window.__mods.masterEnabled === false) return;
            let lh = null;
            try { lh = localHost(); } catch (_) {}
            if (!lh || hostId !== lh.id) return;
            let info = {};
            try { info = await localInfo(true); } catch (_) {}
            if (!localInfo._ok) return;              // still cannot ask; try again next login
            window.__mods.policyAuthoritative = true;
            if (info && info.mods_enabled === false) {
                // The master gate is a PAGE-LOAD decision (loadMods returns
                // before mounting anything when it is off). This session already
                // fail-opened past it, so pinning individual mods on top of that
                // would be applying half a policy: say so and leave the session
                // alone rather than tearing the desktop's mods out from under it.
                console.warn('[mods] this broker disables mods '
                    + '(mods_enabled=false); it applies on the next page load');
                return;
            }
            // #163: a 401'd boot saw no catalog either, so it fetched NO
            // installed package. Load them now — otherwise typing your password
            // leaves every installed mod missing until a reload, which is
            // exactly the hole #157 closed for the pins. Deduped on
            // (kind, id, gen, file), so a package the boot already loaded is
            // never fetched or executed twice (D1).
            //
            // This is not a violation of D1's "next page load" rule, and the
            // distinction is worth being precise about: it is the SAME page load
            // finishing a boot it could not perform without a token. The window
            // it opens is real but tiny and inherited from #157 — if another
            // client installs a mod between this page's 401 and this login, this
            // page picks that mod up rather than the set that existed at load.
            // The alternative is a browser that must be reloaded after every
            // first login, which is the bug #157 fixed.
            window.__mods.policyRaw = (info && info.mod_policy) || null;
            if (info && Array.isArray(info.mods)) window.__mods.catalog = info.mods;
            await _loadInstalledPackages(window.__mods.catalog);
            // Same order boot uses, and for the same reason: sort first, then
            // resolve pins (a pin naming an installed mod is dropped unless that
            // mod is already in `registered`). Any straggler that lands after
            // this rides _lateRegister, which repeats both steps.
            _topoSortRegistered();
            window.__mods.policy = _resolvePins(window.__mods.policyRaw);
            _renderManagerRows();
            _applyPolicyLive();
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
            // #169: the derived theme and the live subscriber COUNT — the
            // acceptance asserts that a fixture mod's ctx.theme.onChange fires on
            // a pick and that disabling the mod drops its subscriber (the count
            // returns to what it was), which is the one hygiene property no
            // source-text test can prove.
            theme: function () { return _themeState(); },
            themeSubscribers: function () {
                return (window.__mods.themeSubs || []).length;
            },
            // #86 (S13): the per-mod enable surface the Mods-pane acceptance drives.
            // setEnabled persists + applies live (master-gated) via the SAME path
            // the pane checkbox uses; the rest are read-only inspectors.
            setEnabled: function (id, on) { return setModEnabled(id, on); },
            isEnabled: function (id) { return isModEnabled(id); },
            disabledIds: function () { return Array.from(_modsDisabled()); },
            masterEnabled: function () { return window.__mods.masterEnabled; },
            // #157: the resolved pin map in force (including the pins a pinned-ON
            // mod's `requires` imply) and one mod's pin — true / false / null.
            // Read-only inspectors: the acceptance asserts that a pin beats the
            // per-browser set, that setEnabled refuses a pinned id, and that
            // pinning a dependent on also pins its dependency on.
            policy: function () { return Object.assign({}, window.__mods.policy); },
            pinOf: function (id) { return _pin(id); },
            // #163: the union status model the Mods pane renders (and S5's
            // provenance badges read) — one row per catalog package OR
            // registration, joined on id. The acceptance asserts a cycle pair
            // reads cycle/blocked-by-cycle, a 404'd script reads fetch-failed, a
            // script that registers nothing reads no-register, and a package
            // that registers the wrong id reads wrong-id — none of which have a
            // `registered` entry to render from.
            statusRows: function () { return _modStatusRows(); },
            statusOf: function (id) {
                return _modStatusRows().find(
                    function (r) { return r.id === id; }) || null;
            },
            // The boot catalog snapshot and the resolved registration ORDER, so
            // the acceptance can prove the runtime topological sort ran and left
            // the shipped order byte-identical when nothing installed takes part.
            catalog: function () { return (window.__mods.catalog || []).slice(); },
            order: function () {
                return window.__mods.registered.map(function (m) { return m.id; });
            },
            cycles: function () {
                return Object.assign({}, window.__mods.cycleState);
            },
            registered: function () {
                return window.__mods.registered.map(function (m) {
                    // #106: expose the declared boot default so an acceptance test
                    // can assert an opt-in mod (clipboard) ships defaultEnabled:false.
                    return { id: m.id, tiers: (m.tiers || []).slice(),
                             defaultEnabled: (m.defaultEnabled !== false),
                             // #121: declared deps, so the Playwright acceptance can
                             // assert a dependent (e.g. #124 scratchpad) requires: [...].
                             requires: (m.requires || []).slice() };
                });
            },
        };
