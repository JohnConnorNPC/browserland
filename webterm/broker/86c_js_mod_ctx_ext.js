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
        //     ctx.windows.closeAll();        // close every one of them (the
        //                                    // pass a disable stages for you)
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
        // THE STAGED TAKE-DOWN and closeAll() are the section further down. The
        // owned-window registry they read is `rec.appWindows` — deliberately on
        // the RECORD, not in this closure, so the loader's staged pass reaches
        // it through its arguments without seeing this fragment's locals (the
        // arguments-not-closure rule).
        //
        // THE CREATE HOOK is the last section of this range.
        // ctx.windows.onAppWindowCreate() fires from the marked line at the end
        // of _createModAppWindow, but its REPLAY set is NOT this registry: it is
        // "live windows whose appKind is one of this mod's REGISTERED kinds",
        // which includes core's unknown-kind restore records this factory never
        // built (the case sticky's hand-written catch-up pass covers by hand).
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

        // ---- the staged take-down + ctx.windows.closeAll() (#194) -----------
        // ONE close pass, TWO entry points. `ctx.windows.closeAll()` is a mod
        // closing its own windows by hand — the core-windows-Map iteration each
        // of the nine copies hand-rolls in its unload (mods/aistatus:459-466) —
        // and the LOADER stages the very same function at the head of
        // _runUnloads, so a disabled mod's factory windows are already gone
        // before the first entry of its teardown chain runs.
        //
        // WHY THE LOADER STAGES IT, AND THIS FRAGMENT DOES NOT REGISTER IT.
        // NEVER model that close pass as an onUnload entry. ctx is built before
        // init() runs, so a cleanup registered while an extender decorates the
        // ctx would be the OLDEST entry in the LIFO chain — and would therefore
        // run LAST: after the mod's own disposers, and after the
        // deleteWindowKind that _modRegisterWindowKind pushes onto that same
        // chain when the mod registers a kind. Closing a window whose kind is no
        // longer registered is exactly the junk-record ordering task-manager's
        // teardown comment documents by hand: closeWindow calls saveAppWindow,
        // saveAppWindow finds NO registry entry for the appKind, falls back to
        // the shared serializer, and /state is left holding a record for a kind
        // nothing can restore (an ephemeral kind's `if (kind && !kind.serialize)
        // return` never gets the chance to fire). Staged at the head of
        // _runUnloads the pass runs BEFORE that chain, i.e. while every kind the
        // mod registered is still registered, which is the whole property.
        //
        // closeAll() is the SAME function rather than a re-derivation of it:
        // two implementations would be two chances to get that ordering wrong,
        // and the hand-called one is the one nobody re-checks.
        //
        // Returns the number of windows it closed, so a caller (and the staged
        // pass's own tests) can tell "closed nothing" from "did not run".
        function _closeModAppWindows(rec) {
            const owned = rec && rec.appWindows;
            if (!owned || typeof owned.entries !== 'function') return 0;
            // A SNAPSHOT, newest first. Snapshot because closeWindow drains
            // win.cleanups, and the prune _ownModAppWindow pushed there deletes
            // from this very Map — plus a mod's own onDispose may close (or
            // open) further windows, so the live Map is mutating underneath.
            // Newest-first because that is the direction every other teardown in
            // the loader runs: a window opened later is the one more likely to
            // depend on an earlier one.
            const list = Array.from(owned.entries()).reverse();
            let closed = 0;
            for (let i = 0; i < list.length; i++) {
                const id = list[i][0];
                const h = list[i][1];
                // Already gone, or replaced at this id by a window built during
                // this very pass: an earlier entry's disposer got there first.
                if (owned.get(id) !== h) continue;
                if (!_modAppWindowLive(h)) { owned.delete(id); continue; }
                // h.close() rather than closeWindow(id) on purpose: the handle's
                // own _modAppWindowLive gate is what makes a stale entry inert
                // instead of lethal, and this pass must not be a second way to
                // close a window that is no longer ours.
                //
                // Isolated per window, like win.cleanups and the extender pass:
                // one window whose teardown throws must not strand this mod's
                // remaining windows on the desktop — and must not escape into
                // the unload chain the loader is standing in front of.
                try { h.close(); closed += 1; }
                catch (e) {
                    try {
                        console.error('[mods] closing app window "' + id
                            + '" failed for "' + (rec && rec.id) + '":', e);
                    } catch (_) { /* console is gone; keep closing */ }
                }
                // closeWindow drains the prune above, so this is belt-and-
                // braces for a window torn down some other way. Identity-
                // guarded, so a window REBUILT at this id mid-pass survives.
                //
                // ...and only if the window is actually GONE. If h.close()
                // threw before core removed the record, the window is still on
                // screen; forgetting it here would make it untracked as well as
                // undead — outliving both this pass and its kind's
                // deregistration, with nothing left that could close it. Left
                // owned, it is at least still ours, and a later closeAll() (or
                // the next disable) can try again.
                if (owned.get(id) === h && !_modAppWindowLive(h)) {
                    owned.delete(id);
                }
            }
            return closed;
        }
        // ---- end staged take-down + closeAll --------------------------------

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
            // The kind must be one THIS mod registered. Two separate defects
            // close here, both found by the #194 review:
            //   - CROSS-MOD ADOPTION. Without it, any mod could say
            //     {kind: 'clipboard', singleton: true}, have the dedupe adopt
            //     clipboard's live window into its OWN rec.appWindows, and
            //     then close it at will -- through closeAll(), or simply by
            //     being disabled. `list()` would not be "this mod's windows"
            //     either. The shared scope means a determined mod can still
            //     reach core directly (#71); what it may not do is get the
            //     factory to do it, wearing another mod's ownership.
            //   - THE JUNK RECORD. A kind nobody registered has no registry
            //     entry, so the staged take-down's saveAppWindow falls through
            //     to the shared serializer and persists a record for a kind
            //     nothing can ever restore -- the exact bug A30's ordering
            //     exists to prevent, reached through the front door.
            // Registration order is the mod's own: registerWindowKind runs in
            // init(), creates come later (a launch, a restore, a click).
            if (!rec || !rec.appWindowKinds || !rec.appWindowKinds.has(kind)) {
                throw new Error(
                    'createAppWindow: this mod has not registered the kind '
                    + JSON.stringify(kind)
                    + ' -- call ctx.registerWindowKind first');
            }
            // A mod being torn down may not open anything. `_closeModAppWindows`
            // walks ONE snapshot, so a window opened after it (from a disposer,
            // or from a later onUnload) would outlive the mod AND its kind
            // deregistration -- alive on screen, owned by nobody, and about to
            // persist the junk record. Refused loudly rather than silently
            // dropped: a teardown that still opens windows is a bug in the mod.
            if (rec.unloading) {
                throw new Error(
                    'createAppWindow: refused during teardown (mod '
                    + JSON.stringify(rec.id || '?') + ')');
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
            // OWN IT FIRST. Everything below can throw -- `spec.onClose` may be
            // a getter, wireAppChrome and the chip touch the DOM -- and a throw
            // between the map insert and ownership used to leave a window that
            // is visible, in the windows Map, and in NOBODY's rec.appWindows:
            // the staged take-down would walk straight past it, and its kind
            // would deregister out from under it. Ownership is bookkeeping and
            // cannot itself fail, so it is the one thing that belongs before
            // the fallible work.
            const h = _ownModAppWindow(rec, win, bodyEl, toolbarEl);
            // Raise / minimize / close / drag / 8-way resize / WM context menu.
            // spec.onClose replaces the × action (the editor's dirty-buffer
            // prompt is requestCloseAppWindow); undefined keeps closeWindow.
            wireAppChrome(win, chrome,
                (typeof spec.onClose === 'function') ? spec.onClose : undefined);
            _modAppWindowChip(win, sid, title);

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
            // #194 / A31: onAppWindowCreate fires HERE, once, for a window that
            // is fully built, chipped, placed and (unless restoring) focused —
            // so a subscriber decorating it is handed the finished article, not
            // a half-assembled one. The fire is guarded end to end (see
            // _fireModAppWindowCreate): a subscriber that throws costs neither
            // this create nor its sibling subscribers.
            //
            // The early returns ABOVE deliberately do not fire. They ADOPT a
            // window that was already live, and a live window of a registered
            // kind is what the subscribe-time REPLAY is for — this point is
            // "this factory built one", not "this factory returned one".
            _fireModAppWindowCreate(win);
            return h;
        }
        // The extender. Decorates the EXISTING v1 `windows` family in place:
        // assigning a fresh object would delete #116's onTerminalCreate.
        function _ctxWindowsFactory(ctx, rec) {
            // The kind-ownership gate reads `rec.appWindowKinds`, so THIS
            // extender installs the tracker that fills it. It used to live in
            // the onAppWindowCreate extender, which made the factory silently
            // depend on a sibling: if that one threw, the registry's own
            // per-extender isolation would leave `createAppWindow` present but
            // refusing every call -- and `needs: ['windows.createAppWindow']`
            // would still resolve MET, so a mod would come up "active" with
            // buttons that throw. An extender must not depend on a sibling for
            // its own preconditions.
            _trackModWindowKinds(ctx, rec);
            const fam = (ctx.windows && typeof ctx.windows === 'object')
                ? ctx.windows : (ctx.windows = {});
            fam.createAppWindow = function (spec) {
                return _createModAppWindow(rec, spec);
            };
            fam.list = function () { return _listModAppWindows(rec); };
            // The SAME pass the loader stages at the head of _runUnloads (see
            // above): a mod that wants its windows gone without being disabled
            // gets the ordering-correct close for free.
            fam.closeAll = function () { return _closeModAppWindows(rec); };
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
            // defineProperty, not assignment: `spec` may be FROZEN (a mod is
            // entitled to freeze what it hands us), and a plain `out.id = …`
            // against a non-writable INHERITED property throws in strict mode
            // and silently no-ops otherwise -- while an inherited SETTER would
            // run and mutate the mod's own object. defineProperty always
            // creates an own property on the child and never calls up the
            // chain, which is the whole reason the child exists.
            const own = function (key, value) {
                Object.defineProperty(out, key, {
                    value: value, writable: true,
                    enumerable: true, configurable: true,
                });
            };
            if (!(out.id != null && String(out.id)) && rid) own('id', rid);
            if (out.restoring === undefined) own('restoring', restoring);
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
            // defineProperty for the same reason `_modRestoreSpec` uses it: a
            // frozen spec makes `wrapped.restore = …` fail against the
            // non-writable inherited property, which would leave core holding
            // the ORIGINAL two-argument hook -- the api silently absent, and a
            // restore that builds nothing.
            Object.defineProperty(wrapped, 'restore', {
                value: function (record, opts) {
                    return _modRestoreResult(rec, restore.call(
                        this, record, opts, _modRestoreApi(rec, record, opts)));
                },
                writable: true, enumerable: true, configurable: true,
            });
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

        // ---- ctx.windows.onAppWindowCreate: fire + REPLAY (#194) ------------
        // A mod that DECORATES app windows — sticky's taskbar-chip recipe is
        // the motivating case — races the windows it wants to decorate.
        // Whether any of them exist when its init() runs is a RACE, not an
        // ordering (#167): restoreAppWindows waits on the control-WS lease and
        // the /state adopt, loadMods on GET /info, and nothing sequences the
        // two. So sticky hand-writes a catch-up pass over the core windows Map
        // ("Notes already open when this init runs get their chips NOW",
        // mods/sticky/sticky.js) and the two halves have to agree by hand.
        // This makes it first class:
        //
        //     const off = ctx.windows.onAppWindowCreate(function (info) {
        //         addChip(info.id);           // info: {win, id, kind, dom,
        //         info.onDispose(cleanup);    //  body, titleBar, replayed,
        //     });                             //  addTitleBarItem, onDispose}
        //
        // THE SET IT FIRES FOR is windows whose `appKind` is one of THIS mod's
        // REGISTERED kinds — deliberately NOT "the windows this mod's factory
        // built" (rec.appWindows) and never all app windows. A record core
        // rebuilt through its unknown-kind fallback before the mod loaded is a
        // window of that kind however it was built, and it counts: that is
        // exactly the record sticky's pass filters `appKind === 'sticky-note'`
        // for. Another mod's kinds never do — and appKind ownership is
        // exclusive, because registerWindowKind refuses a duplicate. Which is
        // why this extender TAPS ctx.registerWindowKind: "the kinds this mod
        // registered" is otherwise nobody's bookkeeping.
        //
        // EXACTLY ONCE PER SUBSCRIPTION — replay XOR live emit. Each
        // subscription carries its own `seen` set of window RECORDS, marked
        // before the handler is called, so:
        //   - a window replayed at subscribe time never also arrives as a live
        //     create (the case that bites: subscribing from inside body(), when
        //     the window being built is already in the windows Map and the
        //     create's own fire is still to come), and
        //   - a live create is never re-delivered by a later replay.
        // PER SUBSCRIPTION, not globally: two subscribers each get their own
        // exactly-once, and a mod that unsubscribes and re-subscribes gets the
        // whole set replayed again. So a mod needs no WeakSet of its own to
        // avoid double-decorating — the trap #194 lists as its own trap 5.
        // Records, not ids, because an id is REUSED: the window that replaces
        // 'app:clip' is a new window and is delivered as one.

        // Every live subscription, ACROSS mods, in subscribe order. The fire
        // point is one mod's create and the subscriber may be another — a kind
        // belongs to whoever registered it, which need not be whoever opened
        // the window. A fragment-level const for the reason the extender
        // registry's is: nothing runs before this fragment, and the first read
        // is at loadMods() time.
        const _appWindowCreateSubs = [];
        // One place the hook reports a failure, and it must not become the
        // second one: `rec.id`/`win.id` are attacker-adjacent reads (a proxy, a
        // throwing getter), and a throw HERE would escape into the create path
        // this stands in front of.
        function _logAppWindowHookError(what, sub, win, e) {
            try {
                console.error('[mods] onAppWindowCreate ' + what
                    + ' failed for "' + (sub && sub.rec && sub.rec.id)
                    + '" on window "' + (win && win.id) + '":', e);
            } catch (_) {
                try { console.error('[mods] onAppWindowCreate failed'); }
                catch (__) { /* console is gone; keep going */ }
            }
        }
        // The bag one subscriber is handed, built per delivery and FROZEN. Same
        // spelling as #116's onTerminalCreate bag (win / addTitleBarItem /
        // onDispose), plus the three facts an app window has and a terminal
        // does not: its id, its appKind, and whether this delivery is a REPLAY.
        // titleBar is derived from win.dom exactly as onTerminalCreate derives
        // it, so a replayed window — built long before this subscriber existed,
        // possibly by core — resolves the same nodes a create-time delivery
        // does. A record with no chrome yields null instead of being skipped:
        // the set is defined by KIND, and dropping a window because core built
        // it thinly would re-open the hole this hook exists to close.
        function _modAppWindowInfo(win, replayed) {
            const dom = win.dom || null;
            const titleBar = (dom && typeof dom.querySelector === 'function')
                ? dom.querySelector('.title-bar') : null;
            return Object.freeze({
                win: win,
                id: win.id,
                kind: win.appKind,
                dom: dom,
                body: win.body || null,
                titleBar: titleBar,
                replayed: !!replayed,
                // BEFORE the min button — left of min/close, the established
                // idiom onTerminalCreate and the factory handle both spell.
                addTitleBarItem: function (node) {
                    if (!node || !titleBar) return null;
                    titleBar.insertBefore(
                        node, titleBar.querySelector('.btn-min'));
                    return node;
                },
                // win.cleanups is drained by closeWindow (73) and by the
                // active-view rebuild (84) — no new teardown plumbing, same as
                // the handle's onDispose. Refused on a dead window, where the
                // array has already been drained and emptied.
                onDispose: function (fn) {
                    if (typeof fn !== 'function') return false;
                    if (win.disposed || !Array.isArray(win.cleanups)) return false;
                    win.cleanups.push(fn);
                    return true;
                },
            });
        }
        // Deliver ONE window to ONE subscription, if it belongs to that
        // subscription and that subscription has not had it. Returns true when
        // it was delivered — a handler that throws does not un-deliver it, or a
        // retry loop would hand a broken subscriber the same window forever.
        function _emitModAppWindowCreate(sub, win, replayed) {
            if (!sub || sub.dead || !win || typeof win !== 'object') return false;
            if (win.disposed || win.type !== 'app') return false;
            const kinds = sub.rec && sub.rec.appWindowKinds;
            if (!kinds || !kinds.has(win.appKind)) return false;
            // THE XOR, and it is these two lines. Marked BEFORE the call, so a
            // handler that re-enters — opens a window, subscribes, registers a
            // kind — cannot be fed this same window from inside its own
            // delivery.
            if (sub.seen.has(win)) return false;
            sub.seen.add(win);
            try {
                sub.fn(_modAppWindowInfo(win, replayed));
            } catch (e) {
                _logAppWindowHookError('handler', sub, win, e);
            }
            return true;
        }
        // Replay the live windows to ONE subscription: every app window in the
        // core Map, oldest first, filtered to `kind` when one is named (the
        // late-registration catch-up below) and to the whole subscription scope
        // when it is not (subscribe time). A SNAPSHOT of the Map, because a
        // handler may open or close windows while this runs.
        function _replayModAppWindowCreate(sub, kind) {
            if (!sub || sub.dead) return 0;
            const list = Array.from(windows.values());
            let n = 0;
            for (let i = 0; i < list.length; i++) {
                if (sub.dead) break;          // a handler dropped this one
                const w = list[i];
                if (kind && (!w || w.appKind !== kind)) continue;
                try {
                    if (_emitModAppWindowCreate(sub, w, true)) n += 1;
                } catch (e) {
                    _logAppWindowHookError('replay', sub, w, e);
                }
            }
            return n;
        }
        // The create-time emit, called from the marked seam at the end of
        // _createModAppWindow. Every live subscription, in subscribe order,
        // over a SNAPSHOT: a handler may subscribe or unsubscribe from inside
        // its own delivery, and one that subscribes here gets this window
        // through its OWN replay (it is already in the windows Map) rather than
        // through a loop it joined halfway. Isolated per subscriber, like
        // win.cleanups and the extender pass — one broken decorator must not
        // strand the window being created, nor its sibling subscribers.
        function _fireModAppWindowCreate(win) {
            const list = _appWindowCreateSubs.slice();
            for (let i = 0; i < list.length; i++) {
                try {
                    _emitModAppWindowCreate(list[i], win, false);
                } catch (e) {
                    _logAppWindowHookError('fire', list[i], win, e);
                }
            }
        }
        // The kinds THIS mod has registered — the hook's whole scope. Tapped
        // off ctx.registerWindowKind (the only way a mod can register one) and
        // kept on the RECORD beside rec.appWindows, so a fire reaches it
        // through its arguments and never through this fragment's locals.
        // Nothing removes a kind again: a mod cannot deregister one through ctx
        // (the loader's own deleteWindowKind rides the teardown chain), and
        // every subscription of a mod that IS torn down is dropped by that same
        // chain — so a stale set has nobody left to answer.
        function _trackModWindowKind(rec, entry) {
            const kind = (entry && typeof entry === 'object'
                && typeof entry.appKind === 'string' && entry.appKind) || '';
            if (!kind || !rec || typeof rec !== 'object') return null;
            if (!rec.appWindowKinds) rec.appWindowKinds = new Set();
            if (rec.appWindowKinds.has(kind)) return kind;
            rec.appWindowKinds.add(kind);
            // CATCH-UP, so subscribe-then-register is as correct as register-
            // then-subscribe. A mod whose init reaches the hook first (sticky's
            // order is settings, then the chip pass, then the kind) would
            // otherwise have replayed against an empty scope and missed every
            // window that already existed — the very race this hook replaces.
            // The same per-subscription `seen` set covers it, so a window
            // already delivered is not delivered twice.
            const list = _appWindowCreateSubs.slice();
            for (let i = 0; i < list.length; i++) {
                if (list[i].rec === rec) _replayModAppWindowCreate(list[i], kind);
            }
            return kind;
        }
        // Subscribe. Returns an unsubscribe fn, which is ALSO pushed onto the
        // mod's LIFO teardown chain — the same discipline #116's
        // onTerminalCreate follows, so a disabled mod stops decorating windows
        // even if it never called the fn it was given.
        function _modOnAppWindowCreate(rec, fn) {
            if (typeof fn !== 'function') return function () {};
            // A mod being torn down may not subscribe. `_runUnloads` has
            // already spliced `rec.unloads`, so a disposer pushed now is never
            // run: the dead mod's handler would stay in the global list for the
            // life of the page, called for every window of a kind it no longer
            // owns. Returns the same inert unsubscribe a non-function does.
            if (rec && rec.unloading) return function () { return false; };
            const sub = { rec: rec, fn: fn, seen: new WeakSet(), dead: false };
            const off = function () {
                if (sub.dead) return false;
                sub.dead = true;
                const i = _appWindowCreateSubs.indexOf(sub);
                if (i !== -1) _appWindowCreateSubs.splice(i, 1);
                return true;
            };
            _appWindowCreateSubs.push(sub);
            // Registered BEFORE the replay runs: a handler that throws, or that
            // tears its own mod down mid-replay, must not be able to leave a
            // subscription behind with nothing left to drop it.
            if (rec && Array.isArray(rec.unloads)) rec.unloads.push(off);
            _replayModAppWindowCreate(sub, null);
            return off;
        }
        // The extender: two halves of one surface. The hook itself DECORATES
        // the v1 `windows` family in place (a fresh object would delete #116's
        // onTerminalCreate and the factory above), and the tap on
        // ctx.registerWindowKind is what tells the hook which kinds are this
        // mod's. WRAPPED, never replaced: the member this closes over is
        // whatever the extenders before it left — A29's restore wrapper, over
        // the loader's own _modRegisterWindowKind — and that is what registers.
        //
        // Its own extender rather than a line inside _ctxWindowsFactory or
        // _ctxWindowKindRestore, for the registry's per-extender isolation: a
        // mod that only opens windows keeps its factory if this one ever
        // throws, and a mod that only registers kinds keeps its restore seam.
        // Wrap ctx.registerWindowKind so every kind this mod registers lands in
        // `rec.appWindowKinds` -- the set the ownership gate and the create
        // hook's scope both read. Idempotent by a flag on the record: two
        // extenders call it, and wrapping twice would double-track (harmless)
        // and double-wrap (not).
        function _trackModWindowKinds(ctx, rec) {
            if (!rec || rec._kindTrackerInstalled) return;
            const reg = ctx.registerWindowKind;
            if (typeof reg !== 'function') return;
            rec._kindTrackerInstalled = true;
            if (!rec.appWindowKinds) rec.appWindowKinds = new Set();
            ctx.registerWindowKind = function (spec) {
                const entry = reg.call(ctx, spec);
                _trackModWindowKind(rec, entry);
                return entry;
            };
        }
        function _ctxWindowCreateHook(ctx, rec) {
            _trackModWindowKinds(ctx, rec);
            const fam = (ctx.windows && typeof ctx.windows === 'object')
                ? ctx.windows : (ctx.windows = {});
            fam.onAppWindowCreate = function (fn) {
                return _modOnAppWindowCreate(rec, fn);
            };
        }
        // ---- end ctx.windows.onAppWindowCreate ------------------------------

        // Guarded like #197's registration: a page assembled without the
        // registry must not throw here.
        if (typeof _registerCtxExtender === 'function') {
            _registerCtxExtender(_ctxWindowsFactory);
            _registerCtxExtender(_ctxWindowKindRestore);
            _registerCtxExtender(_ctxWindowCreateHook);
        }
        // ---- end ctx.windows.createAppWindow --------------------------------
