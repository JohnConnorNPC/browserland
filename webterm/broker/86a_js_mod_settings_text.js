        // ---- mod settings: the free-text primitive (#168) -------------------
        // Split out of 86_js_mod_loader.js, which had grown past the 2500-line
        // per-fragment cap (#68's guard). Assembly puts this fragment in the SAME
        // <script> immediately after the loader and before the mod scripts, so
        // everything here shares one scope with _controlSection / _trackControl /
        // _valueAccessor / _normChoiceOptions up there and with MAX_MOD_TEXT_LEN
        // in 50 — and nothing here runs until a mod's init calls it.
        // ---- #168: free text, the one settings primitive with no domain ------
        // Everything above is choice-constrained, INCLUDING combo — its <input
        // list> only looks free, its accessor rejects anything outside the
        // declared set. That is wrong whenever the legal set is not knowable
        // here: `clockTz`'s options come from Intl.supportedValuesOf, so a zone
        // Chrome enumerates and another engine does not is refused by the picker
        // even though that engine would have RENDERED it happily.
        //
        // Three rules make an unconstrained value safe in a blob every browser
        // pulls, and they are the whole of this primitive:
        //
        //  1. read() is STRUCTURAL, never domain-checked, and never writes. A
        //     string within the cap and free of control characters is returned
        //     AS IS — including one this build's own validator would refuse.
        //     That is the bug fix: an upgrade, a different engine or a stricter
        //     validator can no longer make a stored value evaporate, and because
        //     read() never writes, the blob keeps it even while a fallback shows.
        //  2. coerce() gates what WE write: String -> strip control chars ->
        //     drop unpaired surrogates -> trim -> cap, the treatment core gives
        //     startLabel/startPath (55:146, 160). The cap counts UTF-16 code
        //     units, which is the unit mod-sync's STR_MAX counts, so mod-sync's
        //     own scalar bound can never drop what we store. That bounds the
        //     CARRIER, not a peer: a peer whose control asks for a smaller cap,
        //     or still uses combo, is entitled to refuse what we send — and its
        //     acceptedBy says so out loud rather than planting it.
        //  3. validate is WRITE-ONLY and its rejection is VISIBLE. A silent drop
        //     is invisible for a checkbox and user-hostile for a text box (you
        //     type, it vanishes, nothing says why), so the widget keeps the
        //     rejected draft on screen next to a .set-err message and does not
        //     touch the stored value.
        //
        // Every surrogate properly paired. A scan rather than a regex because
        // the regex forms need lookbehind (ES2018) or isWellFormed (ES2024), and
        // an engine old enough to lack those is exactly the engine this
        // primitive exists to keep working.
        function _modTextPaired(s) {
            for (let i = 0; i < s.length; i++) {
                const c = s.charCodeAt(i);
                if (c < 0xD800 || c > 0xDFFF) continue;
                if (c > 0xDBFF) return false;                     // lone low
                const n = s.charCodeAt(i + 1);
                if (!(n >= 0xDC00 && n <= 0xDFFF)) return false;  // lone high
                i++;
            }
            return true;
        }
        // Drop unpaired surrogates. A lone surrogate is not text: it survives
        // JSON only as an escape, renders as a replacement glyph and could never
        // be retyped — junk whether it arrived that way or was made by slicing a
        // pair in half at the cap. Allocation-free when already clean.
        function _modTextDropLone(s) {
            if (_modTextPaired(s)) return s;
            let out = '';
            for (let i = 0; i < s.length; i++) {
                const c = s.charCodeAt(i);
                if (c >= 0xD800 && c <= 0xDBFF) {
                    const n = s.charCodeAt(i + 1);
                    if (n >= 0xDC00 && n <= 0xDFFF) {
                        out += s.charAt(i) + s.charAt(i + 1);
                        i++;
                    }
                    continue;                                     // else drop it
                }
                if (c >= 0xDC00 && c <= 0xDFFF) continue;         // lone low
                out += s.charAt(i);
            }
            return out;
        }
        // The structural gate: a bounded, control-char-free, well-formed string.
        // Shared by read() (applied to a peer's value on every /state pull) and by
        // mods/mod-sync's acceptedBy, which must answer "would the live control
        // actually take this?" for a kind that has no enumerable option set.
        function _modTextOk(v, max) {
            const cap = (typeof max === 'number' && max > 0
                         && max <= MAX_MOD_TEXT_LEN) ? max : MAX_MOD_TEXT_LEN;
            if (typeof v !== 'string' || v.length > cap) return false;
            // Control chars are C0 + DEL only: those break a one-line input, a
            // chip's textContent and a log line. Everything else (astral text,
            // RTL marks, combining accents) is somebody's legitimate label.
            if (/[\u0000-\u001F\u007F]/.test(v)) return false;
            return _modTextPaired(v);
        }
        // What WE are willing to write. Total: an object whose toString throws
        // yields '' rather than letting the exception out of a settings write.
        // (String(aSymbol) does NOT throw — only implicit coercion does — so a
        // Symbol lands as its own description text, bounded like any string.)
        function _modTextCoerce(v, max) {
            let s;
            try { s = (v == null) ? '' : String(v); } catch (_) { return ''; }
            s = _modTextDropLone(s.replace(/[\u0000-\u001F\u007F]/g, '')).trim();
            // Slicing by code unit can cut a surrogate PAIR in half, so re-drop
            // after the cut; dropping only shortens, so the cap still holds.
            if (s.length > max) s = _modTextDropLone(s.slice(0, max)).trim();
            return s;
        }
        // Free text bound to a synced settings key, with an OPTIONAL suggestion
        // datalist — a superset of combo, whose list is a DOMAIN where this one
        // is only a shortcut. opts:
        //   label, title, isBrowserGlobal, mount   as every other primitive
        //   def          fallback when nothing valid is stored (coerced; '')
        //   options      [{value,label}] suggestions, or omitted for a bare box.
        //                Malformed entries throw (the family's contract); an
        //                empty/absent list just means no datalist, because a mod
        //                computing its suggestions may honestly produce none.
        //   placeholder  shown when the box is empty. Falls back to the label of
        //                an ''-valued option, exactly like combo.
        //   maxLength    clamped into (0, MAX_MOD_TEXT_LEN]
        //   validate(v)  WRITE-ONLY, and SYNCHRONOUS. Return `false`, or a
        //                non-empty string, to REJECT (the string is the message
        //                the user sees); return anything else — `true`, or
        //                nothing — to accept. A throw, and a thenable (an `async`
        //                validator, whose Promise is truthy and would otherwise
        //                sail through as acceptance), both fail CLOSED. It runs
        //                on every write attempt, and more than once per commit,
        //                so keep it pure and cheap.
        function _modSettingText(rec, key, opts) {
            opts = opts || {};
            // Every piece of mutable state is function-local and initialized
            // BEFORE anything can call the closures below — a hoisted function
            // reading a not-yet-initialized `let` is a TDZ ReferenceError that
            // would disable the whole mod, and UI JS never runs in CI.
            let timer = null;          // debounce handle; non-null == pending
            let drafting = false;      // a rejected draft is on screen
            let rejectMsg = '';        // set by valid(), read by commit()
            const max = (typeof opts.maxLength === 'number'
                         && isFinite(opts.maxLength) && opts.maxLength >= 1)
                ? Math.min(Math.floor(opts.maxLength), MAX_MOD_TEXT_LEN)
                : MAX_MOD_TEXT_LEN;
            // #203 (§1): a malformed suggestions list no longer throws out of
            // registration and kills the mod — it costs the datalist and logs a
            // warning. See _modTextSuggestions at the foot of this fragment.
            const suggestions = (opts.options == null)
                ? [] : (Array.isArray(opts.options) && !opts.options.length
                        ? [] : _modTextSuggestions(rec, key, opts.options));
            const fallback = _modTextCoerce(opts.def, max);
            const userValidate = (typeof opts.validate === 'function')
                ? opts.validate : null;
            // Non-destructive read-through, structural ONLY (rule 1 above).
            const read = function () {
                const v = getSettings()[key];
                return _modTextOk(v, max) ? v : fallback;
            };
            // '' == accept, else the message to show. Never throws.
            const check = function (v) {
                if (!_modTextOk(v, max)) return 'value is too long, or holds '
                    + 'characters that cannot be stored';
                if (!userValidate) return '';
                let r;
                try { r = userValidate(v); }
                catch (e) {
                    console.error('[mods] settings text validate threw ("'
                        + rec.id + ':' + key + '"):', e);
                    return 'that value could not be checked';
                }
                if (r && typeof r.then === 'function') {
                    console.error('[mods] settings text validate is async ("'
                        + rec.id + ':' + key + '"): a Promise is truthy, so an '
                        + 'async validator would accept everything');
                    return 'that value could not be checked';
                }
                if (r === false) return 'that value was not accepted';
                if (typeof r === 'string' && r) return r;
                return '';
            };
            const section = _controlSection(rec, opts);
            const row = document.createElement('div');
            row.className = 'set-row';
            if (opts.label) {
                const lab = document.createElement('label');
                lab.textContent = opts.label;
                row.appendChild(lab);
            }
            const input = document.createElement('input');
            input.type = 'text';
            input.maxLength = max;      // same unit as the cap: UTF-16 code units
            if (typeof opts.placeholder === 'string') {
                input.placeholder = opts.placeholder;
            }
            if (suggestions.length) {
                const uid = 'set-mod-' + rec.id + '-' + key + '-list';
                const datalist = document.createElement('datalist');
                datalist.id = uid;
                for (const o of suggestions) {
                    if (o.value === '') {
                        if (!input.placeholder) input.placeholder = o.label;
                        continue;            // no empty datalist row (combo parity)
                    }
                    const op = document.createElement('option');
                    op.value = o.value;
                    if (o.label !== o.value) op.label = o.label;
                    datalist.appendChild(op);
                }
                if (datalist.firstChild) {
                    input.setAttribute('list', uid);
                    row.appendChild(datalist);
                }
            }
            row.appendChild(input);
            section.appendChild(row);
            const err = document.createElement('div');
            err.className = 'set-err';   // 15_css_dialogs.css:83 — .show reveals it
            section.appendChild(err);
            function setErr(msg) {
                drafting = !!msg;
                err.textContent = msg || '';
                if (msg) err.classList.add('show');
                else err.classList.remove('show');
            }
            // Combo's focus guard, for its reason: a /state convergence must not
            // clobber an in-progress edit. It is the ONLY guard. A rejected draft
            // deliberately does NOT block a reflect: notifyModSettings sets
            // entry.last BEFORE calling us, so a skip here is permanent — every
            // later poll sees cur === last and never retries, leaving the box
            // showing a draft that will never be stored while the mod has already
            // moved to the new value. New truth wins; the draft survives blur
            // (below), which is where the user actually needs it.
            function reflect() {
                if (document.activeElement === input) return;
                setErr('');
                input.value = read();
            }
            const entry = {
                modId: rec.id, kind: 'text', key: key, read: read,
                onChange: null, last: read(), section: section, reflect: reflect,
                maxLength: max,          // mod-sync mirrors read()'s gate with it
            };
            // valid() doubles as the rejection CHANNEL for the WIDGET: it is the
            // one place that sees the coerced value the shared writer will judge,
            // so recording the verdict here lets commit() show it without
            // _valueAccessor changing at all. A mod calling accessor.set()
            // directly is still a silent drop, exactly like every other
            // primitive — painting an error beside a box that shows a different
            // value would explain nothing; a mod validates its own writes.
            //
            // `unchanged` (5th arg, #168) replaces the shared writer's default
            // read()-equality for this kind ONLY. read() answers with the
            // FALLBACK for a structurally broken stored value, so the default
            // would make "clear the box" a no-op against exactly the junk that
            // most needs clearing — a peer's over-long string, a hand-edited
            // blob — leaving it in the synced blob to be re-pushed by every
            // savePrefs. Comparing against the RAW value repairs it, and an
            // explicit commit is not the silent destruction read() forbids. The
            // second clause keeps the family's "a read never seeds the default"
            // property: with nothing stored, committing the default writes
            // nothing.
            const accessor = _valueAccessor(entry, key, read,
                function (v) { return _modTextCoerce(v, max); },
                function (v) { rejectMsg = check(v); return rejectMsg === ''; },
                function (v) {
                    const raw = getSettings()[key];
                    return raw === v || (raw === undefined && v === fallback);
                });
            function commit() {
                if (timer) { clearTimeout(timer); timer = null; }
                setErr('');              // BEFORE set(), so its reflect() isn't draft-guarded
                rejectMsg = '';
                accessor.set(input.value);
                if (rejectMsg) setErr(rejectMsg);   // keep the draft, say why
            }
            // Core's start-path cadence (81:1477-1484): debounced on 'input' so
            // typing does not push /state per keystroke, flushed on 'change'
            // (blur / Enter) so an edit is never left hanging.
            input.addEventListener('input', function () {
                setErr('');              // a fresh draft supersedes the last verdict
                if (timer) clearTimeout(timer);
                timer = setTimeout(commit, 400);
            });
            input.addEventListener('change', function () {
                commit();
                if (!drafting) input.value = read();   // show what was STORED
            });
            // Blur reconcile (combo's, and for its reason): reflect() skips a
            // convergence that lands mid-edit, so an edit ending without a change
            // event would leave a remote value visually stale.
            //
            // The flush comes FIRST and is not optional. Without it this handler
            // is a data-loss path: with a commit still pending it would overwrite
            // the box with the OLD stored value, and the timer would then commit
            // that — silently reverting the edit. `change` normally fires before
            // blur and clears the timer, but "normally" is not a contract worth
            // betting an edit on (a programmatic blur, a section hidden by a tab
            // switch).
            input.addEventListener('blur', function () {
                if (timer) commit();
                if (drafting) return;    // the draft is the user's only copy
                input.value = read();
            });
            // The page going away is the one flush teardown cannot cover: a
            // reload or a tab close inside the 400 ms window would otherwise drop
            // the keystroke entirely. savePrefs writes localStorage
            // synchronously, so the value survives locally even when the /state
            // PUT does not get to leave. Removed with the mod.
            const onPageHide = function () { if (timer) commit(); };
            window.addEventListener('pagehide', onPageHide);
            rec.unloads.push(function () {
                window.removeEventListener('pagehide', onPageHide);
            });
            entry.reflect();             // sync widget to the current value
            _trackControl(rec, entry);
            // Flush on teardown. The VALUE outlives the mod (that is the whole
            // read-through contract), so a keystroke still inside the 400 ms
            // window is data loss. Pushed AFTER _trackControl so the LIFO drain
            // (_runUnloads) runs it FIRST — while the entry is still tracked, so
            // the write updates entry.last and no convergence re-fires it.
            rec.unloads.push(function () { if (timer) commit(); });
            return accessor;
        }


        // ---- #203 (§1): a malformed option list degrades, it does not kill ---
        // Until now _normChoiceOptions THREW on an empty/invalid/duplicate list,
        // the throw escaped registration, and initMod's fault isolation rolled
        // the WHOLE mod back — one duplicate string in a computed list disabled
        // everything the mod does. clock ships defensive dedup (clock.js:130-134)
        // against exactly that, which is this platform's bug, not clock's.
        //
        // So the shape errors of an OPTION LIST — and only those — stop being
        // fatal. The primitive mounts no widget and hands back a DEGRADED
        // accessor; the mod keeps running and decides for itself whether a dead
        // control is survivable:
        //
        //   s.ok            false (true on a healthy control)
        //   s.error         'duplicate_option' | 'invalid_options'
        //   s.get()         the coerced def, else '' — never throws
        //   s.set(v)        no-op (the family's existing silent-drop idiom)
        //   s.onChange(fn)  accepted, never fires
        //
        // FEATURE-DETECT WITH `s.ok === false`, NEVER `!s.ok`. An older loader
        // has no `ok` at all, so `!s.ok` reads every HEALTHY accessor on that
        // broker as rejected and would degrade a mod that is working fine.
        //
        // What stays fatal is unchanged and deliberate: a throw from the mod's
        // own init(), ModConflictError on a duplicate id, and the ctxVersion
        // refusal. Those are the isolation contract, not sharp edges.
        //
        // These are function DECLARATIONS with no fragment-level let/const
        // between them: an init-time path that reads a not-yet-initialized
        // fragment binding is a TDZ ReferenceError that disables the whole mod,
        // and this JS never runs in CI (#162).

        // The shape verdict on an options list: '' when it is fine, else the
        // STABLE code both `s.error` and the Mods-pane warning row carry. One
        // vocabulary on both sides, so what the operator reads is what the mod
        // branched on. It mirrors _normChoiceOptions' three throws exactly and
        // never throws itself. ('async_validator' is #203 §2's code and is not
        // decided here.)
        function _modChoiceFault(options) {
            if (!Array.isArray(options) || !options.length) {
                return 'invalid_options';
            }
            const seen = Object.create(null);
            for (const o of options) {
                if (!o || typeof o.value !== 'string') return 'invalid_options';
                if (seen[o.value] === true) return 'duplicate_option';
                seen[o.value] = true;
            }
            return '';
        }

        // Human text for a code, at the one place that owns the vocabulary.
        function _modChoiceFaultText(code) {
            return (code === 'duplicate_option')
                ? 'the options list repeats a value'
                : 'the options list must be a non-empty array of '
                  + '{value,label} with string values';
        }

        // Record a degraded control against the mod's ACTIVE record. Living on
        // `rec` rather than in a keyed bag is the whole teardown story: disableMod
        // drops the record from window.__mods.active, so the warning disappears
        // with the mod and a re-init starts from a clean list — no unload hook to
        // forget and no stale row to explain. console.error still fires, because
        // the console is where a developer looks first.
        function _modSettingWarn(rec, key, code, message) {
            console.error('[mods] settings "' + rec.id + ':' + key + '" — '
                + message + ' (' + code + '); this control is degraded, the '
                + 'rest of the mod keeps running');
            if (!Array.isArray(rec.settingWarnings)) rec.settingWarnings = [];
            // Bounded: a mod looping over a broken computed list must not be
            // able to grow an unbounded array behind a pane nobody has open.
            if (rec.settingWarnings.length < 20) {
                rec.settingWarnings.push(
                    { key: key, code: code, message: message });
            }
        }

        // The degraded accessor. get() answers the declared default put through
        // the same coercion every stored string gets (so a non-string `def`
        // answers '' rather than leaking a number into a mod's string path), and
        // it is a CONSTANT: with no widget and no writer there is nothing that
        // could change it.
        function _modDegradedAccessor(code, def) {
            const value = _modTextCoerce(def, MAX_MOD_TEXT_LEN);
            const accessor = {
                ok: false,
                error: code,
                get: function () { return value; },
                set: function () {},                 // silent drop, as ever
                onChange: function () { return accessor; },
            };
            return accessor;
        }

        // The seam radio/select/combo call FIRST, before a single element is
        // created: a rejected control must mount NO widget in the Control Panel.
        // Returns null when the list is fine (the caller carries on and builds
        // the real control), else the degraded accessor to hand straight back.
        function _modSettingReject(rec, kind, key, options, opts) {
            const code = _modChoiceFault(options);
            if (!code) return null;
            _modSettingWarn(rec, key, code,
                'settings ' + kind + ': ' + _modChoiceFaultText(code));
            return _modDegradedAccessor(code, opts && opts.def);
        }

        // text degrades DIFFERENTLY, and that asymmetry is the point: only its
        // optional SUGGESTIONS list can be malformed, and suggestions are a
        // shortcut, not a domain. A bad list there costs the datalist and earns a
        // warning — the accessor stays fully functional (ok stays true), because
        // a free-text box with no suggestions is still a working free-text box.
        function _modTextSuggestions(rec, key, options) {
            if (options == null) return [];
            if (Array.isArray(options) && !options.length) return [];
            const code = _modChoiceFault(options);
            if (code) {
                _modSettingWarn(rec, key, code,
                    'settings text suggestions: ' + _modChoiceFaultText(code)
                    + ' — the box works, without a suggestion list');
                return [];
            }
            return _normChoiceOptions(options);
        }
