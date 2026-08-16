        // ---- ctx extensions (#194) ------------------------------------------
        // Where NEW per-mod ctx surface lands. 86_js_mod_loader.js is at the
        // #68 2500-line per-fragment cap (_MAX_LINES, ui.py), and the rule for
        // that cap has always been "split, never trim" — 86a (#168) and 86b
        // (#163) are the precedent. So the loader keeps ctx v1 and one CALL to
        // the extender registry; the registry itself lives HERE, and every
        // family added after v1 is declared here (or in a later 86*-ordered
        // fragment) and registered into it.
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
        //: True only while `_applyCtxExtenders` is mid-pass. `ctx.capabilities`
        //: reads it so a map built by an extender (or by a stray spread) during
        //: the pass is answered truthfully but never CACHED -- the ctx is not
        //: finished yet, and a cached partial map would lie for the page's life.
        let _ctxExtendersApplying = false;
        function _applyCtxExtenders(ctx, rec) {
            // SNAPSHOT, never the live array: an extender that registers during
            // the pass would otherwise extend the loop it is running in -- one
            // that appends a fresh function identity every call never
            // terminates, and ctx construction hanging takes the desktop with
            // it. A registration made mid-pass simply applies from the next
            // mod onwards, which is also the only order anyone can reason about.
            const list = _ctxExtenders.slice();
            const wasApplying = _ctxExtendersApplying;
            _ctxExtendersApplying = true;
            try {
                for (let i = 0; i < list.length; i++) {
                    const fn = list[i];
                    if (list.indexOf(fn) !== i) continue;        // dup: run once
                    try {
                        fn(ctx, rec);
                    } catch (e) {
                        // The report must not become the second failure:
                        // `fn.name` and `ctx.id` are attacker-adjacent reads (a
                        // proxy, a throwing getter), and a throw HERE would
                        // escape the loop and cost every remaining extender.
                        try {
                            console.error('[mods] ctx extender failed ("'
                                + (fn.name || 'anonymous') + '") for "'
                                + (ctx && ctx.id) + '":', e);
                        } catch (_) {
                            try { console.error('[mods] ctx extender failed'); }
                            catch (__) { /* console is gone; keep going */ }
                        }
                    }
                }
            } finally {
                _ctxExtendersApplying = wasApplying;
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
                // `hasOwnProperty` itself can throw: an intermediate family
                // that is a Proxy (or a revoked one) runs a trap here. An
                // unmet need must stay a STRUCTURED refusal the pane can word,
                // never a generic init failure -- so anything that throws
                // while being asked reads as absent, exactly like a throwing
                // accessor below.
                let own = false;
                try {
                    own = Object.prototype.hasOwnProperty.call(cur, parts[i]);
                } catch (_) { return false; }
                if (!own) return false;
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
                // NOT enumerable. A read is what freezes the map, and an
                // enumerable accessor is read by things that are not asking:
                // `{...ctx}`, `Object.assign({}, ctx)`, a JSON round-trip, a
                // dev-tools expansion. Any of those happening DURING the
                // extender pass would cache a map missing every family
                // registered after it -- and the mod would then be told, for
                // the rest of the page's life, that it lacks a member it
                // demonstrably has. Non-enumerable also keeps every existing
                // spread of `ctx` byte-identical to before this key existed.
                enumerable: false,
                configurable: false,
                get: function () {
                    // Never cache a map built while the pass is still running:
                    // whoever asked that early gets a truthful snapshot, and
                    // the FIRST read after the ctx is finished is the one that
                    // sticks. `init()` -- the only intended reader -- always
                    // runs after.
                    if (map) return map;
                    const built = _modCapabilityMap(ctx);
                    if (_ctxExtendersApplying) return built;
                    map = built;
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

        // ---- ctx.windows.createAppWindow + ctx.windows.list() (#194) --------
        // Nine shipped mods hand-build the SAME ~30-field app-window scaffold:
        // a `win` literal pushed straight into the core `windows` Map (64), a
        // synthetic kind:'app' session, a hand-appended taskbar chip (75), the
        // #taskbar-empty removal, and a teardown that iterates core state to
        // find its own windows. It is the largest core-coupling surface in the
        // codebase: a change to the record shape breaks all nine at once, and
        // breaks them SILENTLY, because a hand-mirrored literal does not fail
        // loudly. So core owns the scaffold and a mod supplies only what is
        // specific to ITS window:
        //
        //     const h = ctx.windows.createAppWindow({
        //         kind: 'clipboard',         // the appKind (required)
        //         title: 'Clipboard',
        //         sid: 'clip',               // chip/badge short id
        //         singleton: true,           // open-or-FOCUS, never a second
        //         toolbar: function (el, win) { … },   // optional
        //         body: function (el, win, h) { … },
        //     });
        //     ctx.windows.list();            // THIS mod's live windows, frozen
        //
        // MIGRATING A KIND: registerWindowKind's `factory` must keep returning a
        // WINDOW RECORD — openAppWindow hands that value straight back to its
        // callers, and its own dedupe branch returns one — so a factory built on
        // this returns `h.win`, never the handle. (Routing a launch through
        // openAppWindow then costs a second revealAndFocusWindow, since this
        // factory does its own; both are idempotent, and a mod whose launcher
        // calls createAppWindow directly needs its own.)
        //
        // Core-owned from here on, exactly as the copies did it by hand: the
        // chrome (buildAppChrome + wireAppChrome + the eight resize handles,
        // 73), the desktop insertion, the window record, the synthetic session
        // + taskbar chip, the id (newAppId, 54) and the placement tail
        // (finishWindowPlacement, 62a). Every one of those is an EXISTING core
        // function — this factory adds no new core entry point, it just stops
        // nine mods from re-deriving the call order.
        //
        // The spec fields are the union of what the nine copies genuinely VARY,
        // not an invented API: kind/id/title/sid/badge/appClass/bodyClass
        // (clipboard '#clip'/.app-clip/.clip-body vs task-manager '#tm'/.app-tm
        // /.tm-body vs scratchpad '#notes' vs recorder '#rec'), geom + color +
        // locked (all three read `appData` the same way), `resizable:false`
        // (recorder playback appends NO handles — the recording dictates the
        // size), `toolbar` (the .app-toolbar div clipboard/task-manager/
        // scratchpad each build), `onClose` (wireAppChrome's third argument;
        // the editor passes requestCloseAppWindow so a dirty buffer can flush)
        // and `singleton` (clipboard's CLIP_WIN_ID open-or-focus hack, which is
        // re-derived by every windowed mod).
        //
        // THE RESTORE SEAM is wired at the end of this section. `spec.restoring`
        // is accepted here and does exactly one thing — suppress the create-time
        // focus, like openAppWindow's opts.restoring — so a restore that lands
        // before the user is looking does not steal focus; and
        // registerWindowKind's restore hook is HANDED this same API, so a
        // restored window is built by the core-owned path a fresh one is. The
        // dedupe below is what makes that safe: an id that is live is ADOPTED,
        // never double-built (#167).
        //
        // What is NOT here, deliberately, and where it goes:
        //   - THE STAGED TAKE-DOWN / closeAll(). The owned-window registry they
        //     need is `rec.appWindows` — deliberately on the RECORD, not in
        //     this closure, so the loader's take-down pass can reach it without
        //     seeing this fragment's locals (the arguments-not-closure rule).
        //     Whoever writes it: NEVER model that close pass as an onUnload
        //     entry. It would be the OLDEST entry in the LIFO chain and would
        //     run AFTER the mod's own disposers deregistered its kinds, which
        //     is exactly the junk-record ordering task-manager's comment block
        //     documents (a later saveAppWindow falls back to the shared
        //     serializer). Close while every kind is still registered.
        //   - onAppWindowCreate(). Its fire point is the marked line at the end
        //     of _createModAppWindow; its REPLAY set is NOT this registry but
        //     "live windows whose appKind is one of this mod's REGISTERED
        //     kinds", which includes core's unknown-kind restore records this
        //     factory never built (sticky's hand-written catch-up pass).
        //
        // Additive, feature-detected: ctxVersion stays 1, and `windows` is
        // already a v1 capability family (#116's onTerminalCreate lives there),
        // so this DECORATES ctx.windows in place — replacing it would delete a
        // sibling's member. No new _registerModCapability entry is registered
        // for the same reason: the map is per FAMILY and `windows` is already
        // in the seed, so the capability drift gate stays satisfied. A mod tests
        // `typeof ctx.windows.createAppWindow === 'function'` or declares
        // `needs: ['windows.createAppWindow']` — #197 resolves dotted paths
        // against the live ctx, which is the finer-grained answer anyway.

        // THIS mod's factory-built app windows: id -> handle, insertion
        // ordered. On the per-mod record (see above), so the take-down pass and
        // closeAll can reach it; a fresh record per init means a disable/enable
        // cycle starts empty, which is what keeps list() honest afterwards.
        function _modAppWindows(rec) {
            if (!rec || typeof rec !== 'object') return new Map();
            if (!rec.appWindows) rec.appWindows = new Map();
            return rec.appWindows;
        }
        // Is this handle's window still a live member of the core windows Map?
        // Windows die three ways — closeWindow (73), the lease-loss
        // teardownView (84) and a mod closing one by hand — and the first two
        // drain win.cleanups, which is where the prune below rides. This is the
        // belt-and-braces read on top of it, so list() can never report a ghost
        // (and an id REUSED by a later window never resurrects a stale handle).
        function _modAppWindowLive(h) {
            return !!(h && h.win && !h.win.disposed
                      && windows.get(h.win.id) === h.win);
        }
        // The synthetic kind:'app' session + its taskbar chip — the half every
        // one of the nine copies re-typed. kind:'app' is what keeps the
        // /sessions poll reaper off a window with no PTY behind it and what
        // lets formatTitle render the chip; the querySelector guard is the
        // copies' own (a chip for this id may already exist after a rebuild).
        function _modAppWindowChip(win, sid, title) {
            const id = win.id;
            const sess = { key: id, sid: sid, id: id, title: title,
                           stale: false, kind: 'app', hostId: 'app' };
            sessions.set(id, sess);
            const host = document.getElementById('taskbar-items');
            if (host && !host.querySelector('.taskbar-item[data-session-id="'
                    + cssEscape(id) + '"]')) {
                host.appendChild(buildTaskbarItem(sess));
            }
            updateTaskbarColor(id);
            updateTaskbarLabel(id);
            const empty = document.getElementById('taskbar-empty');
            if (empty) empty.remove();
            return sess;
        }
        // Register `win` as one of THIS mod's factory windows and return its
        // handle. Also the ADOPTION path: a record core built for this id
        // before the mod loaded (#167's unknown-kind fallback) becomes ours on
        // the next create, so a restored window is focused rather than
        // double-built. Idempotent by (id, win) identity.
        //
        // The handle IS the contract; `h.win` is the escape hatch the #71 trust
        // model permits, not the API. Each member exists because a scaffold
        // does it by hand today:
        //   isOpen()        every async callback guards on win.disposed
        //   focus()         the (+) menu / tray chip open-or-focus path
        //   close()         unload closes its own windows
        //   setTitle()      editor's rename + re-home (title text + win.name +
        //                   the session title + updateTaskbarLabel: miss one
        //                   and the chip and the title bar disagree)
        //   setColor()      the title-bar color picker (--accent + dark-accent
        //                   + updateTaskbarColor, same three)
        //   save()          persisted kinds call saveAppWindow on every change
        //   addTitleBarItem() editor inserts its color swatch BEFORE min — the
        //                   placement rule mods get wrong; same word as #116's
        //                   onTerminalCreate
        //   onDispose()     win.cleanups.push, spelled the way onTerminalCreate
        //                   already spells it
        //
        // EVERY member is gated on `_modAppWindowLive(h)`, never on the bare id
        // and never on win.disposed alone. A handle outlives its window — a mod
        // keeps one in a closure — and a fixed id is REUSED (app:clip is the
        // same string for the window's next life), so `closeWindow(this.id)` on
        // a stale handle would close the window that replaced it. The gate
        // compares the RECORD, so a stale handle is inert instead of lethal.
        function _ownModAppWindow(rec, win, bodyEl, toolbarEl) {
            const owned = _modAppWindows(rec);
            const id = win.id;
            const already = owned.get(id);
            if (already && already.win === win) return already;
            const h = {
                id: id,
                win: win,
                dom: win.dom,
                body: bodyEl || win.body || null,
                toolbar: toolbarEl || null,
                isOpen: function () { return _modAppWindowLive(h); },
                focus: function () {
                    return _modAppWindowLive(h)
                        ? revealAndFocusWindow(id) : null;
                },
                close: function () { if (_modAppWindowLive(h)) closeWindow(id); },
                setTitle: function (t) {
                    const s = String(t == null ? '' : t);
                    if (!_modAppWindowLive(h)) return s;
                    win.name = s;
                    if (win.titleText) win.titleText.textContent = s;
                    const sess = sessions.get(id);
                    if (sess) sess.title = s;
                    updateTaskbarLabel(id);
                    return s;
                },
                setColor: function (c) {
                    const col = normalizeHex(c);
                    if (!_modAppWindowLive(h)) return col;
                    win.color = col;
                    win.dom.style.setProperty('--accent', col);
                    win.dom.classList.toggle('dark-accent', isDarkAccent(col));
                    updateTaskbarColor(id);
                    return col;
                },
                // Never persist a dead window: saveAppWindow would write a
                // record for something already torn down.
                save: function () { if (_modAppWindowLive(h)) saveAppWindow(win); },
                addTitleBarItem: function (node) {
                    if (!node || !_modAppWindowLive(h)) return null;
                    const bar = win.dom && win.dom.querySelector('.title-bar');
                    if (!bar) return null;
                    bar.insertBefore(node, bar.querySelector('.btn-min'));
                    return node;
                },
                // Refused once the window is dead rather than retained:
                // closeWindow drains `cleanups` and then EMPTIES it, so a
                // disposer pushed after (or during) that drain would be held
                // forever and never run. A caller that needs it now can see
                // isOpen() === false and run it itself.
                onDispose: function (fn) {
                    if (typeof fn !== 'function') return false;
                    if (!_modAppWindowLive(h)) return false;
                    win.cleanups.push(fn);
                    return true;
                },
            };
            // Frozen: a mod may not repoint `h.win` (the next list() would
            // then prune a live window as a ghost) or swap a method for a
            // no-op. The window RECORD behind it stays the documented escape
            // hatch — this freezes the handle, not the desktop.
            Object.freeze(h);
            owned.set(id, h);
            // Prune on every teardown path at once: closeWindow (73) and the
            // active-view rebuild (84) both drain win.cleanups. Guarded by
            // identity so a later window at the same id is not evicted.
            win.cleanups.push(function () {
                if (owned.get(id) === h) owned.delete(id);
            });
            return h;
        }
        // THIS mod's live windows, newest last, as a FROZEN fresh array: a mod
        // that mutates what it got back changes neither core state nor the next
        // call. Dead entries are pruned on the way past, so a list() after a
        // lease-loss rebuild is empty rather than a wall of ghosts.
        function _listModAppWindows(rec) {
            const owned = _modAppWindows(rec);
            const out = [];
            for (const pair of Array.from(owned.entries())) {
                if (_modAppWindowLive(pair[1])) out.push(pair[1]);
                else owned.delete(pair[0]);
            }
            return Object.freeze(out);
        }
        // The factory. Build order is the copies' own, and it is load-bearing:
        // chrome, then the mod's containers, then the desktop insertion, then
        // the record + windows.set, then wireAppChrome, then the chip, THEN the
        // mod's own content, and only then the resize handles (they are
        // absolute overlays and must stay the LAST children) and the placement
        // tail. The mod's body() therefore runs against a window that is
        // already in the document and already in the windows Map — it has a
        // layout box (what CodeMirror needs) and a record to hang state on,
        // though not necessarily its FINAL box: finishWindowPlacement may still
        // tile it, and a tiled window is measured again on the relayout.
        function _createModAppWindow(rec, spec) {
            if (!spec || typeof spec !== 'object' || Array.isArray(spec)) {
                throw new Error('createAppWindow: spec must be an object');
            }
            const kind = spec.kind;
            if (typeof kind !== 'string' || !kind) {
                throw new Error(
                    'createAppWindow: a non-empty string kind is required');
            }
            const singleton = spec.singleton === true;
            // A STABLE id is what makes `singleton` survive a reload — the
            // clipboard's hand-rolled CLIP_WIN_ID is literally 'app:clip' — and
            // an explicit id keeps a migrating mod's existing window keys (and
            // the stored geometry hanging off them) unchanged.
            const id = (spec.id != null && String(spec.id))
                ? String(spec.id)
                : (singleton ? ('app:' + kind) : newAppId(kind));

            // ---- open-or-focus, the dedupe every windowed mod re-derives ----
            // A singleton dedupes on KIND, over the LIVE core windows and not
            // just the ones this mod already owns: core's unknown-kind fallback
            // restores records for a mod-owned kind before the mod loads
            // (#167), under whatever id the stored record carried — which need
            // not be the id asked for here. Scanning only our own registry
            // would leave that one on screen and build a second beside it,
            // which is the one thing `singleton` promises cannot happen.
            // Everything else dedupes on ID, because a second record at one id
            // would strand the first in the windows Map. Both arms mirror
            // openAppWindow's tail exactly: focus what is already there unless
            // the caller is a restore, which is nobody asking for this window
            // now.
            if (singleton) {
                for (const w of Array.from(windows.values())) {
                    if (!w || w.disposed || w.type !== 'app') continue;
                    if (w.appKind !== kind) continue;
                    const h = _ownModAppWindow(rec, w, w.body, null);
                    if (!spec.restoring) revealAndFocusWindow(w.id);
                    return h;
                }
            }
            const open = windows.get(id);
            if (open && open.disposed) {
                // A TOMBSTONE, not a window: closeWindow marks the record
                // disposed and drains win.cleanups before it deletes the Map
                // entry, so the only way to see one is a re-entrant create from
                // inside a teardown (an onDispose that reopens). Building here
                // would put a fresh record at an id the unwinding close is
                // about to delete — Map entry, chip and session ripped out from
                // under a live window, leaving an untracked orphan on the
                // desktop. Refuse; reopen from a timer, after the close.
                throw new Error('createAppWindow: window id "' + id
                    + '" is being torn down; reopen it after its close returns');
            }
            if (open) {
                // Two kinds sharing one id is a programming error, and building
                // over it would strand a live window: refuse loudly rather than
                // corrupt the desktop.
                if (open.appKind !== kind) {
                    throw new Error('createAppWindow: window id "' + id
                        + '" is already open as kind "' + open.appKind + '"');
                }
                const h = _ownModAppWindow(rec, open, open.body, null);
                if (!spec.restoring) revealAndFocusWindow(id);
                return h;
            }

            // ---- the scaffold ----------------------------------------------
            const title = (typeof spec.title === 'string' && spec.title)
                ? spec.title : kind;
            const sid = (typeof spec.sid === 'string' && spec.sid)
                ? spec.sid : kind;
            const appClass = (typeof spec.appClass === 'string' && spec.appClass)
                ? spec.appClass : ('app-' + kind);
            const badge = (typeof spec.badge === 'string' && spec.badge)
                ? spec.badge : ('#' + sid);
            // appDefaultGeom only special-cases 'sticky-note', so passing the
            // appKind is byte-identical to the copies' appDefaultGeom(
            // 'text-editor') for every one of them AND right for a note.
            const geom = clampGeom(spec.geom || appDefaultGeom(kind));
            const color = normalizeHex(spec.color || defaultColor(id));
            const locked = spec.locked !== undefined ? !!spec.locked : true;

            const chrome = buildAppChrome({
                id: id, appClass: appClass, badge: badge, geom: geom,
                color: color, locked: locked, title: title,
            });
            const dom = chrome.dom;
            // The .app-toolbar strip clipboard/task-manager/scratchpad each
            // build by hand, above the body. A window that wants one BELOW its
            // body (recorder playback's transport bar) appends it to win.dom
            // from body() — anything added there still lands before the resize
            // handles, which go on last.
            let toolbarEl = null;
            if (spec.toolbar) {
                toolbarEl = document.createElement('div');
                toolbarEl.className = 'app-toolbar ' + appClass + '-toolbar';
                dom.appendChild(toolbarEl);
            }
            const bodyEl = document.createElement('div');
            bodyEl.className =
                (typeof spec.bodyClass === 'string' && spec.bodyClass)
                    ? spec.bodyClass : (appClass + '-body');
            dom.appendChild(bodyEl);

            const desktop = document.getElementById('desktop');
            desktop.appendChild(dom);
            desktop.classList.remove('empty');

            // The record. ONE copy of the ~30 fields, here, instead of nine
            // hand-mirrored literals; a mod's own per-window state goes on
            // `win` from body(), exactly as it does today.
            const win = {
                id: id, sid: sid, hostId: 'app',
                type: 'app', appKind: kind,
                dom: dom, body: bodyEl, titleText: chrome.titleText,
                term: null, fitAddon: null,
                ws: null, wsOpen: false, termReady: false,
                minimized: false, disposed: false,
                geom: geom, name: title, color: color,
                resizeTimer: null, lastSentDims: null,
                staleSession: false, authFailed: false,
                reattachAttempts: 0, reattachAt: 0, lastOpenAt: 0,
                missingPolls: 0,
                cleanups: [],
                tiled: false,
                floatGeom: spec.floatGeom
                    ? Object.assign({}, spec.floatGeom) : null,
                locked: locked,
                dirty: false,
            };
            windows.set(id, win);
            // Raise / minimize / close / drag / 8-way resize / WM context menu.
            // spec.onClose replaces the × action (the editor's dirty-buffer
            // prompt is requestCloseAppWindow); undefined keeps closeWindow.
            wireAppChrome(win, chrome,
                (typeof spec.onClose === 'function') ? spec.onClose : undefined);
            _modAppWindowChip(win, sid, title);

            const h = _ownModAppWindow(rec, win, bodyEl, toolbarEl);
            // The mod's own content, last, against a window that is already in
            // the windows Map, already in the document and already chipped — so
            // it has a layout box to read and a record to hang state on. (Not
            // its FINAL box: finishWindowPlacement may still tile it, and a
            // tiled window is measured again on the relayout.) A throwing
            // builder propagates: it is the mod's code on the mod's stack, and
            // the window is left built-but-empty exactly as a throw mid-
            // scaffold leaves one today — visible, closable, not a phantom.
            if (typeof spec.toolbar === 'function') spec.toolbar(toolbarEl, win, h);
            if (typeof spec.body === 'function') spec.body(bodyEl, win, h);
            // A builder is allowed to close the window it was handed (a load
            // that fails, a lease that was lost mid-build). Stop here if it
            // did: handles, placement and focus against a torn-down record are
            // at best wasted and at worst resurrect a chip for a dead window.
            if (!_modAppWindowLive(h)) return h;
            // AFTER body(): the eight handles are absolute-positioned overlays
            // whose hit zones must sit on top, i.e. be the last children.
            if (spec.resizable !== false) addResizeHandles(dom);
            finishWindowPlacement(win);
            // A window built NOW for someone who asked for it NOW belongs on
            // the workspace they are looking at (#152) — the same tail
            // openAppWindow runs after a factory returns. A restore is the one
            // caller that is not a person asking.
            if (!spec.restoring) revealAndFocusWindow(id);
            // SEAM (#194 / A31): onAppWindowCreate fires HERE, once, for a
            // window that is fully built, placed and focused. Its replay set is
            // registered-kind based, not this registry — see the header.
            return h;
        }
        // The extender. Decorates the EXISTING v1 `windows` family in place:
        // assigning a fresh object would delete #116's onTerminalCreate.
        function _ctxWindowsFactory(ctx, rec) {
            const fam = (ctx.windows && typeof ctx.windows === 'object')
                ? ctx.windows : (ctx.windows = {});
            fam.createAppWindow = function (spec) {
                return _createModAppWindow(rec, spec);
            };
            fam.list = function () { return _listModAppWindows(rec); };
        }

        // ---- the restore seam: a restore hook receives the factory (#194) ----
        // registerWindowKind's `restore` is the one window-building path a mod
        // may not be able to reach its ctx from. Core calls it DIRECTLY —
        //
        //     win = (kind && kind.restore ? kind.restore : openAppWindow)(
        //         rec, { restoring: true });     // 84_js_active_view_lifecycle
        //
        // — so a hook that is a HOISTED top-level builder (openNoteOrEditor
        // Window's shape, deliberately reachable with its mod disabled) has no
        // ctx in scope and has to stash one on a function property to reach
        // per-mod surface. `editorFile.cap` (mods/editor/editor.js) is exactly
        // that hack, and every mod that needs it re-derives it. So the hook is
        // handed the per-mod window API as a THIRD argument, which is purely
        // additive to core's two-argument call:
        //
        //     restore: function (record, opts, api) {
        //         const h = api.createAppWindow({ kind: 'scratchpad', … });
        //         return h.win;          // core wants a window RECORD…
        //     }                          // …though a handle is unwrapped too
        //
        // Two spec defaults come with it, and both are overridable:
        //   id         defaults to the RECORD's id. A restore that built at
        //              some other id would orphan the record it was handed AND
        //              step around the id dedupe, so the next pass would build
        //              a SECOND window rather than adopt this one.
        //   restoring  defaults to the flag core passed (true — restoreApp
        //              Windows is the one automatic caller), so a restored
        //              window is not REVEALED: no un-minimize, and no re-home
        //              to whichever workspace the page happened to boot on
        //              (#152). Exactly what openAppWindow's opts.restoring
        //              suppresses, and no more — a float still takes front
        //              through finishWindowPlacement's bringToFront, as every
        //              window built by any factory always has.
        //
        // WHY THE DEFAULTS, AND NOT A "RUN ONCE" GATE. Restore and the mod
        // loader are independent async chains: boot restores a mod-owned kind
        // before it is registered, core's unknown-kind fallback returns null,
        // and the record is re-attempted once the loader settles (#167's
        // restoreAppWindowsAfterMods). A lease-loss rebuild runs the whole pass
        // again, and a mod's own init-time catch-up pass (mods/sticky) can
        // restore the same window from the other side. None of that can be
        // gated on `_booted` — bootActiveView sets it BEFORE its state await —
        // so "restore runs at most once" is not a property anything here may
        // assume. Adopting is: with the two defaults above, _createModAppWindow
        // ADOPTS the live window (by id, or for a singleton by kind at ANY id,
        // which is what a reload needs — the stored record's id and the mod's
        // own singleton id need not agree) and builds nothing.

        // The API one restore invocation is handed. Frozen, and built per call:
        // the defaults are baked in from the record + opts core actually passed,
        // so nothing about them can be stale.
        function _modRestoreApi(rec, record, opts) {
            const rid = (record && record.id != null && String(record.id))
                ? String(record.id) : '';
            // Absent/odd opts reads as a restore: this hook has exactly one core
            // call site and it always is one, so the SAFE default is the one
            // that does not reveal-and-re-home a window nobody asked for.
            const restoring = (opts && opts.restoring !== undefined)
                ? !!opts.restoring : true;
            return Object.freeze({
                createAppWindow: function (spec) {
                    return _createModAppWindow(
                        rec, _modRestoreSpec(spec, rid, restoring));
                },
                list: function () { return _listModAppWindows(rec); },
            });
        }
        // The spec, plus the restore defaults, WITHOUT touching the mod's own
        // object: a hook that reuses one literal across restores would
        // otherwise have the FIRST record's id written into it permanently.
        //
        // Object.create(spec), not Object.assign({}, spec): the defaults become
        // own properties of a child object and everything else is READ THROUGH
        // the prototype chain, so a field that is inherited, non-enumerable or
        // an accessor still reads to the factory exactly as it did before this
        // wrapper existed. A flattening copy would silently drop those and turn
        // a spec that worked into one that does not. A non-object passes
        // through untouched, so the factory's own "spec must be an object"
        // refusal is what the mod sees.
        function _modRestoreSpec(spec, rid, restoring) {
            if (!spec || typeof spec !== 'object' || Array.isArray(spec)) {
                return spec;
            }
            const out = Object.create(spec);
            if (!(out.id != null && String(out.id)) && rid) out.id = rid;
            if (out.restoring === undefined) out.restoring = restoring;
            return out;
        }
        // A hook may return the HANDLE it just built; core wants the window
        // record. Unwrapped by IDENTITY against this mod's own registry, so a
        // hook returning anything else — a core-built record, null — keeps
        // returning exactly that.
        function _modRestoreResult(rec, v) {
            if (!v || typeof v !== 'object') return v;
            const owned = rec && rec.appWindows;
            if (owned && typeof owned.get === 'function'
                    && owned.get(v.id) === v) {
                return v.win || null;
            }
            return v;
        }
        // spec -> spec, with `restore` wrapped to receive the API. A spec with
        // no restore hook — every kind shipped today — comes back by IDENTITY:
        // the wrapper must be invisible to the mods that do not use it, and
        // core's validator must stay the thing that refuses a malformed spec
        // (including a non-function `restore`, which is passed through to it).
        function _modKindRestoreSpec(rec, spec) {
            if (!spec || typeof spec !== 'object' || Array.isArray(spec)) {
                return spec;
            }
            if (typeof spec.restore !== 'function') return spec;
            const restore = spec.restore;
            // Object.create for the same reason _modRestoreSpec uses it: every
            // other field core's validator reads — appKind, factory, serialize,
            // menu, the rest — must read exactly as it would have, so the
            // wrapper shadows `restore` on a CHILD and inherits the rest
            // rather than flattening a copy.
            const wrapped = Object.create(spec);
            wrapped.restore = function (record, opts) {
                return _modRestoreResult(rec, restore.call(
                    this, record, opts, _modRestoreApi(rec, record, opts)));
            };
            return wrapped;
        }
        // The extender: ctx.registerWindowKind wrapped IN PLACE, the same way
        // the factory decorates ctx.windows. Its own extender rather than a
        // line inside _ctxWindowsFactory, because the registry's per-extender
        // isolation is worth having between them: a mod that only opens windows
        // keeps its factory if this one ever throws, and a mod that only
        // registers kinds keeps its restore if the factory extender does.
        function _ctxWindowKindRestore(ctx, rec) {
            const reg = ctx.registerWindowKind;
            // Nothing to wrap on a ctx without #80's registration surface.
            if (typeof reg !== 'function') return;
            ctx.registerWindowKind = function (spec) {
                return reg.call(ctx, _modKindRestoreSpec(rec, spec));
            };
        }
        // Guarded like #197's registration: a page assembled without the
        // registry must not throw here.
        if (typeof _registerCtxExtender === 'function') {
            _registerCtxExtender(_ctxWindowsFactory);
            _registerCtxExtender(_ctxWindowKindRestore);
        }
        // ---- end ctx.windows.createAppWindow --------------------------------
