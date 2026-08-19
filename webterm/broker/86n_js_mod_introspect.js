        // ---- ctx.mods / ctx.helpCards / ctx.settings.describe (#202) --------
        //
        //     ctx.mods.list()            // [{id, active, pin, version, tiers}]
        //     ctx.mods.isActive(id)      // true | false
        //     ctx.mods.pinOf(id)         // true | false | null
        //     ctx.settings.describe(modId, key)   // {type, options, default}
        //     ctx.settings.describe(key)          // ...own-mod shorthand
        //     ctx.helpCards.list()       // the sanitized typed-span card DATA
        //
        // WHAT IT REPLACES, precisely. Mods scrape state core already owns:
        // mods/mod-sync reads <option> values back OUT OF THE DOM to learn a
        // choice control's domain (querySelectorAll('option, input[type=
        // "radio"]')), reads window.__mods.registered and
        // window.__mods.settingToggles directly, and sniffs the loader-private
        // `_pin` behind a `typeof` guard; mods/help reads
        // window.__mods.helpCards. **window.__mods IS A TEST FIXTURE, NOT A
        // CONTRACT** -- it is the loader's own mutable bag, named with the
        // double underscore that says so, and it carries live records: the
        // `active` Map holds each mod's unloads array, and a settingToggles
        // entry holds read/reflect/onChange CLOSURES plus its live DOM
        // `section`. A mod reading that reaches straight into core's internals,
        // can mutate them by accident, and pins every field of them as a
        // de-facto API that no test defends. This fragment promotes the SHAPES
        // a mod actually needs into ctx surface, and nothing else. (The
        // mod-side migrations are #204's, not this fragment's: nothing here
        // changes a mod, and every existing scrape still works.)
        //
        // THE DECISIONS THE ISSUE MADE, implemented here rather than rediscussed:
        //
        //  1. GETTERS RETURN CLONES, AND THE SHAPES FREEZE AS CONTRACT. Every
        //     value below is built fresh on each call out of primitives and
        //     handed back Object.freeze()n. Fresh means a caller that mutates
        //     what it got cannot reach core (there is nothing shared to
        //     reach); frozen means the mutation is a visible no-op rather than
        //     a local copy that silently drifts from the truth. Because the
        //     shapes freeze as CONTRACT too, they are deliberately MINIMAL:
        //     adding a field later is additive, changing or removing one is a
        //     break, so nothing is exposed here "because it was handy".
        //     Concretely: no `unloads`, no `section`, no `read`/`reflect`
        //     closure, no Map, no DOM node ever leaves this file.
        //
        //  2. describe IS TWO-ARG. `describe(modId, key)` -- because the
        //     caller must be able to name WHOSE setting it is asking about,
        //     and the motivating caller (mod-sync) describes OTHER mods'
        //     settings, never its own. `describe(key)` is the own-mod
        //     shorthand and is chosen by ARGUMENT COUNT (exactly one), not by
        //     "the second one looks undefined": `describe(a, undefined)` is a
        //     two-arg call with no key, and answers null.
        //
        //  3. INTROSPECTION IS LOCAL-PAGE ONLY. Everything here answers about
        //     THIS page: the mods this build registered, the pins THIS broker
        //     resolved at boot, the controls mounted in THIS Control Panel.
        //     mod-sync's view of a REMOTE broker's pins still comes off the
        //     wire (GET /mods/policy) and must keep doing so -- `pinOf`
        //     replaces its loader-private `typeof _pin` sniffing for the LOCAL
        //     side only. There is no remote form of any of this, on purpose: a
        //     getter that silently answered about a different host would be the
        //     worst possible shape for a sync tool.
        //
        // ITS OWN FRAGMENT, for the reason 86e-86m each got one: 86c is at the
        // #68 2500-line per-fragment cap (_MAX_LINES, ui.py) and the rule for
        // that cap has always been "split, never trim". Ordered AFTER 86c
        // (extenders run in ui._ORDERED order and this one decorates the
        // `settings` family that fragment's makeCtx literal ships) and before
        // the mod-script splice, since a mod's init reads the finished ctx.
        // One <script>, one scope, as with every 86* companion: this file calls
        // the loader's `_pin` / `_normChoiceOptions` and 86a's `_modTextCoerce`
        // directly, and 86c's `_registerCtxExtender` / `_registerModCapability`
        // behind the usual typeof guards.
        //
        // THE STANDING QUESTION -- what a FROZEN CONTRACT has to survive that a
        // fixture never did -- answered case by case, because each case is a
        // line of code below rather than a hope:
        //
        //   * A MOD MUTATES WHAT IT GOT. Fresh + frozen, per call. The next
        //     call is unaffected because it shares nothing with the last one.
        //   * A MOD IS INSTALLED BUT NOT LOADED. list() enumerates
        //     `registered` -- what actually registered on THIS page -- so an
        //     installed package that 404'd, cycled, or registered the wrong id
        //     is ABSENT, not listed as inactive. Said plainly because it is a
        //     real limit: "not in list()" means "did not register here", and
        //     the union catalog/status model (86b) is a different question with
        //     a different answer. A frozen contract may not blur the two.
        //   * A MOD IS DISABLED BETWEEN list() AND isActive(). Both read live
        //     state at CALL time; the snapshot is a snapshot, and it is
        //     therefore normal for `list()[i].active` to be true while a later
        //     isActive() for the same id is false. Neither is cached.
        //   * describe FOR A KEY THAT DOES NOT EXIST, OR A MOD THAT DOES NOT
        //     EXIST. null in both cases -- never a throw, never a half-filled
        //     object with a made-up default. A mod that is off has no mounted
        //     control and so describes as null too, which is the same answer
        //     the Control Panel gives: there is nothing rendered to describe.
        //   * pinOf FOR AN UNKNOWN id. null. NULL MEANS "NO PIN IN FORCE", NOT
        //     "no such mod" -- an unpinned mod and an id nothing ever
        //     registered answer identically, exactly as the loader's own _pin
        //     does. Existence is list()'s question; do not read null as an
        //     answer to it.
        //   * helpCards.list() AFTER THE CONTRIBUTING MOD WAS DISABLED. The
        //     card is gone: ctx.registerHelpCards splices its own entries out
        //     on teardown (86d), so the registry a disabled mod contributed to
        //     no longer holds them and this reads the registry live.
        //
        // WHY helpCards.list() IS STRUCTURED-CLONEABLE, verified against the
        // schema rather than asserted. _sanitizeHelpCard (86d) builds every
        // entry itself: modId/slug/section/title/keys/search are String()
        // coercions and bodyFrags is an array of {t, spans:[{t, v}]} where t is
        // picked from a closed literal table and v is String()-coerced. There
        // is no path by which a function or a Node reaches an entry. This file
        // nonetheless RE-COERCES on the way out rather than trusting that,
        // because window.__mods.helpCards is a plain array anybody on the page
        // can push onto -- and the promise being made here ("this is data")
        // must hold against the bag, not merely against the sanitizer.
        //
        // The per-mod setting DESCRIPTORS, keyed modId -> key -> {type,
        // options, def}. This is a SIDE registry, populated by wrapping the
        // ctx.settings primitives as they are called, and it exists because the
        // live control entry (window.__mods.settingToggles) does NOT retain
        // either the declared option list or the declared default: it keeps a
        // `read()` closure that has already collapsed them into one effective
        // value. Reading the mounted DOM back (what mod-sync does today)
        // recovers the options and can never recover the default. So the
        // declaration is captured where it is made.
        //
        // NAMED LIMIT, so nobody trips on it later: the fallback rules below
        // are a SECOND statement of the rules inside _modSettingBoolean /
        // _modSettingChoice / _modSettingText. They are kept honest by reusing
        // those functions' own normalizers (_normChoiceOptions, _modTextCoerce,
        // MAX_MOD_TEXT_LEN) rather than re-implementing them, and by a
        // source-drift test over the duplicated fallback expressions -- but it
        // IS a duplication, and the fix if it ever bites is for the control
        // entry to carry its declaration, not for this file to guess harder.
        const _MOD_SETTING_DESCRIPTORS = new Map();   // modId -> Map(key -> data)

        // One {value,label} list -> a frozen clone. Values and labels are
        // already strings by _normChoiceOptions' contract (it THROWS on a
        // non-string value); re-coerced anyway, for the same reason the help
        // cards are.
        function _modIntrospectOptions(options) {
            const out = [];
            const raw = Array.isArray(options) ? options : [];
            for (let i = 0; i < raw.length; i++) {
                const o = raw[i];
                if (!o || typeof o !== 'object') continue;
                out.push(Object.freeze({
                    value: String(o.value == null ? '' : o.value),
                    label: String(o.label == null ? '' : o.label),
                }));
            }
            return Object.freeze(out);
        }

        // The frozen public descriptor. Built FRESH on every describe() call
        // from the stored data, so two callers never share one object.
        function _modIntrospectDescriptor(data) {
            if (!data) return null;
            return Object.freeze({
                type: data.type,
                options: _modIntrospectOptions(data.options),
                default: data.def,
            });
        }

        // What one ctx.settings.<kind>(...) call DECLARED, in the shape
        // describe answers with. Returns null when the surrounding build has no
        // normalizer to borrow (an assembly without the loader/86a is not a page
        // this can describe, and a guess would be worse than silence). Throws
        // only where the primitive itself would already have thrown -- it is
        // called AFTER the primitive returned, so in practice it cannot.
        function _modIntrospectDeclared(kind, a, b) {
            if (kind === 'boolean') {
                return { type: 'boolean', options: [], def: !!a };
            }
            if (typeof _normChoiceOptions !== 'function') return null;
            if (kind === 'text') {
                if (typeof _modTextCoerce !== 'function') return null;
                const opts = (a && typeof a === 'object') ? a : {};
                // 86a's clamp, VERBATIM (a drift test compares the two
                // expressions): an out-of-range/absent maxLength is the cap,
                // never a mod's number unchecked.
                const max = (typeof opts.maxLength === 'number'
                             && isFinite(opts.maxLength) && opts.maxLength >= 1)
                    ? Math.min(Math.floor(opts.maxLength), MAX_MOD_TEXT_LEN)
                    : MAX_MOD_TEXT_LEN;
                // text's `options` are SUGGESTIONS, not a domain -- describe
                // reports them because the pane renders them, and a caller must
                // not read them as a validation set. The type says which it is:
                // 'text' is the one kind whose options do not bound it.
                // Gated on _modChoiceFault, the PURE verdict -- not on
                // _normChoiceOptions (which throws) and not on
                // _modTextSuggestions (which warns, and would warn a second
                // time for one declaration). Since #203 a malformed
                // suggestions list costs the datalist and NOTHING else, so the
                // control is live and describable; calling the throwing
                // normalizer here left a WORKING control with no descriptor at
                // all -- the inverse of the ghost above, and just as wrong. A
                // bad list describes as no suggestions, which is what mounted.
                const suggestions = (opts.options == null)
                    ? [] : (Array.isArray(opts.options) && !opts.options.length
                            ? [] : (_modChoiceFault(opts.options)
                                    ? [] : _normChoiceOptions(opts.options)));
                const fallback = _modTextCoerce(opts.def, max);
                return { type: 'text', options: suggestions, def: fallback };
            }
            const opts = (b && typeof b === 'object') ? b : {};
            const options = _normChoiceOptions(a);
            // Object.create(null), not {}: a plain object INHERITS
            // constructor/toString/hasOwnProperty, so `valid['constructor']`
            // is truthy and describe() would report 'constructor' as the
            // default of a control whose only option is 'safe'. The membership
            // table has to contain only what was declared.
            const valid = Object.create(null);
            for (const o of options) valid[o.value] = true;
            const fallback = (typeof opts.def === 'string' && valid[opts.def] === true)
                ? opts.def : options[0].value;
            return { type: kind, options: options, def: fallback };
        }

        // Remember one mod's declaration, and arm its removal. The whole per-mod
        // map is dropped by ONE unload (pushed when the map is created), not one
        // per control: the descriptors describe controls that are themselves
        // unmounted by _controlSection's own rollback, so they die together, and
        // a mod's re-enable builds a new record and a new map from scratch.
        function _modIntrospectRecord(rec, kind, key, a, b) {
            const modId = rec && rec.id;
            if (typeof modId !== 'string' || !modId) return;
            if (typeof key !== 'string' || !key) return;
            let data = null;
            try { data = _modIntrospectDeclared(kind, a, b); }
            catch (_) { return; }
            if (!data) return;
            let byKey = _MOD_SETTING_DESCRIPTORS.get(modId);
            if (!byKey) {
                byKey = new Map();
                _MOD_SETTING_DESCRIPTORS.set(modId, byKey);
                if (rec && Array.isArray(rec.unloads)) {
                    rec.unloads.push(function () {
                        _MOD_SETTING_DESCRIPTORS.delete(modId);
                    });
                }
            }
            byKey.set(key, data);       // a re-declared key: last call wins
        }

        // The public answer. Unknown mod, unknown key, non-string either -> null.
        function _modIntrospectDescribe(modId, key) {
            if (typeof modId !== 'string' || typeof key !== 'string') return null;
            const byKey = _MOD_SETTING_DESCRIPTORS.get(modId);
            if (!byKey) return null;
            return _modIntrospectDescriptor(byKey.get(key) || null);
        }

        // ---- the read-only getters ------------------------------------------
        // Each reads window.__mods LIVE and copies out primitives. Every one is
        // total: a bag that is missing, half-built (the loader's TDZ note) or
        // holding junk answers empty rather than throwing, because a mod asking
        // a question must never be the thing that breaks.
        function _modIntrospectIsActive(id) {
            if (typeof id !== 'string' || !id) return false;
            try {
                const bag = window.__mods;
                return !!(bag && bag.active && bag.active.has(id));
            } catch (_) { return false; }
        }

        // true (pinned on) / false (pinned off) / null (no pin in force -- and
        // that is ALSO the answer for an id nothing registered; see the
        // standing-question note above). The loader's own `_pin` is the single
        // source; guarded so a build without it degrades to "nothing is pinned",
        // which is what mod-sync's _localPin does by hand today.
        function _modIntrospectPinOf(id) {
            if (typeof id !== 'string' || !id) return null;
            try {
                return (typeof _pin === 'function') ? _pin(id) : null;
            } catch (_) { return null; }
        }

        function _modIntrospectList() {
            const out = [];
            let regs = null;
            try { regs = (window.__mods && window.__mods.registered) || null; }
            catch (_) { regs = null; }
            if (!Array.isArray(regs)) return Object.freeze(out);
            for (let i = 0; i < regs.length; i++) {
                const m = regs[i];
                if (!m || typeof m.id !== 'string' || !m.id) continue;
                const tiers = [];
                const raw = Array.isArray(m.tiers) ? m.tiers : [];
                for (let j = 0; j < raw.length; j++) {
                    if (typeof raw[j] === 'string') tiers.push(raw[j]);
                }
                out.push(Object.freeze({
                    id: m.id,
                    active: _modIntrospectIsActive(m.id),
                    pin: _modIntrospectPinOf(m.id),
                    version: (typeof m.version === 'string') ? m.version : '0',
                    tiers: Object.freeze(tiers),
                }));
            }
            return Object.freeze(out);
        }

        // One sanitized help card -> a frozen data clone. See the
        // structured-clone note above for why this re-coerces instead of slicing
        // the registry: the entries ARE data by 86d's construction, and this
        // owes that promise against the bag rather than against the sanitizer.
        function _modIntrospectHelpCard(card) {
            if (!card || typeof card !== 'object') return null;
            const blocks = [];
            const rawBlocks = Array.isArray(card.bodyFrags) ? card.bodyFrags : [];
            for (let i = 0; i < rawBlocks.length; i++) {
                const blk = rawBlocks[i];
                if (!blk || typeof blk !== 'object') continue;
                const spans = [];
                const rawSpans = Array.isArray(blk.spans) ? blk.spans : [];
                for (let j = 0; j < rawSpans.length; j++) {
                    const sp = rawSpans[j];
                    if (!sp || typeof sp !== 'object') continue;
                    spans.push(Object.freeze({
                        t: String(sp.t == null ? '' : sp.t),
                        v: String(sp.v == null ? '' : sp.v),
                    }));
                }
                blocks.push(Object.freeze({
                    t: String(blk.t == null ? '' : blk.t),
                    spans: Object.freeze(spans),
                }));
            }
            return Object.freeze({
                modId: String(card.modId == null ? '' : card.modId),
                slug: String(card.slug == null ? '' : card.slug),
                section: String(card.section == null ? '' : card.section),
                title: String(card.title == null ? '' : card.title),
                bodyFrags: Object.freeze(blocks),
                keys: String(card.keys == null ? '' : card.keys),
                search: String(card.search == null ? '' : card.search),
            });
        }

        function _modIntrospectHelpCards() {
            const out = [];
            let reg = null;
            try { reg = (window.__mods && window.__mods.helpCards) || null; }
            catch (_) { reg = null; }
            if (!Array.isArray(reg)) return Object.freeze(out);
            for (let i = 0; i < reg.length; i++) {
                const c = _modIntrospectHelpCard(reg[i]);
                if (c) out.push(c);
            }
            return Object.freeze(out);
        }

        // ---- the extender ----------------------------------------------------
        // Two NEW families (`mods`, `helpCards`) and one new MEMBER of an
        // existing one (`settings.describe`), which is why only the two families
        // register a capability entry: #197's map is per FAMILY, and a mod that
        // needs the member asks for the dotted path `settings.describe`, which
        // _modCtxHas resolves against the live ctx.
        //
        // The settings primitives are WRAPPED, not reimplemented: the wrapper
        // calls the shipped primitive FIRST and records only if it RETURNED (an
        // invalid option list throws out of _normChoiceOptions and disables just
        // that mod, exactly as before -- a refused control must not leave a
        // describable ghost behind). ctx.settings is a fresh object literal per
        // makeCtx call, so wrapping it touches one mod's ctx and never core's.
        function _ctxModIntrospect(ctx, rec) {
            ctx.mods = Object.freeze({
                list: function () { return _modIntrospectList(); },
                isActive: function (id) { return _modIntrospectIsActive(id); },
                pinOf: function (id) { return _modIntrospectPinOf(id); },
            });
            ctx.helpCards = Object.freeze({
                list: function () { return _modIntrospectHelpCards(); },
            });
            const settings = ctx.settings;
            if (!settings || typeof settings !== 'object') return;
            const kinds = ['boolean', 'radio', 'select', 'combo', 'text'];
            for (const kind of kinds) {
                const orig = settings[kind];
                if (typeof orig !== 'function') continue;
                settings[kind] = (function (kind, orig) {
                    return function (key, a, b) {
                        const accessor = orig.apply(this, arguments);
                        // A DEGRADED control is not a control. Since #203 a bad
                        // options list or an async validator RETURNS instead of
                        // throwing, so 'it returned' stopped meaning 'it
                        // mounted' -- and describe() would report a full
                        // descriptor for a widget that is not on screen, which
                        // is exactly the ghost the record ordering exists to
                        // prevent. `ok === false`, never `!ok`: ok is absent on
                        // an older loader and the negation would suppress every
                        // healthy descriptor there.
                        if (accessor && accessor.ok === false) return accessor;
                        try { _modIntrospectRecord(rec, kind, key, a, b); }
                        catch (_) { /* describe is never worth a broken control */ }
                        return accessor;
                    };
                })(kind, orig);
            }
            // Own-mod shorthand by ARGUMENT COUNT (see decision 2 above).
            settings.describe = function (modId, key) {
                if (arguments.length < 2) {
                    return _modIntrospectDescribe(rec && rec.id, modId);
                }
                return _modIntrospectDescribe(modId, key);
            };
        }
        // Guarded like every other registration in an extension fragment: a page
        // assembled without #194's registry or #197's capability map must not
        // throw at load.
        if (typeof _registerCtxExtender === 'function') {
            _registerCtxExtender(_ctxModIntrospect);
        }
        if (typeof _registerModCapability === 'function') {
            _registerModCapability('mods', 1);
            _registerModCapability('helpCards', 1);
        }
        // ---- end ctx.mods / ctx.helpCards / ctx.settings.describe ------------
