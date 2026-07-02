        // ---- mod: scratchpad (#124) ---------------------------------------
        // A singleton notes window whose content lives SERVER-side (via
        // ctx.serverStore -> /mod-store/scratchpad), so it is shared across every
        // browser on this broker and survives a reload / cache clear. Notes are
        // organized as internal tabs, each a CodeMirror editor built on the ONE
        // shared CM build (loadCodeMirror()'s cache — never a second import, which
        // would silently break highlighting, see #36). Every save keeps a bounded
        // newest-first revision ring server-side, surfaced by a History panel.
        //
        // requires:['editor'] — CM stays tied to the Text-editor mod: disable the
        // editor and the scratchpad won't init (so loadCodeMirror() is never
        // called and CM stays unloaded). We still call loadCodeMirror() directly.
        //
        // Persistence split: CONTENT is server-only ({v:1, tabs:[{id,name,text}]}
        // in ctx.serverStore); the localStorage app-window record carries ONLY
        // view state ({id,appKind,open,geom,activeTab}) so a tab SWITCH never
        // churns a server revision and note text never lands in
        // webterm:appwindows:v1. Writes reuse the /state single-active-client
        // lease: a NON-active browser reads but its saves get 409 not_active
        // (surfaced as a read-only banner); it takes over writes by becoming
        // active. Conflicts (rare — only one browser can be active) rebase onto
        // the live rev and re-push the LATEST local content (last-writer-wins for
        // the active browser; a slow in-flight save can never clobber newer keys
        // because gatherValue() re-captures at send time and saves are single-
        // in-flight).
        registerMod({
            id: 'scratchpad',
            version: '1.0.0',
            ctxVersion: 1,
            requires: ['editor'],          // shares the editor's single CM build
            tiers: ['storage', 'window'],  // ctx.serverStore (#124) + a window kind
            init: function (ctx) {
                // The whole point is durable server storage; no-op on an older
                // loader that predates ctx.serverStore (#124).
                if (!ctx.serverStore) return;

                // Stable id for the SINGLE scratchpad window (open-or-focus): a
                // fixed id through openAppWindow dedupes + un-minimizes, so a
                // second launch focuses the existing window (cf. clipboard #118).
                const SCRATCH_WIN_ID = 'app:scratch';
                const SAVE_DEBOUNCE = 800;   // ms of idle before an autosave
                const MAX_TABS = 20;         // soft cap (v1)
                const NAME_MAX = 60;         // tab-name length cap

                let _tabSeq = 0;
                function newTabId() {
                    _tabSeq += 1;
                    return 't' + Date.now().toString(36) + '-' + _tabSeq;
                }
                // Coerce an untrusted tab record (from the server / a revision)
                // into {id,name,text}; drop non-objects. Names/text are always
                // rendered with textContent, never innerHTML.
                function sanitizeTab(t) {
                    if (!t || typeof t !== 'object') return null;
                    const id = (typeof t.id === 'string' && t.id) ? t.id : newTabId();
                    let name = String(t.name == null ? 'Notes' : t.name)
                        .slice(0, NAME_MAX);
                    if (!name) name = 'Notes';
                    return { id: id, name: name,
                             text: String(t.text == null ? '' : t.text) };
                }
                function fmtTs(ts) {
                    if (!ts) return '';
                    try {
                        const d = new Date(ts * 1000);
                        const p2 = (n) => (n < 10 ? '0' + n : '' + n);
                        return d.getFullYear() + '-' + p2(d.getMonth() + 1) + '-'
                            + p2(d.getDate()) + ' ' + p2(d.getHours()) + ':'
                            + p2(d.getMinutes()) + ':' + p2(d.getSeconds());
                    } catch (_) { return ''; }
                }

                // ---- window builder -------------------------------------------
                function openScratchWindow(appData) {
                    const id = String(appData.id);
                    const title = 'Scratchpad';
                    const geom = clampGeom(
                        appData.geom || appDefaultGeom('text-editor'));
                    const color = normalizeHex(appData.color || defaultColor(id));
                    const locked = appData.locked !== undefined
                        ? !!appData.locked : true;

                    const chrome = buildAppChrome({
                        id, appClass: 'app-scratch', badge: '#notes',
                        geom, color, locked, title,
                    });
                    const { dom, titleText } = chrome;
                    titleText.title = 'Scratchpad — notes stored on this broker';

                    const stopProp = (e) => e.stopPropagation();
                    const btn = (label, cls, ttl, onClick) => {
                        const b = document.createElement('button');
                        b.type = 'button';
                        b.className = cls;
                        b.textContent = label;
                        if (ttl) b.title = ttl;
                        b.addEventListener('mousedown', stopProp);
                        b.addEventListener('click', (e) => {
                            e.stopPropagation(); onClick(e);
                        });
                        return b;
                    };

                    // Tab strip (mirrors the editor's .app-tabs) + a toolbar with
                    // the New-tab and History actions.
                    const tabBar = document.createElement('div');
                    tabBar.className = 'app-tabs';
                    const toolbar = document.createElement('div');
                    toolbar.className = 'app-toolbar app-scratch-toolbar';
                    const addBtn = btn('+', 'app-scratch-add',
                        'new tab', () => addTab());
                    const histBtn = btn('History', 'app-scratch-hist-btn',
                        'browse + restore past revisions', () => toggleHistory());
                    toolbar.appendChild(addBtn);
                    toolbar.appendChild(histBtn);

                    // Read-only banner (hidden until a save is refused because this
                    // browser isn't the active one).
                    const roBanner = document.createElement('div');
                    roBanner.className = 'app-scratch-ro';
                    roBanner.style.display = 'none';
                    const roText = document.createElement('span');
                    roText.textContent =
                        'Another browser is active — notes are read-only here.';
                    const roRetry = btn('Take over', 'app-scratch-ro-retry',
                        'become the active browser, then save', () => runSave());
                    roBanner.appendChild(roText);
                    roBanner.appendChild(roRetry);

                    // CM host + the History panel (shown in place of the body).
                    const body = document.createElement('div');
                    body.className = 'app-scratch-body';
                    body.textContent = 'loading…';
                    const histPanel = document.createElement('div');
                    histPanel.className = 'app-scratch-history';
                    histPanel.style.display = 'none';

                    dom.appendChild(tabBar);
                    dom.appendChild(toolbar);
                    dom.appendChild(roBanner);
                    dom.appendChild(body);
                    dom.appendChild(histPanel);
                    addResizeHandles(dom);   // last children: on-top hit zones

                    document.getElementById('desktop').appendChild(dom);
                    document.getElementById('desktop').classList.remove('empty');

                    const win = {
                        id, sid: 'notes', hostId: 'app',
                        type: 'app', appKind: 'scratchpad',
                        dom, body, titleText,
                        term: null, fitAddon: null,
                        ws: null, wsOpen: false, termReady: false,
                        minimized: false, disposed: false,
                        geom, name: title, color,
                        resizeTimer: null, lastSentDims: null,
                        staleSession: false, authFailed: false,
                        reattachAttempts: 0, reattachAt: 0, lastOpenAt: 0,
                        missingPolls: 0,
                        cleanups: [],
                        tiled: false,
                        floatGeom: appData.floatGeom
                            ? Object.assign({}, appData.floatGeom) : null,
                        locked,
                        dirty: false,
                        // scratchpad state
                        scratchTabs: [], activeTab: 0, baseRev: 0,
                        cmView: null, serverRO: false, _loading: true,
                        _saving: false, _saveAgain: false, _saveTimer: null,
                        _suppressCm: false, _makeState: null, _histPreview: null,
                    };
                    windows.set(id, win);

                    // Raise / minimize / close / drag / 8-way resize / WM menu.
                    // Default close = closeWindow (no dirty prompt — content is
                    // autosaved to the server; a pending edit is flushed below).
                    wireAppChrome(win, chrome);

                    // Synthetic kind:'app' session + taskbar item (keeps the poll
                    // reaper off this window; lets formatTitle render it) — same
                    // scaffold as the clipboard / task-manager app windows.
                    const appSess = { key: id, sid: 'notes', id, title,
                                      stale: false, kind: 'app', hostId: 'app' };
                    sessions.set(id, appSess);
                    const itemsHost = document.getElementById('taskbar-items');
                    if (!itemsHost.querySelector('.taskbar-item[data-session-id="'
                            + cssEscape(id) + '"]')) {
                        itemsHost.appendChild(buildTaskbarItem(appSess));
                    }
                    updateTaskbarColor(id);
                    updateTaskbarLabel(id);
                    const emptyMsg = document.getElementById('taskbar-empty');
                    if (emptyMsg) emptyMsg.remove();

                    // While read-only, a click on the window re-attempts the save —
                    // so once the user takes over the lease it resyncs on the next
                    // interaction (no polling). bringToFront already fires here.
                    const onDomDown = () => { if (win.serverRO) runSave(); };
                    dom.addEventListener('mousedown', onDomDown);
                    win.cleanups.push(
                        () => dom.removeEventListener('mousedown', onDomDown));

                    // Best-effort flush of a pending debounced save on close so an
                    // edit in the last SAVE_DEBOUNCE ms still reaches the server.
                    win.cleanups.push(() => {
                        if (win._saveTimer) {
                            clearTimeout(win._saveTimer);
                            win._saveTimer = null;
                            if (!win.serverRO && !win._loading) {
                                try { runSave(); } catch (_) {}
                            }
                        }
                    });

                    // ---- tab UI -----------------------------------------------
                    function captureActive() {
                        const t = win.scratchTabs[win.activeTab];
                        if (!t || !win.cmView) return;
                        try {
                            t.text = win.cmView.state.doc.toString();
                            t.cmState = win.cmView.state;
                        } catch (_) {}
                    }
                    function showTabState(t) {
                        if (!win.cmView || !t) return;
                        win._suppressCm = true;
                        try {
                            win.cmView.setState(t.cmState || win._makeState(t));
                        } finally { win._suppressCm = false; }
                    }
                    function refreshTabBar() {
                        tabBar.textContent = '';
                        win.scratchTabs.forEach((t, i) => {
                            const b = document.createElement('button');
                            b.type = 'button';
                            b.className = 'app-tab'
                                + (i === win.activeTab ? ' active' : '');
                            const lbl = document.createElement('span');
                            lbl.className = 'app-tab-label';
                            lbl.textContent = t.name;
                            b.appendChild(lbl);
                            if (win.scratchTabs.length > 1) {
                                const x = document.createElement('span');
                                x.className = 'app-tab-close';
                                x.textContent = '×';
                                x.title = 'close tab';
                                x.addEventListener('mousedown', stopProp);
                                x.addEventListener('click', (e) => {
                                    e.stopPropagation(); closeTab(i);
                                });
                                b.appendChild(x);
                            }
                            b.addEventListener('mousedown', stopProp);
                            b.addEventListener('click', (e) => {
                                e.stopPropagation(); switchTab(i);
                            });
                            b.addEventListener('dblclick', (e) => {
                                e.stopPropagation(); renameTab(i, lbl);
                            });
                            tabBar.appendChild(b);
                        });
                    }
                    function switchTab(idx) {
                        if (idx < 0 || idx >= win.scratchTabs.length) return;
                        if (idx === win.activeTab) { focusCm(); return; }
                        captureActive();
                        win.activeTab = idx;
                        showTabState(win.scratchTabs[idx]);
                        refreshTabBar();
                        saveAppWindow(win);   // persist activeTab (view state)
                        focusCm();
                    }
                    function nextName() {
                        // "Notes", then "Notes 2", "Notes 3", … avoiding collisions.
                        const have = new Set(win.scratchTabs.map((t) => t.name));
                        if (!have.has('Notes')) return 'Notes';
                        let n = 2;
                        while (have.has('Notes ' + n)) n += 1;
                        return 'Notes ' + n;
                    }
                    function addTab() {
                        if (win.scratchTabs.length >= MAX_TABS) return;
                        captureActive();
                        const t = { id: newTabId(), name: nextName(),
                                    text: '', cmState: null };
                        win.scratchTabs.push(t);
                        win.activeTab = win.scratchTabs.length - 1;
                        showTabState(t);
                        refreshTabBar();
                        saveAppWindow(win);
                        scheduleSave();       // structural change -> server
                        focusCm();
                    }
                    function closeTab(i) {
                        if (win.scratchTabs.length <= 1) return;   // keep >=1
                        captureActive();
                        win.scratchTabs.splice(i, 1);
                        if (win.activeTab >= win.scratchTabs.length) {
                            win.activeTab = win.scratchTabs.length - 1;
                        } else if (i < win.activeTab) {
                            win.activeTab -= 1;
                        }
                        showTabState(win.scratchTabs[win.activeTab]);
                        refreshTabBar();
                        saveAppWindow(win);
                        scheduleSave();
                        focusCm();
                    }
                    function renameTab(i, lblEl) {
                        const t = win.scratchTabs[i];
                        if (!t) return;
                        const input = document.createElement('input');
                        input.type = 'text';
                        input.className = 'app-tab-rename';
                        input.value = t.name;
                        input.maxLength = NAME_MAX;
                        lblEl.replaceWith(input);
                        input.focus();
                        input.select();
                        let done = false;
                        const commit = () => {
                            if (done) return;
                            done = true;
                            const v = input.value.trim().slice(0, NAME_MAX);
                            t.name = v || t.name;
                            refreshTabBar();
                            saveAppWindow(win);
                            scheduleSave();
                        };
                        input.addEventListener('mousedown', stopProp);
                        input.addEventListener('keydown', (e) => {
                            e.stopPropagation();
                            if (e.key === 'Enter') { commit(); }
                            else if (e.key === 'Escape') {
                                done = true; refreshTabBar();
                            }
                        });
                        input.addEventListener('blur', commit);
                    }
                    function focusCm() {
                        if (win.cmView) { try { win.cmView.focus(); } catch (_) {} }
                    }
                    win._refreshScratchTabBar = refreshTabBar;

                    // ---- save pipeline ----------------------------------------
                    // The value is pure content; view state (activeTab/geom) rides
                    // the localStorage record, so a tab switch never bumps a rev.
                    function gatherValue() {
                        captureActive();
                        return { v: 1, tabs: win.scratchTabs.map((t) => ({
                            id: t.id, name: t.name, text: t.text })) };
                    }
                    function scheduleSave() {
                        if (win._loading || win.serverRO) return;
                        if (win._saveTimer) clearTimeout(win._saveTimer);
                        win._saveTimer = setTimeout(() => {
                            win._saveTimer = null; runSave();
                        }, SAVE_DEBOUNCE);
                    }
                    function enterRO() {
                        win.serverRO = true;
                        roBanner.style.display = '';
                    }
                    function exitRO() {
                        win.serverRO = false;
                        roBanner.style.display = 'none';
                    }
                    // Single in-flight save; re-captures the LATEST content each
                    // call (never replays a stale snapshot). Callable directly
                    // (Ctrl+S / retry / restore) — the RO/loading gate lives in
                    // scheduleSave, not here, so a retry can attempt while RO.
                    async function runSave() {
                        if (win._loading || win.disposed) return;
                        if (win._saving) { win._saveAgain = true; return; }
                        win._saving = true;
                        try {
                            const value = gatherValue();
                            const res = await ctx.serverStore.set(
                                value, win.baseRev);
                            if (res && res.ok) {
                                win.baseRev = res.rev;
                                if (win.serverRO) exitRO();
                            } else if (res && res.error === 'not_active') {
                                if (typeof res.rev === 'number') {
                                    win.baseRev = res.rev;
                                }
                                enterRO();
                            } else if (res && res.error === 'conflict') {
                                // Someone advanced the rev (a restore, or a browser
                                // that took the lease). Adopt it and re-push OUR
                                // latest content (last-writer-wins for the active
                                // browser). No editor overwrite — the user is typing.
                                if (typeof res.rev === 'number') {
                                    win.baseRev = res.rev;
                                }
                                win._saveAgain = true;
                            }
                            // else transport/400/413/500: leave state; a later
                            // edit reschedules.
                        } finally {
                            win._saving = false;
                            if (win._saveAgain && !win.serverRO && !win._loading) {
                                win._saveAgain = false;
                                scheduleSave();
                            }
                        }
                    }
                    win._saveToServer = runSave;   // Ctrl+S hook

                    // ---- History panel ----------------------------------------
                    function historyOpen() {
                        return histPanel.style.display !== 'none';
                    }
                    function toggleHistory() {
                        if (historyOpen()) closeHistory();
                        else openHistory();
                    }
                    function closeHistory() {
                        histPanel.style.display = 'none';
                        body.style.display = '';
                        win._histPreview = null;
                        focusCm();
                    }
                    async function openHistory() {
                        histPanel.style.display = '';
                        body.style.display = 'none';
                        histPanel.textContent = 'loading history…';
                        let got = null;
                        try { got = await ctx.serverStore.get(); } catch (_) {}
                        if (win.disposed || !historyOpen()) return;
                        if (!got) { histPanel.textContent =
                            'history unavailable'; return; }
                        renderHistory(got);
                    }
                    function renderHistory(got) {
                        histPanel.textContent = '';
                        const bar = document.createElement('div');
                        bar.className = 'app-scratch-hist-bar';
                        const label = document.createElement('span');
                        label.textContent = 'Revision history';
                        bar.appendChild(label);
                        bar.appendChild(btn('Close', 'app-scratch-hist-close',
                            'close history', closeHistory));
                        histPanel.appendChild(bar);

                        const list = document.createElement('div');
                        list.className = 'app-scratch-hist-list';
                        list.appendChild(histRow(got.rev, null, true));
                        (got.revisions || []).forEach((r) => {
                            list.appendChild(histRow(r.rev, r.ts, false));
                        });
                        histPanel.appendChild(list);

                        const preview = document.createElement('div');
                        preview.className = 'app-scratch-hist-preview';
                        preview.textContent = 'select a revision to preview';
                        histPanel.appendChild(preview);
                        win._histPreview = preview;
                    }
                    function histRow(rev, ts, isCurrent) {
                        const row = document.createElement('div');
                        row.className = 'app-scratch-hist-row';
                        const label = document.createElement('span');
                        label.className = 'app-scratch-hist-label';
                        label.textContent = isCurrent
                            ? ('rev ' + rev + ' — current')
                            : ('rev ' + rev + '  ' + fmtTs(ts));
                        row.appendChild(label);
                        if (!isCurrent) {
                            row.appendChild(btn('Restore', 'app-scratch-hist-restore',
                                'restore this revision as a new save',
                                (e) => { e.stopPropagation();
                                         restoreRevision(rev); }));
                        }
                        row.addEventListener('click', () => previewRevision(rev));
                        return row;
                    }
                    function tabsFromValue(val) {
                        const tabs = (val && val.v === 1 && Array.isArray(val.tabs))
                            ? val.tabs : [];
                        return tabs.map(sanitizeTab).filter(Boolean);
                    }
                    async function previewRevision(rev) {
                        const res = await ctx.serverStore.getRevision(rev);
                        const box = win._histPreview;
                        if (!box) return;
                        if (!res || res.ok === false) {
                            box.textContent = (res && res.error === 'no_such_rev')
                                ? 'that revision is no longer available '
                                  + '(scrolled off history)'
                                : 'preview failed';
                            return;
                        }
                        box.textContent = '';
                        const tabs = tabsFromValue(res.value);
                        if (!tabs.length) { box.textContent = '(empty)'; return; }
                        tabs.forEach((t) => {
                            const h = document.createElement('div');
                            h.className = 'app-scratch-hist-tabname';
                            h.textContent = t.name;
                            const pre = document.createElement('pre');
                            pre.className = 'app-scratch-hist-text';
                            pre.textContent = t.text;
                            box.appendChild(h);
                            box.appendChild(pre);
                        });
                    }
                    async function restoreRevision(rev) {
                        const res = await ctx.serverStore.getRevision(rev);
                        if (!res || res.ok === false) {
                            openHistory();   // stale list -> refresh
                            return;
                        }
                        let tabs = tabsFromValue(res.value);
                        if (!tabs.length) {
                            tabs = [{ id: newTabId(), name: 'Notes', text: '' }];
                        }
                        win.scratchTabs = tabs.map((t) => ({
                            id: t.id, name: t.name, text: t.text, cmState: null }));
                        win.activeTab = 0;
                        showTabState(win.scratchTabs[0]);
                        refreshTabBar();
                        closeHistory();
                        saveAppWindow(win);
                        // Persist immediately as a NEW rev, superseding any pending
                        // debounced autosave (cancel it so we don't double-write).
                        if (win._saveTimer) {
                            clearTimeout(win._saveTimer); win._saveTimer = null;
                        }
                        runSave();
                    }

                    // ---- hydrate (async): fetch content, load CM, mount --------
                    async function hydrate() {
                        let got = null;
                        try { got = await ctx.serverStore.get(); } catch (_) {}
                        if (win.disposed || !windows.has(id)) return;
                        win.baseRev = (got && typeof got.rev === 'number')
                            ? got.rev : 0;
                        let tabs = tabsFromValue(got && got.value);
                        if (!tabs.length) {
                            tabs = [{ id: newTabId(), name: 'Notes', text: '' }];
                        }
                        win.scratchTabs = tabs.map((t) => ({
                            id: t.id, name: t.name, text: t.text, cmState: null }));
                        const want = (typeof appData.activeTab === 'number')
                            ? appData.activeTab : 0;
                        win.activeTab = Math.max(0,
                            Math.min(want, win.scratchTabs.length - 1));

                        let CM = null;
                        try { CM = await loadCodeMirror(); } catch (_) {}
                        if (win.disposed || !windows.has(id)) return;
                        if (!CM) {
                            body.textContent = 'CodeMirror failed to load — '
                                + 'notes are unavailable (offline?).';
                            win._loading = false;
                            return;
                        }
                        buildCmView(CM);
                        refreshTabBar();
                        win._loading = false;
                        // No immediate save — the local content mirrors the server;
                        // the first edit triggers the first autosave.
                    }

                    function buildCmView(CM) {
                        const { EditorView } = CM.view;
                        const { EditorState } = CM.state;
                        const { keymap, drawSelection, highlightActiveLine,
                                rectangularSelection, crosshairCursor } = CM.view;
                        const { history, defaultKeymap, historyKeymap,
                                indentWithTab } = CM.commands;
                        const { syntaxHighlighting, defaultHighlightStyle,
                                indentOnInput } = CM.language;
                        const { oneDark } = CM.theme;
                        // Notes read as Markdown (the single shared build already
                        // ships the language) — a plain-text tab still renders fine.
                        const mkMarkdown = CM.langs && CM.langs.markdown;

                        const fillTheme = EditorView.theme({
                            '&': { height: '100%', fontSize: '13px' },
                            '.cm-scroller': {
                                fontFamily: "Consolas, 'Liberation Mono', monospace",
                                lineHeight: '1.5',
                            },
                        });

                        let mdExt = [];
                        if (mkMarkdown) {
                            try { mdExt = mkMarkdown(); } catch (_) { mdExt = []; }
                        }

                        win._makeState = (tab) => EditorState.create({
                            doc: tab ? (tab.text || '') : '',
                            extensions: [
                                history(),
                                drawSelection(),
                                indentOnInput(),
                                rectangularSelection(),
                                crosshairCursor(),
                                highlightActiveLine(),
                                syntaxHighlighting(defaultHighlightStyle,
                                    { fallback: true }),
                                mdExt,
                                EditorView.lineWrapping,
                                keymap.of([
                                    { key: 'Mod-s', preventDefault: true,
                                      run: () => { runSave(); return true; } },
                                    ...defaultKeymap,
                                    ...historyKeymap,
                                    indentWithTab,
                                ]),
                                oneDark,
                                fillTheme,
                                EditorView.updateListener.of((u) => {
                                    if (u.docChanged && !win._suppressCm) {
                                        captureActive();
                                        scheduleSave();
                                    }
                                }),
                            ],
                        });

                        const view = new EditorView({
                            state: win._makeState(win.scratchTabs[win.activeTab]) });
                        body.textContent = '';
                        body.appendChild(view.dom);
                        win.cmView = view;
                        win.cleanups.push(() => {
                            try { view.destroy(); } catch (_) {}
                        });
                    }

                    hydrate();

                    if (findKeyInLayout(id)) placeWindowTiled(win);
                    else bringToFront(id);
                    // Persist the view-state record so a reload restores the window
                    // (content then re-hydrates from the server).
                    saveAppWindow(win);
                    return win;
                }

                // ---- window-kind registration ---------------------------------
                // serialize persists ONLY view state — NEVER note content (that
                // lives on the server). saveAppWindow writes exactly what this
                // returns, so webterm:appwindows:v1 carries no note text.
                ctx.registerWindowKind({
                    appKind: 'scratchpad',
                    factory: function (d) { return openScratchWindow(d); },
                    serialize: function (win) {
                        return {
                            id: win.id, appKind: 'scratchpad', open: true,
                            geom: win.geom,
                            activeTab: win.activeTab || 0,
                        };
                    },
                    menu: {
                        label: 'Scratchpad',
                        iconKey: 'scratchpad',   // #119 SVG notepad in the (+) menu
                        launch: function () {
                            return openAppWindow({
                                id: SCRATCH_WIN_ID, appKind: 'scratchpad' });
                        },
                    },
                });

                // Teardown — registered AFTER registerWindowKind so LIFO closes any
                // live scratchpad window WHILE the kind is still registered (so
                // saveAppWindow sees the serialize and the record is handled by
                // closeWindow), same reasoning as the sticky/clipboard mods.
                ctx.onUnload(function () {
                    for (const w of Array.from(windows.values())) {
                        if (w && w.type === 'app' && w.appKind === 'scratchpad') {
                            closeWindow(w.id);
                        }
                    }
                });
            },
        });
