        // ---- #40 in-app Help: a floating, searchable reference window -------
        // The "?" taskbar chip / the toggle-help hotkey / the (+) menu open a
        // single floating Help app window (appKind 'help', ephemeral like the
        // task manager). The static cards come from the SINGLE-SOURCE wiki
        // (wiki/*.md, parsed by webterm/broker/help_corpus.py and served at
        // /help-corpus.json - issue #60); a few entries are still GENERATED from
        // live state (keybindings, launch profiles, MCP status). Card bodies are
        // typed plain-data blocks rendered by helpRenderFrags with DOM APIs only
        // (createElement + textContent), never markup. Search is a
        // case-insensitive substring over each card's precomputed search text
        // plus its section label + keys.
        let helpCorpusData = null;       // the {sections:[...]} payload, once fetched
        let helpCorpusEntries = null;    // flattened wiki cards -> help entries
        let helpCorpusPromise = null;    // in-flight fetch (dedupes fast re-opens)
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
            helpCorpusPromise = fetch(hostHttpUrl(localHost(), '/help-corpus.json'))
                .then(r => { if (!r.ok) throw new Error('http ' + r.status); return r.json(); })
                .then(data => {
                    helpCorpusData = data;
                    helpCorpusEntries = flattenHelpCorpus(data);
                    return helpCorpusEntries;
                })
                .catch(() => { helpCorpusPromise = null; return []; });
            return helpCorpusPromise;
        }
        // A single-paragraph typed body block for the generated (non-wiki) entries.
        function helpTextBlock(text) {
            return { t: 'p', spans: [{ t: 'text', v: text == null ? '' : String(text) }] };
        }
        // Assemble the live corpus: wiki cards (once fetched) + generated entries,
        // then cache a lower-cased haystack on each for the substring filter.
        function buildHelpEntries() {
            const entries = (helpCorpusEntries || []).slice();
            try {
                const map = (getSettings().keybindings) || {};
                for (const act of KEY_ACTIONS) {
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
        // Per-section rail/header glyph, keyed by section SLUG (the stable id),
        // never the display label. Unknown sections fall back to a neutral dot.
        const HELP_SECTION_ICONS = {
            'getting-started': '★', 'keyboard-shortcuts': '⌨',
            'window-modes': '▦', 'arranging-windows': '❐',
            'columns-and-widths': '▥', 'snapping-and-pop-out': '⤢',
            'floating-window-controls': '❖', 'workspaces': '⊞',
            'taskbar': '▭', 'context-menus': '☰', 'window-types': '＋',
            'hosts-and-multi-browser': '⌂', 'mcp-and-ai-agents': '◆',
            // Generated (non-wiki) sections:
            'live-keyboard-shortcuts': '⌨', 'launching': '＋',
        };
        function helpSectionIcon(slug) {
            return Object.prototype.hasOwnProperty.call(HELP_SECTION_ICONS, slug)
                ? HELP_SECTION_ICONS[slug] : '•';
        }
        // Append `text` to `el`, wrapping case-insensitive matches of `q` in a
        // <mark> so the user sees WHY a result matched. Text nodes only (never
        // innerHTML) so help prose can't inject markup.
        function helpAppendHighlighted(el, text, q) {
            text = text == null ? '' : String(text);
            if (!q) { el.appendChild(document.createTextNode(text)); return; }
            const hay = text.toLowerCase();
            let pos = 0, idx;
            while ((idx = hay.indexOf(q, pos)) !== -1) {
                if (idx > pos) el.appendChild(document.createTextNode(text.slice(pos, idx)));
                const mark = document.createElement('mark');
                mark.className = 'help-mark';
                mark.textContent = text.slice(idx, idx + q.length);
                el.appendChild(mark);
                pos = idx + q.length;
            }
            if (pos < text.length) el.appendChild(document.createTextNode(text.slice(pos)));
        }
        // Render a typed-fragment card body (from the wiki corpus) into `el`
        // using DOM APIs only: each block is a paragraph / bullet / sub-heading
        // of typed inline spans (text / strong / code / kbd). Matches of `q` are
        // wrapped via helpAppendHighlighted. This is the XSS-safety boundary —
        // wiki text is only ever added as text nodes, never parsed as markup.
        function helpRenderFrags(el, blocks, q) {
            if (!Array.isArray(blocks)) return;
            for (const blk of blocks) {
                const line = document.createElement('div');
                line.className = 'help-b help-b-' +
                    (blk.t === 'bullet' ? 'li' : blk.t === 'sub' ? 'sub' : 'p');
                if (blk.t === 'bullet') {
                    const dot = document.createElement('span');
                    dot.className = 'help-b-dot'; dot.textContent = '• ';
                    line.appendChild(dot);
                }
                for (const sp of (blk.spans || [])) {
                    if (sp.t === 'strong') {
                        const node = document.createElement('strong');
                        helpAppendHighlighted(node, sp.v, q); line.appendChild(node);
                    } else if (sp.t === 'code') {
                        const node = document.createElement('code');
                        node.className = 'help-code';
                        helpAppendHighlighted(node, sp.v, q); line.appendChild(node);
                    } else if (sp.t === 'kbd') {
                        const node = document.createElement('span');
                        node.className = 'help-kbd';
                        node.textContent = sp.v == null ? '' : String(sp.v);
                        line.appendChild(node);
                    } else {
                        helpAppendHighlighted(line, sp.v, q);
                    }
                }
                el.appendChild(line);
            }
        }
        function markHelpSeen() {
            try {
                const s = getSettings();
                if (!s.helpHintSeen) { s.helpHintSeen = true; savePrefs(); }
            } catch (_) {}
        }
        // Scroll-spy: highlight the rail button for whichever section currently
        // sits at the top of the scroll viewport.
        function updateHelpActiveSection(win) {
            const h = win._help;
            if (!h || !h.sectionEls || !h.sectionEls.length) return;
            const top = h.scrollEl.scrollTop;
            let activeId = h.sectionEls[0].id;
            for (const s of h.sectionEls) {
                if (s.offsetTop - 16 <= top) activeId = s.id; else break;
            }
            for (const btn of h.railEl.querySelectorAll('.help-rail-btn')) {
                btn.classList.toggle('active', btn.dataset.target === activeId);
            }
        }
        // Render the filtered corpus into a Help window: left rail (one button per
        // section, click-to-jump + scroll-spy), the scrollable card grid (sticky
        // section headers), and the result count. Reads win._helpCorpus (snapshotted
        // on open) so a keystroke filters the snapshot, never rebuilds live state.
        function renderHelpInto(win, query) {
            const h = win._help; if (!h) return;
            const raw = query || '';
            const q = raw.trim().toLowerCase();
            const entries = (win._helpCorpus || []).filter(
                e => !q || (e._hay || '').indexOf(q) !== -1);
            h.clearBtn.classList.toggle('show', !!raw.length);
            // Group by section SLUG (stable id), remembering each slug's display
            // label, preserving first-seen order.
            const order = []; const bySlug = new Map(); const labelOf = new Map();
            for (const e of entries) {
                if (!bySlug.has(e.slug)) {
                    bySlug.set(e.slug, []); labelOf.set(e.slug, e.section || e.slug);
                    order.push(e.slug);
                }
                bySlug.get(e.slug).push(e);
            }
            const n = entries.length;
            h.countEl.textContent = q
                ? (n === 0 ? 'No results' : n === 1 ? '1 result' : n + ' results')
                : (n + ' topics');
            h.railEl.textContent = '';
            h.scrollEl.textContent = '';
            h.sectionEls = [];
            if (!order.length) {
                const empty = document.createElement('div');
                empty.className = 'help-empty';
                const strong = document.createElement('strong');
                strong.textContent = 'No matches';
                empty.appendChild(strong);
                empty.appendChild(document.createTextNode(
                    'Try a feature, command, profile, or key name.'));
                h.scrollEl.appendChild(empty);
                return;
            }
            order.forEach((slug, i) => {
                const secId = 'hsec-' + i;
                const label = labelOf.get(slug);
                const rb = document.createElement('button');
                rb.type = 'button';
                rb.className = 'help-rail-btn';
                rb.dataset.target = secId;
                const ric = document.createElement('span');
                ric.className = 'help-rail-ic'; ric.textContent = helpSectionIcon(slug);
                const rlab = document.createElement('span');
                rlab.className = 'help-rail-label'; rlab.textContent = label;
                const rcnt = document.createElement('span');
                rcnt.className = 'help-rail-count';
                rcnt.textContent = String(bySlug.get(slug).length);
                rb.appendChild(ric); rb.appendChild(rlab); rb.appendChild(rcnt);
                rb.addEventListener('click', () => {
                    const target = h.scrollEl.querySelector('#' + secId);
                    if (target) h.scrollEl.scrollTo({ top: Math.max(0, target.offsetTop - 6) });
                });
                h.railEl.appendChild(rb);

                const secDiv = document.createElement('div');
                secDiv.className = 'help-section';
                secDiv.id = secId;
                const head = document.createElement('div');
                head.className = 'help-section-header';
                const sic = document.createElement('div');
                sic.className = 'help-section-icon'; sic.textContent = helpSectionIcon(slug);
                const stitle = document.createElement('div');
                stitle.className = 'help-section-title'; stitle.textContent = label;
                const scount = document.createElement('div');
                scount.className = 'help-section-count';
                scount.textContent = String(bySlug.get(slug).length);
                head.appendChild(sic); head.appendChild(stitle); head.appendChild(scount);
                secDiv.appendChild(head);

                const grid = document.createElement('div');
                grid.className = 'help-entry-grid';
                for (const e of bySlug.get(slug)) {
                    const card = document.createElement('div');
                    card.className = 'help-entry';
                    const headRow = document.createElement('div');
                    headRow.className = 'help-entry-head';
                    const t = document.createElement('div');
                    t.className = 'help-entry-title';
                    helpAppendHighlighted(t, e.title, q);
                    headRow.appendChild(t);
                    if (e.keys) {
                        const kbd = document.createElement('span');
                        kbd.className = 'help-kbd';
                        kbd.textContent = e.keys;
                        headRow.appendChild(kbd);
                    }
                    card.appendChild(headRow);
                    if (e.bodyFrags && e.bodyFrags.length) {
                        const b = document.createElement('div');
                        b.className = 'help-entry-body';
                        helpRenderFrags(b, e.bodyFrags, q);
                        card.appendChild(b);
                    }
                    grid.appendChild(card);
                }
                secDiv.appendChild(grid);
                h.scrollEl.appendChild(secDiv);
                h.sectionEls.push(secDiv);
            });
            updateHelpActiveSection(win);
        }
        // Build the Help window body (header + search + rail + scroll grid). Stashes
        // element refs on win._help and registers listeners in win.cleanups. Returns
        // the .help-body root for the caller to append.
        function buildHelpBody(win) {
            const body = document.createElement('div');
            body.className = 'help-body';

            const top = document.createElement('div');
            top.className = 'help-top';
            const heading = document.createElement('div');
            heading.className = 'help-heading';
            const icon = document.createElement('div');
            icon.className = 'help-icon'; icon.textContent = '?';
            const titleWrap = document.createElement('div');
            const title = document.createElement('div');
            title.className = 'help-title'; title.textContent = 'Help';
            const subtitle = document.createElement('div');
            subtitle.className = 'help-subtitle'; subtitle.textContent = 'Interface guide';
            titleWrap.appendChild(title); titleWrap.appendChild(subtitle);
            const count = document.createElement('div');
            count.className = 'help-count';
            heading.appendChild(icon); heading.appendChild(titleWrap); heading.appendChild(count);

            const searchWrap = document.createElement('div');
            searchWrap.className = 'help-search-wrap';
            const search = document.createElement('input');
            search.type = 'text';
            search.className = 'help-search';
            search.autocomplete = 'off'; search.spellcheck = false;
            search.placeholder = 'Search help…  (snap, tab, split, workspace, MCP, …)';
            const clear = document.createElement('button');
            clear.type = 'button'; clear.className = 'help-clear';
            clear.title = 'Clear search'; clear.textContent = '×';
            searchWrap.appendChild(search); searchWrap.appendChild(clear);

            top.appendChild(heading); top.appendChild(searchWrap);

            const main = document.createElement('div');
            main.className = 'help-main';
            const rail = document.createElement('nav');
            rail.className = 'help-rail';
            const scroll = document.createElement('div');
            scroll.className = 'help-scroll';
            main.appendChild(rail); main.appendChild(scroll);

            body.appendChild(top); body.appendChild(main);

            win._help = { searchEl: search, clearBtn: clear, countEl: count,
                          railEl: rail, scrollEl: scroll, sectionEls: [] };

            const onInput = () => renderHelpInto(win, search.value);
            search.addEventListener('input', onInput);
            const onClear = () => { search.value = ''; renderHelpInto(win, ''); search.focus(); };
            clear.addEventListener('click', onClear);
            const onScroll = () => updateHelpActiveSection(win);
            scroll.addEventListener('scroll', onScroll, { passive: true });
            // Scoped Escape (bound to the body, so it fires only when focus is
            // inside Help): clear the query if any, else close the window. stopProp
            // keeps the global Escape (which closes the settings modal) from firing.
            const onKey = (e) => {
                if (e.key !== 'Escape') return;
                e.stopPropagation();
                if (search.value) { search.value = ''; renderHelpInto(win, ''); search.focus(); }
                else closeWindow(win.id);
            };
            body.addEventListener('keydown', onKey);
            win.cleanups.push(() => {
                search.removeEventListener('input', onInput);
                clear.removeEventListener('click', onClear);
                scroll.removeEventListener('scroll', onScroll);
                body.removeEventListener('keydown', onKey);
            });
            return body;
        }
        // The single live Help window (or null) — Help is single-instance.
        function findHelpWindow() {
            for (const w of windows.values()) {
                if (w && !w.disposed && w.appKind === 'help') return w;
            }
            return null;
        }
        // Re-snapshot win._helpCorpus from live state and re-render, preserving the
        // current query — used when a cold profiles/MCP cache warms after open.
        function refreshHelpCorpus(win) {
            if (!win || win.disposed || !win._help) return;
            win._helpCorpus = buildHelpEntries();
            renderHelpInto(win, win._help.searchEl.value);
        }
        // Centered ~860×600 default, biased slightly up; clampGeom fits it to the
        // desktop on small viewports.
        function helpDefaultGeom() {
            const d = document.getElementById('desktop').getBoundingClientRect();
            const width = Math.min(880, Math.max(440, Math.round(d.width - 48)));
            const height = Math.min(620, Math.max(360, Math.round(d.height - 64)));
            const left = Math.max(12, Math.round((d.width - width) / 2));
            const top = Math.max(12, Math.round((d.height - height) / 2 - 20));
            return { left, top, width, height };
        }
        // The floating Help app window (appKind 'help'). Ephemeral like the task
        // manager: never persisted, single-instance (see focusOrOpenHelp). Mirrors
        // the openTaskManagerWindow chrome wiring exactly.
        function openHelpWindow(appData) {
            appData = appData || {};
            const id = String(appData.id || newAppId('help'));
            const existing = windows.get(id);
            if (existing) {
                if (existing.minimized) restoreWindow(id); else bringToFront(id);
                return existing;
            }
            // Single-instance invariant enforced HERE, not only in focusOrOpenHelp:
            // openAppWindow delegates appKind 'help' to this factory, so any other
            // caller (now or later) focuses the live Help window instead of
            // spawning a second one.
            const liveHelp = findHelpWindow();
            if (liveHelp) {
                if (liveHelp.minimized) restoreWindow(liveHelp.id);
                bringToFront(liveHelp.id);
                return liveHelp;
            }
            const title = appData.title || 'Help';
            const geom = clampGeom(appData.geom || helpDefaultGeom());
            const color = normalizeHex(appData.color || defaultColor(id));

            const dom = document.createElement('div');
            dom.className = 'term-window app-window app-help';
            dom.dataset.sessionId = id;
            dom.style.left = geom.left + 'px';
            dom.style.top = geom.top + 'px';
            dom.style.width = (geom.width - 4) + 'px';
            dom.style.height = (geom.height - 4) + 'px';
            dom.style.setProperty('--accent', color);
            dom.classList.toggle('dark-accent', isDarkAccent(color));

            const titleBar = document.createElement('div');
            titleBar.className = 'title-bar';
            const idBadge = document.createElement('span');
            idBadge.className = 'ti-id-badge';
            idBadge.textContent = '#help';
            const titleText = document.createElement('span');
            titleText.className = 'title-text';
            titleText.textContent = title;
            const minBtn = document.createElement('button');
            minBtn.type = 'button';
            minBtn.className = 'tb-btn btn-min';
            minBtn.textContent = '_';
            minBtn.title = 'minimize';
            const closeBtn = document.createElement('button');
            closeBtn.type = 'button';
            closeBtn.className = 'tb-btn btn-close';
            closeBtn.textContent = '×';
            closeBtn.title = 'close';
            titleBar.appendChild(idBadge);
            titleBar.appendChild(titleText);
            titleBar.appendChild(minBtn);
            titleBar.appendChild(closeBtn);
            dom.appendChild(titleBar);

            const win = {
                id, sid: 'help', hostId: 'app',
                type: 'app', appKind: 'help',
                dom, body: null, titleText,
                term: null, fitAddon: null,
                ws: null, wsOpen: false, termReady: false,
                minimized: false, disposed: false,
                geom, name: title, color,
                resizeTimer: null, lastSentDims: null,
                cleanups: [],
                tiled: false,
                floatGeom: appData.floatGeom
                    ? Object.assign({}, appData.floatGeom) : null,
                locked: false,
                dirty: false,
                _helpCorpus: [],
            };

            const helpBody = buildHelpBody(win);
            win.body = win._help.scrollEl;
            // Raising Help from OUTSIDE (taskbar click / programmatic bringToFront)
            // routes focus to the search so the body-scoped Escape can close a
            // taskbar-raised window. focusWin runs focusEditor on every mousedown
            // too, so guard on "focus is currently outside this window": a click on
            // a card (reading/selecting help text) must NOT yank the caret into the
            // search box. (The scroll body is a non-focusable div, so the default
            // focusWin fallback is a no-op — hence focusEditor.)
            win.focusEditor = () => {
                try {
                    if (!win.dom.contains(document.activeElement)) win._help.searchEl.focus();
                } catch (_) {}
            };
            // Ephemeral: if the user tiled/pinned Help, the shared tiling helpers
            // wrote prefs['app:help:...'] (saveAppWindow refuses to persist Help, so
            // nothing else cleans it). Drop it on close so it never lingers in
            // localStorage / synced /state. No-op for the common never-tiled case.
            win.cleanups.push(() => {
                if (prefs[id]) { delete prefs[id]; savePrefs(); }
            });
            dom.appendChild(helpBody);

            for (const dir of ['n','s','e','w','nw','ne','sw','se']) {
                const hnd = document.createElement('div');
                hnd.className = 'rh rh-' + dir;
                hnd.dataset.dir = dir;
                dom.appendChild(hnd);
            }

            document.getElementById('desktop').appendChild(dom);
            document.getElementById('desktop').classList.remove('empty');
            windows.set(id, win);

            const stopProp = (e) => e.stopPropagation();
            const onMouseDown = () => bringToFront(id);
            dom.addEventListener('mousedown', onMouseDown);
            win.cleanups.push(() => dom.removeEventListener('mousedown', onMouseDown));

            const onMinClick = (e) => { e.stopPropagation(); minimizeWindow(id); };
            const onCloseClick = (e) => { e.stopPropagation(); closeWindow(id); };
            minBtn.addEventListener('mousedown', stopProp);
            minBtn.addEventListener('click', onMinClick);
            closeBtn.addEventListener('mousedown', stopProp);
            closeBtn.addEventListener('click', onCloseClick);
            win.cleanups.push(() => {
                minBtn.removeEventListener('mousedown', stopProp);
                minBtn.removeEventListener('click', onMinClick);
                closeBtn.removeEventListener('mousedown', stopProp);
                closeBtn.removeEventListener('click', onCloseClick);
            });

            wireDrag(win, titleBar);
            const onTitleCtx = (e) => {
                e.preventDefault();
                e.stopPropagation();
                bringToFront(win.id);
                buildWindowMenu(win, e.clientX, e.clientY);
            };
            titleBar.addEventListener('contextmenu', onTitleCtx);
            win.cleanups.push(() =>
                titleBar.removeEventListener('contextmenu', onTitleCtx));
            for (const handle of dom.querySelectorAll('.rh')) {
                wireResize(win, handle, handle.dataset.dir);
            }

            const appSess = { key: id, sid: 'help', id, title, stale: false,
                              kind: 'app', hostId: 'app' };
            sessions.set(id, appSess);
            const itemsHost = document.getElementById('taskbar-items');
            if (!itemsHost.querySelector(
                    '.taskbar-item[data-session-id="' + cssEscape(id) + '"]')) {
                itemsHost.appendChild(buildTaskbarItem(appSess));
            }
            updateTaskbarColor(id);
            updateTaskbarLabel(id);
            const emptyMsg = document.getElementById('taskbar-empty');
            if (emptyMsg) emptyMsg.remove();

            win._helpCorpus = buildHelpEntries();   // live entries render immediately
            renderHelpInto(win, '');
            // Fetch the wiki-sourced static cards (#60) + warm cold generated
            // sources (profiles / MCP); re-render as each resolves. The live
            // entries above mean Help is never blank while these load.
            try { fetchHelpCorpus().then(() => refreshHelpCorpus(win)).catch(() => {}); } catch (_) {}
            try { fetchProfiles(localHost()).then(() => refreshHelpCorpus(win)).catch(() => {}); } catch (_) {}
            try { fetchMcpConfig(localHost()).then(() => refreshHelpCorpus(win)).catch(() => {}); } catch (_) {}

            if (findKeyInLayout(id)) placeWindowTiled(win);
            else bringToFront(id);
            setTimeout(() => { try { win._help.searchEl.focus(); } catch (_) {} }, 0);
            return win;
        }
        // Open Help, or focus/restore the existing one (single-instance). Shared by
        // the "?" chip, the (+) menu, and the toggle hotkey.
