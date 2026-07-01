        // ---- mod: help (S5 / #78) -----------------------------------------
        // The in-app Help window, the taskbar "?" chip, the show/hide toggle,
        // and the ctx.registerHelpCards extension point — extracted from core
        // (#78). Byte-identical behavior: the "?" chip / the toggle-help hotkey /
        // the (+) menu open a single floating Help app window (appKind 'help',
        // ephemeral like the task manager). The static cards still come from the
        // SINGLE-SOURCE wiki via the CORE corpus pipeline (help_corpus.py ->
        // /help-corpus.json -> 80_js_help_window.js's fetchHelpCorpus +
        // buildHelpEntries, which merge wiki cards with live keybindings/profiles/
        // MCP state); this mod renders that data and lets OTHER mods contribute
        // typed Help cards through ctx.registerHelpCards.
        //
        // HOISTING NOTE (do NOT wrap these in init/an IIFE): the window/open
        // functions below are TOP-LEVEL `function` declarations so their call sites
        // keep resolving them by hoisting across the one concatenated <script> —
        // openHelpWindow + launchHelp are consumed by this mod's own
        // ctx.registerWindowKind (the factory + (+) menu launch, #100), toggleHelpWindow
        // by the toggle-help keybinding in CORE fragment 78, and applyHelpButton by
        // init — exactly as before, and even when mods_enabled=false (loadMods gates
        // init(), never parsing), so the keybinding still opens Help mods-off. Only the
        // chip, the window-kind registration, the Control Panel toggle, the first-run
        // hint, and card-registration refresh are init-owned (so a disabled/absent mod
        // has no chip and no (+) Help entry).

        // The taskbar "?" chip visibility (moved from 65_js_display_theming.js):
        // same show/hide-on-an-`.on`-class pattern as the clock. The mod shows the
        // chip unconditionally while enabled (#101 dropped the redundant per-setting
        // toggle — the mod's own enable/disable is the single control); disabling the
        // mod removes the chip entirely (init's ctx.onUnload), not just its `.on`.
        function applyHelpButton(on) {
            try {
                const el = document.getElementById('help-chip');
                if (el) el.classList.toggle('on', !!on);
            } catch (_) {}
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
        // #119: paint a section glyph into the rail button / header box. The icon
        // is a TAGGED value: { svg } is a trusted APP_ICON_SVG string (injected as
        // markup) and { text } is an emoji / '•' fallback (kept as textContent).
        // Only registry output is ever tagged `svg`, so mod-supplied metadata
        // (mod.json help.icon via secIcon) can never reach innerHTML even if it
        // happened to look like markup.
        function helpSetSectionIcon(el, icon) {
            if (icon && icon.svg) el.innerHTML = icon.svg;
            else el.textContent = (icon && icon.text) || '';
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
        // Render a typed-fragment card body (from the wiki corpus or a mod's
        // registerHelpCards) into `el` using DOM APIs only: each block is a
        // paragraph / bullet / sub-heading of typed inline spans (text / strong /
        // code / kbd). Matches of `q` are wrapped via helpAppendHighlighted. This
        // is the XSS-safety boundary — card text is only ever added as text nodes,
        // never parsed as markup.
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
        // The Help corpus the window renders: the CORE merge (wiki cards + live
        // keybindings/profiles/MCP, from buildHelpEntries — kept in core because
        // it reads core state) PLUS every mod-contributed card registered through
        // ctx.registerHelpCards (window.__mods.helpCards). Each contributed card
        // gets the same lower-cased search haystack the core entries carry. Read
        // LIVE so a card registered after Help opened appears on the next refresh.
        function helpEntriesAll() {
            const entries = buildHelpEntries();
            const cards = (window.__mods && window.__mods.helpCards) || [];
            for (const c of cards) {
                const e = {
                    slug: c.slug, section: c.section, title: c.title,
                    bodyFrags: c.bodyFrags, keys: c.keys || '',
                    search: c.search || '',
                };
                e._hay = ((e.search || '') + ' ' + (e.section || '') + ' '
                    + (e.keys || '')).toLowerCase();
                entries.push(e);
            }
            return entries;
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
            // #119: prefer the trusted SVG app icon keyed by the section's owning
            // mod id (e.owner, the corpus's per-section `mod` field), stored TAGGED
            // as { svg } so only registry output is ever injected as markup. #113:
            // else the mod's own declared glyph (e.secIcon); else the static
            // HELP_SECTION_ICONS map for wiki / generated slugs — both kept as
            // { text } (textContent). First-seen wins. appIconSvg returns '' for a
            // wiki/un-owned section, so those take the text path.
            const iconOf = new Map();
            for (const e of entries) {
                if (!bySlug.has(e.slug)) {
                    bySlug.set(e.slug, []); labelOf.set(e.slug, e.section || e.slug);
                    const svg = appIconSvg(e.owner);
                    iconOf.set(e.slug, svg
                        ? { svg: svg }
                        : { text: e.secIcon || helpSectionIcon(e.slug) });
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
                ric.className = 'help-rail-ic';
                helpSetSectionIcon(ric, iconOf.get(slug));   // #119: SVG or emoji
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
                sic.className = 'help-section-icon';
                helpSetSectionIcon(sic, iconOf.get(slug));   // #119: SVG or emoji
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
        // current query — used when a cold profiles/MCP cache warms after open, or
        // when a mod registers/unregisters Help cards while Help is open.
        function refreshHelpCorpus(win) {
            if (!win || win.disposed || !win._help) return;
            win._helpCorpus = helpEntriesAll();
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

            // Shared chrome (#79): the help mod reuses the CORE window-chrome
            // factory (hoisted core functions, reachable from this concatenated mod
            // script even when mods are disabled) for the .term-window shell, title
            // bar (_ / ×), eight resize handles, and the raise/drag/resize/context-
            // menu wiring — byte-identical to the built-in app windows.
            const chrome = buildAppChrome({
                id, appClass: 'app-help', badge: '#help',
                geom, color, locked: false, title,
            });
            const { dom, titleText } = chrome;

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
            addResizeHandles(dom);   // last children: edge/corner hit zones on top

            document.getElementById('desktop').appendChild(dom);
            document.getElementById('desktop').classList.remove('empty');
            windows.set(id, win);

            // Raise / minimize / close / drag / 8-way resize / WM context menu.
            wireAppChrome(win, chrome);

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

            win._helpCorpus = helpEntriesAll();     // live entries render immediately
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
        function focusOrOpenHelp() {
            markHelpSeen();   // any deliberate open is the strongest "seen" signal
            const ex = findHelpWindow();
            if (ex) {
                if (ex.minimized) restoreWindow(ex.id);
                bringToFront(ex.id);
                setTimeout(() => { try { ex._help.searchEl.focus(); } catch (_) {} }, 0);
                return ex;
            }
            return openHelpWindow({});
        }
        function launchHelp() { return focusOrOpenHelp(); }
        // The hotkey toggles: if Help is the focused, non-minimized front window,
        // close it; otherwise open/focus it.
        function toggleHelpWindow() {
            const ex = findHelpWindow();
            if (ex && !ex.minimized && frontId === ex.id) { closeWindow(ex.id); return; }
            focusOrOpenHelp();
        }
        // Register the mod: contribute the 'help' window kind (#100), create the "?"
        // chip (shown unconditionally while enabled — #101), and schedule the one-time
        // first-run nudge. The chip is appended LAST in the taskbar, so the clock
        // (added "before #help-chip") keeps its slot.
        registerMod({
            id: 'help',
            version: '1.0.0',
            ctxVersion: 1,
            tiers: ['taskbar'],   // #101: taskbar chip only (no synced setting), like clock
            init: function (ctx) {
                // #100: contribute the 'help' window kind through ctx (the same core
                // registry the built-ins use), so its (+) launch-menu entry rides the
                // mod's enable/disable — _modRegisterWindowKind auto-pushes
                // deleteWindowKind onto this mod's unload list, removing the kind (and
                // its menu entry) on disable. Ephemeral (no serialize -> never
                // restored), matching Help's old core registration. No onUnload closes
                // a live Help window: it holds no live resource (its render/corpus fns
                // are hoisted/core), so it closes normally after disable.
                ctx.registerWindowKind({
                    appKind: 'help',
                    factory: function (d) { return openHelpWindow(d); },
                    menu: { label: 'Help', iconKey: 'help',   // #119: SVG ?-in-circle
                            launch: function () { return launchHelp(); } },
                });

                // Build the "?" chip (was static #help-chip markup in 40_body.html).
                // Hidden until applyHelpButton adds .on; #help-chip rules ship in
                // this mod's help.css.
                const chip = document.createElement('div');
                chip.id = 'help-chip';
                chip.title = 'Help — interface guide';
                chip.setAttribute('role', 'button');
                chip.tabIndex = 0;
                chip.setAttribute('aria-label', 'Help');
                chip.textContent = '?';
                const bar = document.getElementById('taskbar');
                if (bar) bar.appendChild(chip);
                const onChipClick = () => focusOrOpenHelp();
                const onChipKey = (e) => {
                    if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); focusOrOpenHelp(); }
                };
                chip.addEventListener('click', onChipClick);
                chip.addEventListener('keydown', onChipKey);
                ctx.onUnload(function () {
                    chip.removeEventListener('click', onChipClick);
                    chip.removeEventListener('keydown', onChipKey);
                    if (chip.parentNode) chip.parentNode.removeChild(chip);
                });

                // #101: the "?" chip is shown unconditionally while the mod is
                // enabled — the redundant Control Panel "Show help (?) button" toggle
                // is gone (enabling the mod already reveals the chip; two controls for
                // the same thing was confusing). Reveal it now: the early-boot
                // applyThemeSettings() ran before this chip existed.
                applyHelpButton(true);

                // First-run nudge: once, point new users at the "?" chip (now always
                // visible while the mod is enabled). Deferred until the initial /state
                // has been ADOPTED so it reflects the synced helpHintSeen rather than
                // pre-pull defaults; capped (~10s) so an offline/auth-blocked broker
                // still nudges from local prefs instead of hanging forever.
                let hintTries = 0;
                function maybeShowHelpHint() {
                    try {
                        if (!_stateReady && hintTries++ < 20) {
                            setTimeout(maybeShowHelpHint, 500);
                            return;
                        }
                        const s = getSettings();
                        if (s.helpHintSeen) return;
                        showNotice('Tip: click the "?" on the taskbar for the interface guide.', 7000);
                        s.helpHintSeen = true;
                        savePrefs();
                    } catch (_) {}
                }
                const hintTimer = setTimeout(maybeShowHelpHint, 1800);
                ctx.onUnload(function () { clearTimeout(hintTimer); });
            },
        });
