        // ---- runtime-installed mod packages (#163 / S4) ---------------------
        // The client half of runtime mod install. A SHIPPED mod is spliced into
        // the one inline <script> at assembly time and has called registerMod by
        // the time loadMods() runs; an INSTALLED mod is a separate classic
        // <script src="/mods/<id>/<gen>/<file>.js"> this fragment injects from
        // the boot catalog (/info's `mods`, whose rows carry `source`), so its
        // registerMod lands ASYNCHRONOUSLY, after loadMods has already awaited
        // /info.
        //
        // D1 — a mod script is fetched AT MOST ONCE per page load, and an
        // install/uninstall/replace takes effect on the NEXT page load. This is
        // forced, not chosen: JavaScript global lexical bindings cannot be
        // removed, so a mod whose top level says `const DB = …` cannot be
        // re-executed in this page (the second execution dies with
        // "Identifier 'DB' has already been declared") and a `var` one instead
        // hits registerMod's duplicate-id throw. _takeDown() is a TEARDOWN, not
        // an unloader. So nothing here ever re-executes a package, there is no
        // live replace and no live-uninstall teardown — the same contract #157's
        // pins already ship ("applies the next time a browser loads that page").
        //
        // Everything is same-origin and content-addressed: CSP is
        // `script-src 'sha256-…' 'self'` (#143/#146), the URL carries the
        // generation hash, and `integrity` pins the exact bytes — so a
        // replacement published mid-boot can neither be served under the old URL
        // nor silently substituted under it.

        // Rescue hatch: `?nomods=1` makes loadMods() return before it fetches or
        // inits anything, so an installed mod that bricks the desktop cannot
        // also make the Control Panel (where you would uninstall it) unreachable.
        // Read only — never written to localStorage or /state, so it cannot
        // become sticky state.
        //
        // Deliberately NOT scrubbed from the URL the way ?token= is. The token
        // scrub exists because a URL credential leaks (#144); `nomods` is not a
        // credential, and replaceState'ing it away would mean a reload silently
        // re-enables the very mod you are here to remove — which is the one
        // thing this flag exists to prevent.
        function _nomodsRequested() {
            try {
                return new URLSearchParams(window.location.search)
                    .get('nomods') === '1';
            } catch (_) { return false; }
        }

        // Lazily-created bag on window.__mods. Object.create(null) so a mod id
        // can never reach Object.prototype (ids are `[a-z0-9][a-z0-9-]{0,63}`,
        // so `__proto__` is unrepresentable anyway — this is belt and braces),
        // and lazy so a caller reaching one of these before this fragment's
        // top-level ran is `undefined`-safe rather than a TDZ throw (the
        // fragment header's rule).
        function _modBag(name) {
            const m = window.__mods;
            if (!m[name]) m[name] = Object.create(null);
            return m[name];
        }

        // The boot catalog row for an id, or null. The catalog is /info's `mods`
        // — shipped rows first, then installed rows topologically sorted — and
        // every row carries `source`.
        function _modCatalogRow(id) {
            const cat = window.__mods.catalog;
            if (!Array.isArray(cat)) return null;
            for (const row of cat) {
                if (row && row.id === id) return row;
            }
            return null;
        }
        function _modIsRegistered(id) {
            return window.__mods.registered.some(function (m) { return m.id === id; });
        }

        // Each segment is encodeURIComponent'd even though the install/scan
        // grammar already rejects '?', '#', '%', ':' and every control char: the
        // URL builder must be correct on its own terms, not because something
        // upstream happens to be strict today.
        function _modAssetUrl(id, gen, name) {
            return '/mods/' + encodeURIComponent(id) + '/'
                + encodeURIComponent(gen) + '/' + encodeURIComponent(name);
        }

        // The per-package load record, created once per (id) per page load.
        //   state: 'loading' | 'loaded' | 'fetch-failed' | 'timeout'
        //        | 'cycle' | 'blocked-by-cycle' | 'wrong-id'
        // It is a LOAD state, not a mod state: "loaded but never registered"
        // (no-register), "off", "blocked" and "failed" are derived by
        // _modStatusRow, which joins this with the registration.
        function _modPackage(row) {
            const bag = _modBag('packages');
            let pkg = bag[row.id];
            if (pkg) return pkg;
            pkg = {
                id: row.id,
                gen: (typeof row.gen === 'string') ? row.gen : '',
                version: (typeof row.version === 'string') ? row.version : '',
                title: (typeof row.title === 'string' && row.title)
                    ? row.title : row.id,
                scripts: Array.isArray(row.scripts)
                    ? row.scripts.filter(function (n) { return typeof n === 'string' && n; })
                    : [],
                styles: Array.isArray(row.styles)
                    ? row.styles.filter(function (n) { return typeof n === 'string' && n; })
                    : [],
                integrity: (row.integrity && typeof row.integrity === 'object')
                    ? row.integrity : {},
                state: 'loading',
                wrongId: null,
                done: false,
            };
            bag[row.id] = pkg;
            return pkg;
        }

        // Inject one asset element and resolve when it settles. NEVER rejects —
        // {ok:true} on `load`, {ok:false} on `error` (a 404, a transport
        // failure, or an SRI MISMATCH all arrive as `error`). The ENTIRE body,
        // element construction through appendChild, is inside the try: a promise
        // that can reject is a promise a caller has to defend against, and this
        // one's whole contract is that it does not.
        //
        // Deduplicated on KIND + URL, and the URL encodes (id, gen, file): the
        // post-login retry re-walks the whole catalog, and without this it would
        // inject a second copy of every script the boot already loaded — a
        // second execution, which D1 says is fatal. The kind is in the key
        // because a <link> and a <script> are different injections; the install
        // grammar already forces .css/.js apart, so this only stops a future
        // relaxation there from silently turning a script into a stylesheet's
        // cached promise and never inserting the <script> at all.
        function _loadModAsset(kind, url, build) {
            const inflight = _modBag('assetLoads');
            const key = kind + ' ' + url;
            if (inflight[key]) return inflight[key];
            const p = new Promise(function (resolve) {
                const finish = function (ok) {
                    if (finish.settled) return;
                    finish.settled = true;
                    resolve({ ok: ok, url: url });
                };
                try {
                    const el = build(url);
                    el.addEventListener('load', function () { finish(true); });
                    el.addEventListener('error', function () {
                        console.error('[mods] failed to load', url);
                        finish(false);
                    });
                    (document.head || document.documentElement).appendChild(el);
                } catch (e) {
                    console.error('[mods] could not inject', url, e);
                    finish(false);
                }
            });
            inflight[key] = p;
            return p;
        }

        // Fetch every installed package the catalog names. Resolves (never
        // rejects) when every script has settled OR the deadline passes.
        //
        // PACKAGES load in parallel; a package's OWN scripts load in its
        // manifest order, one after the next. Both halves matter and they are
        // different problems:
        //   - Across packages, ordering is irrelevant because
        //     _topoSortRegistered() sorts the REGISTRATIONS afterwards, and
        //     document-ordered async (`script.async = false`) would let one slow
        //     file head-of-line block every other mod on the page.
        //   - Within a package the topo sort says nothing at all — it orders mod
        //     declarations, not the files of one mod. A package whose second
        //     file reads a global its first file defined would work or not
        //     depending on network timing, which is exactly the class of bug
        //     that is impossible to reproduce. `scripts` is an ORDERED list in
        //     the manifest; this honours that. A single-script package (the
        //     common case) pays nothing.
        // A package that spans several scripts should still call registerMod
        // from its LAST one: a registration is acted on the moment it arrives,
        // so registering from the first file would let the mod init before its
        // siblings had loaded.
        //
        // STYLES are injected but NOT awaited and NOT part of the package's
        // state. This mirrors shipped mod CSS exactly (ui.py splices every mod's
        // stylesheet into the page at assembly time, independent of the runtime
        // gate — "present but inert", because a disabled mod's selectors match
        // nothing until its JS adds the markup). So an installed mod's CSS is
        // also live regardless of enable/pin/init, and a teardown cannot remove
        // it — the same posture, deliberately, not an oversight. A style that
        // 404s or fails SRI is logged by _loadModAsset and is not fatal.
        //
        // A row carrying a broker-side `error` (requires_cycle /
        // blocked_by_cycle) is NOT FETCHED AT ALL — it gets a status row and
        // nothing else.
        function _loadInstalledPackages(catalog) {
            // Function-local, not a fragment-level const: a hoisted function
            // reading a not-yet-initialized fragment `let`/`const` throws a TDZ
            // ReferenceError that disables the whole mod, and CI never runs this
            // JS (see the 86 header note).
            const MOD_SCRIPT_TIMEOUT_MS = 5000;
            const rows = Array.isArray(catalog) ? catalog : [];
            const pending = [];
            const started = [];
            for (const row of rows) {
                // One bad row must not cost the others their load — the loop
                // builds URLs and elements, and every one of those can throw.
                try {
                    const p = _startPackage(row);
                    if (p) { started.push(p.pkg); pending.push(p.wait); }
                } catch (e) {
                    console.error('[mods] could not start installed package',
                        row && row.id, e);
                }
            }
            if (!pending.length) return Promise.resolve();
            return new Promise(function (resolve) {
                let settled = false;
                const finish = function () {
                    if (settled) return;
                    settled = true;
                    resolve();
                };
                // A PROCEED-ANYWAY deadline, NOT a cancel: nothing can cancel an
                // in-flight <script>. When one lands after this fires it still
                // executes, still calls registerMod, and _lateRegister brings it
                // up — the row just says `timeout` until then.
                const timer = setTimeout(function () {
                    for (const pkg of started) {
                        if (!pkg.done) pkg.state = 'timeout';
                    }
                    console.warn('[mods] some installed packages had not loaded'
                        + ' after ' + MOD_SCRIPT_TIMEOUT_MS
                        + ' ms; booting without them');
                    finish();
                }, MOD_SCRIPT_TIMEOUT_MS);
                Promise.all(pending).then(
                    function () { clearTimeout(timer); finish(); },
                    function () { clearTimeout(timer); finish(); });
            });
        }

        // Begin ONE installed package: mark it requested, inject its styles, and
        // chain its scripts in manifest order. Returns {pkg, wait} or null when
        // the row is not fetchable (already settled, a broker-side cycle error,
        // or a malformed row). `wait` never rejects.
        function _startPackage(row) {
            if (!row || typeof row !== 'object') return null;
            if (row.source !== 'installed') return null;
            if (typeof row.id !== 'string' || !row.id) return null;
            const pkg = _modPackage(row);
            if (pkg.done) return null;          // already settled this page load
            if (row.error) {
                pkg.state = (row.error === 'requires_cycle')
                    ? 'cycle' : 'blocked-by-cycle';
                pkg.done = true;
                return null;
            }
            if (!pkg.gen || !pkg.scripts.length) {
                console.error('[mods] installed row "' + row.id
                    + '" carries no generation/scripts; skipping it');
                pkg.state = 'fetch-failed';
                pkg.done = true;
                return null;
            }
            // This page ASKED for this package — the gate _lateRegister checks,
            // so a script that lands minutes later for something this page never
            // requested cannot bring a mod up.
            _modBag('requested')[row.id] = true;
            for (const name of pkg.styles) {
                _loadModAsset('css', _modAssetUrl(pkg.id, pkg.gen, name),
                    function (url) {
                        const link = document.createElement('link');
                        link.rel = 'stylesheet';
                        const sri = pkg.integrity[name];
                        if (sri) link.integrity = sri;
                        link.href = url;
                        return link;
                    });
            }
            let chain = Promise.resolve(true);
            for (const name of pkg.scripts) {
                chain = chain.then(function (okSoFar) {
                    return _loadModAsset('js', _modAssetUrl(pkg.id, pkg.gen, name),
                        function (url) {
                            const s = document.createElement('script');
                            // async, NOT document-ordered: this package's files
                            // are already serialized by the chain, and ordering
                            // it against OTHER packages is what would head-of-
                            // line block them.
                            s.async = true;
                            // Binds the REGISTRATION to the package: registerMod
                            // reads document.currentScript and refuses a
                            // declaration whose id is not this one.
                            s.dataset.modPackage = pkg.id;
                            const sri = pkg.integrity[name];
                            if (sri) s.integrity = sri;
                            s.src = url;
                            return s;
                        }).then(function (r) { return okSoFar && r.ok; });
                });
            }
            const wait = chain.then(function (ok) {
                pkg.done = true;
                // A wrong-id refusal is a verdict on the package, not on the
                // transport — do not overwrite it with a load outcome.
                if (pkg.state !== 'wrong-id') {
                    pkg.state = ok ? 'loaded' : 'fetch-failed';
                }
                // A package that settles AFTER boot (a deadline straggler, or
                // the post-login batch) has no other repaint: if it registers,
                // _lateRegister repaints, but a package that loads and registers
                // NOTHING would otherwise sit on a stale `timeout` row.
                if (window.__mods.sorted) _renderManagerRows();
            });
            return { pkg: pkg, wait: wait };
        }

        // The package id of the script currently executing, or null.
        //
        // A CORRECTNESS CONVENTION, NOT A BOUNDARY. #163 settled fork-trust: an
        // installed mod runs same-origin with the page's token and could call
        // registerMod from a timeout (where currentScript is null), or reach in
        // and rewrite this. What it buys is that an accepted `x-wrapper`
        // package whose script registers `clock` cannot silently collide with
        // the shipped registration, show the wrong provenance, and make the
        // broker's dependency analysis disagree with the client's.
        function _currentPackageId() {
            let el = null;
            try { el = document.currentScript; } catch (_) { el = null; }
            if (!el || !el.dataset) return null;
            return el.dataset.modPackage || null;
        }

        // ---- requires: from positional to runtime topological (#163 / §6) ---
        // Shipped ordering used to be positional in ui._MODS, guarded by a test
        // that made cycles / self-require / missing deps UNREPRESENTABLE — which
        // is why the loader had no sort. An installed set has no such list, so
        // the invariant moves from test-established to runtime-established.
        //
        // ONE graph over shipped ∪ installed ids. An edge to an id that is in
        // NEITHER is DROPPED from the sort and recorded as missingRequires — it
        // must not contribute an indegree, or an installed mod requiring a
        // shipped mod would come out marked cyclic.
        //
        // Kahn's residual is NOT the cycle set: for `A→B, B→A, C→A` the residual
        // is {A,B,C} but C is merely blocked BY a cycle. So Kahn gives the order
        // and Tarjan splits the residual into in-cycle (an SCC of size > 1, or a
        // self-loop) and blocked-by-cycle, which are distinct statuses.
        //
        // Reorders window.__mods.registered IN PLACE, ties broken by the CURRENT
        // index — so when nothing installed participates the result is the
        // identity permutation and the shipped order is byte-preserved. That is
        // what lets _bringUp, _takeDown, _applyPolicyLive and _resolvePins keep
        // working verbatim: they all assume dependency-precedes-dependent.
        //
        // One documented gap, which no ordering can close: cycle members are
        // appended in their existing relative order and are NOT in dependency
        // order (they cannot be), so _resolvePins's single backward pass does
        // not propagate an implied pin THROUGH a cycle member. It does not
        // matter — a mod in a cycle never inits, so a pin implied by one governs
        // nothing; an EXPLICIT pin on the far side is unaffected because
        // explicit always wins.
        function _topoSortRegistered() {
            const regs = window.__mods.registered;
            const index = new Map();
            for (let i = 0; i < regs.length; i++) index.set(regs[i].id, i);
            const universe = new Set(index.keys());
            const cat = window.__mods.catalog;
            if (Array.isArray(cat)) {
                for (const row of cat) {
                    if (row && typeof row.id === 'string' && row.id) universe.add(row.id);
                }
            }
            const edges = new Map();
            const missing = Object.create(null);
            for (const m of regs) {
                const out = [];
                const absent = [];
                for (const dep of (m.requires || [])) {
                    if (index.has(dep)) {
                        // A self-require lands here too and becomes a self-loop:
                        // permanent indegree 1 -> residual -> Tarjan calls it a
                        // cycle. No special case needed.
                        if (out.indexOf(dep) === -1) out.push(dep);
                    } else if (!universe.has(dep)) {
                        absent.push(dep);
                    }
                    // else: known to the broker but not registered here (its
                    // package 404'd, or it is disabled-and-absent) — not an
                    // edge, and NOT "missing" either; the row shows `blocked`.
                }
                edges.set(m.id, out);
                if (absent.length) missing[m.id] = absent;
            }
            const order = _modKahn(regs, edges, index);
            const placed = new Set(order);
            const residual = [];
            for (const m of regs) if (!placed.has(m.id)) residual.push(m.id);
            const inCycle = _modCycleMembers(residual, edges);
            const cycleState = Object.create(null);
            for (const id of residual) {
                cycleState[id] = inCycle.has(id) ? 'cycle' : 'blocked-by-cycle';
            }
            if (residual.length) {
                console.error('[mods] dependency cycle: '
                    + residual.map(function (id) {
                          return id + '=' + cycleState[id]; }).join(', '));
            }
            window.__mods.cycleState = cycleState;
            window.__mods.missingRequires = missing;
            // Reorder IN PLACE (same array identity — __test.registered, the
            // Mods pane and every cascade hold references to it). Cycle members
            // keep their relative order and go last; they never init anyway.
            const byId = new Map();
            for (const m of regs) byId.set(m.id, m);
            const sorted = order.concat(residual).map(function (id) {
                return byId.get(id); });
            regs.length = 0;
            for (const m of sorted) regs.push(m);
            return regs;
        }

        // Kahn's algorithm, ties broken by the CURRENT registration index. That
        // tie-break is what makes the sort the identity permutation whenever the
        // existing order is already a valid topological order: at every step the
        // lowest-index not-yet-emitted mod has all its dependencies emitted, so
        // it is ready, and it is the one picked.
        function _modKahn(regs, edges, index) {
            const indeg = new Map();
            const dependents = new Map();
            for (const m of regs) {
                indeg.set(m.id, edges.get(m.id).length);
                dependents.set(m.id, []);
            }
            for (const m of regs) {
                for (const dep of edges.get(m.id)) dependents.get(dep).push(m.id);
            }
            const ready = [];
            for (const m of regs) if (indeg.get(m.id) === 0) ready.push(m.id);
            const out = [];
            while (ready.length) {
                let best = 0;
                for (let i = 1; i < ready.length; i++) {
                    if (index.get(ready[i]) < index.get(ready[best])) best = i;
                }
                const id = ready.splice(best, 1)[0];
                out.push(id);
                for (const dependent of dependents.get(id)) {
                    const n = indeg.get(dependent) - 1;
                    indeg.set(dependent, n);
                    if (n === 0) ready.push(dependent);
                }
            }
            return out;
        }

        // The residual's members that are actually IN a cycle: an SCC of size
        // > 1, or a self-loop. Tarjan, iterative so a deep graph cannot blow the
        // stack. Mirrors modinstall._cycle_members so the two sides agree.
        function _modCycleMembers(residual, edges) {
            const inside = new Set(residual);
            const idx = new Map();
            const low = new Map();
            const onStack = new Map();
            const stack = [];
            const found = new Set();
            let counter = 0;
            for (const root of residual) {
                if (idx.has(root)) continue;
                const work = [[root, 0]];
                while (work.length) {
                    const frame = work[work.length - 1];
                    const node = frame[0];
                    if (frame[1] === 0) {
                        idx.set(node, counter);
                        low.set(node, counter);
                        counter++;
                        stack.push(node);
                        onStack.set(node, true);
                    }
                    const children = (edges.get(node) || []).filter(
                        function (d) { return inside.has(d); });
                    if (frame[1] < children.length) {
                        const child = children[frame[1]];
                        frame[1]++;
                        if (!idx.has(child)) work.push([child, 0]);
                        else if (onStack.get(child)) {
                            low.set(node, Math.min(low.get(node), idx.get(child)));
                        }
                        continue;
                    }
                    work.pop();
                    if (work.length) {
                        const parent = work[work.length - 1][0];
                        low.set(parent, Math.min(low.get(parent), low.get(node)));
                    }
                    if (low.get(node) === idx.get(node)) {
                        const component = [];
                        for (;;) {
                            const member = stack.pop();
                            onStack.set(member, false);
                            component.push(member);
                            if (member === node) break;
                        }
                        if (component.length > 1
                                || (edges.get(node) || []).indexOf(node) !== -1) {
                            for (const member of component) found.add(member);
                        }
                    }
                }
            }
            return found;
        }

        // ---- a registration that arrives after the boot loop ----------------
        // Deliberately narrow: accepted ONLY if this page ASKED for the package
        // (window.__mods.requested), so a script that lands minutes later for a
        // package this page never requested cannot bring a mod up. A refusal
        // un-registers the declaration and logs loudly.
        //
        // Two callers: the MOD_SCRIPT_TIMEOUT_MS deadline overrun (the script
        // was never cancelled, so it still executes), and notifyModsHostAuth
        // (the packages a 401'd boot never saw).
        function _lateRegister(decl) {
            const reg = window.__mods.registered;
            if (!_modBag('requested')[decl.id]) {
                const i = reg.indexOf(decl);
                if (i !== -1) reg.splice(i, 1);
                console.error('[mods] refusing a late registration for "'
                    + decl.id + '": this page never requested that package');
                return { ok: false, reason: 'not-requested' };
            }
            const pkg = _modBag('packages')[decl.id];
            if (pkg && pkg.state !== 'wrong-id') pkg.state = 'loaded';
            // Re-establish the ordering invariant every cascade assumes, THEN
            // re-resolve the pins: _resolvePins drops a pin whose id is not yet
            // in `registered`, so a pin naming this mod only becomes real here
            // (the same hole #157 closed for the post-login path).
            _topoSortRegistered();
            const before = _modPinSig(window.__mods.policy);
            window.__mods.policy = _resolvePins(window.__mods.policyRaw);
            _renderManagerRows();
            if (window.__mods.masterEnabled === false) {
                return { ok: true, id: decl.id, brought: false };
            }
            // If the pin map MOVED, reconcile everything — not just this mod.
            // A pin naming this mod pins its `requires` on too, and those
            // dependencies are registered EARLIER, which _bringUp cannot reach:
            // it inits `decl` and then walks FORWARD only. Without this, pinning
            // a late-arriving mod on while its dependency is locally off leaves
            // BOTH inactive forever, because for a deadline straggler no later
            // _applyPolicyLive ever runs.
            if (_modPinSig(window.__mods.policy) !== before) {
                _applyPolicyLive();      // reconciles, repaints, announces
                return { ok: true, id: decl.id,
                         brought: window.__mods.active.has(decl.id) };
            }
            let brought = false;
            if (isModEnabled(decl.id)) {
                _bringUp(decl);
                brought = window.__mods.active.has(decl.id);
            }
            if (brought) restoreAppWindowsAfterMods();
            if (window.__mods._reflectManager) {
                try { window.__mods._reflectManager(); } catch (_) {}
            }
            _refreshHelpIfOpen();
            notifyModTheme();
            return { ok: true, id: decl.id, brought: brought };
        }
        // A stable signature of a resolved pin map, so "did the pins move?" is
        // one string compare over a small flat {id: bool}.
        function _modPinSig(pins) {
            if (!pins) return '';
            return Object.keys(pins).sort().map(
                function (id) { return id + '=' + (pins[id] ? 1 : 0); }).join(',');
        }

        // Rebuild the Mods pane's rows. _modRegisterPane calls spec.render()
        // EXACTLY ONCE, so a row set built before the installed mods registered
        // would be frozen stale — this is how a late registration reaches the
        // pane. A no-op when the pane was never mounted (master gate off).
        function _renderManagerRows() {
            const fn = window.__mods && window.__mods._rebuildManagerRows;
            if (typeof fn !== 'function') return;
            try { fn(); }
            catch (e) { console.error('[mods] Mods pane rebuild failed:', e); }
        }

        // ---- "Mods" Control Panel pane (#86 / S13) --------------------------
        // CORE loader UI (NOT a toggleable mod) listing every registered mod with
        // an enable checkbox + its declared trust tiers + live status (active /
        // off / failed). Built on the S1 pane scaffold (_modRegisterPane) through a
        // STATIC loader-owned rec whose teardown never runs — the pane is permanent
        // core chrome, so it never appears in registered/active and can't disable
        // itself. Idempotent: a second loadMods() can't double-mount it. Mounted
        // ONLY when the master gate is on (loadMods returns before here when off),
        // so master-off means no mod UI at all and the gate's meaning stays crisp.
        // The reflect closure is registered via _trackControl (kind:'pane'), so it
        // re-syncs on Control Panel open AND on every /state pull (idempotent, read
        // only) like every other pane.
        // #163: the rows are the UNION of catalog packages and registered
        // declarations (_modStatusRows), NOT a walk of `registered` — a mod in a
        // dependency cycle, one whose script 404'd and one whose SRI did not
        // match never call registerMod, so a registered[]-driven pane cannot
        // show them at all. And because _modRegisterPane calls spec.render()
        // EXACTLY ONCE, the row set is rebuildable (_rebuildRows) so a late
        // registration is not frozen out of a pane built before it landed.
        function _mountModsManagerPane() {
            if (window.__mods._managerMounted) return;
            window.__mods._managerMounted = true;
            const rec = { id: '__mods_manager__', unloads: [] };  // permanent; unloads never run
            let rows = [];
            let listEl = null;
            function _reflectManager() {
                const byId = Object.create(null);
                for (const s of _modStatusRows()) byId[s.id] = s;
                for (const r of rows) {
                    const s = byId[r.id];
                    if (!s) continue;
                    r.cb.checked = s.enabled;
                    // #157: a mod this broker PINS is not this browser's call —
                    // show the pin and lock the box. #163 also locks a row with
                    // no registration (nothing to init). Reflected (not decided)
                    // here: the pin came from the boot policy snapshot, so this
                    // runs on every /state pull without changing what is in force.
                    r.cb.disabled = !s.toggleable;
                    r.pin.textContent = (s.pin === null) ? ''
                        : (s.pin ? 'pinned on' : 'pinned off');
                    r.pin.title = (s.pin === null) ? ''
                        : ('this broker pins ' + r.id + ' ' + (s.pin ? 'on' : 'off')
                           + ' for every browser that loads its page');
                    r.status.textContent = s.label;
                    r.status.dataset.state = s.state;
                }
            }
            function _rebuildRows() {
                if (!listEl) return;
                rows = [];
                listEl.textContent = '';
                for (const s of _modStatusRows()) {
                    const row = document.createElement('div');
                    row.className = 'set-mod-row';
                    row.dataset.modId = s.id;
                    row.dataset.modSource = s.source;
                    const label = document.createElement('label');
                    label.className = 'set-check set-mod-toggle';
                    const cb = document.createElement('input');
                    cb.type = 'checkbox';
                    const mid = s.id;
                    cb.addEventListener('change', function () {
                        setModEnabled(mid, cb.checked);
                        _reflectManager();   // refresh status after the live toggle
                    });
                    label.appendChild(cb);
                    const name = document.createElement('span');
                    name.className = 'set-mod-name';
                    name.textContent = s.id;
                    label.appendChild(name);
                    const status = document.createElement('span');
                    status.className = 'set-mod-status';
                    label.appendChild(status);
                    const pin = document.createElement('span');   // #157
                    pin.className = 'set-mod-pin';
                    label.appendChild(pin);
                    row.appendChild(label);
                    const tiers = document.createElement('div');
                    tiers.className = 'set-mod-tiers';
                    if (s.tiers.length) {
                        for (const t of s.tiers) {
                            const b = document.createElement('span');
                            b.className = 'set-mod-tier';
                            b.textContent = t;
                            tiers.appendChild(b);
                        }
                    } else {
                        const b = document.createElement('span');
                        b.className = 'set-mod-tier set-mod-tier-none';
                        b.textContent = 'unspecified';
                        tiers.appendChild(b);
                    }
                    row.appendChild(tiers);
                    rows.push({ id: s.id, cb: cb, status: status, pin: pin });
                    listEl.appendChild(row);
                }
                _reflectManager();
            }
            _modRegisterPane(rec, {
                id: 'mods',
                title: 'Mods',
                isBrowserGlobal: true,    // per-browser state -> local tab only
                render: function () {
                    const wrap = document.createElement('div');
                    const hint = document.createElement('div');
                    hint.className = 'set-hint';
                    hint.textContent = 'Enable or disable installed mods (this '
                        + 'browser). The broker’s master switch can disable all at '
                        + 'once, and a mod this broker pins (see “Mods on this '
                        + 'broker”) is locked here.';
                    wrap.appendChild(hint);
                    listEl = document.createElement('div');
                    listEl.className = 'set-mods-list';
                    _rebuildRows();
                    wrap.appendChild(listEl);
                    return wrap;
                },
                reflect: function () { _reflectManager(); },
            });
            window.__mods._reflectManager = _reflectManager;
            window.__mods._rebuildManagerRows = _rebuildRows;
        }

        // ---- the status model (#163 / §5) -----------------------------------
        // The pane renders the UNION of catalog packages and registered
        // declarations, JOINED ON ID — not a re-render of registered[]. Cycle
        // rows, 404s and SRI mismatches never call registerMod, so they have no
        // entry there and a registered[]-driven pane simply cannot show them.
        //
        // Row shape (S5 renders this; it must not have to re-derive it):
        //   id, title, version, source: 'shipped'|'installed', gen|null,
        //   state, label, requires[], missing[], missingRequires[], tiers[],
        //   registered, active, enabled, pin: true|false|null, toggleable
        //
        // state is one of:
        //   active            init'd and running
        //   off               registered, not enabled (or pinned off)
        //   blocked           enabled but a declared `requires` is not active;
        //                     the label distinguishes a dep that is registered
        //                     but off from one that never loaded / is not
        //                     installed here
        //   cycle             in a dependency cycle
        //   blocked-by-cycle  depends into one
        //   failed            deps satisfied, init() threw
        //   fetch-failed      404 / transport failure / SRI MISMATCH
        //   timeout           still in flight when the boot deadline passed
        //   loading           in flight (transient; only visible on the
        //                     post-login path, which repaints when it settles)
        //   no-register       the script loaded but never called registerMod.
        //                     Includes a top-level throw and — the case D1
        //                     makes likely — a global name collision with core
        //                     ("Identifier 'X' has already been declared"),
        //                     which is raised at INSTANTIATION: the element
        //                     still fires `load` and the failure is reported to
        //                     window.onerror. (A genuine PARSE error fires
        //                     `error` instead, so it reads as fetch-failed.)
        //   wrong-id          the package registered a DIFFERENT mod id
        function _modStatusRows() {
            const byId = Object.create(null);
            for (const m of window.__mods.registered) byId[m.id] = m;
            const rows = [];
            const seen = Object.create(null);
            const cat = window.__mods.catalog;
            if (Array.isArray(cat)) {
                for (const row of cat) {
                    if (!row || typeof row.id !== 'string' || !row.id) continue;
                    if (seen[row.id]) continue;
                    seen[row.id] = true;
                    rows.push(_modStatusRow(row.id, row, byId[row.id] || null));
                }
            }
            // A registration with no catalog row: an older/headless broker, a
            // boot whose /info 401'd, or a __test.run fixture. Appended in
            // registration order so the pane is never empty just because /info
            // could not be read.
            for (const m of window.__mods.registered) {
                if (seen[m.id]) continue;
                seen[m.id] = true;
                rows.push(_modStatusRow(m.id, _modCatalogRow(m.id), m));
            }
            return rows;
        }

        function _modStatusRow(id, catRow, decl) {
            const mods = window.__mods;
            const pkg = _modBag('packages')[id] || null;
            const source = ((catRow && catRow.source === 'installed') || pkg)
                ? 'installed' : 'shipped';
            const requires = decl
                ? (decl.requires || []).slice()
                : (((catRow && catRow.requires) || []).filter(
                      function (d) { return typeof d === 'string' && d; }));
            const missingRequires = (_modBag('missingRequires')[id] || []).slice();
            const enabled = isModEnabled(id);
            const active = mods.active.has(id);
            const pin = _pin(id);
            const missing = [];
            let state = null;
            let label = '';
            // The client's own verdict wins over the broker's: it sees the whole
            // shipped ∪ installed graph, where the broker only sorts the
            // installed half.
            let cyc = _modBag('cycleState')[id] || null;
            if (!cyc && catRow && catRow.error === 'requires_cycle') cyc = 'cycle';
            if (!cyc && catRow && catRow.error === 'blocked_by_cycle') {
                cyc = 'blocked-by-cycle';
            }
            if (cyc === 'cycle') {
                state = 'cycle';
                label = 'dependency cycle';
            } else if (cyc) {
                state = 'blocked-by-cycle';
                label = 'blocked by a dependency cycle';
            } else if (pkg && pkg.state === 'fetch-failed') {
                // Checked BEFORE the registration branch on purpose. A package
                // with several scripts can have one register while another 404s
                // or fails SRI: the mod is live but the package is incomplete,
                // and a row reading `active` would hide that completely.
                state = 'fetch-failed';
                label = decl ? 'fetch failed (partly loaded)' : 'fetch failed';
            } else if (!decl) {
                if (pkg && pkg.state === 'wrong-id') {
                    state = 'wrong-id';
                    label = 'wrong id: the package registered "'
                        + (pkg.wrongId || '?') + '"';
                } else if (pkg && pkg.state === 'timeout') {
                    state = 'timeout';
                    label = 'timed out';
                } else if (pkg && pkg.state === 'loading') {
                    state = 'loading';
                    label = 'loading…';
                } else {
                    state = 'no-register';
                    label = 'loaded, registered nothing';
                }
            } else if (!enabled) {
                state = 'off';
                label = 'off';
            } else if (active) {
                state = 'active';
                label = 'active';
            } else {
                for (const dep of requires) {
                    if (!mods.active.has(dep)) missing.push(dep);
                }
                if (missing.length) {
                    state = 'blocked';
                    // Distinguish a dep that IS here but off from one that is
                    // absent — "needs: editor" and "needs: x-notes (not
                    // installed)" are very different problems.
                    label = 'needs: ' + missing.map(function (dep) {
                        if (_modIsRegistered(dep)) return dep;
                        if (missingRequires.indexOf(dep) !== -1) {
                            return dep + ' (not installed)';
                        }
                        return dep + ' (not loaded)';
                    }).join(', ');
                } else {
                    state = 'failed';
                    label = 'failed';
                }
            }
            return {
                id: id,
                title: (catRow && typeof catRow.title === 'string' && catRow.title)
                    ? catRow.title : (pkg ? pkg.title : id),
                version: decl ? decl.version
                    : ((catRow && catRow.version) || (pkg && pkg.version) || ''),
                source: source,
                gen: pkg ? pkg.gen : null,
                state: state,
                label: label,
                requires: requires,
                missing: missing,
                missingRequires: missingRequires,
                tiers: decl ? (decl.tiers || []).slice() : [],
                registered: !!decl,
                active: active,
                enabled: enabled,
                pin: pin,
                // A row with no registration has nothing to init and a pinned
                // row is not this browser's call — setModEnabled refuses both,
                // so the checkbox must not pretend otherwise.
                toggleable: !!decl && pin === null,
            };
        }
