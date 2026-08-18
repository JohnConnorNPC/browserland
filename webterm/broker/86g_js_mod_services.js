        // ---- ctx.provide / ctx.consume: inter-mod services (#199) ------------
        //
        //     // the PROVIDER, from its init:
        //     ctx.provide('pattern', { apply: applyPattern, PATTERNS: PATTERNS });
        //
        //     // the CONSUMER, per use:
        //     const p = ctx.consume('pattern', 'pattern');
        //     if (p) p.apply(name);        // absent/inactive provider -> undefined
        //
        // WHAT IT REPLACES. Cross-mod calls today are hoisted globals plus
        // fragment parse order: pattern leaves `applyPattern` and `PATTERNS` at
        // the top level of the one shared <script> (mods/pattern/pattern.js) and
        // theme probes `typeof applyPattern === 'function'` before calling it
        // (mods/theme/theme.js). That probe answers exactly one question — "did
        // some fragment declaring that name evaluate?" — and it cannot answer any
        // of the ones that matter. Whose name is it? Is that mod still ACTIVE, or
        // was it switched off in the Control Panel ten minutes ago? What happens
        // to a reference taken while it was? A top-level `function` declaration
        // outlives the mod that declared it, so the probe reads TRUE for a dead
        // mod and the call lands in a torn-down closure — silently, and only in a
        // live run (#162 is the same shape one step worse: a hoisted fn reading a
        // later `let` threw TDZ and disabled the whole mod). The migration of the
        // pattern/theme pair is A51's, not this fragment's: nothing here changes
        // a mod, and every existing call site is untouched.
        //
        // ITS OWN FRAGMENT, for the reason 86a/86b/86c+86d/86e/86f each got one:
        // 86c_js_mod_ctx_ext.js is at the #68 2500-line per-fragment cap
        // (_MAX_LINES, ui.py — 11 lines spare when this was written) and the rule
        // for that cap has always been "split, never trim". Ordered after 86f and
        // still before the mod-script splice, because a mod's init reads the
        // finished ctx. One <script>, one scope, as with every 86* companion:
        // this file calls 86c's _registerCtxExtender / _registerModCapability the
        // same typeof-guarded way 86e and 86f do, and it reaches the loader's
        // active-mod map (window.__mods.active) the way every companion does.
        //
        // THE DECISIONS #199 MADE, implemented here rather than rediscussed:
        //
        //  1. `consume` IMPLIES NO DEPENDENCY EDGE. It returns undefined when the
        //     provider is inactive — the same soft `typeof`-guard idiom mods
        //     already write. `requires` stays the only way to force ordering and
        //     cascade bring-up. An implicit edge would turn a soft probe into a
        //     take-down cascade: disabling pattern would take theme down with it.
        //     Corollary: if you need a service AT YOUR OWN INIT, declare
        //     `requires`; otherwise consume lazily, PER USE.
        //  2. WHAT `consume` HANDS BACK IS A REVOCABLE PROXY, PER PROVIDER
        //     ACTIVATION. After the provider tears down, every member read/call
        //     on that proxy returns undefined / no-ops, with ONE console.warn.
        //     A stale capture degrades instead of acting on a dead provider.
        //  3. RE-PROVIDING A LIVE NAME FROM THE SAME MOD THROWS. That is a real
        //     programming error (two halves of one mod claiming one name), unlike
        //     consume's soft miss, and it is the same call the loader already
        //     makes for a duplicate mod id — hence the same ModConflictError.
        //
        // THE HONEST LIMIT, stated here because a reader of this file is exactly
        // who needs it: values a consumer has already EXTRACTED THROUGH the api
        // are not all revoked. A FUNCTION read off the proxy is (the proxy hands
        // back a wrapper that re-checks liveness at CALL time, so a plucked
        // method dies with its provider) — but a NESTED OBJECT (`p.config`), a
        // value RETURNED by a member, and anything a consumer obtained by any
        // other route are the provider's own objects and keep working. Deepening
        // the proxy into a membrane would not change that either: a service can
        // always hand out a live closure. THE PROXY IS TEARDOWN-RACE HYGIENE,
        // NOT A SECURITY BOUNDARY — a mod that can call another mod's api can
        // already do whatever that api does. Per-use `consume` stays the
        // recommended pattern precisely because it needs none of this.
        //
        // NOT `Proxy.revocable`, and this is the one place the obvious
        // implementation is the wrong one. `revoke()` makes EVERY operation on
        // the proxy throw a TypeError, which is the exact opposite of decision 2:
        // a stale capture would take the consumer down instead of degrading, and
        // it would do it from whatever line happened to touch the reference next.
        // Revocation here is therefore a flag on the registry entry that every
        // trap reads, plus dropping the api reference so the provider's object
        // can actually be collected.
        //
        // NOR IS THE PROXY'S TARGET THE api ITSELF. It is a neutral empty object
        // (a neutral arrow function when the api is callable), and every trap
        // forwards to `entry.api` by hand. A proxy over the api directly cannot
        // keep its promise: JS proxy invariants say a `get` must return the
        // target's own value for a non-writable, non-configurable data property,
        // so the FIRST mod to publish `ctx.provide('x', Object.freeze({…}))` —
        // an idiomatic, defensive thing to write — would get a dead proxy that
        // throws TypeError on every read instead of returning undefined. A
        // neutral target has no such properties, so deadness is unconditional and
        // does not depend on the shape of somebody else's api.
        //
        // THE PATHS THE `typeof applyPattern` PROBE NEVER HAD, each answered:
        //
        //   - INSTALLED BUT NOT YET LOADED (#163: an install applies on the NEXT
        //     page load). There is no activation record, so `consume` returns
        //     undefined — the same answer as a typo'd id, which is correct until
        //     the reload. The old probe would have read `undefined` too, but for
        //     the unrelated reason that no script had declared the global.
        //   - TORN DOWN WHILE A CONSUMER IS MID-CALL. A synchronous frame that
        //     has already entered the provider runs to completion; no proxy can
        //     unwind a stack. What IS guaranteed is that the FIRST thing
        //     _runUnloads does is set `rec.unloading` (86), which is one of the
        //     two things `_modServiceLive` reads — so from that instant every
        //     further operation through every proxy for that activation is dead,
        //     including one issued from the in-flight call's own continuation,
        //     and including the case where the provider tears ITSELF down from
        //     inside the call. Deadness therefore starts at the head of the
        //     take-down path, not at the disposer that happens to revoke.
        //   - TWO MODS PROVIDING THE SAME NAME. A non-event, structurally: there
        //     is no global service table to collide IN. Each activation's entries
        //     live on ITS OWN record and `consume` takes the provider id as its
        //     first argument, so `a:thing` and `b:thing` are two entries on two
        //     records that never meet. Disabling one leaves the other untouched.
        //   - A CAPTURE USED AFTER TEARDOWN. The documented degrade: undefined
        //     reads, no-op calls, exactly one console.warn naming the service.
        //   - A MOD CONSUMING ITSELF, or consuming a name it also provides. Both
        //     just work, and neither is special-cased: the lookup is by provider
        //     id, a mod's own record is active while it is running, and since
        //     consume creates NO dependency edge there is no cycle to detect.
        //
        // A NEW CAPABILITY ENTRY IS OWED FOR EACH, and this is the argument, not
        // an assumption: #197's map is per FAMILY — a bare top-level ctx member
        // name, never a dotted path — and `provide` and `consume` are TWO new
        // top-level members, exactly like #195's `hosts` and #198's `signal`.
        // (A41's saveChain owed none because it decorates `serverStore`, a member
        // of a family that already has an entry.) Registering only one would let
        // `needs: ['consume']` refuse a mod on a build that demonstrably has it.
        // They install as a PAIR (below), so the two entries are always both
        // present or both absent, which is what makes either one a sound thing to
        // declare. ctxVersion stays 1: additive, feature-detected surface, and a
        // bump would refuse every mod that pins v1.
        //
        // NO SURFACE ON A BUILD THAT CANNOT CARRY IT (the CP8 no-half-family
        // rule): an engine without Proxy/Reflect/WeakMap gets NEITHER member —
        // not a `provide` whose consumers could never get a revocable handle, and
        // not a `consume` that would have to hand back the raw api and quietly
        // drop decision 2. ctx.capabilities then reports both absent, which is
        // true, and `needs: ['consume']` blocks the mod with a reason instead of
        // letting it run against a seam that silently does something else.

        // Symbol.toPrimitive, resolved once. Used by the dead-read path below;
        // the fallback string is never a real key, it just keeps the comparison
        // total on an engine without Symbol.
        const _MOD_SERVICE_TO_PRIMITIVE =
            (typeof Symbol === 'function' && Symbol.toPrimitive)
                ? Symbol.toPrimitive : '@@toPrimitive';
        // How far up a prototype chain the function-shape snapshot walks. A
        // service api is a plain object or a small class instance; the bound is
        // here so a pathological (or cyclic-looking) chain cannot make provide()
        // expensive.
        const _MOD_SERVICE_PROTO_DEPTH = 8;

        // Is this entry still usable? TWO reads, and both matter:
        //   - `revoked`, set by the teardown disposer below;
        //   - `rec.unloading`, set by _runUnloads (86) BEFORE it aborts the
        //     signal, closes the mod's factory windows or drains one disposer.
        // The second is what makes deadness begin at the head of the take-down
        // path rather than at whatever point in the LIFO drain the disposer
        // happens to sit — see "TORN DOWN WHILE A CONSUMER IS MID-CALL" above.
        // Everything in a try because `entry.rec` is an attacker-adjacent read
        // and a liveness check that throws would be worse than a dead call.
        function _modServiceLive(entry) {
            try {
                if (!entry || entry.revoked) return false;
                const rec = entry.rec;
                return !!rec && !rec.unloading;
            } catch (_) { return false; }
        }
        // ONE warning per dead entry, ever. The flag lives on the ENTRY, which
        // dies with the activation, so: a consumer that hammers a dead proxy in a
        // render loop warns once and then degrades in silence, and a provider
        // that is re-enabled gets a fresh entry with a fresh warn budget. The
        // flag is set BEFORE the console call, so a console that throws (or is
        // gone) cannot turn "warn once" into "warn every time".
        function _modServiceWarnOnce(entry) {
            try {
                if (!entry || entry.warned) return false;
                entry.warned = true;
                console.warn('[mods] service "' + entry.id + ':' + entry.name
                    + '" is gone — its provider was torn down. This capture is '
                    + 'dead (reads undefined, calls no-op); re-consume per use. '
                    + 'Warned once.');
                return true;
            } catch (_) { return false; }
        }
        // A throwing api member is isolated the way every ctx surface isolates
        // mod code: logged, and the consumer sees undefined rather than an
        // exception thrown across a mod boundary it did not write. The honest
        // consequence, said out loud: a consumer cannot distinguish "member
        // threw" from "provider dead" from "no such member" — all three are
        // undefined, which is the whole point of a soft seam. Only a SYNCHRONOUS
        // throw is caught; a rejected promise returned by a member is the
        // consumer's to handle, because swallowing it would change the value's
        // meaning rather than isolate a failure.
        function _modServiceThrew(entry, key, e) {
            try {
                console.error('[mods] service "' + (entry && entry.id) + ':'
                    + (entry && entry.name) + '" member "' + String(key)
                    + '" threw (isolated; the consumer sees undefined):', e);
            } catch (_) { /* console is gone; degrade quietly */ }
        }
        // Which keys were FUNCTIONS when the service was published. Snapshotted
        // at provide time, while the provider is alive, because it is what the
        // dead-read path needs: `cm.load()` on a dead proxy must no-op, and a
        // `get` that returned plain undefined would instead throw "cm.load is not
        // a function" INSIDE the consumer — the failure this seam exists to
        // prevent. Reading the api at revoke time instead would mean touching a
        // provider that is already tearing down, which is the other thing this
        // must not do.
        //
        // Own DATA properties only, up the prototype chain (so a class-instance
        // api keeps its methods) and stopping before Object/Function.prototype
        // (`toString`, `call`, `bind` are not the service's members). Descriptors
        // rather than reads, so an ACCESSOR is never invoked here: running
        // provider code during provide() is exactly the kind of surprise a
        // snapshot must not carry. A symbol-keyed member is not tracked, so a
        // dead read of one is undefined — noted rather than fixed, since a
        // service's callable surface is string-keyed in every case that exists.
        function _modServiceFnKeys(api) {
            const keys = new Set();
            let obj = api;
            let depth = 0;
            while (obj && obj !== Object.prototype && obj !== Function.prototype
                    && depth < _MOD_SERVICE_PROTO_DEPTH) {
                let names = [];
                try { names = Object.getOwnPropertyNames(obj); }
                catch (_) { break; }
                for (const key of names) {
                    let d = null;
                    try { d = Object.getOwnPropertyDescriptor(obj, key); }
                    catch (_) { d = null; }
                    if (d && typeof d.value === 'function') keys.add(key);
                }
                try { obj = Object.getPrototypeOf(obj); }
                catch (_) { break; }
                depth += 1;
            }
            return keys;
        }
        // Wrap a function the consumer read off the proxy. The wrapper re-checks
        // liveness AT CALL TIME, so `const apply = p.apply;` taken while the
        // provider was live still no-ops after teardown, and it applies the
        // member to the api itself — provider code sees its own object, never the
        // proxy. Cached per raw function in a WeakMap on the entry, so repeated
        // reads return the SAME wrapper (identity comparisons and
        // removeEventListener-style pairing keep working) without retaining
        // functions an accessor manufactures per read.
        function _modServiceWrap(entry, fn, key) {
            let wrapped = null;
            try { wrapped = entry.wrappers.get(fn); } catch (_) { wrapped = null; }
            if (wrapped) return wrapped;
            wrapped = function () {
                if (!_modServiceLive(entry)) {
                    _modServiceWarnOnce(entry);
                    return undefined;
                }
                try { return fn.apply(entry.api, arguments); }
                catch (e) { _modServiceThrew(entry, key, e); return undefined; }
            };
            try { entry.wrappers.set(fn, wrapped); } catch (_) {}
            return wrapped;
        }
        // The revocable proxy itself, built once per entry (so every consumer of
        // one activation's service shares one object, and one revocation covers
        // all of them). The trap set is deliberately the operations a consumer
        // actually performs — read, probe, write, delete, enumerate, call. What
        // is NOT trapped (getPrototypeOf, defineProperty, extensibility,
        // construct) lands on the neutral target and never reaches the api: a
        // consumer cannot `instanceof` across this proxy and should not try. That
        // is the "not a membrane" clause of the honest limit above, made
        // concrete.
        function _modServiceProxy(entry) {
            // Callable api -> callable proxy. An ARROW function is the neutral
            // target, not `function () {}`: a normal function has a
            // non-configurable own `prototype`, which the ownKeys trap would then
            // be obliged to report — an invariant violation waiting for the first
            // consumer to call Object.keys(). An arrow has no `prototype` and its
            // own `length`/`name` are configurable.
            const target = (typeof entry.api === 'function')
                ? (() => undefined) : {};
            function getTrap(_t, key) {
                if (!_modServiceLive(entry)) {
                    _modServiceWarnOnce(entry);
                    // A dead READ must not become a dead THROW somewhere else:
                    // without this, `'saw ' + p` (or console.log of the capture)
                    // would find no toString/@@toPrimitive and raise "Cannot
                    // convert object to primitive value" — a crash in the
                    // consumer, from a mechanism whose contract is to degrade.
                    if (key === 'toString' || key === _MOD_SERVICE_TO_PRIMITIVE) {
                        return entry.deadLabel;
                    }
                    return entry.fnKeys.has(key) ? entry.deadFn : undefined;
                }
                let value;
                // The api is BOTH the object read and the receiver: an accessor
                // on it must run against the provider's own object, never
                // re-enter this proxy (which would recurse forever).
                try { value = Reflect.get(entry.api, key, entry.api); }
                catch (e) { _modServiceThrew(entry, key, e); return undefined; }
                return (typeof value === 'function')
                    ? _modServiceWrap(entry, value, key) : value;
            }
            return new Proxy(target, {
                get: getTrap,
                has: function (_t, key) {
                    if (!_modServiceLive(entry)) {
                        _modServiceWarnOnce(entry);
                        return false;
                    }
                    try { return Reflect.has(entry.api, key); }
                    catch (e) { _modServiceThrew(entry, key, e); return false; }
                },
                // Writes forward while alive and are DROPPED once dead. Both
                // return true, never false: a set trap that reports failure
                // throws a TypeError at a strict-mode call site, and "your write
                // went nowhere" must not be louder than "your call went nowhere".
                set: function (_t, key, value) {
                    if (!_modServiceLive(entry)) {
                        _modServiceWarnOnce(entry);
                        return true;
                    }
                    try { Reflect.set(entry.api, key, value, entry.api); }
                    catch (e) { _modServiceThrew(entry, key, e); }
                    return true;
                },
                deleteProperty: function (_t, key) {
                    if (!_modServiceLive(entry)) {
                        _modServiceWarnOnce(entry);
                        return true;
                    }
                    try { Reflect.deleteProperty(entry.api, key); }
                    catch (e) { _modServiceThrew(entry, key, e); }
                    return true;
                },
                // ownKeys + getOwnPropertyDescriptor exist so that Object.keys,
                // spread and JSON.stringify see the api rather than the empty
                // neutral target — silently reporting {} for a LIVE service would
                // be a wrong answer, not a degraded one, and wrong answers are
                // how a general mechanism breaks a path its hand-rolled
                // predecessor never had.
                ownKeys: function () {
                    if (!_modServiceLive(entry)) {
                        _modServiceWarnOnce(entry);
                        return [];
                    }
                    try { return Reflect.ownKeys(entry.api); }
                    catch (e) { _modServiceThrew(entry, 'ownKeys', e); return []; }
                },
                getOwnPropertyDescriptor: function (t, key) {
                    if (!_modServiceLive(entry)) {
                        _modServiceWarnOnce(entry);
                        return undefined;
                    }
                    let d = null;
                    try { d = Object.getOwnPropertyDescriptor(entry.api, key); }
                    catch (e) { _modServiceThrew(entry, key, e); return undefined; }
                    if (!d) return undefined;
                    // Always reported CONFIGURABLE and WRITABLE, whatever the api
                    // says: describing a property as non-configurable when the
                    // (neutral) target does not have it is an invariant violation
                    // that throws — the frozen-api trap again, from the other
                    // side. The VALUE goes through getTrap, so an enumerated
                    // member is the same wrapped function a plain read gives —
                    // which does mean Object.keys() over a service api RUNS its
                    // accessors, the one visible cost of describing a live
                    // member truthfully rather than reporting undefined.
                    return { value: getTrap(t, key), writable: true,
                             enumerable: !!d.enumerable, configurable: true };
                },
                apply: function (_t, _thisArg, args) {
                    if (!_modServiceLive(entry)) {
                        _modServiceWarnOnce(entry);
                        return undefined;
                    }
                    try { return entry.api.apply(entry.api, args); }
                    catch (e) { _modServiceThrew(entry, '(call)', e); return undefined; }
                },
            });
        }
        // ctx.provide(name, api). Publishes one named api on THIS activation's
        // record. Throws — the only member of this pair that does — for the two
        // programming errors: an unusable name/api, and re-providing a name this
        // activation already has live. A throw from init() is already handled the
        // way it should be (initMod logs it, runs _runUnloads to reverse the
        // partial init and releases the slot), which is the other half of why
        // this can afford to be strict where consume cannot.
        function _provideModService(rec, name, api) {
            if (typeof name !== 'string' || !name) {
                throw new Error('ctx.provide: a non-empty string service name '
                    + 'is required');
            }
            // A primitive cannot carry members and cannot be proxied; refusing it
            // here is a clear message at the provide site instead of a TypeError
            // from deep inside the first consume.
            if (!api || (typeof api !== 'object' && typeof api !== 'function')) {
                throw new Error('ctx.provide("' + name + '"): the api must be an '
                    + 'object or a function');
            }
            const services = rec && rec.services;
            if (!services) return;
            // A DEAD ACTIVATION PUBLISHES NOTHING, and quietly: this is provide
            // called from an async continuation that resolved after the mod was
            // switched off (#196's saveChain drops a write from a dead activation
            // for the same reason). It is not a programming error — the mod could
            // not have known — and nobody could consume the entry anyway, since
            // `unloading` already reads dead.
            if (rec.unloading) return;
            const existing = services.get(name);
            if (existing && !existing.revoked) {
                const msg = 'ctx.provide: mod "' + rec.id + '" already provides "'
                    + name + '"';
                // The loader's own conflict type when this page has it (the same
                // error registerMod raises for a duplicate mod id — the identical
                // "two claims on one name" mistake), a plain Error otherwise.
                throw (typeof ModConflictError === 'function')
                    ? ModConflictError(msg) : new Error(msg);
            }
            const entry = {
                rec: rec, id: rec.id, name: name, api: api,
                fnKeys: _modServiceFnKeys(api),
                wrappers: new WeakMap(),
                revoked: false, warned: false, proxy: null,
                deadFn: null, deadLabel: null,
            };
            // The two constants a dead read hands back. Per entry so they name
            // the service in a debugger, and built now so the dead path allocates
            // nothing and cannot fail.
            entry.deadFn = function () { return undefined; };
            entry.deadLabel = function () {
                return '[mods] dead service "' + entry.id + ':' + entry.name + '"';
            };
            services.set(name, entry);
        }
        // ctx.consume(modId, name). Undefined for EVERY miss — an unknown id, a
        // mod that is installed but not loaded this page (#163), one that is
        // disabled, one mid-teardown, an unpublished name, a revoked entry — and
        // never an error, because "not there" is the answer a consumer is
        // supposed to branch on. Looking the provider up in window.__mods.active
        // (the loader's map of ACTIVE records) rather than in a table this
        // fragment keeps is what makes that true without any bookkeeping: a
        // disabled mod's record is deleted from it, and a re-enabled mod gets a
        // brand-new record with an empty service map. A fragment-level map keyed
        // by mod id is the obvious wrong implementation, and it is the one #198
        // documents failing silently — a stale activation's services would answer
        // for a mod that has since been switched off and on again.
        function _consumeModService(modId, name) {
            if (typeof modId !== 'string' || !modId) return undefined;
            if (typeof name !== 'string' || !name) return undefined;
            let rec = null;
            try {
                const active = window.__mods && window.__mods.active;
                rec = (active && typeof active.get === 'function')
                    ? active.get(modId) : null;
            } catch (_) { return undefined; }
            if (!rec || rec.unloading) return undefined;
            let entry = null;
            try {
                const services = rec.services;
                entry = (services && typeof services.get === 'function')
                    ? services.get(name) : null;
            } catch (_) { return undefined; }
            if (!entry || entry.revoked) return undefined;
            // Built lazily and cached on the entry: one proxy per (activation,
            // name), shared by every consumer, so revocation is one flag and a
            // per-use consume in a hot path allocates nothing.
            if (!entry.proxy) {
                try { entry.proxy = _modServiceProxy(entry); }
                catch (_) { return undefined; }
            }
            return entry.proxy;
        }
        // The teardown disposer: revoke every service this activation published.
        // Dropping `api` and `wrappers` is not tidiness — it is what makes the
        // revocation real for memory, since a consumer may still be holding the
        // proxy and the proxy's own target is the neutral object, not the api.
        // Every step isolated: a mod's teardown must not fail because one entry
        // is odd.
        function _revokeModServices(rec) {
            let services = null;
            try { services = rec && rec.services; } catch (_) { return; }
            if (!services || typeof services.forEach !== 'function') return;
            try {
                services.forEach(function (entry) {
                    try {
                        entry.revoked = true;
                        entry.api = null;
                        entry.wrappers = null;
                    } catch (_) {}
                });
            } catch (_) {}
            try { services.clear(); } catch (_) {}
        }
        // The extender. Per activation, hung on the RECORD (the arguments-not-
        // closure rule: this fragment cannot see makeCtx's per-mod locals), so a
        // disabled-then-re-enabled mod gets an empty service map for free.
        //
        // The disposer is registered HERE — at ctx construction, before init()
        // has run and so before anything a mod writes can throw — rather than at
        // the first provide(). It is therefore the OLDEST entry on rec.unloads
        // and runs LAST in the LIFO drain, which costs nothing: `rec.unloading`
        // is set at the head of _runUnloads and already deadens every proxy, so
        // the disposer's job is releasing memory, not closing a window. The one
        // thing that would be wrong is registering it late: a provider that
        // throws halfway through init would then leave a published service on a
        // record that is being rolled back.
        function _ctxServices(ctx, rec) {
            if (!ctx || !rec || typeof rec !== 'object') return;
            // No half-family (the CP8 rule): both members or neither. Without
            // Proxy there is no revocable handle to hand back, and a `consume`
            // that returned the raw api would silently drop decision 2 —
            // teardown would stop revoking anything and every stale capture would
            // call into a dead mod, which is precisely the bug this replaces.
            if (typeof Proxy !== 'function' || typeof Reflect !== 'object'
                    || typeof WeakMap !== 'function') return;
            // Idempotent by construction: a ctx decorated twice keeps the one
            // service map and the one disposer, so nothing can be double-revoked
            // or stranded.
            if (!rec.services) {
                rec.services = new Map();
                if (Array.isArray(rec.unloads)) {
                    rec.unloads.push(function () { _revokeModServices(rec); });
                }
            }
            ctx.provide = function (name, api) {
                return _provideModService(rec, name, api);
            };
            // Not scoped to the caller in any way: consume names the provider
            // explicitly, and this fragment never consults the consumer's own
            // record. That is what makes a mod consuming ITSELF, or consuming a
            // name it also provides, unremarkable rather than a special case.
            ctx.consume = function (modId, name) {
                return _consumeModService(modId, name);
            };
        }
        // Guarded like every other registration in an extension fragment: a page
        // assembled without #194's registry or #197's capability map must not
        // throw at load.
        if (typeof _registerCtxExtender === 'function') {
            _registerCtxExtender(_ctxServices);
        }
        if (typeof _registerModCapability === 'function') {
            _registerModCapability('provide', 1);
            _registerModCapability('consume', 1);
        }
        // ---- end ctx.provide / ctx.consume -----------------------------------
