        // ---- ctx.commands: the dispatch surface (#199) ----------------------
        //
        //     ctx.commands.register('save', {
        //         scope: 'window',              // 'global' (default) | 'window'
        //         when: function (where) { return where.win.kind === 'note'; },
        //         run: function (args, where) { return save(where.win); },
        //     });                               // -> 'scratchpad:save'
        //     const r = await ctx.commands.execute('scratchpad:save', args);
        //     // {ok:true, value} | {ok:false, reason:'absent' | 'inactive'
        //     //                                       | 'blocked' | 'error'}
        //
        // WHAT IT REPLACES. Dispatch INTO a mod rides duck-typed fields today:
        // scratchpad's Ctrl+S goes through `win._saveToServer`, a property core
        // pokes by name on a specific window, and help's functions are hoisted
        // to the top level ON PURPOSE because the mod system cannot contribute a
        // keybinding target at all. Both are the same missing thing — a NAMED,
        // owned dispatch target — and both fail the same way: an id nobody
        // registers is a typo that reads as "nothing happened", a disabled mod's
        // field is `undefined` and reads as "nothing happened", and a throw
        // inside either takes down whatever called it. (The migrations are
        // A51/#204's, not this fragment's: nothing here changes a mod, and every
        // existing call site is untouched.)
        //
        // ITS OWN FRAGMENT, for the reason 86a/86b/86c+86d/86e/86f each got one:
        // 86c_js_mod_ctx_ext.js is at the #68 2500-line per-fragment cap
        // (_MAX_LINES, ui.py) and the rule for that cap has always been "split,
        // never trim" — shrinking a neighbour to make room is exactly what it
        // forbids. The LOADER is at that cap too, which is why the three
        // registrar wrappers there call into this file behind a `typeof` guard
        // (the way they call _abortModSignal (#198) and _emitModEvent (#195))
        // instead of growing the normalization in place. Ordered among the 86*
        // companions, still before the mod-script splice, because a mod's init
        // reads the finished ctx. One <script>, one scope, as with every 86*
        // companion: this file calls 86c's _registerCtxExtender /
        // _registerModCapability the same guarded way.
        //
        // THE OUTCOME VOCABULARY IS THE POINT, and it is closed and branchable:
        //
        //   'absent'   nothing ever registered that id in this page. A typo, or
        //              a mod that is not INSTALLED. Not fixable by toggling.
        //   'inactive' the id was registered by a mod that is not active now —
        //              switched off, mid-teardown, or torn down since. Fixable
        //              by toggling that mod on, which is a different sentence
        //              from 'absent' and the reason a tombstone is kept below.
        //   'blocked'  the command declined THIS invocation: `when()` said no,
        //              or it is window-scoped and no window is focused.
        //   'error'    the command failed to answer — `run` threw, its promise
        //              rejected, or `when()` threw (see below).
        //
        // Every one of those resolves. `execute` ALWAYS returns a Promise and
        // that promise NEVER rejects, because its callers are a key dispatcher
        // and a menu click handler: one bad command must not take either down,
        // and neither has anywhere to put an exception.
        //
        // THE PATHS `win._saveToServer` NEVER HAD. A field on a window is only
        // ever reached with that window in hand, by core, on purpose. A named,
        // globally-addressable command is reachable from a keybinding, a menu, a
        // console, and another mod — so each of these is a case the hand-rolled
        // version could not express, decided here rather than discovered later:
        //
        //  1. A WINDOW-SCOPED COMMAND WITH NO FOCUSED WINDOW resolves
        //     {ok:false, reason:'blocked'} and calls NEITHER `run` NOR `when`.
        //     It does not run with `win: null`. Handing every window-scoped
        //     command a null it must re-check would rebuild, inside every mod,
        //     precisely the `if (typeof win._saveToServer === 'function')` guard
        //     this family exists to delete — and the first mod to forget the
        //     check throws a TypeError out of the key dispatcher. `scope` is a
        //     PRECONDITION, and an unmet precondition is 'blocked'.
        //  2. A GLOBAL-SCOPED COMMAND GETS `{win: null}` EVEN WHEN A WINDOW IS
        //     FOCUSED. `scope` is a contract, not a hint: passing the focused
        //     window opportunistically would make a global command quietly
        //     window-sensitive, and would make a later `global` -> `window`
        //     migration a no-op change that alters nothing until the day nothing
        //     is focused. A command that wants the focused window says so.
        //  3. A MOD DISABLED BETWEEN register AND execute resolves 'inactive',
        //     never 'absent'. The teardown disposer does not delete the entry;
        //     it REPLACES it with a tombstone that keeps the id and the owner
        //     and drops the closures (so a dead activation's `run`, its `rec`
        //     and everything they capture are released). Deleting outright would
        //     collapse "turn that mod on" into "that id does not exist", and the
        //     operator-facing difference between those two sentences is the
        //     whole reason the vocabulary has four words instead of one.
        //     rec.unloading is checked too, so a command executed from an abort
        //     listener or from another disposer — i.e. mid-teardown, before this
        //     command's own disposer has run — already reads 'inactive'.
        //  4. A THROWING `when()` resolves 'error', NOT 'blocked', and `run` is
        //     not called. A guard that throws did not say no; it failed to
        //     answer, and mapping that to the same word as a deliberate veto
        //     would hide a broken guard as a normal "not right now" forever.
        //     'blocked' is a decision. 'error' is a defect. They are reported
        //     apart on purpose.
        //  5. AN ID COLLISION BETWEEN TWO MODS IS UNREPRESENTABLE. `register`
        //     auto-prefixes '<modId>:' and REFUSES a local id containing ':',
        //     so mod 'b' cannot spell 'a:save' — the refusal is what makes the
        //     prefix a namespace instead of a convention. (It also refuses the
        //     double-prefix trap: a mod that helpfully passes its own 'a:save'
        //     gets a throw at init, not a live 'a:a:save' nothing dispatches.)
        //     Within one mod a duplicate is a ModConflictError, exactly as
        //     registerKeyActions does it: last-wins would let one file silently
        //     steal a target another file in the same mod already published.
        //
        // WHY `run` IS CALLED SYNCHRONOUSLY even though `execute` is async: a
        // command dispatched from a menu click or a keydown may need the user
        // gesture (clipboard writes, fullscreen, window.open). Only the RESULT
        // is wrapped in a Promise; the call itself happens in the caller's tick.
        // The one exception is a `when()` that returns a thenable — resolving it
        // necessarily defers `run`, which is why an async guard is documented as
        // costing the command its synchronous dispatch. A thenable `when` is
        // RESOLVED rather than coerced: `!!promise` is true, so the coerced
        // version of this line would let every async guard allow everything —
        // shipped once already as #168's async-validator bug.
        //
        // WHAT THE MENU PATH DELIBERATELY DOES NOT DO: it does not call `when()`
        // at render time to grey an item out. Two arbiters (a synchronous
        // render-time guard and an asynchronous dispatch-time one) is how a menu
        // shows an item enabled and then does nothing — or worse, disables an
        // item whose guard merely threw. `execute` is the single arbiter, at
        // click time, so no mod code runs during a menu render at all and a
        // throwing `when()` cannot reach the renderer.
        //
        // A NEW CAPABILITY ENTRY IS OWED, and this is the argument: #197's map
        // is per FAMILY — a bare top-level ctx member name, never a dotted path
        // — and `commands` is a NEW top-level member, exactly like #195's
        // `hosts` and #198's `signal`. (#196's saveChain owed none: it decorates
        // `serverStore`, whose family entry already exists.) Without the
        // registration ctx.capabilities would lie by omission about surface the
        // build demonstrably has, and `needs: ['commands']` — or the finer
        // `needs: ['commands.register']`, which #197 resolves as a path against
        // the live ctx — could never be met. ctxVersion stays 1: additive,
        // feature-detected surface (`if (ctx.commands)`), and a bump would
        // refuse every mod that pins v1.
        //
        // NO SURFACE ON A BUILD THAT CANNOT CARRY IT (the CP8 no-half-family
        // rule): without a usable Map/Promise, or without the per-activation
        // record that owns registration and teardown, there is no ctx.commands
        // at all rather than a register() that returns nothing dispatchable. The
        // capability map then reports it absent — which is true — and
        // `needs: ['commands']` blocks the mod with a reason instead of letting
        // it run against a registry that silently drops everything.

        // FULL id -> a live entry {id, modId, rec, scope, run, when} or a
        // tombstone {id, modId, scope, dead:true}. Keyed by the PREFIXED id, so
        // two mods with the same local name are two keys and neither can see the
        // other's. Bounded by the ids this page's mods have ever registered: a
        // tombstone replaces an entry, it is not appended.
        const _MOD_COMMANDS = new Map();

        // The focused window, or null. Reads core's own `frontId` + `windows`
        // (64/73), the same pair closeFrontWindow (78) reads, and applies the
        // same rule: a front id naming a window that is gone is no focus at all.
        // Everything is inside the try because `frontId` is a `let` in an
        // earlier fragment of this one script — `typeof` on a binding still in
        // its temporal dead zone THROWS rather than returning 'undefined'
        // (#162's failure mode), and a page assembled without 64 must degrade to
        // "nothing is focused", not take the dispatcher down.
        function _focusedModWindow() {
            try {
                if (typeof frontId === 'undefined' || !frontId) return null;
                if (typeof windows === 'undefined' || !windows
                        || typeof windows.get !== 'function') return null;
                const win = windows.get(frontId);
                return (win && !win.disposed) ? win : null;
            } catch (_) { return null; }
        }

        // One 'error' outcome, logged once. The log is the isolation being
        // VISIBLE: a swallowed exception with no console line is how a broken
        // command becomes "the button does nothing" for a week. `error` carries
        // the exception for a caller that wants it, mirroring initMod's
        // {ok:false, reason:'init-threw', error:e}.
        function _modCommandFailed(id, stage, e) {
            try {
                console.error('[mods] command "' + id + '" ' + stage
                    + '() failed:', e);
            } catch (_) { /* console is gone; still resolve */ }
            return { ok: false, reason: 'error', error: e };
        }

        // Register one command for `rec`'s activation. Throws on a malformed
        // spec or a duplicate — a registration error is a programmer error, and
        // throwing from init is what makes initMod roll the mod back rather than
        // leave it half-wired (the same contract registerKeyActions has).
        function _registerModCommand(rec, id, spec) {
            const modId = rec && rec.id;
            if (typeof id !== 'string' || !id || id.indexOf(':') !== -1) {
                throw new Error('ctx.commands.register: id must be a non-empty '
                    + 'string without ":" (register prefixes "' + modId + ':")');
            }
            if (!spec || typeof spec !== 'object' || Array.isArray(spec)
                    || typeof spec.run !== 'function') {
                throw new Error('ctx.commands.register("' + id
                    + '"): {run:function} is required');
            }
            const scope = (spec.scope === 'window') ? 'window' : 'global';
            if (spec.scope !== undefined && spec.scope !== 'window'
                    && spec.scope !== 'global') {
                throw new Error('ctx.commands.register("' + id
                    + '"): scope must be "global" or "window"');
            }
            if (spec.when !== undefined && typeof spec.when !== 'function') {
                throw new Error('ctx.commands.register("' + id
                    + '"): when must be a function');
            }
            const full = modId + ':' + id;
            // A registration arriving from a stray timer AFTER the activation
            // died is refused, loudly and inertly. It cannot throw (nobody is
            // there to catch it) and it must not resurrect a dead activation's
            // code under a live id — so nothing is recorded and `execute` keeps
            // answering for whatever the current activation registered.
            if (rec.unloading) {
                try {
                    console.warn('[mods] ignoring late ctx.commands.register("'
                        + full + '"): "' + modId + '" is tearing down');
                } catch (_) {}
                return function () {};
            }
            const live = _MOD_COMMANDS.get(full);
            if (live && !live.dead) {
                throw ModConflictError('command "' + full + '" already exists');
            }
            const entry = { id: full, modId: modId, rec: rec, scope: scope,
                            run: spec.run,
                            when: (typeof spec.when === 'function')
                                ? spec.when : null,
                            dead: false };
            _MOD_COMMANDS.set(full, entry);
            // The disposer rides rec.unloads, so a disable, an initMod rollback
            // after a later throw, and the LIFO drain all release it exactly
            // once. Identity-checked: a stale disposer from a previous
            // activation must never tombstone the CURRENT one's entry.
            const off = function () {
                if (_MOD_COMMANDS.get(full) !== entry) return;
                _MOD_COMMANDS.set(full, { id: full, modId: modId, scope: scope,
                                          dead: true });
            };
            rec.unloads.push(off);
            return off;
        }

        // Call `run` and normalize whatever it returns. The call is synchronous
        // (see the header: a gesture-gated command must not lose the gesture);
        // only the result is adopted. Promise.resolve handles both a plain value
        // and a thenable, including a hostile `then` getter — that rejects,
        // which lands on the 'error' branch like any other failure.
        function _runModCommand(entry, args, where) {
            let out;
            try { out = entry.run(args, where); }
            catch (e) { return Promise.resolve(_modCommandFailed(entry.id, 'run', e)); }
            return Promise.resolve(out).then(function (value) {
                return { ok: true, value: value };
            }, function (e) {
                return _modCommandFailed(entry.id, 'run', e);
            });
        }

        // Execute a command by its FULL id. ALWAYS returns a Promise; that
        // promise ALWAYS resolves, to {ok:true, value} or to {ok:false, reason}
        // with a reason out of the closed four-word vocabulary above.
        function _execModCommand(fullId, args) {
            const entry = (typeof fullId === 'string')
                ? _MOD_COMMANDS.get(fullId) : undefined;
            if (!entry) return Promise.resolve({ ok: false, reason: 'absent' });
            if (entry.dead || !entry.rec || entry.rec.unloading
                    || typeof entry.run !== 'function') {
                return Promise.resolve({ ok: false, reason: 'inactive' });
            }
            let win = null;
            if (entry.scope === 'window') {
                win = _focusedModWindow();
                // Case 1: neither run nor when is called, and neither ever sees
                // a null win. `detail` is informational only — branch on
                // `reason`, which is the part that is closed and stable.
                if (!win) {
                    return Promise.resolve({ ok: false, reason: 'blocked',
                                             detail: 'no-window' });
                }
            }
            const where = { win: win };
            if (entry.when) {
                let allowed;
                try { allowed = entry.when(where); }
                catch (e) {
                    return Promise.resolve(
                        _modCommandFailed(entry.id, 'when', e));
                }
                // A thenable guard is RESOLVED, never coerced (#168). It costs
                // the command its synchronous dispatch, which is why a
                // gesture-gated command wants a synchronous guard.
                if (allowed && typeof allowed.then === 'function') {
                    return Promise.resolve(allowed).then(function (v) {
                        if (!v) {
                            return { ok: false, reason: 'blocked',
                                     detail: 'when' };
                        }
                        // Re-checked after the await: the mod may have been torn
                        // down while its own guard was pending, and running a
                        // dead activation's code is the one thing this family
                        // must never do.
                        if (entry.dead || !entry.rec || entry.rec.unloading) {
                            return { ok: false, reason: 'inactive' };
                        }
                        return _runModCommand(entry, args, where);
                    }, function (e) {
                        return _modCommandFailed(entry.id, 'when', e);
                    });
                }
                if (!allowed) {
                    return Promise.resolve({ ok: false, reason: 'blocked',
                                            detail: 'when' });
                }
            }
            return _runModCommand(entry, args, where);
        }

        // ---- `command:` on the key-action and menu registrars ---------------
        // Both normalizers are called from the loader's ctx wrappers behind a
        // `typeof` guard, and both are IDENTITY-PRESERVING: with nothing
        // declaring a `command:`, the very same array/items object goes through
        // to core, so every existing caller is byte-for-byte unaffected.

        // registerKeyActions([{id, label, command:'mod:cmd'}]) -> core's
        // required {id, label, run}. Declaring BOTH `command` and `run` throws:
        // this runs at registration time, inside init, where a throw is a
        // rollback and a clear message — and silently preferring one of the two
        // is how a keybinding ends up dispatching the thing nobody edited.
        function _modCommandKeyActions(rec, actions) {
            const list = Array.isArray(actions) ? actions : [actions];
            let touched = false;
            const out = list.map(function (a) {
                if (!a || typeof a !== 'object' || Array.isArray(a)
                        || typeof a.command !== 'string' || !a.command) return a;
                if (typeof a.run === 'function') {
                    throw new Error('registerKeyActions: "' + a.id + '" ('
                        + (rec && rec.id) + ') declares both command: and run:');
                }
                touched = true;
                const cmd = a.command;
                const args = a.commandArgs;
                const copy = {};
                for (const k of Object.keys(a)) copy[k] = a[k];
                // The dispatcher ignores the return value; execute never
                // rejects, so there is no unhandled rejection to leak either.
                copy.run = function () { _execModCommand(cmd, args); };
                return copy;
            });
            return touched ? out : actions;
        }

        // A menu item may carry `command:` (+ optional `commandArgs`) instead of
        // `action:`. An item declaring both keeps its `action` rather than
        // throwing — the asymmetry with the key path is deliberate: this runs at
        // RENDER time, where a throw would cost the whole context menu, and
        // _pushMenuItems's contract is already "a broken contributor must not
        // take the menu down". `enabled` defaults to true for a command item
        // (renderMenu wires a click only for enabled items, and an item that
        // names a dispatch target it cannot dispatch is not an item).
        function _modCommandMenuItems(items) {
            if (!Array.isArray(items)) return items;
            let touched = false;
            const out = items.map(function (it) {
                if (!it || typeof it !== 'object'
                        || typeof it.command !== 'string' || !it.command
                        || typeof it.action === 'function') return it;
                touched = true;
                const cmd = it.command;
                const args = it.commandArgs;
                const copy = {};
                for (const k of Object.keys(it)) copy[k] = it[k];
                if (!('enabled' in copy)) copy.enabled = true;
                copy.action = function () { _execModCommand(cmd, args); };
                return copy;
            });
            return touched ? out : items;
        }
        // Wrap a menu contributor so its returned items get the pass above. The
        // contributor is called OUTSIDE the try on purpose: a throwing
        // contributor stays _pushMenuItems's to log and swallow, exactly as
        // before this fragment existed. A non-function passes straight through
        // so core's own "fn must be a function" throw is unchanged.
        function _modCommandMenuFn(rec, fn) {
            if (typeof fn !== 'function') return fn;
            return function (arg) {
                const items = fn(arg);
                try { return _modCommandMenuItems(items); }
                catch (e) {
                    try {
                        console.error('[mods] command menu items failed for "'
                            + (rec && rec.id) + '":', e);
                    } catch (_) {}
                    return items;
                }
            };
        }

        // The extender. No record, no Map, no Promise -> no ctx.commands at all
        // (the no-half-family rule): registration is owned by the activation
        // record, so a surface without one could register nothing that teardown
        // could ever release.
        function _ctxCommands(ctx, rec) {
            if (!rec || typeof rec !== 'object') return;
            if (typeof Map !== 'function' || typeof Promise !== 'function') return;
            ctx.commands = {
                register: function (id, spec) {
                    return _registerModCommand(rec, id, spec);
                },
                // Takes the FULL id — a mod dispatching its own command spells
                // its own prefix. `register` is the only namespaced call, and
                // that asymmetry is what keeps "execute anything by name" and
                // "register only into your own namespace" both true.
                execute: function (id, args) {
                    return _execModCommand(id, args);
                },
            };
        }
        // Guarded like every other registration in an extension fragment: a page
        // assembled without #194's registry or #197's capability map must not
        // throw at load.
        if (typeof _registerCtxExtender === 'function') {
            _registerCtxExtender(_ctxCommands);
        }
        if (typeof _registerModCapability === 'function') {
            _registerModCapability('commands', 1);
        }
        // ---- end ctx.commands ------------------------------------------------
