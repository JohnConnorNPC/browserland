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
        // an enable checkbox + its declared trust tiers + the `permissions` it
        // declares (#193 — the half the broker actually CHECKS, unlike the tiers
        // beside them) + live status (active / off / failed).
        // Built on the S1 pane scaffold (_modRegisterPane) through a
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
                    // #163 (S5): the Uninstall button's only live state. Read
                    // ONLY from a bag this page writes when ITS OWN uninstall
                    // succeeded — never from a fetch, never from server state,
                    // so this stays a pure repaint on every /state pull.
                    if (r.un) {
                        const gone = !!_modBag('uninstalled')[r.id];
                        const busy = !!_modBag('opInFlight')[r.id];
                        r.un.disabled = gone || busy;
                        r.un.textContent = gone ? 'uninstalled'
                            : (busy ? 'working…' : 'Uninstall');
                        r.un.title = gone
                            ? 'uninstalled on this broker — still running in this '
                              + 'page, gone on the next page load'
                            : (busy ? 'a request for this mod is in flight'
                                    : 'remove this mod from this broker');
                    }
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
                    label.appendChild(_modSourceBadge(s));
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
                    // #193: what this mod is PERMITTED to do, beside the tiers
                    // it merely claims. Rendered for shipped and installed rows
                    // alike, and never as a pass the pane did not get — see
                    // _modPermissionsView for why an absent key is 'unchecked'.
                    const perms = document.createElement('div');
                    perms.className = 'set-mod-tiers set-mod-perms';
                    perms.dataset.permState = s.permissions.state;
                    perms.title = _modPermissionsTitle(s.permissions.state);
                    if (s.permissions.state === 'declared') {
                        for (const p of s.permissions.names) {
                            const b = document.createElement('span');
                            b.className = 'set-mod-tier set-mod-perm';
                            b.textContent = p;
                            perms.appendChild(b);
                        }
                    } else {
                        const b = document.createElement('span');
                        b.className = 'set-mod-tier set-mod-tier-none '
                            + 'set-mod-perm';
                        b.textContent = _modPermissionsText(
                            s.permissions.state);
                        perms.appendChild(b);
                    }
                    row.appendChild(perms);
                    // #163 (S5): Uninstall, on INSTALLED rows only. A shipped
                    // mod is in the page's own bundle and cannot be removed by
                    // any broker call, so offering the control there would be a
                    // button that can only ever fail.
                    const actions = document.createElement('div');
                    actions.className = 'set-mod-actions';
                    let un = null;
                    if (s.source === 'installed') {
                        un = document.createElement('button');
                        un.type = 'button';
                        un.className = 'set-mod-uninstall';
                        un.textContent = 'Uninstall';
                        un.addEventListener('click', function () {
                            _modUninstallDialog(mid);
                        });
                        actions.appendChild(un);
                    }
                    row.appendChild(actions);
                    rows.push({ id: s.id, cb: cb, status: status, pin: pin,
                                un: un });
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
                    // #163 (S5): the operator entry point. It targets the LOCAL
                    // broker — the one that served this page — so there is no
                    // build skew to feature-detect: if this code is running,
                    // that broker has /mods/install. (A headless broker serves
                    // no page and a master-gate-off broker never mounts this
                    // pane, so both are already excluded.)
                    const head = document.createElement('div');
                    head.className = 'set-mods-head';
                    const install = document.createElement('button');
                    install.type = 'button';
                    install.className = 'set-mod-install';
                    install.textContent = 'Install a mod…';
                    install.title = 'add a mod to this broker from a folder or a '
                        + 'single .js file';
                    install.addEventListener('click', function () {
                        _modInstallDialog();
                    });
                    head.appendChild(install);
                    wrap.appendChild(head);
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
        //   permissions: {state, names[]} (#193 — see _modPermissionsView),
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
                // #193: from the CATALOG ROW only — never from the
                // registration. registerMod deliberately does not carry
                // `permissions`: a runtime copy nothing guarantees to match the
                // manifest the broker linted would be worse than no copy, since
                // it is the one an operator would read while deciding.
                permissions: _modPermissionsView(catRow),
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

        // ---- the `permissions` display (#193) -------------------------------
        // What a catalog row says about `permissions`, as ONE of four display
        // states. The PRESENCE OF THE KEY is the whole protocol, and that is
        // #157's rule applied literally: a new key is invisible to an old
        // broker, so a row with no `permissions` key at all means "this broker
        // cannot tell you" — never an error, and above all never a pass. A
        // client that read that absence as "declares nothing" would convert an
        // absent answer into a positive claim about the mod, which is the exact
        // shape of the bug #157 exists to prevent.
        //
        //   unchecked   no catalog row, or a row with no `permissions` key —
        //               an older broker, a headless one, an /info that 401'd.
        //               NOTHING was checked and the pane says exactly that.
        //   undeclared  the key is present and NOT a list (null on the wire):
        //               this broker does check, and THIS generation was
        //               installed before it started — grandfathered, still
        //               serving, still undeclared.
        //   none        an explicit []: the manifest's positive claim to use
        //               none of them. Deliberately NOT the same rendering as
        //               `undeclared` — collapsing the two would put a claim
        //               nobody made into the mod's mouth.
        //   declared    a non-empty list, which for an installed mod this
        //               broker checked against the package's own source at
        //               install time.
        function _modPermissionsView(catRow) {
            if (!catRow || !('permissions' in catRow)) {
                return { state: 'unchecked', names: [] };
            }
            const raw = catRow.permissions;
            if (!Array.isArray(raw)) return { state: 'undeclared', names: [] };
            const names = [];
            for (const p of raw.slice(0, 16)) {
                const one = _modClamp(p, 32);
                if (one && names.indexOf(one) === -1) names.push(one);
            }
            return { state: names.length ? 'declared' : 'none', names: names };
        }

        // The word each non-list state shows in place of the chips. Three
        // different facts, so three different words: "unchecked" is about this
        // BROKER, "undeclared (pre-lint)" is about this GENERATION, and
        // "declares none" is about this MANIFEST. None of them is the word
        // "verified", because none of them is one.
        function _modPermissionsText(state) {
            if (state === 'undeclared') return 'undeclared (pre-lint)';
            if (state === 'none') return 'declares none';
            return 'unchecked';
        }

        function _modPermissionsTitle(state) {
            if (state === 'declared') {
                return 'capabilities this mod declares in its manifest. For an '
                    + 'installed mod this broker checked the declaration '
                    + 'against the package’s own source before storing it — a '
                    + 'source-text lint, not a sandbox, and not containment.';
            }
            if (state === 'undeclared') {
                return 'this mod was installed before this broker checked '
                    + 'permissions, so its manifest carries no declaration at '
                    + 'all. That is not the same as declaring none: nothing '
                    + 'here has been checked. Reinstalling it under today’s '
                    + 'rules requires the declaration.';
            }
            if (state === 'none') {
                return 'this mod’s manifest positively declares that it uses '
                    + 'none of the reviewable capabilities, and this broker '
                    + 'checked that claim against its source.';
            }
            return 'this broker reported no permissions information at all, so '
                + 'nothing here was checked — an older build does not know '
                + 'about this declaration. Unknown, not clean.';
        }

        // ---- provenance + install/uninstall UI (#163 / S5, design §9) -------
        // Everything below drives the LOCAL broker only — the one that served
        // this page. That is the whole reason there is no feature detection
        // here: this fragment ships inside the page that broker assembled, so
        // it cannot be talking to a build without /mods/install. Installing on
        // a REMOTE broker is a separate trust decision and deliberately has no
        // control (see the per-host pane in 81_js_control_panel.js).
        //
        // Every string that originates OUTSIDE this file — a mod's title or
        // version, a manifest's own JSON, a broker error `detail`, a picked
        // file's name — reaches the DOM through textContent or a .title
        // property assignment and never through innerHTML. renderMenu's
        // invariant (the only innerHTML in this app is our own markup) holds
        // here too.

        // The `shipped` / `installed` badge. REFLECTED, never decided: `s`
        // comes from _modStatusRows(), which reads the BOOT catalog snapshot
        // plus this page's own package records — no fetch, no server state, so
        // a /state pull can never move it.
        function _modSourceBadge(s) {
            const el = document.createElement('span');
            el.className = 'set-mod-source';
            el.dataset.modSource = s.source;
            el.textContent = s.source;
            if (s.source === 'installed') {
                // A mod-supplied version and a broker-supplied generation.
                // .title is a PROPERTY assignment (no HTML parsing), but a
                // registration's `version` is whatever the mod's own object
                // literal said and is not length-capped anywhere, so clamp it
                // rather than hand the tooltip a megabyte.
                el.title = 'installed on this broker — v'
                    + (_modClamp(s.version, 32) || '?') + ' · '
                    + (_modClamp(s.gen, 8) || '?');
            } else {
                el.title = 'shipped in this broker’s own page bundle';
            }
            return el;
        }

        // A short, single-line, length-capped rendering of an untrusted string.
        // SLICE FIRST, then normalize: a registration's `version` is whatever
        // the mod's own object literal said and is capped nowhere, so
        // normalizing before slicing would scan the whole string on every
        // repaint to show 32 characters of it.
        function _modClamp(v, n) {
            if (typeof v !== 'string' || !v) return '';
            return v.slice(0, n + 8).replace(/[\r\n\t]+/g, ' ').slice(0, n);
        }

        function _modFmtBytes(n) {
            if (n < 0) return 'size unknown';
            if (!(n > 0)) return '0 B';
            if (n < 1024) return n + ' B';
            return (n / 1024).toFixed(1) + ' KiB';
        }

        // The byte length the BROKER will write, not the length of the string
        // and not the size on disk. Blob.text() UTF-8-decodes (and strips a
        // BOM), so file.size can differ from what is actually sent; the preview
        // must show what will be written or it is not a preview.
        //
        // NEVER text.length as a fallback: that is UTF-16 code units, so "é"
        // would read as 1 byte instead of 2 and an emoji as 2 instead of 4 — a
        // preview that is quietly wrong about bytes is worse than one that says
        // it does not know. Blob's size IS the UTF-8 length, so it is an exact
        // second source; -1 means genuinely unknown and renders as such.
        function _modByteLen(text) {
            if (typeof text !== 'string') return 0;
            try { return new TextEncoder().encode(text).length; } catch (_) {}
            try { return new Blob([text]).size; } catch (_) { return -1; }
        }

        // Every refusal code the install/uninstall API can answer with, as a
        // sentence. A raw error code in the UI is a bug: these are the words an
        // operator can act on. An UNKNOWN code (a newer broker, which cannot
        // happen for the local one, or a bug) still degrades to a sentence.
        function _modErrorText(code, status) {
            const map = {
                too_large: 'that mod is too large to send in one request.',
                bad_json: 'this broker could not parse the request.',
                bad_mod_id: 'that mod id is not a legal id (lowercase letters, '
                    + 'digits and hyphens).',
                bad_generation: 'the files did not hash to the generation this '
                    + 'broker expected.',
                reserved_id: 'an installed mod id must start with “x-” — ids '
                    + 'without that prefix are reserved for mods this broker '
                    + 'ships itself.',
                id_in_use: 'a mod with that id is already installed here. Tick '
                    + '“Replace the copy already installed on this broker” to '
                    + 'upgrade it, or uninstall it first (that is where the '
                    + 'purge option lives).',
                not_installed: 'this broker says that mod is not installed. If '
                    + 'this was a retry after a failed attempt, the first '
                    + 'attempt may already have removed it.',
                bad_file_name: 'one of the file names is not accepted (ASCII '
                    + 'letters, digits, dot, dash and underscore; .js, .css or '
                    + '.md only).',
                reserved_file_name: 'that file name belongs to the broker — it '
                    + 'writes mod.json itself from the manifest.',
                too_many_files: 'that package has more files than this broker '
                    + 'accepts.',
                file_too_large: 'one file is over this broker’s per-file limit.',
                total_too_large: 'the package is over this broker’s per-mod '
                    + 'size limit.',
                bad_encoding: 'a file is not usable text: it must be UTF-8, '
                    + 'must not start with a byte-order mark, and must end in a '
                    + 'newline.',
                bad_scripts: '`scripts` must be a non-empty ordered list of .js '
                    + 'names that are all in the package (the shipped tree’s '
                    + '`entry` field is not accepted here).',
                bad_styles: '`styles` must be a list of .css names that are all '
                    + 'in the package.',
                bad_requires: '`requires` must be a list of mod ids, and a mod '
                    + 'cannot require itself.',
                bad_manifest_field: 'a manifest field has the wrong type or is '
                    + 'too long.',
                unknown_manifest_key: 'the manifest carries a key this broker '
                    + 'does not accept. Unknown keys are refused rather than '
                    + 'ignored, so a typo is loud.',
                css_external_reference: 'a stylesheet loads from another origin '
                    + '(@import, or an absolute url()). Installed CSS must be '
                    + 'self-contained — data: and relative URLs are fine.',
                undeclared_capability: 'this mod’s own source uses a capability '
                    + 'its manifest never declares in `permissions`. The file '
                    + 'and line quoted below is the FIRST one found, not the '
                    + 'only one: add the permission (or drop the use) and send '
                    + 'it again. The check is a source-text lint over the '
                    + 'package, not a sandbox — it catches a declaration that '
                    + 'drifted from the code, and is no substitute for reading '
                    + 'the code.',
                too_many_mods: 'this broker already holds as many installed '
                    + 'mods as it accepts.',
                write_failed: 'this broker could not write to its mod store.',
            };
            // typeof-string, not truthiness: `map` is an object literal, so
            // map['constructor'] / map['toString'] / map['__proto__'] are
            // INHERITED and truthy. A broker answering error:"toString" would
            // otherwise put a native function's source into the dialog.
            if (typeof map[code] === 'string') return map[code];
            return 'this broker refused the request (HTTP ' + (status || '?')
                + (code ? ', ' + _modClamp(code, 64) : '') + ').';
        }

        // Whether a refusal PROVES nothing was written. The validator runs
        // entirely in memory before a single byte is written, so every
        // validation code does prove it — write_failed and an unrecognised code
        // do not, and claiming otherwise would be the install half of the
        // purge-reported-as-success bug.
        //
        // `undeclared_capability` (#193) belongs in this list for exactly that
        // reason and no other: the capability lint runs inside validation,
        // BEFORE anything is stored, so a package refused by it left nothing
        // behind — no directory, no generation, no index entry.
        function _modRefusalProvesNoWrite(code) {
            return [
                'too_large', 'bad_json', 'bad_mod_id', 'bad_generation',
                'reserved_id', 'id_in_use', 'bad_file_name',
                'reserved_file_name', 'too_many_files', 'file_too_large',
                'total_too_large', 'bad_encoding', 'bad_scripts', 'bad_styles',
                'bad_requires', 'bad_manifest_field', 'unknown_manifest_key',
                'css_external_reference', 'undeclared_capability',
                'too_many_mods',
            ].indexOf(code) !== -1;
        }

        // What to say when a request's OUTCOME IS UNKNOWN: the transport failed,
        // or a 2xx arrived whose body would not parse. A POST can commit and
        // then lose its response, so "nothing was installed" is a claim this
        // client is not entitled to make. Reload and read the list instead.
        function _modUnknownOutcomeLines(what) {
            return ['this broker could not be reached, or answered something '
                    + 'unreadable, so the outcome of the ' + what + ' is '
                    + 'UNKNOWN — the request may have been carried out before '
                    + 'the answer was lost.',
                    'Reload and check this list before trying again.'];
        }

        // One flow at a time, and cancelling it cancels everything it started.
        // openDialog is a SINGLETON: a continuation that survives its own
        // cancellation would later open a dialog over — and silently cancel —
        // whatever the operator opened in the meantime. `advanced` marks the
        // handover between stages, so the singleton cancelling the previous
        // stage is not mistaken for the operator backing out.
        function _modFlow() {
            return { live: true, advanced: false };
        }
        // Per-mod mutation guard: a second click while a POST is in flight
        // would race the first (one succeeds, the other 404s, and only the
        // failure's dialog survives the singleton).
        function _modOpBusy(id) {
            return !!_modBag('opInFlight')[id];
        }

        // The lines a failed install/uninstall shows: the mapped sentence, then
        // the broker's own `detail` quoted and capped. `detail` echoes names
        // that came from the picked mod, so it is untrusted text — it rides a
        // text node, like everything else here.
        function _modFailLines(r, j) {
            const code = (j && typeof j.error === 'string') ? j.error : '';
            const lines = [_modErrorText(code, r ? r.status : 0)];
            const detail = (j && typeof j.detail === 'string') ? j.detail : '';
            if (detail) lines.push('This broker said: ' + _modClamp(detail, 400));
            return lines;
        }

        // A result modal: N text lines, and (for an outcome that only takes
        // effect on the next page load) a reload button. D1 again — an install
        // or uninstall NEVER changes this page, so the offer to reload is the
        // honest end of the flow, not a nicety.
        function _modOpResult(title, lines, offerReload) {
            const buttons = [];
            if (offerReload) {
                buttons.push({ label: 'Reload now', value: 'reload',
                               primary: true });
            }
            buttons.push({ label: 'Close', value: 'close',
                           primary: !offerReload });
            return openDialog({
                title: title,
                body: function (c) {
                    for (const line of lines) {
                        if (!line) continue;
                        const p = document.createElement('div');
                        p.className = 'app-dialog-msg';
                        p.textContent = line;
                        c.appendChild(p);
                    }
                },
                buttons: buttons,
            }).then(function (r) {
                if (r && r.value === 'reload') {
                    try { window.location.reload(); } catch (_) {}
                }
            });
        }

        // ---- admin credential class (#191) ----------------------------------
        // When a broker sets `admin_token`, its /info carries
        // `admin: {required: true, routes: [...]}` and every route on that
        // list refuses the page token alone with 403 admin_required. The
        // credential travels ONLY in the X-Webterm-Admin header — never a
        // URL, never Authorization (that slot already carries the page token
        // on remote wires; two credential classes must not share one slot).
        //
        // Detection is FEATURE-BASED off /info data the page already holds
        // (localInfo()'s memo for the serving broker, modCatalogCache for a
        // Control Panel peer) — never a probe: probing the POST IS the write.
        // An /info with no `admin` key is an old or non-enforcing build and
        // gets today's wire, byte for byte: no prompt, no header.
        //
        // The token is CLOSURE-HELD for this page session and nowhere else:
        // not localStorage (same-origin-readable by every mod), not the
        // prefs settings blob (it SYNCS, #153), not the /state blob, not a
        // URL.
        // A reload forgets it; that is the design. What this cannot do — the
        // issue is explicit — is hide the prompt from a hostile mod already
        // live in this page; it ends the STANDING capability, nothing more.
        function _adminHeld() {
            if (!_adminHeld._m) _adminHeld._m = Object.create(null);
            return _adminHeld._m;
        }
        // Does `adminInfo` (an /info `admin` value, or null) gate `route`?
        // No admin key, or required !== true -> no. A routes list narrows
        // enforcement to its members; a build that omits the list is read as
        // enforcing everywhere — fail toward one needless prompt, never
        // toward a header-less write that can only 403.
        function _adminRequiredFor(adminInfo, route) {
            if (!adminInfo || typeof adminInfo !== 'object') return false;
            if (adminInfo.required !== true) return false;
            if (Array.isArray(adminInfo.routes)) {
                return adminInfo.routes.indexOf(route) !== -1;
            }
            return true;
        }
        // The serving broker's /info `admin` value (or null). Rides the
        // memoized boot fetch — this never issues a request of its own.
        async function _localAdminInfo() {
            const j = await localInfo();
            return (j && j.admin && typeof j.admin === 'object')
                ? j.admin : null;
        }
        // The ONE styled admin-token prompt — built on openDialog, so it
        // matches every other dialog here. Resolves the typed token, or null on
        // cancel/Escape/backdrop/empty. `refused` selects the honest
        // re-prompt wording after a 403 admin_required on a held token.
        function _adminTokenPrompt(host, act, refused) {
            let input = null;
            const label = (host && (host.label || host.url)) || 'this broker';
            return openDialog({
                title: 'Admin token required',
                body: function (c) {
                    const p = document.createElement('div');
                    p.className = 'app-dialog-msg';
                    p.textContent = refused
                        ? (label + ' refused the admin token this page was '
                            + 'holding — it is wrong or stale. Enter it again '
                            + 'to ' + act + ', or cancel.')
                        : (label + ' requires its admin token to ' + act
                            + '. The page password is not enough for this — '
                            + 'that broker gates administration behind a '
                            + 'separate credential (admin_token in its '
                            + 'broker_config.json).');
                    c.appendChild(p);
                    const row = document.createElement('div');
                    row.className = 'set-row app-dialog-field';
                    input = document.createElement('input');
                    // type=password: the value must never be readable off a
                    // shared or streamed screen.
                    input.type = 'password';
                    input.autocomplete = 'off';
                    row.appendChild(input);
                    c.appendChild(row);
                    const keep = document.createElement('div');
                    keep.className = 'set-hint';
                    keep.textContent = 'Held in this page until you reload — '
                        + 'never saved to this browser, never synced.';
                    c.appendChild(keep);
                    // openDialog focuses its own field inputs or the primary
                    // button; this input is body-built, so focus it once the
                    // dialog is actually in the DOM.
                    setTimeout(function () {
                        try { input.focus(); } catch (_) {}
                    }, 0);
                },
                buttons: [
                    { label: 'Continue', value: true, primary: true },
                    { label: 'Cancel', value: false },
                ],
            }).then(function (r) {
                if (!r || !r.value || !input) return null;
                return (typeof input.value === 'string' && input.value)
                    ? input.value : null;
            });
        }
        // A 403 whose body says admin_required — the ONLY answer that means
        // "the admin credential was absent or wrong". A 401 stays the
        // page-token realm (the login overlay's business) and must never
        // open this prompt. clone() so the caller can still read the body.
        async function _adminRefused(res) {
            if (!res || res.status !== 403) return false;
            let j = null;
            try { j = await res.clone().json(); } catch (_) { return false; }
            return !!(j && j.error === 'admin_required');
        }
        function _adminPost(host, route, opts, tok) {
            const o = Object.assign({}, opts || {});
            const headers = new Headers(o.headers || undefined);
            headers.set('X-Webterm-Admin', tok);
            o.headers = headers;
            // A credential-bearing request never follows a redirect: fetch
            // keeps custom headers across a same-origin 30x, so a broker
            // (or anything answering for it) could bounce this to a path —
            // or after a preflight, an origin — the operator never named.
            // An admin act must land where it was aimed or fail loudly.
            o.redirect = 'error';
            return hostFetch(host, route, o);
        }
        // The identity a held credential belongs to. NOT the host id alone:
        // ids are stable across URL edits BY DESIGN (so per-host prefs
        // survive a re-point), which is exactly why an id is the wrong key
        // for a secret — re-pointing `peer` at another machine would hand
        // that machine the token learned for the old one. Same lesson as
        // the update mod's host fingerprint.
        function _adminKey(host) {
            if (!host || !host.id) return 'local|';
            return host.id + '|' + String(host.url || '');
        }
        // Drop a held credential only if it is still the one that failed.
        function _adminForget(key, tok) {
            const held = _adminHeld();
            if (held[key] === tok) delete held[key];
        }
        // One admin-aware POST to a gated route. Resolves {res, aborted}:
        //   aborted null        — `res` is the wire answer; handle as always
        //   aborted 'cancelled' — the operator declined the prompt; NOTHING
        //                         was sent
        //   aborted 'refused'   — sent and answered 403 admin_required (after
        //                         the one re-prompt, or the re-prompt was
        //                         declined); `res` is that 403. An admin
        //                         refusal happens before the broker reads a
        //                         body or takes a lock, so nothing was
        //                         written.
        // Rejects only where hostFetch itself rejects, so every caller's
        // existing outcome-unknown path keeps its meaning.
        async function adminGatedFetch(host, route, opts, adminInfo, act) {
            if (!_adminRequiredFor(adminInfo, route)) {
                // Old / non-enforcing broker: today's wire, byte for byte.
                const res0 = await hostFetch(host, route, opts);
                // ...unless it answers admin_required anyway. The `admin`
                // key this page cached predates an operator turning the
                // realm ON, and the broker's own 403 is proof both that the
                // realm exists NOW and that nothing was written (the gate
                // refuses before the body is read). Treat the answer as the
                // detection it is and continue into the prompt, rather than
                // handing back a refusal the operator cannot act on until
                // they think to reload.
                if (!(await _adminRefused(res0))) {
                    return { res: res0, aborted: null };
                }
            }
            const held = _adminHeld();
            const key = _adminKey(host);
            let tok = held[key];
            if (!tok) {
                tok = await _adminTokenPrompt(host, act, false);
                if (!tok) return { res: null, aborted: 'cancelled' };
                held[key] = tok;
            }
            let res = await _adminPost(host, route, opts, tok);
            if (!(await _adminRefused(res))) return { res: res, aborted: null };
            // The held token is wrong or stale. Clear it, ask ONCE more.
            // Compare-and-delete: two acts can be in flight with the same
            // stale token, and a late refusal must not evict the REPLACEMENT
            // the operator typed for the earlier one -- that would prompt
            // again for a credential the page already has, forever.
            _adminForget(key, tok);
            tok = await _adminTokenPrompt(host, act, true);
            if (!tok) return { res: res, aborted: 'refused' };
            held[key] = tok;
            res = await _adminPost(host, route, opts, tok);
            if (await _adminRefused(res)) {
                _adminForget(key, tok);
                return { res: res, aborted: 'refused' };
            }
            return { res: res, aborted: null };
        }

        // ---- uninstall ------------------------------------------------------
        function _modUninstallDialog(id) {
            if (_modOpBusy(id)) return;
            let purgeCb = null;
            // openDialog SWALLOWS a throw from spec.body. Without this flag a
            // body that died halfway would leave an Uninstall button over a
            // dialog that never showed the purge wording or its checkbox — and
            // the operator would be confirming something they were never told.
            let bodyOk = false;
            openDialog({
                title: 'Uninstall ' + id,
                body: function (c) {
                    const p = document.createElement('div');
                    p.className = 'app-dialog-msg';
                    p.textContent = 'Remove ' + id + ' from this broker.\n\n'
                        + 'It keeps running in this page until you reload, and '
                        + 'other browsers keep running it until they reload — '
                        + 'an uninstall applies on the next page load.';
                    c.appendChild(p);
                    const lab = document.createElement('label');
                    lab.className = 'set-check';
                    purgeCb = document.createElement('input');
                    purgeCb.type = 'checkbox';
                    lab.appendChild(purgeCb);
                    const t = document.createElement('span');
                    // VERBATIM, and it must stay verbatim: the purge spans
                    // three sidecars and cannot be one transaction, it is
                    // BROKER-side only, and no broker can reach what other
                    // browsers hold in their own localStorage. Every clause
                    // here is load-bearing — do not reflow it.
                    t.textContent = "Also delete this mod's server-side data on "
                        + "this broker (its /mod-store value and its pin). Data "
                        + "stored in other browsers is not affected.";
                    lab.appendChild(t);
                    c.appendChild(lab);
                    bodyOk = true;
                },
                buttons: [
                    { label: 'Uninstall', value: true, primary: true,
                      danger: true },
                    { label: 'Cancel', value: false },
                ],
            }).then(function (r) {
                if (!r || !r.value) return;
                if (!bodyOk) {
                    return _modOpResult('Uninstall cancelled',
                        ['this dialog could not be drawn, so nothing was sent.'],
                        false);
                }
                // Read at COMMIT time from the element itself. The node is
                // detached by then, but the closure still holds it and its
                // .checked is the value the operator actually left.
                return _modUninstallRun(id, !!(purgeCb && purgeCb.checked));
            }).catch(function (e) {
                console.error('[mods] uninstall dialog failed:', e);
            });
        }

        async function _modUninstallRun(id, purge) {
            if (_modOpBusy(id)) return;
            _modBag('opInFlight')[id] = true;
            _renderManagerRows();
            try {
                await _modUninstallPost(id, purge);
            } finally {
                delete _modBag('opInFlight')[id];
                _renderManagerRows();
            }
        }

        async function _modUninstallPost(id, purge) {
            let r = null;
            try {
                // #191: when this broker enforces the admin class, the write
                // rides adminGatedFetch (prompt + X-Webterm-Admin header);
                // when it does not, that call IS today's hostFetch.
                const gated = await adminGatedFetch(localHost(),
                    '/mods/uninstall', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ id: id, purge: purge }),
                    }, await _localAdminInfo(), 'uninstall ' + id);
                if (gated.aborted === 'cancelled') {
                    _modOpResult('Uninstall cancelled',
                        ['this broker requires its admin token for an '
                         + 'uninstall, and none was entered — nothing was '
                         + 'sent. ' + id + ' is still installed.'], false);
                    return;
                }
                if (gated.aborted === 'refused') {
                    _modOpResult('Uninstall refused',
                        ['this broker requires its admin token and did not '
                         + 'accept the one entered. An admin refusal happens '
                         + 'before anything is read or written — ' + id
                         + ' is still installed.'], false);
                    return;
                }
                r = gated.res;
            } catch (e) {
                // The POST may have LANDED and only its answer been lost. This
                // client is not entitled to say what happened.
                _modOpResult('Uninstall outcome unknown',
                    _modUnknownOutcomeLines('uninstall of ' + id), true);
                return;
            }
            let j = null;
            let parsed = true;
            try { j = await r.json(); } catch (_) { j = null; parsed = false; }
            if (r.ok && !parsed) {
                // A 2xx whose body will not parse is not a success we can
                // describe — same unknown as a dropped connection.
                _modOpResult('Uninstall outcome unknown',
                    _modUnknownOutcomeLines('uninstall of ' + id), true);
                return;
            }
            if (!r.ok || !j || j.ok !== true) {
                // A PURGE that could not write answers write_failed WITH THE
                // MOD STILL INSTALLED — data-first, code-last, so the code is
                // never removed while its data survives. Reporting that as any
                // flavour of success would invert the one guarantee the
                // ordering exists to give.
                const lines = _modFailLines(r, j);
                const code = (j && typeof j.error === 'string') ? j.error : '';
                if (code === 'write_failed') {
                    // Only the FIRST clause is guaranteed. The purge walks three
                    // sidecars and cannot be one transaction, so it may have
                    // deleted the /mod-store value and failed on the pin, or the
                    // other way round — "nothing was removed" would be a
                    // guess dressed as a fact.
                    lines.push(id + ' is STILL INSTALLED. The server-side data '
                        + 'deletion may have been PARTIAL — it is three separate '
                        + 'writes and cannot be one transaction. Check its pin '
                        + 'under “Mods on this broker” before retrying.');
                } else if (code !== 'not_installed') {
                    lines.push(id + ' is still installed.');
                }
                _modOpResult('Uninstall failed', lines, false);
                return;
            }
            _modBag('uninstalled')[id] = true;
            _renderManagerRows();
            const purged = (j.purged && typeof j.purged === 'object')
                ? j.purged : {};
            const lines = [id + ' is uninstalled on this broker.'];
            if (purge) {
                const gone = [];
                if (purged.mod_store) gone.push('its /mod-store value');
                if (purged.pin) gone.push('its pin');
                lines.push(gone.length
                    ? 'Deleted ' + gone.join(' and ') + '.'
                    : 'There was no server-side data on this broker to delete.');
                lines.push('Data this mod stored in other browsers is not '
                    + 'affected — no broker can reach it.');
            }
            lines.push('It is STILL RUNNING in this page. It disappears on the '
                + 'next page load.');
            _modOpResult('Uninstalled', lines, true);
        }

        // ---- install --------------------------------------------------------
        // Bounds on what a PICK may hand us, function-local (the fragment rule:
        // a hoisted function reading a not-yet-initialized fragment const throws
        // a TDZ ReferenceError that kills the whole script).
        //
        // `entries` is the important one and it is checked from FileList.length,
        // which is O(1), BEFORE any iteration: a folder pick hands us every file
        // under the chosen directory, so a mis-click on a home directory is tens
        // of thousands of entries and the walk would run synchronously on the
        // one UI thread. `files` mirrors the broker's own cap so the refusal is
        // immediate instead of a round trip, and `readBytes` bounds how much
        // text we are willing to decode before the broker gets a say.
        // `manifestBytes` mirrors the broker's own MAX_META_BYTES and is the
        // ONLY bound on mod.json — it is not one of the package `files`, so it
        // is not covered by `readBytes`, and without this a folder holding a
        // small script beside a multi-gigabyte mod.json would be read and
        // JSON.parse'd synchronously on the one UI thread.
        function _modPickLimits() {
            return { entries: 2000, files: 32, readBytes: 2 * 1024 * 1024,
                     manifestBytes: 64 * 1024, warnings: 12 };
        }
        // Mirrors modinstall.MANIFEST_KEYS, and a drift test pins the two
        // together. It drives a WARNING ("this broker will refuse these keys"),
        // so a key missing here is not cosmetic: it tells the operator their
        // correct manifest will be rejected. `permissions` (#193) is accepted.
        function _modManifestKeys() {
            return ['id', 'version', 'ctxVersion', 'title', 'description',
                    'scripts', 'styles', 'requires', 'tiers', 'help',
                    'defaultEnabled', 'permissions'];
        }
        function _modAllowedPickName(name) {
            return /\.(?:js|css|md)$/i.test(name || '');
        }
        function _modCsvList(s) {
            if (typeof s !== 'string') return [];
            return s.split(',').map(function (v) { return v.trim(); })
                .filter(function (v) { return !!v; });
        }

        // Stage 1: pick a folder or a single .js. Both inputs are REAL elements
        // inside the dialog, so the click that opens the OS picker is directly
        // the user's own gesture — routing it through a promise first would put
        // the .click() outside the activation the browser requires.
        function _modInstallDialog() {
            const flow = _modFlow();
            let statusEl = null;
            let folderIn = null;
            let fileIn = null;
            let busy = false;
            const say = function (msg, cls) {
                if (!statusEl) return;
                statusEl.textContent = msg || '';
                statusEl.className = cls || 'mod-install-note';
            };
            const lock = function (on) {
                busy = on;
                if (folderIn) folderIn.disabled = on;
                if (fileIn) fileIn.disabled = on;
            };
            const onPicked = function (p) {
                if (busy) return;
                lock(true);
                say('reading…');
                p.then(function (pick) {
                    // The operator can cancel (Escape / backdrop / Cancel)
                    // WHILE the read is in flight. Without this the continuation
                    // would open the preview afterwards — over, and therefore
                    // cancelling, whatever they opened instead.
                    if (!flow.live) return null;
                    if (!pick) { lock(false); say(''); return null; }
                    say('checking this broker…');
                    return _modInstallPreview(pick, flow);
                }).then(function () { lock(false); },
                        function (e) {
                            lock(false);
                            if (!flow.live) return;
                            say((e && e.message) ? e.message
                                : 'could not read that.', 'mod-install-danger');
                        });
            };
            openDialog({
                title: 'Install a mod',
                body: function (c) {
                    const p = document.createElement('div');
                    p.className = 'app-dialog-msg';
                    p.textContent = 'Add a mod to this broker — no source edit '
                        + 'and no restart.\n\nPick the mod’s own folder (it must '
                        + 'contain mod.json), or a single .js file and type its '
                        + 'manifest. You see exactly what will be sent before '
                        + 'anything is written.\n\nAn install applies on the '
                        + 'NEXT page load. Nothing in this page changes until '
                        + 'you reload.';
                    c.appendChild(p);
                    const r1 = document.createElement('div');
                    r1.className = 'set-row app-dialog-field';
                    const l1 = document.createElement('label');
                    l1.textContent = 'Folder';
                    r1.appendChild(l1);
                    folderIn = document.createElement('input');
                    folderIn.type = 'file';
                    // The property alone is not enough on every engine, and the
                    // non-standard `directory` is what Firefox reads.
                    try {
                        folderIn.webkitdirectory = true;
                        folderIn.setAttribute('webkitdirectory', '');
                        folderIn.setAttribute('directory', '');
                    } catch (_) {}
                    folderIn.addEventListener('change', function () {
                        onPicked(_modReadFolder(folderIn.files));
                    });
                    r1.appendChild(folderIn);
                    c.appendChild(r1);
                    const r2 = document.createElement('div');
                    r2.className = 'set-row app-dialog-field';
                    const l2 = document.createElement('label');
                    l2.textContent = 'Single .js';
                    r2.appendChild(l2);
                    fileIn = document.createElement('input');
                    fileIn.type = 'file';
                    fileIn.accept = '.js';
                    fileIn.addEventListener('change', function () {
                        const f = fileIn.files && fileIn.files[0];
                        if (!f) return;
                        onPicked(_modReadSingle(f).then(function (one) {
                            if (!one || !flow.live) return null;
                            return _modInstallFields(one, flow);
                        }));
                    });
                    r2.appendChild(fileIn);
                    c.appendChild(r2);
                    statusEl = document.createElement('div');
                    statusEl.className = 'mod-install-note';
                    c.appendChild(statusEl);
                },
                buttons: [{ label: 'Cancel', value: false }],
            }).then(function () {
                // Cancel / Escape / backdrop kills the whole flow. A later
                // STAGE opening its own dialog also resolves this promise (the
                // openDialog singleton), which is what `advanced` distinguishes
                // — otherwise the handover would cancel itself.
                if (!flow.advanced) flow.live = false;
            }).catch(function (e) {
                flow.live = false;
                console.error('[mods] install dialog failed:', e);
            });
        }

        // Read a picked directory into {manifest, files, notes}. Rejects with an
        // Error whose message is shown to the operator verbatim.
        async function _modReadFolder(list) {
            const lim = _modPickLimits();
            if (!list || !list.length) return null;
            if (list.length > lim.entries) {
                throw new Error('that folder holds ' + list.length + ' entries; '
                    + 'pick the mod’s own folder (a mod is at most '
                    + lim.files + ' files).');
            }
            const notes = [];
            const picked = [];
            let manifestFile = null;
            let bytes = 0;
            for (let i = 0; i < list.length; i++) {
                const f = list[i];
                const rel = (f && (f.webkitRelativePath || f.name)) || '';
                const parts = rel.split('/');
                const name = parts[parts.length - 1];
                // A mod is ONE FLAT FOLDER: the filename grammar has no path
                // separator, so a nested file has no name it could be installed
                // under. Reported, never silently dropped.
                if (parts.length > 2) {
                    notes.push('not sent: ' + rel + ' (a mod is one flat folder)');
                    continue;
                }
                if (name.toLowerCase() === 'mod.json') { manifestFile = f; continue; }
                if (!_modAllowedPickName(name)) {
                    notes.push('not sent: ' + name + ' (only .js, .css and .md)');
                    continue;
                }
                picked.push({ name: name, file: f });
                bytes += (f && f.size) || 0;
            }
            if (!manifestFile) {
                throw new Error('that folder has no mod.json. Pick the mod’s own '
                    + 'folder, or use the single-file option and type the '
                    + 'manifest fields.');
            }
            if (!picked.length) {
                throw new Error('that folder has no .js, .css or .md files.');
            }
            if (picked.length > lim.files) {
                throw new Error(picked.length + ' files; this broker installs at '
                    + 'most ' + lim.files + '.');
            }
            if (bytes > lim.readBytes) {
                throw new Error('those files are ' + _modFmtBytes(bytes)
                    + '; far over this broker’s per-mod limit.');
            }
            if (((manifestFile && manifestFile.size) || 0) > lim.manifestBytes) {
                throw new Error('that folder’s mod.json is '
                    + _modFmtBytes(manifestFile.size) + '; a manifest is at most '
                    + _modFmtBytes(lim.manifestBytes) + '.');
            }
            let manifest = null;
            try { manifest = JSON.parse(await manifestFile.text()); }
            catch (_) {
                throw new Error('that folder’s mod.json is not valid JSON.');
            }
            if (!manifest || typeof manifest !== 'object'
                    || Array.isArray(manifest)) {
                throw new Error('that folder’s mod.json is not a JSON object.');
            }
            // Object.create(null) so a file literally named __proto__.* could
            // not reach Object.prototype. The grammar already forbids a leading
            // underscore, which is belt to this braces.
            const files = Object.create(null);
            for (const p of picked) files[p.name] = await p.file.text();
            return { manifest: manifest, files: files, notes: notes };
        }

        async function _modReadSingle(file) {
            const lim = _modPickLimits();
            const name = (file && file.name) || '';
            if (!/\.js$/i.test(name)) {
                throw new Error('pick a .js file (the mod’s entry script).');
            }
            if (((file && file.size) || 0) > lim.readBytes) {
                throw new Error('that file is ' + _modFmtBytes(file.size)
                    + '; far over this broker’s per-file limit.');
            }
            return { name: name, text: await file.text() };
        }

        // A plausible starting id for a single-file pick. Only a default — the
        // field is editable and its validator, not this, is what decides.
        function _modGuessId(name) {
            let base = String(name || '').replace(/\.js$/i, '').toLowerCase();
            base = base.replace(/[^a-z0-9-]+/g, '-').replace(/^-+|-+$/g, '');
            if (!base) base = 'mod';
            return (base.slice(0, 2) === 'x-') ? base : ('x-' + base);
        }

        // Stage 2 (single-file path only): the manifest the broker will write.
        // A SEPARATE stage rather than fields on the preview, so the preview can
        // show the finished manifest — a preview built from half-typed fields
        // would show something other than what is sent.
        async function _modInstallFields(one, flow) {
            if (flow) flow.advanced = true;      // the handover, not a cancel
            const r = await openDialog({
                title: 'Manifest for ' + _modClamp(one.name, 64),
                body: function (c) {
                    const p = document.createElement('div');
                    p.className = 'app-dialog-msg';
                    p.textContent = 'This broker writes mod.json itself from '
                        + 'these fields. An installed mod id must start with '
                        + '“x-”; ids without that prefix are reserved for mods '
                        + 'the broker ships.\n\n`requires` and `tiers` are '
                        + 'comma-separated.';
                    c.appendChild(p);
                },
                fields: [
                    { key: 'id', label: 'id', value: _modGuessId(one.name),
                      placeholder: 'x-notes',
                      validate: function (v) {
                          const id = (v || '').trim();
                          if (!MOD_ID_RE.test(id)) {
                              return 'an id is lowercase letters, digits and '
                                  + 'hyphens';
                          }
                          if (id.slice(0, 2) !== 'x-') {
                              return 'an installed mod id must start with “x-”';
                          }
                          return '';
                      } },
                    { key: 'title', label: 'title', value: '' },
                    { key: 'version', label: 'version', value: '' },
                    { key: 'requires', label: 'requires',
                      placeholder: 'editor, clock' },
                    { key: 'tiers', label: 'tiers',
                      placeholder: 'settings, window' },
                ],
                buttons: [{ label: 'Continue', value: true, primary: true },
                          { label: 'Cancel', value: false }],
            });
            if (!r || !r.value) return null;
            const f = r.fields;
            const id = (f.id || '').trim();
            const files = Object.create(null);
            files[one.name] = one.text;
            return {
                manifest: {
                    id: id,
                    version: (f.version || '').trim(),
                    ctxVersion: 1,
                    title: (f.title || '').trim() || id,
                    description: '',
                    scripts: [one.name],
                    styles: [],
                    requires: _modCsvList(f.requires),
                    tiers: _modCsvList(f.tiers),
                },
                files: files,
                notes: [],
            };
        }

        // What THIS BROKER already holds under that id. #172: /mod-store and the
        // pin map have always accepted any id-shaped key, so state can pre-exist
        // an install (console code, a hand-edited sidecar, a locally hacked mod)
        // and installing would silently adopt it. The install response is the
        // authoritative answer; this is the one the operator gets BEFORE
        // confirming. `modStore: null` means "could not check" and says so —
        // never "no".
        async function _modAdoptionCheck(id) {
            // pinValue is kept SEPARATE from "a pin exists": a pin of `false`
            // and a pin of `true` are opposite outcomes for the mod about to be
            // installed, and collapsing them into a boolean would let the
            // dialog imply the wrong one.
            const out = { pin: false, pinValue: null, modStore: null };
            const raw = window.__mods.policyRaw;
            if (raw && typeof raw === 'object' && !Array.isArray(raw)
                    && typeof raw[id] === 'boolean') {
                out.pin = true;
                out.pinValue = raw[id];
            }
            try {
                const r = await hostFetch(localHost(),
                    '/mod-store/' + encodeURIComponent(id), { cache: 'no-store' });
                if (r.ok) {
                    const j = await r.json();
                    out.modStore = !!(j && (j.rev > 0 || j.value !== null));
                }
            } catch (_) { /* leave null — "could not check" */ }
            return out;
        }

        // The refusals this payload is heading for, in the operator's words.
        // Advisory ONLY: the broker's validator is authoritative and Install
        // stays enabled, because a client-side gate that disagrees with the
        // server is a worse bug than a round trip. Anything not listed here
        // still comes back as a mapped error.
        // CAPPED, and every list it walks is sliced: `scripts`, `styles` and the
        // manifest's own key set come from a picked mod.json and can be
        // thousands of entries, which would be thousands of DOM nodes built
        // synchronously.
        function _modInstallWarnings(pick, names, sizes, total) {
            const lim = _modPickLimits();
            const raw = _modInstallWarningsRaw(pick, names, sizes, total, lim);
            if (raw.length <= lim.warnings) return raw;
            const out = raw.slice(0, lim.warnings);
            out.push('…and ' + (raw.length - lim.warnings)
                + ' more problem(s) with this package.');
            return out;
        }

        function _modInstallWarningsRaw(pick, names, sizes, total, lim) {
            const out = [];
            const m = pick.manifest || {};
            const id = m.id;
            if (typeof id !== 'string' || !MOD_ID_RE.test(id)) {
                out.push('the manifest has no usable `id`; this broker will '
                    + 'refuse it.');
            } else if (id.slice(0, 2) !== 'x-') {
                out.push('`' + _modClamp(id, 64) + '` is in the first-party '
                    + 'namespace — an installed mod id must start with “x-”, so '
                    + 'this broker will refuse it.');
            }
            const known = _modManifestKeys();
            const unknown = Object.keys(m).slice(0, 64).filter(function (k) {
                return known.indexOf(k) === -1; });
            if (unknown.length) {
                out.push('the manifest carries key(s) this broker does not '
                    + 'accept and will refuse: '
                    + _modClamp(unknown.join(', '), 200)
                    + '. (A shipped mod’s mod.json declares `entry`; an '
                    + 'installed one declares `scripts`.)');
            }
            const scripts = Array.isArray(m.scripts) ? m.scripts : null;
            if (!scripts || !scripts.length) {
                out.push('`scripts` is required and must name at least one .js '
                    + 'file in this package.');
            } else {
                for (const n of scripts.slice(0, 64)) {
                    if (names.indexOf(n) === -1) {
                        out.push('`scripts` names ' + _modClamp(String(n), 80)
                            + ', which is not one of the files above.');
                    }
                }
            }
            const styles = Array.isArray(m.styles) ? m.styles.slice(0, 64) : [];
            for (const n of styles) {
                if (names.indexOf(n) === -1) {
                    out.push('`styles` names ' + _modClamp(String(n), 80)
                        + ', which is not one of the files above.');
                }
            }
            for (const n of names) {
                const text = pick.files[n];
                if (typeof text !== 'string' || text.slice(-1) !== '\n') {
                    out.push(n + ' does not end in a newline; this broker will '
                        + 'refuse it.');
                }
                if (sizes[n] > 256 * 1024) {
                    out.push(n + ' is ' + _modFmtBytes(sizes[n])
                        + ', over the 256 KiB per-file limit.');
                }
            }
            if (names.length > lim.files) {
                out.push(names.length + ' files, over the ' + lim.files
                    + '-file limit.');
            }
            if (total > 512 * 1024) {
                out.push('the package is ' + _modFmtBytes(total)
                    + ', over the 512 KiB per-mod limit.');
            }
            return out;
        }

        // Stage 3: show EXACTLY what will be POSTed, then send it. The manifest
        // is rendered as its literal JSON — not a prettified summary — because
        // the one thing this dialog must never do is describe a payload
        // different from the one it sends. The broker writes mod.json from this
        // object, so a mod.json that was in the picked folder is shown here and
        // is otherwise not stored.
        async function _modInstallPreview(pick, flow) {
            const names = Object.keys(pick.files).sort();
            const sizes = Object.create(null);
            let total = 0;
            let exact = true;
            for (const n of names) {
                sizes[n] = _modByteLen(pick.files[n]);
                if (sizes[n] < 0) exact = false;
                else total += sizes[n];
            }
            const warnings = _modInstallWarnings(pick, names, sizes, total);
            const id = (pick.manifest && typeof pick.manifest.id === 'string')
                ? pick.manifest.id : '';
            const adopt = (id && MOD_ID_RE.test(id))
                ? await _modAdoptionCheck(id)
                : { pin: false, pinValue: null, modStore: null };
            // Cancelled while the adoption check was in flight: opening now
            // would cancel whatever the operator opened instead.
            if (flow && !flow.live) return;
            const installedRow = id ? _modCatalogRow(id) : null;
            const alreadyHere = !!(installedRow
                && installedRow.source === 'installed');
            let json = '';
            try { json = JSON.stringify(pick.manifest, null, 2); }
            catch (_) { json = '(this manifest cannot be encoded as JSON)'; }
            let replaceCb = null;
            // openDialog SWALLOWS a throw from spec.body. Without this flag a
            // half-drawn preview would still leave an Install button that sends
            // `pick` — which defeats the one promise this dialog makes.
            let bodyOk = false;
            if (flow) flow.advanced = true;      // the handover, not a cancel
            const r = await openDialog({
                title: 'Install ' + (_modClamp(id, 64) || 'this mod'),
                body: function (c) {
                    const intro = document.createElement('div');
                    intro.className = 'app-dialog-msg';
                    intro.textContent = 'POST /mods/install to this broker. The '
                        + 'manifest below is written to mod.json by the broker '
                        + 'itself — this is literally what will be sent:';
                    c.appendChild(intro);
                    const pre = document.createElement('pre');
                    pre.className = 'mod-install-pre';
                    pre.textContent = json;
                    c.appendChild(pre);
                    const filesWrap = document.createElement('div');
                    filesWrap.className = 'mod-install-files';
                    for (const n of names) {
                        const row = document.createElement('div');
                        row.className = 'mod-install-file';
                        const a = document.createElement('span');
                        a.textContent = n;
                        const b = document.createElement('span');
                        b.textContent = _modFmtBytes(sizes[n]);
                        row.appendChild(a);
                        row.appendChild(b);
                        filesWrap.appendChild(row);
                    }
                    c.appendChild(filesWrap);
                    const totals = document.createElement('div');
                    totals.className = 'mod-install-note';
                    totals.textContent = names.length + ' file'
                        + (names.length === 1 ? '' : 's') + ', '
                        + (exact ? (_modFmtBytes(total)
                                    + ' as UTF-8 — the bytes the broker will '
                                    + 'write.')
                                 : 'total size could not be measured here.');
                    c.appendChild(totals);
                    const notes = (pick.notes || []).slice(0, 8);
                    for (const n of notes) {
                        const el = document.createElement('div');
                        el.className = 'mod-install-note';
                        el.textContent = _modClamp(n, 200);
                        c.appendChild(el);
                    }
                    if ((pick.notes || []).length > notes.length) {
                        const el = document.createElement('div');
                        el.className = 'mod-install-note';
                        el.textContent = '…and '
                            + ((pick.notes || []).length - notes.length)
                            + ' more file(s) not sent.';
                        c.appendChild(el);
                    }
                    for (const w of warnings) {
                        const el = document.createElement('div');
                        el.className = 'mod-install-danger';
                        el.textContent = w;
                        c.appendChild(el);
                    }
                    // #172's residual case, surfaced BEFORE the confirm.
                    if (adopt.pin) {
                        const el = document.createElement('div');
                        el.className = 'mod-install-warn';
                        // The pin's VALUE, not merely its existence: "pinned
                        // on" and "pinned off" are opposite outcomes for the
                        // mod about to be installed.
                        el.textContent = 'This broker already pins “' + id
                            + '” ' + (adopt.pinValue ? 'ON' : 'OFF')
                            + '. That pin takes the choice away from every '
                            + 'browser, so this mod will come up '
                            + (adopt.pinValue ? 'ENABLED' : 'disabled')
                            + ' regardless of the default below.';
                        c.appendChild(el);
                    }
                    const store = document.createElement('div');
                    if (adopt.modStore === true) {
                        store.className = 'mod-install-warn';
                        store.textContent = 'This broker already holds '
                            + '/mod-store data under “' + id + '”. This mod '
                            + 'will inherit it — if it is a different author’s '
                            + 'mod of the same id, uninstall the old one with '
                            + 'the purge option first.';
                    } else if (adopt.modStore === null) {
                        store.className = 'mod-install-note';
                        store.textContent = 'Could not check whether this broker '
                            + 'already holds /mod-store data under “' + id
                            + '”; if it does, this mod inherits it.';
                    } else {
                        store.className = 'mod-install-note';
                        store.textContent = 'This broker holds no /mod-store '
                            + 'data under “' + id + '”.';
                    }
                    c.appendChild(store);
                    const ls = document.createElement('div');
                    ls.className = 'mod-install-note';
                    ls.textContent = 'What a mod of this id stored in a '
                        + 'browser’s own localStorage cannot be inspected from '
                        + 'here, and is not covered by any of this.';
                    c.appendChild(ls);
                    const lab = document.createElement('label');
                    lab.className = 'set-check';
                    replaceCb = document.createElement('input');
                    replaceCb.type = 'checkbox';
                    lab.appendChild(replaceCb);
                    const rt = document.createElement('span');
                    rt.textContent = 'Replace the copy already installed on this '
                        + 'broker (sends "replace": true). Without this, an id '
                        + 'that is already installed is refused.';
                    lab.appendChild(rt);
                    c.appendChild(lab);
                    if (alreadyHere) {
                        const el = document.createElement('div');
                        el.className = 'mod-install-warn';
                        el.textContent = id + ' v'
                            + (_modClamp(installedRow.version, 32) || '?')
                            + ' was already installed here when this page '
                            + 'loaded — tick Replace to upgrade it.';
                        c.appendChild(el);
                    }
                    // INSTALLING IS THE TRUST DECISION, and the copy has to say
                    // so. An installed mod is reported default-OFF, but
                    // _loadInstalledPackages fetches and EXECUTES every
                    // installed package regardless: "off" means init() is not
                    // called, and the package's stylesheets are live either
                    // way. Wording that let "installed but not enabled" read as
                    // a containment step would be false, and false in the
                    // direction that gets somebody owned.
                    const trust = document.createElement('div');
                    trust.className = 'mod-install-warn';
                    trust.textContent = 'Installing IS the decision to run this '
                        + 'code. From the next page load its scripts execute and '
                        + 'its stylesheets load whether or not you enable it — '
                        + 'leaving it off only means its init() is not called. '
                        + 'It runs same-origin with this broker’s full '
                        + 'authority. Install only what you would put in the '
                        + 'source tree.';
                    c.appendChild(trust);
                    const tail = document.createElement('div');
                    tail.className = 'app-dialog-msg';
                    // The DEFAULT, stated separately from the OUTCOME: this
                    // browser may already hold an enable choice for that id
                    // (uninstalling does not clear localStorage), and a broker
                    // pin outranks both. Saying "you will have to enable it"
                    // would be wrong in exactly the case above.
                    tail.textContent = 'An installed mod’s reported DEFAULT is '
                        + 'off, whatever its manifest says. What actually runs '
                        + 'after the reload is this browser’s own choice for '
                        + 'that id — which may survive from an earlier install '
                        + '— unless a broker pin overrides it.\n\nIt appears on '
                        + 'the NEXT page load. Nothing in this page changes.';
                    c.appendChild(tail);
                    bodyOk = true;
                },
                buttons: [{ label: 'Install', value: true, primary: true },
                          { label: 'Cancel', value: false }],
            });
            if (!r || !r.value) return;
            if (!bodyOk) {
                _modOpResult('Install cancelled',
                    ['the preview could not be drawn, so nothing was sent.'],
                    false);
                return;
            }
            await _modInstallRun(pick, !!(replaceCb && replaceCb.checked));
        }

        async function _modInstallRun(pick, replace) {
            let body = null;
            try {
                body = JSON.stringify({ manifest: pick.manifest,
                                        files: pick.files,
                                        replace: !!replace });
            } catch (_) {
                _modOpResult('Install failed',
                    ['that mod could not be encoded as JSON; nothing was sent.'],
                    false);
                return;
            }
            let r = null;
            try {
                // #191: same admin gate as the uninstall — feature-detected
                // off the memoized /info, byte-identical wire when this
                // broker does not enforce the admin class.
                const gated = await adminGatedFetch(localHost(),
                    '/mods/install', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: body,
                    }, await _localAdminInfo(), 'install a mod');
                if (gated.aborted === 'cancelled') {
                    _modOpResult('Install cancelled',
                        ['this broker requires its admin token for an '
                         + 'install, and none was entered — nothing was '
                         + 'sent.'], false);
                    return;
                }
                if (gated.aborted === 'refused') {
                    _modOpResult('Install refused',
                        ['this broker requires its admin token and did not '
                         + 'accept the one entered. An admin refusal happens '
                         + 'before the package is read — nothing was '
                         + 'written.'], false);
                    return;
                }
                r = gated.res;
            } catch (_) {
                _modOpResult('Install outcome unknown',
                    _modUnknownOutcomeLines('install'), true);
                return;
            }
            let j = null;
            let parsed = true;
            try { j = await r.json(); } catch (_) { j = null; parsed = false; }
            if (r.ok && !parsed) {
                _modOpResult('Install outcome unknown',
                    _modUnknownOutcomeLines('install'), true);
                return;
            }
            if (!r.ok || !j || j.ok !== true) {
                const lines = _modFailLines(r, j);
                const code = (j && typeof j.error === 'string') ? j.error : '';
                // Only a VALIDATION refusal proves nothing was written — the
                // validator runs entirely in memory before a single byte. A
                // write_failed happened mid-commit and an unrecognised code is
                // unrecognised, so neither may claim it.
                lines.push(_modRefusalProvesNoWrite(code)
                    ? 'Nothing was written — this broker validates the whole '
                      + 'package before it writes a byte.'
                    : 'This broker may have written part of it; reload and '
                      + 'check this list before trying again.');
                _modOpResult('Install refused', lines, false);
                return;
            }
            const adopts = (j.adopts_existing_state
                && typeof j.adopts_existing_state === 'object')
                ? j.adopts_existing_state : {};
            const lines = [
                _modClamp(String(j.id || ''), 64)
                    + (j.replaced ? ' was replaced' : ' is installed')
                    + ' on this broker.',
                'Generation ' + _modClamp(String(j.gen || ''), 12) + '.',
            ];
            if (adopts.mod_store) {
                lines.push('It inherited /mod-store data this broker already '
                    + 'held under that id.');
            }
            if (adopts.pin) {
                lines.push('A pin this broker already held under that id now '
                    + 'applies to it.');
            }
            lines.push('Its reported default is off — check this list after the '
                + 'reload for what it actually came up as. Its code runs from '
                + 'the next page load either way; "off" only means its init() '
                + 'is not called.');
            lines.push('It appears on the NEXT page load. Nothing in this page '
                + 'changed.');
            _modOpResult('Installed', lines, true);
        }
