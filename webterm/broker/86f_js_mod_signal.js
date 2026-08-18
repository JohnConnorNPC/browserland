        // ---- ctx.signal: the per-activation AbortSignal (#198) --------------
        //
        //     ctx.signal                    // aborted when THIS activation ends
        //     await ctx.file.read(path, { signal: ctx.signal });
        //     await hostFetch(host, '/state', { signal: ctx.signal });
        //     const t = setTimeout(fn, ms);
        //     ctx.signal.addEventListener('abort', function () {
        //         clearTimeout(t);
        //     });
        //
        // WHAT IT REPLACES. Nothing aborted a mod's in-flight async work at
        // teardown, so mods hand-rolled dead flags: update sets `restartOpDead`
        // (mods/update/update.js) and `applyOpDead` from its FIRST registered
        // onUnload "so a wait loop already in flight when the mod is switched
        // off stops touching the DOM … rather than running to its own timeout in
        // the background of a mod that is no longer loaded". That is a correct
        // pattern re-typed per mod, per loop, and each copy is one forgotten
        // early-return away from the bug it exists to prevent. The platform now
        // does it unconditionally, first, always — one signal per activation.
        // (The migrations themselves are #204's, not this fragment's: nothing
        // here changes a mod, and every existing call site is untouched.)
        //
        // ITS OWN FRAGMENT, for the reason 86a/86b/86c+86d/86e each got one:
        // 86c_js_mod_ctx_ext.js is at the #68 2500-line per-fragment cap
        // (_MAX_LINES, ui.py) and the rule for that cap has always been "split,
        // never trim" — shrinking a neighbouring block to make room is exactly
        // what it forbids. 86e was the other candidate and was refused: it is
        // #196's save pipeline, a DIFFERENT concern, and specifically the one
        // this surface must not break (see THE COUNTER-EXAMPLE below) — the two
        // do not belong in one file with nothing but proximity keeping them
        // apart. Ordered LAST among the 86* companions in ui._ORDERED, still
        // before the mod-script splice, because a mod's init reads the finished
        // ctx. One <script>, one scope, as with every 86* companion: the
        // loader's _runUnloads calls _abortModSignal below (typeof-guarded, as
        // it guards every call into a companion), and this file calls 86c's
        // _registerCtxExtender / _registerModCapability the same way.
        //
        // TWO DECISIONS THE ISSUE MADE, implemented here rather than rediscussed:
        //
        //  1. THE SIGNAL ABORTS BEFORE THE onUnload CHAIN RUNS. Abort listeners
        //     fire synchronously, so every in-flight loop observes the abort
        //     before the first disposer dismantles the DOM it might touch. The
        //     dispatch therefore sits at the head of _runUnloads (86) — see
        //     WHERE THE DISPATCH SITS below, which states its position against
        //     all three take-down stages, because "before the unload chain" is
        //     no longer a single point.
        //  2. EXPLICIT PASS, NOT AMBIENT WIRING. Helpers already take {signal}
        //     (_modFileApi and _modSessionApi forward opts.signal; hostFetch
        //     COMPOSES it with its own deadline rather than replacing it) and
        //     the loader does NOT inject ctx.signal into them. An ambient signal
        //     would abort work a mod deliberately wants to finish at teardown,
        //     and would silently change every existing call site. Explicit is
        //     also the shape feature detection already speaks.
        //
        // WHERE THE DISPATCH SITS, stated in full because the take-down path has
        // three stages and this is ahead of all of them:
        //
        //     rec.unloading = true             #169's mid-teardown flag
        //     _abortModSignal(rec)             <- HERE (#198)
        //     _closeModAppWindows(rec)         #194's staged factory-window close
        //     rec.unloads.splice(0).reverse()  the LIFO chain, which is where
        //                                      #195's _fireModTeardowns rides
        //                                      (it is an ENTRY on that list)
        //
        //   - AHEAD OF THE STAGED WINDOW CLOSE (#194), and this is the clause
        //     that decides the position. That pass closes the mod's factory
        //     windows before any disposer runs, i.e. it is the first thing that
        //     DISMANTLES anything. A loop that is about to write into one of
        //     those windows must have been told to stop before the window it
        //     targets is taken away; abort it afterwards and the loop's
        //     completion handler lands in the gap between "window closed" and
        //     "dead flag set" — precisely the window the hand-rolled flags were
        //     invented to close.
        //   - AHEAD OF THE LIFO CHAIN, so ahead of #195's per-window
        //     onModTeardown disposers too: _fireModTeardowns is not a call in
        //     _runUnloads but an entry pushed onto rec.unloads at the mod's
        //     first accepted registration, so it necessarily runs inside the
        //     drain. One position covers both.
        //   - AFTER rec.unloading = true, deliberately. An abort listener is mod
        //     code: it may write a setting (#169's re-entrancy hazard, which is
        //     what that flag guards) and it may touch a #196 saveChain, whose
        //     dead-activation gate reads exactly this flag. Both must already
        //     see a record that is dead. Setting a boolean dismantles nothing,
        //     so it costs the ordering above nothing.
        //   - INSIDE ITS OWN try/catch, because abort listeners run OUTSIDE
        //     _runUnloads's per-callback isolation (that isolation wraps the
        //     entries on rec.unloads, and a listener is not one of them). See
        //     _abortModSignal for what that guard can and cannot catch.
        //
        // PER ACTIVATION, and the controller lives on the RECORD. initMod builds
        // a fresh rec for every activation, so a disabled-then-re-enabled mod
        // gets a NEW controller and an un-aborted signal for free — the
        // freshness is structural, not maintained. Keying the controller by mod
        // id instead (a fragment-level map) is the obvious wrong implementation
        // and it fails SILENTLY: the re-enabled mod would be handed the dead
        // activation's aborted signal, every request it carries would abort
        // instantly, and the mod would look broken with nothing in the console.
        // The old activation's signal stays aborted forever, which is the whole
        // point — its loops must not come back to life when the mod does.
        //
        // WHAT AN ABORTED CALL RESOLVES TO. The never-rejects contract holds
        // unchanged: the abort reason is a DOMException named 'AbortError', so
        // each family maps it to ITS OWN cancel shape rather than a new one.
        // ctx.file -> {ok:false, aborted:true, error:'cancelled'} (fileApiError,
        // 68 — note it is 'cancelled', NOT 'aborted': the shipped shape wins
        // over the issue's prose); ctx.session -> {status:0, json:{ok:false,
        // error:String(e)}} — the reason STRINGIFIED, not the DOMException
        // itself (`_modSessionApi`'s catch, 86 — so a caller reads
        // 'AbortError: mod "<id>" was torn down', which is why the reason
        // carries a message at all), status-carrying as ever. A TimeoutError
        // stays distinguishable from a cancel, which is why the reason is named
        // rather than left to abort()'s default.
        //
        // THE COUNTER-EXAMPLE — TEARDOWN-SURVIVING WORK MUST NOT CARRY THE
        // SIGNAL. #196's saveChain ends with a deliberate final flush(), called
        // from scratchpad's WINDOW cleanup precisely so a last edit reaches the
        // server. That flush must NOT be handed ctx.signal, and it is not: the
        // chain's own sends go through _modStoreUpdate, which nothing here
        // touches, and decision 2 above is what keeps it that way. The rule
        // generalizes — if a piece of work is supposed to OUTLIVE the teardown,
        // it does not take the signal, and the two shapes for that work are the
        // ones #196 already documents (a window-close cleanup, or a Ctrl+S
        // handler), never an onUnload. A migration that reflexively threads
        // ctx.signal through every call will abort its own last save.
        //
        // A NEW CAPABILITY ENTRY IS OWED, and this is the argument, not an
        // assumption: #197's map is per FAMILY — a bare top-level ctx member
        // name, never a dotted path — and `signal` is a new top-level member of
        // the ctx, exactly like #195's `hosts`. (A40's saveChain owed none
        // because it decorates `serverStore`, a member of a family that already
        // has an entry.) Without the registration ctx.capabilities would lie by
        // omission about surface the build demonstrably has. It is a VALUE, not
        // a function, which changes only how a mod asks: `if (ctx.signal)` or
        // `needs: ['signal']` — _modCtxHas resolves it fine (it requires an own
        // property that is neither null nor undefined; only INTERMEDIATE path
        // segments have to be objects), but `typeof ctx.signal === 'function'`
        // is the detection that would be wrong. ctxVersion stays 1: additive,
        // feature-detected surface, and a bump would refuse every mod that pins
        // v1.
        //
        // NO SURFACE ON A BUILD THAT CANNOT CARRY IT (the CP8 no-half-family
        // rule): an engine without a constructible AbortController gets no
        // ctx.signal at all rather than a placeholder that never aborts, so
        // ctx.capabilities reports it absent — which is true — and
        // `needs: ['signal']` blocks the mod with a reason instead of letting it
        // run against a signal that silently does nothing.

        // The abort reason. Named so a console shows WHICH mod's teardown
        // cancelled the request, and 'AbortError' so hostFetch's composition
        // (63) forwards a reason every helper's failure mapping already
        // understands — a cancel, distinct from its TimeoutError deadline.
        // undefined on an engine with no constructible DOMException: abort()
        // then synthesizes the spec's own AbortError, which is the same name and
        // a graceful degradation rather than a shim. Everything is inside the
        // try because `rec.id` is an attacker-adjacent read (a proxy, a throwing
        // getter) and building the reason must not become the failure.
        function _modSignalReason(rec) {
            try {
                return new DOMException(
                    'mod "' + (rec && rec.id) + '" was torn down',
                    'AbortError');
            } catch (_) { return undefined; }
        }
        // The extender: one controller per activation, hung on the record so the
        // take-down path can find it with nothing but the `rec` it already has
        // (the arguments-not-closure rule — this fragment cannot see makeCtx's
        // per-mod locals, and a fragment-level map keyed by id is the failure
        // mode described above). Idempotent by construction: a rec that already
        // carries a controller keeps it, so a ctx decorated twice can never
        // strand a controller nothing would ever abort.
        function _ctxSignal(ctx, rec) {
            if (!rec || typeof rec !== 'object') return;
            if (typeof AbortController !== 'function') return;
            let ctrl = rec.signalCtrl;
            if (!ctrl) {
                try { ctrl = new AbortController(); }
                catch (_) { return; }
                rec.signalCtrl = ctrl;
            }
            ctx.signal = ctrl.signal;
        }
        // The dispatch, called from the head of _runUnloads (86). Returns true
        // when this call is the one that aborted, so the caller (and this
        // fragment's tests) can tell "already aborted" and "no signal on this
        // record" from "did not run". Idempotent: a second _runUnloads on the
        // same record dispatches nothing.
        //
        // WHAT THE try/catch DOES AND DOES NOT CATCH, said plainly so nobody
        // reads it as a stronger promise than it is. Under DOM event dispatch a
        // throwing listener does NOT propagate to whoever called abort() — the
        // exception is reported to the global handler (window.onerror in a
        // browser) and the remaining listeners still run — so the teardown chain
        // standing behind this is safe from a throwing listener whether or not
        // this guard exists. The guard covers what is left: a non-conforming or
        // polyfilled controller that DOES rethrow, a hostile `rec` whose
        // `signalCtrl` getter throws, and the `signal.aborted` read itself.
        // Losing the abort must never cost the mod its teardown.
        function _abortModSignal(rec) {
            try {
                const ctrl = rec && rec.signalCtrl;
                if (!ctrl || typeof ctrl.abort !== 'function') return false;
                if (ctrl.signal && ctrl.signal.aborted) return false;
                ctrl.abort(_modSignalReason(rec));
                return true;
            } catch (e) {
                try {
                    console.error('[mods] ctx.signal abort failed for "'
                        + (rec && rec.id) + '":', e);
                } catch (_) { /* console is gone; keep tearing down */ }
                return false;
            }
        }
        // Guarded like every other registration in an extension fragment: a page
        // assembled without #194's registry or #197's capability map must not
        // throw at load.
        if (typeof _registerCtxExtender === 'function') {
            _registerCtxExtender(_ctxSignal);
        }
        if (typeof _registerModCapability === 'function') {
            _registerModCapability('signal', 1);
        }
        // ---- end ctx.signal --------------------------------------------------
