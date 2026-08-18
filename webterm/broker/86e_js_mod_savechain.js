        // ---- ctx.serverStore.saveChain (#196) --------------------------------
        // scratchpad's save pipeline, canned as a shared surface. Its OWN
        // fragment because 86c_js_mod_ctx_ext.js is at the #68 2500-line
        // per-fragment cap and the rule for that cap has always been "split,
        // never trim" (86a/#168, 86b/#163, 86c+86d/#194 are the precedent —
        // shrinking a neighbouring block to make room is exactly what the rule
        // forbids). Assembly puts this fragment in the SAME <script>, ordered
        // after 86d and before the mod-script splice, so everything here shares
        // one scope with the loader and with 86c: _modStoreUpdate (A40's CAS,
        // 86c) and _registerCtxExtender (the #194 registry, 86c) are both
        // hoisted function declarations this file can call, and extenders are
        // applied from makeCtx at loadMods() time — long after every fragment
        // has evaluated — so registering from a LATER fragment is fine.
        //
        //     const chain = ctx.serverStore.saveChain({
        //         host?, debounceMs?, purgeRevisions?, noHistory? });
        //     chain.save(fn);      // -> true (queued) | false (dropped)
        //     chain.flush();       // -> Promise<result>, never rejects
        //     chain.onState(fn);   // 'idle'|'saving'|'conflict' -> RO banner
        //                          // -> unsubscribe fn
        //
        // WHAT IT IS. Three mods hand-rolled a compare-and-swap; one of them
        // (scratchpad, mods/scratchpad/scratchpad.js) wrapped it in a debounced,
        // single-in-flight save chain with a read-only banner, and that pipeline
        // is what this cans. It adds NOTHING to the write itself: every send
        // goes through A40's _modStoreUpdate, so the 409 rebase, the bounded
        // retry budget, the fail-closed host lookup and the verbatim
        // purgeRevisions / noHistory passthrough are inherited rather than
        // re-implemented. What the chain adds is WHEN to write, HOW MANY writes
        // to make, and what to tell the user while it is happening.
        //
        // THE REDUCER SHAPE, NOT A SNAPSHOT-GETTER. `save(fn)` takes the SAME
        // pure (value, rev) => next reducer update() takes, and a snapshot-getter
        // shape — save(gatherValue), the shape the reference implementation
        // actually had — was considered and REFUSED (#196 trap 3): re-sending a
        // replacement snapshot after a 409 is #158's clobber with retries. The
        // winner's value has to flow THROUGH the reducer, which means the late
        // read (the live editor buffer at send time) belongs INSIDE `fn`, where
        // it re-runs over the winner. `fn` is therefore pure over (value, rev)
        // by the same contract update() states: it runs once per attempt, so a
        // side effect in it happens once per attempt, and that includes calling
        // this chain's own save() (a rebase would then queue the same work
        // twice).
        //
        // COALESCED BY COMPOSITION, NOT BY REPLACEMENT. Rapid saves queue and
        // the queue is spliced into ONE batch, composed in order —
        // fn2(fn1(value, rev), rev) — and handed to update() as a single
        // reducer. Replacement ("the newest reducer wins") is what the reference
        // implementation effectively did, because its reducer produced the whole
        // document every time; a SHARED surface cannot assume that, and dropping
        // an earlier caller's delta on the floor is a silent data loss no test
        // would catch. Composition degenerates to last-wins for a full-snapshot
        // reducer, so scratchpad's semantics survive the migration unchanged.
        // Two consequences to know: every queued reducer sees the SAME `rev` (it
        // is the batch's base revision, not a per-reducer one), and one reducer
        // that throws or forgets to return refuses the WHOLE batch — the same
        // answer update() gives one reducer, applied to the group it was
        // coalesced into.
        //
        // SINGLE IN FLIGHT, PER CHAIN. At most one write from a given chain is
        // ever outstanding; saves that arrive during it are queued and leave as
        // exactly one more write. Per CHAIN is the honest scope: two chains in
        // one mod address the SAME per-mod record (ctx.serverStore is per mod,
        // and opts.host only picks which broker's copy), so they serialize only
        // through the server's rev CAS — each one's reducer re-applies over the
        // other's winner, which is correct but spends conflict budget. One mod,
        // one chain per record is the shape to write; a shared per-record lane
        // is deliberately NOT built here.
        //
        // DEAD-ACTIVATION WRITES ARE DROPPED, NOT RACED (#196 trap 7). flush()
        // returns a promise but disable is SYNCHRONOUS and will not await it, so
        // "flush on the way down" is precisely the write that lands after
        // re-enable and clobbers the new activation. The chain therefore carries
        // an activation generation and drops instead. THE GENERATION IS `rec`:
        // initMod builds a fresh per-activation record and _runUnloads sets
        // rec.unloading = true SYNCHRONOUSLY, at the top of the drain, before a
        // single disposer runs — so the instant a disable begins, every chain
        // from that activation reads dead, including from inside an onUnload.
        // Re-enable builds a NEW rec, so the old chain's is dead for good.
        // Checked in four places, deliberately: at save() (nothing is queued),
        // at flush() (nothing is sent), in pump() after the state listeners run
        // (a listener may itself tear the mod down), and inside the GATE below —
        // which is what closes the get/set window, since a disable landing
        // between update()'s read and its write would otherwise still PUT. What
        // no client-side check can recall is a request already handed to the
        // network; that residual window needs a server-side fence or #198's
        // ctx.signal, and is named here rather than papered over.
        //
        // TEARDOWN DROPS. Deterministically, and it is the same decision as
        // above rather than a second one: a pending debounce at mod teardown is
        // discarded, the timer is cleared, the state listeners are released and
        // every outstanding flush() promise RESOLVES {ok:false,
        // error:'unloaded'} (a teardown that left a promise pending would hang
        // the caller awaiting it). A mod that wants a deliberate final flush
        // must call flush() while it is still ALIVE — from a window-close
        // cleanup (win.cleanups, which also runs on an ordinary close, with the
        // mod up) or from a Ctrl+S handler, NOT from an onUnload, where
        // rec.unloading is already set. Per #196 trap 6 such a flush must also
        // not carry ctx.signal once #198 lands.
        //
        // WHAT IT NEVER DOES: retry a failed batch by itself. update() already
        // rebases a 409 up to its budget; past that the batch is dropped and the
        // chain reports 'conflict', because the mirror of #158's silent clobber
        // is a silent infinite rebase against a hot writer — and every attempt
        // writes a revision into the 50-deep ring (#175). The caller decides:
        // scratchpad re-attempts on the next interaction while the banner is up.
        // And it never rejects — every outcome, including a throw anywhere
        // inside, RESOLVES to a result.
        //
        // NOT A NEW FAMILY, so NO capability entry is owed — the same reasoning
        // A40 wrote out for update(), and it holds here for the same reason:
        // `serverStore` is already in ctx v1's seed list and #197's map is per
        // FAMILY (a name, never a dotted path). This adds a MEMBER, exactly like
        // ctx.windows.createAppWindow, and a mod gates on it the same way —
        // `typeof ctx.serverStore.saveChain === 'function'`, or
        // `needs: ['serverStore.saveChain']`, which _modCtxHas resolves segment
        // by segment against the live ctx. ctxVersion stays 1.

        // The reference implementation's SAVE_DEBOUNCE, so the mod whose
        // semantics this cans keeps its feel across the migration. Trailing:
        // each save pushes the deadline out, which is what makes a burst one
        // write — and, inherited from that same reference, means a genuinely
        // continuous writer is only written out by the in-flight/flush paths.
        const _MOD_CHAIN_DEBOUNCE_MS = 800;
        // A pending batch is DROPPED at teardown, so an absurd debounce is data
        // loss wearing a save's clothes. Clamped for the same reason A40 clamps
        // `retries`: the bound is not the caller's to remove.
        const _MOD_CHAIN_MAX_DEBOUNCE_MS = 60000;
        function _modChainWait(n) {
            if (typeof n !== 'number' || !isFinite(n) || n < 0) {
                return _MOD_CHAIN_DEBOUNCE_MS;
            }
            return Math.min(Math.floor(n), _MOD_CHAIN_MAX_DEBOUNCE_MS);
        }
        // The one refusal a dead chain makes, in the store's own result shape so
        // update() reads it exactly as it reads a transport failure: a failed
        // READ aborts before the reducer is ever invoked, and a refused WRITE is
        // a non-conflict failure, so neither rebases and neither retries. Built
        // fresh per call — update() copies results, but a shared literal handed
        // out repeatedly is one careless mutation away from a puzzle.
        function _modChainDropped() {
            return { ok: false, status: 0, error: 'unloaded' };
        }
        function _modStoreSaveChain(store, opts, rec) {
            // Snapshotted ONCE, and that single object is handed to every
            // update() call this chain ever makes — A40's discipline, for A40's
            // reason: nothing rebuilds the options between writes, so
            // purgeRevisions (#65) and noHistory (#192, PRESENCE-keyed: absent
            // stays off the wire, an explicit boolean rides it verbatim) cannot
            // be dropped by a coalesce, a rebase or a retry. `host` and
            // `retries` ride through to the transport and to update() the same
            // way; `debounceMs` is read here and simply ignored downstream.
            const wopts = Object.assign({}, opts);
            const wait = _modChainWait(wopts.debounceMs);
            const queue = [];        // reducers queued, not yet sent
            const subs = [];         // onState listeners
            let waiters = [];        // flush() resolvers, with their barriers
            let timer = null;        // the armed debounce, if any
            let ready = false;       // the debounce elapsed (or a flush forced)
            let sending = false;     // a write is outstanding
            let conflicted = false;  // the last write ended in a refusable 409
            let dead = false;        // this chain's own disposer ran
            let state = 'idle';
            let lastRes = null;      // the last write's result
            let sent = 0;            // completed batches — the flush barrier

            // The activation generation. `dead` alone is not enough: a chain
            // built during init registers its disposer on rec.unloads, but
            // rec.unloading is set BEFORE that list is drained, so this reads
            // true from the very first instant of a teardown — and stays true
            // for the life of a record that will never be reactivated.
            function isDead() {
                return dead || !!(rec && rec.unloading);
            }
            // Derived, never assigned ad hoc: a write in flight is 'saving', an
            // unresolved refusable 409 is 'conflict' (scratchpad's read-only
            // banner), anything else is 'idle'. Deduped, so a repeat conflict
            // does not re-raise a banner that is already up. Listeners are
            // isolated (a throwing banner must not cost the chain its pump) and
            // their return value is never awaited.
            function refresh(res) {
                const next = sending ? 'saving'
                    : (conflicted ? 'conflict' : 'idle');
                if (next === state) return;
                state = next;
                const list = subs.slice();
                for (let i = 0; i < list.length; i++) {
                    try { list[i](next, res || null); }
                    catch (e) {
                        try {
                            console.error('[mods] saveChain onState threw:', e);
                        } catch (_) { /* console is gone */ }
                    }
                }
            }
            // Resolve every flush() whose barrier has been reached. `need` is
            // the batch count that has to COMPLETE before that flush's caller
            // has been told the truth, which is what keeps a flush from waiting
            // on writes queued after it — a continuous writer can starve a
            // debounce, it must not starve a teardown.
            function settle(res, all) {
                const keep = [];
                const fire = [];
                for (let i = 0; i < waiters.length; i++) {
                    const w = waiters[i];
                    if (all || sent >= w.need) fire.push(w); else keep.push(w);
                }
                waiters = keep;
                for (let i = 0; i < fire.length; i++) {
                    try { fire[i].resolve(res); } catch (_) { /* gone */ }
                }
            }
            function die() {
                dead = true;
                if (timer !== null) { clearTimeout(timer); timer = null; }
                queue.length = 0;
                ready = false;
                // Released so a write that is already on the wire cannot call
                // back into a mod that no longer exists when it lands.
                subs.length = 0;
                settle({ ok: false, error: 'unloaded' }, true);
            }
            // The store update() actually writes through. Both members refuse
            // once the activation is dead, which is what makes a disable landing
            // BETWEEN the read and the write a drop rather than a race.
            const gate = {
                get: function (o) {
                    if (isDead()) return Promise.resolve(_modChainDropped());
                    return store.get(o);
                },
                set: function (value, baseRev, o) {
                    if (isDead()) return Promise.resolve(_modChainDropped());
                    return store.set(value, baseRev, o);
                },
            };
            // One batch, as one reducer. Threads each queued reducer's output
            // into the next, and stops at the first `undefined` so update()
            // refuses the batch under its own bad_value name instead of sending
            // a hole. Async by construction: an async reducer is first-class
            // here exactly as it is in update().
            function batchReducer(batch) {
                return async function (value, rev) {
                    let next = value;
                    for (let i = 0; i < batch.length; i++) {
                        next = await batch[i](next, rev);
                        if (next === undefined) return undefined;
                    }
                    return next;
                };
            }
            function finish(res) {
                sent += 1;
                lastRes = res;
                // The two 409s that raise the banner: 'conflict' means update()
                // spent its rebase budget against a hotter writer, 'not_active'
                // means this browser does not hold the write lease at all
                // (never rebasable — A40 returns it straight back). Every other
                // failure leaves the chain idle; a later save retries it, which
                // is the reference implementation's posture for a transport or
                // 4xx/5xx failure verbatim.
                conflicted = !!(res && res.ok === false
                    && (res.error === 'conflict' || res.error === 'not_active'));
                sending = false;
                if (isDead()) {
                    settle({ ok: false, error: 'unloaded' }, true);
                    return;
                }
                settle(res, false);
                pump();          // may start the next batch (-> 'saving')
                refresh(res);    // ...and if it did not, land on idle/conflict
            }
            function pump() {
                if (isDead()) {
                    queue.length = 0;
                    ready = false;
                    settle({ ok: false, error: 'unloaded' }, true);
                    return;
                }
                if (sending) return;              // single in flight, per chain
                // An empty queue clears the force flag as well: a flush() that
                // found nothing to do must not leave the NEXT burst armed to
                // skip its debounce.
                if (!queue.length) { ready = false; return; }
                if (!ready) return;               // still inside the debounce
                const batch = queue.splice(0);
                ready = false;
                if (timer !== null) { clearTimeout(timer); timer = null; }
                // Claimed BEFORE the listeners run, so an onState handler that
                // calls flush() cannot start a second write underneath this one.
                sending = true;
                refresh(null);
                // ...and re-checked after them, because a handler is also
                // allowed to tear its mod down. The batch is dropped, which is
                // the whole contract.
                if (isDead()) {
                    sending = false;
                    settle({ ok: false, error: 'unloaded' }, true);
                    return;
                }
                let p;
                try {
                    p = _modStoreUpdate(gate, batchReducer(batch), wopts);
                } catch (e) {
                    p = Promise.reject(e);
                }
                p.then(finish, function (e) {
                    try {
                        console.error('[mods] serverStore.saveChain failed:', e);
                    } catch (_) { /* console is gone */ }
                    // finish() is the ONLY path that clears `sending`, so it
                    // runs on the failure branch too or the chain wedges.
                    finish({ ok: false, error: 'save_failed' });
                });
            }
            function save(fn) {
                if (typeof fn !== 'function') return false;
                if (isDead()) return false;
                queue.push(fn);
                if (timer !== null) clearTimeout(timer);
                timer = setTimeout(function () {
                    timer = null;
                    ready = true;
                    pump();
                }, wait);
                return true;
            }
            function flush() {
                if (isDead()) {
                    return Promise.resolve({ ok: false, error: 'unloaded' });
                }
                if (!sending && !queue.length) {
                    if (timer !== null) { clearTimeout(timer); timer = null; }
                    // Nothing outstanding — but do not call that a success while
                    // the banner is up: a flush with nothing pending reports the
                    // conflict that is still unresolved rather than a fresh ok.
                    return Promise.resolve(conflicted && lastRes
                        ? lastRes : { ok: true, idle: true });
                }
                // The barrier: an in-flight write must complete, and anything
                // already queued goes out in the batch after it.
                const need = sent + (sending ? 1 : 0) + (queue.length ? 1 : 0);
                const p = new Promise(function (resolve) {
                    waiters.push({ need: need, resolve: resolve });
                });
                ready = true;
                pump();
                return p;
            }
            function onState(fn) {
                if (typeof fn !== 'function' || isDead()) {
                    return function () {};
                }
                subs.push(fn);
                // Fired once on subscribe, so a banner mounted after a conflict
                // is correct without the caller reconstructing the state.
                try { fn(state, lastRes); }
                catch (e) {
                    try {
                        console.error('[mods] saveChain onState threw:', e);
                    } catch (_) { /* console is gone */ }
                }
                return function () {
                    const i = subs.indexOf(fn);
                    if (i !== -1) subs.splice(i, 1);
                };
            }
            // Rides the mod's own LIFO teardown list, like every other ctx
            // family's disposer. rec.unloading already makes the chain dead on
            // its own; this is what releases the timer and the listeners.
            if (rec && Array.isArray(rec.unloads)) rec.unloads.push(die);
            return { save: save, flush: flush, onState: onState };
        }
        function _ctxServerStoreSaveChain(ctx, rec) {
            const store = ctx && ctx.serverStore;
            // No half-family (the CP8 rule): a ctx without the get/set/update
            // this is built on gets no saveChain at all, rather than one that
            // always refuses — ctx.capabilities is OBSERVED, and
            // `needs: ['serverStore.saveChain']` then reads true.
            if (!store || typeof store !== 'object'
                    || typeof store.get !== 'function'
                    || typeof store.set !== 'function'
                    || typeof store.update !== 'function') return;
            // A40's CAS is what every send goes through. A page assembled
            // without 86c has neither, and gets no chain either.
            if (typeof _modStoreUpdate !== 'function') return;
            // Decorated in place, per mod, capturing that mod's own store and
            // its own activation record — the chain is scoped to both exactly
            // as get/set/update are scoped to the store.
            store.saveChain = function (opts) {
                return _modStoreSaveChain(store, opts, rec);
            };
        }
        // Guarded like every other registration in 86c: a page assembled
        // without #194's registry must not throw at load.
        if (typeof _registerCtxExtender === 'function') {
            _registerCtxExtender(_ctxServerStoreSaveChain);
        }
        // ---- end ctx.serverStore.saveChain -----------------------------------
