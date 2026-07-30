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
        // was already in flight when the memo was dropped must NOT write itself
        // back into it — its answer predates the token.
        let helpCorpusGen = 0;
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
            if (helpCorpusEntries) return Promise.resolve(helpCorpusEntries);
            if (helpCorpusPromise) return helpCorpusPromise;
            const gen = helpCorpusGen;
            // ~200 KB of wiki text, and over a tailnet that is a transfer, not a
            // round trip. The default deadline would abort a corpus that was
            // mid-flight and drop Help back to its live-only entries.
            helpCorpusPromise = hostFetch(localHost(), '/help-corpus.json',
                                          { timeoutMs: 8000 })
                .then(r => { if (!r.ok) throw new Error('http ' + r.status); return r.json(); })
                .then(data => {
                    const entries = flattenHelpCorpus(data);
                    // Superseded by a login while we were out: hand this answer
                    // to whoever asked, but do not let it become the memo — the
                    // request that replaced it is the one carrying the token.
                    if (gen !== helpCorpusGen) return entries;
                    helpCorpusData = data;
                    helpCorpusEntries = entries;
                    return entries;
                })
                .catch(() => {
                    // Same guard: a stale failure must not null a NEWER
                    // in-flight promise out from under its callers.
                    if (gen === helpCorpusGen) helpCorpusPromise = null;
                    return [];
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
        function notifyHelpHostAuth(hostId) {
            let lh = null;
            try { lh = localHost(); } catch (_) {}
            if (!lh || hostId !== lh.id) return;
            helpCorpusGen++;
            helpCorpusData = null;
            helpCorpusEntries = null;
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
