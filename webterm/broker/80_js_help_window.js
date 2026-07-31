        // ---- #40/#60 in-app Help corpus (DATA) — kept in core --------------
        // The Help WINDOW (the floating searchable panel), the taskbar "?" chip,
        // the show/hide toggle, and the ctx.registerHelpCards extension point all
        // moved to mods/help/help.js (#78). What stays here is the corpus DATA
        // pipeline, because it reads CORE state: the static cards come from the
        // SINGLE-SOURCE wiki (wiki/*.md, parsed by webterm/broker/help_corpus.py
        // and served at /help-corpus.json — issue #60), and a few entries are
        // GENERATED from live state (keybindings, launch profiles, MCP status).
        // The help mod calls fetchHelpCorpus() + buildHelpEntries() (both hoisted
        // here) and merges the result with any mod-contributed cards. Card bodies
        // are typed plain-data blocks rendered by the mod's helpRenderFrags with
        // DOM APIs only (createElement + textContent), never markup.
        let helpCorpusData = null;       // the {sections:[...]} payload, once fetched
        let helpCorpusEntries = null;    // flattened wiki cards -> help entries
        let helpCorpusPromise = null;    // in-flight fetch (dedupes fast re-opens)
        // Bumped by notifyHelpHostAuth to retire everything above. A fetch that
        // was already in flight when the memo was retired must NOT write itself
        // back into it — its answer predates the token.
        let helpCorpusGen = 0;
        // The generation helpCorpusEntries was fetched at. Retiring the memo
        // makes it STALE, never ABSENT: it is still the best corpus we have and
        // buildHelpEntries must keep rendering it, but fetchHelpCorpus may no
        // longer hand it back, so a login forces exactly one refetch. Nulling
        // helpCorpusEntries instead would be a hole — see notifyHelpHostAuth.
        let helpCorpusEntriesGen = -1;
        // Flatten the wiki corpus into the flat per-card entry shape the renderer
        // groups by slug: {slug, section(label), title, bodyFrags, search}.
        function flattenHelpCorpus(data) {
            const out = [];
            if (!data || !Array.isArray(data.sections)) return out;
            for (const sec of data.sections) {
                for (const card of (sec.cards || [])) {
                    out.push({
                        slug: sec.slug, section: sec.label, title: card.title,
                        bodyFrags: Array.isArray(card.body) ? card.body : [],
                        search: card.search || '',
                        // #113: mod-owned sections carry their owning mod id (so
                        // buildHelpEntries can hide them when that mod is disabled)
                        // and an optional per-section icon. Core wiki sections have
                        // neither, so these stay '' and change nothing.
                        owner: sec.mod || '', secIcon: sec.icon || '',
                        // The section's AUDIENCE, from a wiki page's
                        // `<!-- help:tier dev -->` front matter. help_corpus.py
                        // emits the key ONLY for a developer/operator page, so a
                        // user-tier section arrives without it and lands here as
                        // ''. The help mod's renderHelpInto reads it through an
                        // allowlist (absent/'' => 'user'), so carrying it raw is
                        // enough — normalizing here would only move the default.
                        tier: sec.tier || '',
                    });
                }
            }
            return out;
        }
        // Fetch /help-corpus.json once and cache it. Resolves to the flattened
        // entries (or [] on failure, so Help still shows its live entries and
        // never goes blank). A single shared promise dedupes concurrent opens; a
        // failure clears it so a later open retries.
        function fetchHelpCorpus() {
            // Current-generation memo only: after a login the memo is still
            // renderable but no longer answers for the token we now hold.
            if (helpCorpusEntries && helpCorpusEntriesGen === helpCorpusGen)
                return Promise.resolve(helpCorpusEntries);
            if (helpCorpusPromise) return helpCorpusPromise;
            const gen = helpCorpusGen;
            // ~695 KB of wiki text — the developer/operator pages roughly
            // doubled it — and over a tailnet that is a transfer, not a round
            // trip. The default deadline would abort a corpus that was
            // mid-flight and drop Help back to its live-only entries. The
            // deadline grew with the payload: only the slowest link ever
            // notices it, and that link is the whole reason it exists.
            // (test_ui_assets checks the figure above against the real
            // help_corpus.json, so it cannot quietly go stale again.)
            helpCorpusPromise = hostFetch(localHost(), '/help-corpus.json',
                                          { timeoutMs: 15000 })
                .then(r => { if (!r.ok) throw new Error('http ' + r.status); return r.json(); })
                .then(data => {
                    // Superseded by a login while we were out. This answer
                    // predates the token, so it must neither become the memo
                    // nor be handed back as if it were current — a caller that
                    // renders what fetchHelpCorpus() RESOLVES to (the help mod
                    // re-reads the globals instead, but a mod need not) would
                    // paint the pre-login corpus. Chain onto the current
                    // generation instead: that is the memo if the replacement
                    // already landed, the replacement's promise if it is still
                    // out, and otherwise a fresh request. Bounded by the number
                    // of logins, so it cannot spin.
                    if (gen !== helpCorpusGen) return fetchHelpCorpus();
                    // Swapped in whole, never cleared-then-filled: no render can
                    // observe a half-replaced corpus.
                    helpCorpusData = data;
                    helpCorpusEntries = flattenHelpCorpus(data);
                    helpCorpusEntriesGen = gen;
                    return helpCorpusEntries;
                })
                .catch(() => {
                    // Same guard, both ways: a stale failure must not null a
                    // NEWER in-flight promise out from under its callers, and
                    // it must not be reported to its own caller as "the corpus
                    // is empty" when a current-generation answer is available.
                    if (gen !== helpCorpusGen) return fetchHelpCorpus();
                    helpCorpusPromise = null;   // cleared, so a later open retries
                    // A stale memo still beats nothing: a failed post-login
                    // refetch leaves the pre-login corpus on screen (minus the
                    // installed sections) rather than blanking the wiki.
                    return helpCorpusEntries || [];
                });
            return helpCorpusPromise;
        }
        // #173: /help-corpus.json serves the INSTALLED mods' help sections only
        // to a caller holding the token. A Help window opened before the login
        // overlay was answered therefore memoized a corpus with those sections
        // missing, and the memo lives as long as the page — so they would stay
        // missing until a reload, which is the hole #157 closed for the mod
        // pins and #163 closed for the packages themselves. Retire the memo when
        // the LOCAL broker authenticates and let the next open re-ask. Only the
        // local one: the corpus is fetched from localHost() and nowhere else.
        //
        // INVALIDATE, DO NOT CLEAR. Bumping the generation alone retires the
        // memo for fetchHelpCorpus while leaving it renderable, and that
        // distinction is load-bearing rather than tidy. The refetch is ~695 KB
        // and this same login
        // synchronously drives re-renders that read the memo
        // directly: 63 fires the async notifyModsHostAuth one line BEFORE this,
        // and its continuation (localHost /info + the installed packages, a far
        // cheaper round trip) reaches _applyPolicyLive, so any installed mod
        // whose init calls ctx.registerHelpCards lands in _refreshHelpIfOpen ->
        // refreshHelpCorpus -> buildHelpEntries. Nulling helpCorpusEntries here
        // let that snapshot an EMPTY wiki corpus into an open Help window --
        // every wiki and shipped-mod section gone until the fetch landed. The
        // stale corpus is exactly what that window was already showing, so
        // keeping it is invisible; the fetch swaps it whole on arrival.
        //
        // Nulling helpCorpusPromise as it bumps the generation is also what
        // keeps the "chain onto the current generation" branches above from
        // ever awaiting themselves: a superseded fetch can only ever find null
        // or a DIFFERENT promise there.
        function notifyHelpHostAuth(hostId) {
            let lh = null;
            try { lh = localHost(); } catch (_) {}
            if (!lh || hostId !== lh.id) return;
            helpCorpusGen++;
            helpCorpusPromise = null;
        }
        // A single-paragraph typed body block for the generated (non-wiki) entries.
        function helpTextBlock(text) {
            return { t: 'p', spans: [{ t: 'text', v: text == null ? '' : String(text) }] };
        }
        // Assemble the live corpus: wiki cards (once fetched) + generated entries,
        // then cache a lower-cased haystack on each for the substring filter.
        function buildHelpEntries() {
            // #113: hide a mod-owned section while its mod is disabled. The server
            // emits every mod section (disabled state is per-browser localStorage),
            // so the client filters here. isModEnabled is a hoisted fn declaration
            // in the same concatenated <script> (86_js_mod_loader); typeof-guarded
            // so an absent loader (or an un-owned wiki/generated entry) is untouched.
            const entries = (helpCorpusEntries || []).filter(e => !(
                e.owner && typeof isModEnabled === 'function'
                && !isModEnabled(e.owner)));
            try {
                const map = (getSettings().keybindings) || {};
                for (const act of keyActions()) {
                    const combo = map[act.id] || '';
                    const body = combo ? ('Bound to ' + combo + '.')
                        : 'Unbound - assign a key in Control Panel -> Keyboard shortcuts.';
                    entries.push({
                        slug: 'live-keyboard-shortcuts', section: 'Keyboard shortcuts',
                        title: act.label, bodyFrags: [helpTextBlock(body)],
                        keys: combo || '',
                        search: (act.label + ' ' + body).toLowerCase(),
                    });
                }
            } catch (_) {}
            try {
                const pc = profilesCache.get(localHost().id);
                if (pc && Array.isArray(pc.profiles) && pc.profiles.length) {
                    const names = pc.profiles
                        .map(p => (typeof p === 'string' ? p : (p && (p.name || p.id))))
                        .filter(Boolean);
                    if (names.length) {
                        const body = 'The + menu can launch: ' + names.join(', ') + '.'
                            + (pc.default ? ' Default profile: ' + pc.default + '.' : '');
                        entries.push({
                            slug: 'launching', section: 'Launching',
                            title: 'Terminal profiles (this host)',
                            bodyFrags: [helpTextBlock(body)],
                            search: ('Terminal profiles (this host) ' + body).toLowerCase(),
                        });
                    }
                }
            } catch (_) {}
            try {
                const m = mcpConfigCache.get(localHost().id);
                if (m) {
                    const body = 'MCP is currently ' + (m.enabled ? 'ENABLED' : 'disabled')
                        + '; default mode for new windows: ' + (m.default_mode || 'off')
                        + '; launching via MCP is ' + (m.allow_launch ? 'allowed' : 'blocked')
                        + '. Change these in Control Panel -> MCP.';
                    entries.push({
                        slug: 'mcp-and-ai-agents', section: 'MCP & AI Agents',
                        title: 'MCP status (this host)',
                        bodyFrags: [helpTextBlock(body)],
                        search: ('MCP status (this host) ' + body).toLowerCase(),
                    });
                }
            } catch (_) {}
            for (const e of entries) {
                e._hay = ((e.search || '') + ' ' + (e.section || '') + ' '
                    + (e.keys || '')).toLowerCase();
            }
            return entries;
        }
