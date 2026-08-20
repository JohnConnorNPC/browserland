        // ---- mod help cards: the sanitizer + the registry (#78 / S5) --------
        // Split out of 86_js_mod_loader.js VERBATIM (#194), which had reached the
        // 2500-line per-fragment cap (#68's guard) — the same split 86a (#168)
        // and 86b (#163) got, and for the same reason. Assembly puts this
        // fragment in the SAME <script> after the loader and before the mod
        // scripts, so everything here shares one scope with makeCtx /
        // setModEnabled / _applyPolicyLive up there (which call
        // _refreshHelpIfOpen) and with 86b's _lateRegister — and nothing here
        // runs at load: they are all declarations, reached only when a mod's
        // init calls ctx.registerHelpCards or a mod is toggled.
        //
        // The family is self-contained by construction: the sanitizers are pure
        // functions over plain data, _modRegisterHelpCards takes the per-mod
        // record as an ARGUMENT (a companion fragment cannot see the loader's
        // per-mod locals), and _refreshHelpIfOpen only typeof-guards two hoisted
        // help-mod globals. Nothing below reads a `const` declared in the loader.

        // ---- Help-card contribution (#78 / S5) ------------------------------
        // ctx.registerHelpCards sanitizes mod-supplied cards to the SAME typed
        // block/span schema the wiki corpus uses, so a contributed card can only
        // ever be rendered as text nodes (the help renderer is textContent-only)
        // — never raw HTML. Unknown block/span types degrade to the nearest safe
        // type; every value is coerced to String. The sanitized entries live on
        // window.__mods.helpCards (the Help mod merges them with the core
        // corpus) and are removed on the contributing mod's teardown.
        const _HELP_BLOCK_TYPES = { p: 1, bullet: 1, sub: 1, pre: 1 };
        const _HELP_SPAN_TYPES = { text: 1, strong: 1, code: 1, kbd: 1 };
        function _sanitizeHelpSpan(sp) {
            if (!sp || typeof sp !== 'object') return null;
            const t = _HELP_SPAN_TYPES[sp.t] ? sp.t : 'text';
            return { t: t, v: sp.v == null ? '' : String(sp.v) };
        }
        function _sanitizeHelpBlock(blk) {
            if (!blk || typeof blk !== 'object') return null;
            const t = _HELP_BLOCK_TYPES[blk.t] ? blk.t : 'p';
            const spans = [];
            const raw = Array.isArray(blk.spans) ? blk.spans : [];
            for (let i = 0; i < raw.length; i++) {
                const s = _sanitizeHelpSpan(raw[i]);
                if (s) spans.push(s);
            }
            return { t: t, spans: spans };
        }
        function _sanitizeHelpBlocks(body) {
            const out = [];
            const raw = Array.isArray(body) ? body : [];
            for (let i = 0; i < raw.length; i++) {
                const b = _sanitizeHelpBlock(raw[i]);
                if (b) out.push(b);
            }
            return out;
        }
        // One card -> a normalized Help entry, or null when it lacks a title (the
        // minimum to render). `search` defaults to the title/section/keys + all
        // sanitized body text, lower-cased, so a contributed card is discoverable
        // by its body even when the mod omits an explicit search string.
        function _sanitizeHelpCard(card, modId) {
            if (!card || typeof card !== 'object') return null;
            const title = card.title == null ? '' : String(card.title);
            if (!title) return null;
            const slug = card.slug == null ? ('mod-' + modId) : String(card.slug);
            const section = card.section == null ? (slug || modId) : String(card.section);
            const keys = card.keys == null ? '' : String(card.keys);
            const bodyFrags = _sanitizeHelpBlocks(
                card.body != null ? card.body : card.bodyFrags);
            let search = card.search == null ? '' : String(card.search);
            if (!search) {
                const parts = [title, section, keys];
                for (const b of bodyFrags) for (const s of b.spans) parts.push(s.v);
                search = parts.join(' ');
            }
            return { modId: modId, slug: slug, section: section, title: title,
                     bodyFrags: bodyFrags, keys: keys, search: search.toLowerCase() };
        }
        // Re-render the live Help window (if any) so newly (un)registered cards
        // appear without a reopen.
        //
        // #199/#213: through the COMMAND, not by hoisted name. This used to
        // call findHelpWindow()/refreshHelpCorpus() behind a typeof guard --
        // and a typeof guard answers "did a fragment declaring that name
        // evaluate?", which is TRUE for a help mod that was switched off ten
        // minutes ago, so the call landed in a torn-down closure. Executing
        // 'help:refresh-corpus' resolves 'inactive' for exactly that case and
        // 'absent' when the mod is not installed at all. execute() never
        // rejects, so there is no unhandled rejection to leak either; the
        // typeof guard here is on the DISPATCHER, for a page assembled without
        // 86h.
        function _refreshHelpIfOpen() {
            try {
                if (typeof _execModCommand === 'function') {
                    _execModCommand('help:refresh-corpus', null);
                }
            } catch (_) {}
        }
        function _modRegisterHelpCards(rec, cards) {
            if (!Array.isArray(window.__mods.helpCards)) window.__mods.helpCards = [];
            const list = Array.isArray(cards) ? cards : [cards];
            const added = [];
            for (let i = 0; i < list.length; i++) {
                const norm = _sanitizeHelpCard(list[i], rec.id);
                if (norm) { window.__mods.helpCards.push(norm); added.push(norm); }
            }
            // Forget exactly these entries on teardown (the DOM is re-rendered by
            // _refreshHelpIfOpen), then refresh the open Help window.
            rec.unloads.push(function () {
                const reg = window.__mods.helpCards || [];
                for (const e of added) {
                    const idx = reg.indexOf(e);
                    if (idx !== -1) reg.splice(idx, 1);
                }
                _refreshHelpIfOpen();
            });
            _refreshHelpIfOpen();
            return added.length;
        }
