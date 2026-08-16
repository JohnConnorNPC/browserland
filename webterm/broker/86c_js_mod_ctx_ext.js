        // ---- ctx extensions (#194) ------------------------------------------
        // Where NEW per-mod ctx surface lands. 86_js_mod_loader.js is at the
        // #68 2500-line per-fragment cap (_MAX_LINES, ui.py), and the rule for
        // that cap has always been "split, never trim" — 86a (#168) and 86b
        // (#163) are the precedent. So the loader keeps ctx v1, the
        // EXTENDER REGISTRY lives HERE, and every family added after it is
        // declared here (or
        // in a later 86*-ordered fragment) and registered into that registry.
        //
        // How to add one:
        //
        //     // ---- ctx.<family> (#<issue>) ----
        //     function _ctx<Family>(ctx, rec) {
        //         ctx.<family> = { … };          // decorate in place
        //     }
        //     _registerCtxExtender(_ctx<Family>);
        //
        // The three rules that make that safe, all enforced by the registry
        // block immediately below:
        //
        //  1. ARGUMENTS, NOT CLOSURE. Assembly concatenates every fragment into
        //     ONE <script>, so a top-level function declared here is callable
        //     from the loader and vice versa — but this fragment can NOT see
        //     makeCtx's per-mod locals (modId, ns, …). An extender is handed
        //     `ctx` (whose `id` is the mod id) and `rec` (the active-mod record:
        //     `rec.unloads` is the LIFO teardown list every ctx family already
        //     registers its disposers on), and anything else it needs has to
        //     arrive as an argument too. Same discipline as
        //     mods/update/update-apply.js.
        //  2. ISOLATION AND ORDER. Extenders run in registration order, which is
        //     ui._ORDERED order; a throwing one is logged and skipped without
        //     taking its siblings — or the mod's ctx — down. So a surface that
        //     fails to install is one missing family a mod can feature-detect,
        //     never a dead desktop.
        //  3. ADDITIVE, FEATURE-DETECTED. `ctxVersion` stays 1 (declared in the
        //     loader, enforced in initMod): every family here is additive, and a
        //     mod tests for it the way it tests for ctx.file —
        //     `if (ctx.<family>)` / `typeof ctx.<family>.<fn> === 'function'`.
        //     Bumping the version would refuse every mod that pins v1.
        //
        // Nothing runs at load beyond the _registerCtxExtender calls themselves
        // (and the capability registrations below, which are declarations too):
        // an extender body executes once per mod init, from makeCtx.
        //
        // #197's ctx.capabilities is the first family to ride the registry;
        // #194/#195/#196/#198 land beside it in this same fragment, and each
        // registers its own capability entry so the map below stays a true
        // inventory of what this build hands a mod.

        // ---- ctx-extender registry (#194) -----------------------------------
        // The seam a LATER ctx surface is added through: its own ordered
        // fragment (86c_js_mod_ctx_ext.js and successors) declares a NAMED
        // function and registers it at top level —
        //
        //     function _ctxWindowsFactory(ctx, rec) { ctx.windows.createAppWindow = …; }
        //     _registerCtxExtender(_ctxWindowsFactory);
        //
        // — and makeCtx applies every registered extender to the ctx object it
        // is building. That is what lets this fragment stop growing: it sits at
        // the #68 2500-line cap, so new surface CANNOT land here, and a registry
        // means each extension fragment owns its own members. One shared
        // extension function could not compose — the moment a second fragment
        // declared its own, the later declaration would win and the earlier
        // fragment's members would silently vanish.
        //
        // Four properties the extension fragments are entitled to rely on:
        //   - ORDER. Extenders run in registration order, which is _ORDERED
        //     (fragment) order: every fragment's top-level code runs in that
        //     order inside the one <script>, so a later fragment sees what an
        //     earlier one put on the ctx.
        //   - PER-EXTENDER ISOLATION. A throwing extender is logged like every
        //     other per-mod failure and its siblings still run; ctx construction
        //     is never abandoned, so one broken surface cannot cost a mod its
        //     init (let alone every mod theirs).
        //   - IDENTITY-IDEMPOTENT. Registering the SAME function twice runs it
        //     once. Guarded at BOTH edges — the registrar refuses a repeat, and
        //     the apply loop skips any entry that is not its own first
        //     occurrence — so a fragment that pushes onto the array by hand
        //     cannot decorate a ctx twice either.
        //   - ARGUMENTS, NOT CLOSURE. An extender receives (ctx, rec) and
        //     nothing else: a companion fragment shares this scope but NOT
        //     makeCtx's per-mod locals, so everything it needs arrives as an
        //     argument (the mods/update/update-apply.js pattern).
        //
        // A fragment-level `const` is safe here, unlike the hoisted functions
        // the header's TDZ note warns about: nothing that runs BEFORE this
        // fragment registers or applies an extender — the pushes come from LATER
        // fragments' top level and the apply first runs at loadMods() time.
        const _ctxExtenders = [];
        // Register one ctx extender. Returns true when it was added, false for a
        // non-function or a duplicate (by function IDENTITY), so a double-loaded
        // fragment is a no-op rather than a doubly-applied surface.
        function _registerCtxExtender(fn) {
            if (typeof fn !== 'function') return false;
            if (_ctxExtenders.indexOf(fn) !== -1) return false;
            _ctxExtenders.push(fn);
            return true;
        }
        // Apply the registry to one ctx under construction. Returns the SAME
        // object it was handed (extenders decorate in place; a returned value is
        // ignored, so an extender cannot swap the ctx out from under makeCtx).
        function _applyCtxExtenders(ctx, rec) {
            // SNAPSHOT, never the live array: an extender that registers during
            // the pass would otherwise extend the loop it is running in -- one
            // that appends a fresh function identity every call never
            // terminates, and ctx construction hanging takes the desktop with
            // it. A registration made mid-pass simply applies from the next
            // mod onwards, which is also the only order anyone can reason about.
            const list = _ctxExtenders.slice();
            for (let i = 0; i < list.length; i++) {
                const fn = list[i];
                if (list.indexOf(fn) !== i) continue;            // dup: run once
                try { fn(ctx, rec); }
                catch (e) {
                    // The report must not become the second failure: `fn.name`
                    // and `ctx.id` are attacker-adjacent reads (a proxy, a
                    // throwing getter), and a throw HERE would escape the loop
                    // and cost every remaining extender.
                    try {
                        console.error('[mods] ctx extender failed ("'
                            + (fn.name || 'anonymous') + '") for "'
                            + (ctx && ctx.id) + '":', e);
                    } catch (_) {
                        try { console.error('[mods] ctx extender failed'); }
                        catch (__) { /* console itself is gone; keep going */ }
                    }
                }
            }
            return ctx;
        }
        // ---- end ctx-extender registry --------------------------------------

        // ---- ctx.capabilities + the `needs` gate (#197) ---------------------
        // Two halves of one question a mod cannot otherwise ask: WHAT DOES THIS
        // BUILD'S ctx ACTUALLY OFFER?
        //
        //   ctx.capabilities   a FROZEN per-mod map, {family: <integer>}, of the
        //                      surface this ctx really carries. It is the answer
        //                      mods reach for today by sniffing loader PRIVATES
        //                      by name (mod-sync tests `typeof _modTextOk` and
        //                      `typeof _pin`). Integers rather than booleans on
        //                      purpose: a version constraint ('serverStore>=2')
        //                      can be added later without a shape change.
        //   needs: ['file', 'windows.onTerminalCreate']
        //                      a registerMod declaration naming ctx surface —
        //                      member names or dotted member paths — that must
        //                      be PRESENT for this mod to run here. An unmet
        //                      entry blocks init (nothing partially initialized,
        //                      the slot released) and the Mods pane row reads
        //                      `blocked (needs windows.onTerminalCreate)`
        //                      instead of showing a mod that is "active" and
        //                      silently doing nothing (mods/git/git.js:33).
        //
        // Four rules worth stating before the code:
        //
        //  1. OBSERVED, NOT PROMISED. The map is built by checking each
        //     registered capability NAME against the ctx being handed to THIS
        //     mod, and `needs` resolves against that same live object. So a
        //     family whose extender threw (the registry logs it and moves on) is
        //     absent from both, and the two can never disagree with each other
        //     or with the ctx. The map is built LAZILY, on first read, because
        //     an extender registered after this one has not decorated the ctx
        //     yet while this one runs — by the time a mod reads ctx.capabilities
        //     from its init(), every extender has.
        //  2. FROZEN, PER MOD. Every ctx gets its own frozen map, so a mod that
        //     mutates its copy cannot make the NEXT mod's map (or gate) read a
        //     lie. The trust model is unchanged — a mod can reach anything (#71)
        //     — but nothing it does here is INHERITED by its neighbours.
        //  3. A PIN CANNOT OVERRIDE A NEED. A pin is policy; a need is a fact
        //     about this build. Pinning a mod on cannot conjure the surface it
        //     asked for, so a pinned-on mod with an unmet need stays blocked and
        //     says why.
        //  4. ABSENCE IS NEVER AN ERROR (#157). An older loader ignores `needs`
        //     entirely — registerMod copies decl fields selectively, so an
        //     unknown one is simply dropped and the mod runs there exactly as it
        //     does today, unguarded. The loader's two call sites are `typeof`
        //     guarded for the mirror case (this fragment served without the
        //     loader half) and the registration below is guarded the same way
        //     for a page assembled without #194's registry. Mods keep their own
        //     `typeof ctx.x` feature detection as the second line of defence:
        //     `needs` makes the degradation VISIBLE, it does not replace it.
        //
        // `needs` is NOT `requires` (which names mod ids and cascades through
        // pins and take-downs) and NOT #193's manifest `permissions` (review
        // capabilities, linted server-side at install time). Three declarations,
        // three questions: what other MODS must run, what a REVIEWER permits,
        // and what this BUILD offers.

        // The capability registry: name -> version integer, seeded below with
        // the families ctx v1 carries today — read off makeCtx's own ctx literal
        // in 86_js_mod_loader.js, minus `id`/`ctxVersion`, which are metadata
        // about the ctx rather than surface a mod can use. A family added later
        // registers its entry from its OWN fragment, beside its extender, so an
        // entry always ships with the surface it names.
        //
        // Object.create(null) for the reason window.__mods.packages gives: these
        // keys are NAMES out of a declaration, and an inherited `constructor` /
        // `toString` would answer for something nobody registered — while a
        // literal `__proto__` key would write through a setter instead of
        // landing in the map at all.
        const _MOD_CTX_CAPABILITIES = Object.create(null);
        // Register one capability: an identifier naming a ctx MEMBER (never a
        // dotted path — the map is per family; `needs` is what resolves paths)
        // at a version integer. false for a malformed name, for a duplicate (no
        // silent re-levelling of a family somebody else registered) and for
        // `capabilities` itself, which would make the lazy map re-enter its own
        // getter. Registration happens at FRAGMENT TOP LEVEL, before loadMods
        // builds any ctx, which is why nothing here re-inits an already-blocked
        // mod: there is no path that adds a capability after boot.
        function _registerModCapability(name, level) {
            if (typeof name !== 'string'
                    || !/^[A-Za-z_$][A-Za-z0-9_$]*$/.test(name)) return false;
            if (name === 'capabilities') return false;
            if (name in _MOD_CTX_CAPABILITIES) return false;
            _MOD_CTX_CAPABILITIES[name] =
                (typeof level === 'number' && isFinite(level)) ? level : 1;
            return true;
        }
        for (const _cap of ['onUnload', 'visibility', 'storage', 'file',
                            'serverStore', 'session', 'clipboard', 'taskbar',
                            'desktop', 'registerKeyActions',
                            'registerWindowMenuItems',
                            'registerDesktopMenuItems', 'windows', 'settings',
                            'theme', 'registerSettingsPane',
                            'registerHelpCards', 'registerWindowKind']) {
            _registerModCapability(_cap, 1);
        }

        // Does `path` — 'file', or 'windows.onTerminalCreate' — resolve to a
        // real member of `obj`? OWN properties at every segment (hasOwnProperty,
        // not `in`), or every mod would "have" toString, constructor and
        // valueOf; and the resolved value must not be null/undefined, so a
        // member that exists but was never wired up reads as unmet rather than
        // met. A member that THROWS on access is unmet too: a surface a mod
        // cannot even read is not one it can rely on.
        function _modCtxHas(obj, path) {
            if (!obj || typeof path !== 'string' || !path) return false;
            const parts = path.split('.');
            let cur = obj;
            for (let i = 0; i < parts.length; i++) {
                if (!parts[i]) return false;            // '', 'a..b', 'a.', '.a'
                if (cur === null || cur === undefined) return false;
                if (typeof cur !== 'object' && typeof cur !== 'function') {
                    return false;                       // a string has no members
                }
                if (!Object.prototype.hasOwnProperty.call(cur, parts[i])) {
                    return false;
                }
                try { cur = cur[parts[i]]; } catch (_) { return false; }
            }
            return cur !== null && cur !== undefined;
        }

        // THIS ctx's map: every registered capability the ctx really carries, at
        // its declared version. A fresh frozen object per mod (shallow is
        // enough — the values are integers).
        function _modCapabilityMap(ctx) {
            const map = Object.create(null);
            for (const name of Object.keys(_MOD_CTX_CAPABILITIES)) {
                if (_modCtxHas(ctx, name)) {
                    map[name] = _MOD_CTX_CAPABILITIES[name];
                }
            }
            return Object.freeze(map);
        }
        // The extender. ctx.capabilities is computed on FIRST READ (rule 1) and
        // cached for the life of that ctx, through a getter-only, non-
        // configurable property: a mod cannot replace the map, and the frozen
        // object means it cannot edit its own copy either.
        function _ctxCapabilities(ctx) {
            let map = null;
            Object.defineProperty(ctx, 'capabilities', {
                enumerable: true,
                configurable: false,
                get: function () {
                    if (!map) map = _modCapabilityMap(ctx);
                    return map;
                },
            });
        }
        // Guarded for the same reason the loader's calls into this fragment are:
        // a page assembled without #194's registry must not throw here.
        if (typeof _registerCtxExtender === 'function') {
            _registerCtxExtender(_ctxCapabilities);
        }

        // registerMod's normalizer (called from the loader, guarded): a decl's
        // `needs` -> an array of non-empty strings, mirroring `requires` and
        // `tiers` exactly. Anything else is dropped rather than thrown on — a
        // malformed declaration must not be able to take the page down, and the
        // Mods pane row is where an operator learns what happened.
        function _modNeedsDecl(decl) {
            const raw = decl && decl.needs;
            return Array.isArray(raw)
                ? raw.filter(function (n) { return typeof n === 'string' && n; })
                : [];
        }
        // The declared needs THIS ctx does not satisfy, in declaration order,
        // deduped. Re-normalizes on the way in, because a raw __test.run() decl
        // never went through registerMod.
        function _modUnmetNeeds(decl, ctx) {
            const out = [];
            for (const name of _modNeedsDecl(decl)) {
                if (!_modCtxHas(ctx, name) && out.indexOf(name) === -1) {
                    out.push(name);
                }
            }
            return out;
        }
        // initMod's gate, one guarded call in 86. null => every need is met and
        // init proceeds. Otherwise the mod is REFUSED before its init() runs:
        // the record's teardowns are drained (a ctx extender may have registered
        // one), the slot claimed before ctx construction is released, and a
        // structured refusal goes back the way every other initMod refusal does
        // — never a throw. The unmet list is stashed for the Mods pane and
        // CLEARED on the met path, so a mod that comes up later never reads
        // blocked on stale news.
        function _modNeedsGate(id, decl, ctx, rec) {
            const unmet = _modUnmetNeeds(decl, ctx);
            const bag = _modBag('unmetNeeds');
            if (!unmet.length) { delete bag[id]; return null; }
            bag[id] = unmet;
            console.info('[mods] "' + id + '" needs ctx surface this build does '
                + 'not offer: ' + unmet.join(', '));
            _runUnloads(rec);
            window.__mods.active.delete(id);
            return { ok: false, reason: 'needs', needs: unmet };
        }
        // ---- end ctx.capabilities + needs gate ------------------------------
