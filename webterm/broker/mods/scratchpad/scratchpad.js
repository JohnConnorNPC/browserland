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
        // active.
        //
        // Saves ride ctx.serverStore.saveChain (#196, 86e) — the shared
        // debounced, single-in-flight, rebase-on-409 chain that was canned FROM
        // the pipeline this file used to hand-roll, so this mod is that issue's
        // reference migration and the swap is the proof. What it keeps: the same
        // 800ms trailing debounce, one write outstanding at a time, and the
        // late read — the reducer handed to save() reads the live CM buffer at
        // SEND time and re-reads it on every rebase, so a slow save can never
        // re-PUT a snapshot that already lost (#196 trap 3, the #158 clobber).
        // Content is one whole document, so composing coalesced reducers
        // degenerates to last-writer-wins for the active browser, exactly as
        // before. What it gains: update()'s bounded rebase instead of a single
        // hand-rolled re-push, and the banner driven off chain.onState.
        registerMod({
            id: 'scratchpad',
            version: '1.0.0',
            ctxVersion: 1,
            requires: ['editor'],          // shares the editor's single CM build
            tiers: ['storage', 'window'],  // ctx.serverStore (#124) + a window kind
            // #196/#197: the surface this mod cannot run without. Saving IS the
            // mod — content lives only on the broker — so a build without the
            // chain should report "blocked (needs serverStore.saveChain)" rather
            // than open a notes window that silently drops every keystroke. That
            // is the opposite call from help's (#195), and deliberately: help
            // feature-detects because a missing bus costs it ONE feature, while
            // a missing chain costs this mod all of its persistence.
            // #219 adds the factory: the whole window is core-built now, so
            // without it the (+) entry is a dead button and the kind's factory
            // would throw on every launch -- the same reason clipboard declares
            // it. `needs` makes that visible in the Mods pane instead of
            // leaving a mod that reads "active" and does nothing.
            needs: ['serverStore.saveChain', 'windows.createAppWindow'],
            init: function (ctx) {
                // The whole point is durable server storage; no-op on an older
                // loader that predates ctx.serverStore (#124) or the save chain
                // (#196). The `needs` gate above already refuses those builds —
                // but it lives in 86c, and a page assembled without THAT has no
                // gate at all, so the runtime check stays.
                if (!ctx.serverStore
                        || typeof ctx.serverStore.saveChain !== 'function') {
                    return;
                }

                // Stable id for the SINGLE scratchpad window (open-or-focus): a
                // fixed id through openAppWindow dedupes + un-minimizes, so a
                // second launch focuses the existing window (cf. clipboard #118).
                const SCRATCH_WIN_ID = 'app:scratch';

                // #199/#219: THE SAVE IS A NAMED COMMAND, not a field.
                // `win._saveToServer = runSave` used to sit on every scratchpad
                // window, commented "Ctrl+S hook" -- and nothing read it.
                // Core's only reader (73's unsaved-changes prompt) is gated on
                // `win.appKind === 'text-editor'`, and no key dispatcher looked
                // it up either, so the hook was dead on this window from the
                // day it was written. That is exactly the failure #199 names: a
                // duck-typed field is unaddressable, so nobody notices when
                // nothing is reading it.
                //
                // One registration, not one per window. The id is namespaced to
                // 'scratchpad:save' by the registrar, so a second register()
                // would be a collision rather than a per-window entry; the
                // focused window's saver is looked up here instead. A WeakMap
                // and not a field, for the reason above -- and so a closed
                // window's closure is not held alive by the registry.
                const scratchSavers = new WeakMap();   // win -> runSave
                if (ctx.commands && typeof ctx.commands.register === 'function') {
                    ctx.commands.register('save', {
                        scope: 'window',
                        // `when` is the honest gate: window-scoped already
                        // means "a window is focused", and this narrows it to
                        // one of OURS that has finished building. A window
                        // mid-build has no saver yet, and answering 'blocked'
                        // is the truthful outcome for it.
                        when: function (where) {
                            return scratchSavers.has(where.win);
                        },
                        run: function (args, where) {
                            const save = scratchSavers.get(where.win);
                            return save ? save() : false;
                        },
                    });
                }
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
                // #194/#219: the ~30-field scaffold is core's. buildAppChrome,
                // addResizeHandles, the desktop insertion and its `empty`
                // class, the win literal, windows.set, wireAppChrome, the
                // synthetic kind:'app' session, the hand-appended taskbar chip
                // with its cssEscape'd guard, updateTaskbarColor/Label, the
                // #taskbar-empty removal and finishWindowPlacement were all
                // re-typed here.
                //
                // THE FIVE-CHILD LAYOUT SURVIVES IT. This window is not
                // toolbar-then-body: it is tabBar, toolbar, roBanner, body,
                // histPanel. The factory owns the middle two, so body() places
                // the other three around them -- tabBar before the toolbar it
                // is handed, roBanner before the body, histPanel appended to
                // win.dom, which still lands before the resize handles because
                // those go on after body() returns (86c says so in as many
                // words, for recorder's below-the-body transport bar).
                function openScratchWindow(appData) {
                    const d = appData || {};
                    const h = ctx.windows.createAppWindow({
                        kind: 'scratchpad',
                        id: (d.id != null && String(d.id))
                            ? String(d.id) : SCRATCH_WIN_ID,
                        title: 'Scratchpad',
                        sid: 'notes',
                        badge: '#notes',
                        appClass: 'app-scratch',
                        // The factory would name it 'app-scratchpad-body';
                        // the shipped stylesheet matches '.app-scratch-body'.
                        bodyClass: 'app-scratch-body',
                        // Absent fields keep the factory's defaults, which are
                        // the deleted scaffold's own. appDefaultGeom only
                        // special-cases 'sticky-note', so asking for the kind
                        // is the same box the old 'text-editor' ask gave.
                        geom: d.geom,
                        color: d.color,
                        locked: d.locked,
                        floatGeom: d.floatGeom,
                        toolbar: function () { /* filled from body(), see below */ },
                        // `d` rides along because hydrate() restores the
                        // ACTIVE TAB from the stored record, and the factory
                        // does not pass the appData through to body().
                        body: function (bodyEl, win, handle) {
                            buildScratchWindow(bodyEl, win, handle, d);
                        },
                    });
                    // THE TRAP: openAppWindow hands a registered kind's factory
                    // return value straight back to its callers, and they want
                    // a window RECORD. Return h.win, never the handle.
                    return h.win;
                }

                // Everything specific to a scratchpad window, against a record
                // core already built, chipped and inserted.
                function buildScratchWindow(body, win, handle, appData) {
                    const id = win.id;
                    const title = win.name;
                    const dom = win.dom;
                    const titleText = win.titleText;
                    if (titleText) {
                        titleText.title =
                            'Scratchpad — notes stored on this broker';
                    }

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
                    // The factory built and classed this one ('app-toolbar
                    // app-scratch-toolbar', derived from appClass -- byte-equal
                    // to what the deleted line wrote).
                    const toolbar = handle.toolbar;
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

                    // CM host (core built it; this is its initial content) +
                    // the History panel, shown in place of the body.
                    body.textContent = 'loading…';
                    const histPanel = document.createElement('div');
                    histPanel.className = 'app-scratch-history';
                    histPanel.style.display = 'none';

                    // The three children the factory does not own, placed
                    // around the two it does: tabBar above the toolbar,
                    // roBanner between the toolbar and the body, histPanel
                    // after the body (and still before the resize handles,
                    // which are appended once body() returns).
                    dom.insertBefore(tabBar, toolbar);
                    dom.insertBefore(roBanner, body);
                    dom.appendChild(histPanel);

                    // The record is core's. Only the scratchpad-specific
                    // state is set here -- and no baseRev / _saving /
                    // _saveAgain / _saveTimer, because the CAS revision, the
                    // single-in-flight latch and the debounce all live inside
                    // the save chain (#196), and a field nothing updates is
                    // worse than no field at all.
                    win.scratchTabs = [];
                    win.activeTab = 0;
                    win.cmView = null;
                    win.serverRO = false;
                    win._loading = true;
                    win._suppressCm = false;
                    win._makeState = null;
                    win._histPreview = null;

                    // While read-only, a click on the window re-attempts the save —
                    // so once the user takes over the lease it resyncs on the next
                    // interaction (no polling). bringToFront already fires here.
                    const onDomDown = () => { if (win.serverRO) runSave(); };
                    dom.addEventListener('mousedown', onDomDown);
                    win.cleanups.push(
                        () => dom.removeEventListener('mousedown', onDomDown));

                    // (The final flush on close is registered with the save
                    // chain below, where the chain it flushes is in scope.)

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

                    // ---- save pipeline (on ctx.serverStore.saveChain, #196) ---
                    // The debounce, the single-in-flight latch and the 409
                    // rebase that used to be written out here are the chain's
                    // now (86e), which was canned from this very pipeline. The
                    // debounce is passed explicitly even though 86e's default is
                    // this same 800ms, so the two can never silently drift.
                    // No purgeRevisions / no noHistory: the revision ring IS the
                    // History panel (#124), and notes are not credentials (#192).
                    const chain = ctx.serverStore.saveChain({
                        debounceMs: SAVE_DEBOUNCE });

                    // The value is pure content; view state (activeTab/geom) rides
                    // the localStorage record, so a tab switch never bumps a rev.
                    function gatherValue() {
                        captureActive();
                        return { v: 1, tabs: win.scratchTabs.map((t) => ({
                            id: t.id, name: t.name, text: t.text })) };
                    }
                    // THE LATE READ, and the reason save() takes a reducer rather
                    // than a snapshot: the live CM buffer is read HERE, when the
                    // batch is actually sent, and read AGAIN on each rebase — so
                    // a 409 re-pushes what the user has typed by then, never the
                    // body that already lost (#196 trap 3 / the #158 clobber).
                    // The winner's `value` is deliberately unread: content is one
                    // whole document and the write lease means the active browser
                    // is its only writer, so this is the last-writer-wins the
                    // hand-rolled chain had, unchanged. Pure over (value, rev) as
                    // the contract demands — captureActive() only copies CM's
                    // buffer into the tab record, so re-running it per attempt
                    // observes more, never does more.
                    function saveReducer() { return gatherValue(); }
                    // Trailing debounce: every edit calls save() again, and each
                    // call pushes the chain's deadline out. Queuing the same
                    // reducer per edit is what re-arms it; the chain composes a
                    // coalesced batch, which for a whole-document reducer
                    // degenerates to last-wins — one PUT carrying the newest
                    // content, exactly as before.
                    function scheduleSave() {
                        if (win._loading || win.serverRO) return;
                        chain.save(saveReducer);
                    }
                    function enterRO() {
                        win.serverRO = true;
                        roBanner.style.display = '';
                    }
                    function exitRO() {
                        win.serverRO = false;
                        roBanner.style.display = 'none';
                    }
                    // The read-only banner is the chain's 'conflict' state: a 409
                    // this browser cannot resolve — 'not_active' (another browser
                    // holds the write lease: the common case, and the only one
                    // the old code raised the banner for) or a rebase budget
                    // spent against a hotter writer, which the old code retried
                    // forever instead. onState fires once on subscribe, so the
                    // banner is already correct before the first save; 'saving'
                    // leaves it exactly as it is.
                    //
                    // The banner comes DOWN only on a genuine success, never on
                    // 'idle' alone. 'idle' is also where a transport failure, a
                    // 5xx and a 413 land, and every save now begins with a GET
                    // (update() reads before it writes), so a broker that is
                    // merely unreachable would otherwise retract a warning that
                    // is still true. Worse, it would stay retracted: refresh()
                    // dedupes on the state, so a PERSISTENT failure never fires
                    // 'conflict' again and the user types into a window that
                    // shows no warning and stores nothing. The old code cleared
                    // it on `res.ok` for exactly this reason; the result rides
                    // the second argument, so the gate is the same one.
                    win.cleanups.push(chain.onState(function (state, res) {
                        if (state === 'conflict') enterRO();
                        else if (state === 'idle' && res && res.ok === true) {
                            exitRO();
                        }
                    }));
                    // Save NOW, past the debounce and past the RO gate: Ctrl+S,
                    // the "Take over" button, a click anywhere in the window
                    // while read-only, and a History restore all land here. The
                    // gate lives in scheduleSave, not here, so a retry can
                    // attempt while RO. Returns the chain's result promise, which
                    // never rejects.
                    function runSave() {
                        if (win._loading || win.disposed) {
                            return Promise.resolve(null);
                        }
                        chain.save(saveReducer);
                        return chain.flush();
                    }
                    // Addressable as 'scratchpad:save' (see the registration
                    // in init). No field on the window: the one it replaced was
                    // read by nothing.
                    scratchSavers.set(win, runSave);
                    // The deliberate final flush (#196 trap 6), from the WINDOW
                    // cleanup — which runs on an ordinary close with the mod
                    // still alive, so an edit made in the last SAVE_DEBOUNCE ms
                    // still reaches the server. Never from ctx.onUnload: by then
                    // rec.unloading is set and 86e drops the batch, because a
                    // disable is synchronous and cannot await a flush. This also
                    // fixes what the block it replaces only claimed to do — that
                    // one called runSave(), which returns early on win.disposed,
                    // and closeWindow sets disposed BEFORE draining cleanups.
                    // captureActive() runs first because flush() resolves
                    // asynchronously while a LATER cleanup in this same drain
                    // destroys the CM view: the reducer must not be the only
                    // thing that ever reads the buffer. A chain with nothing
                    // queued makes no request at all.
                    // dispose() when the flush has SETTLED, never beside it. The
                    // chain is per WINDOW while rec.unloads is per ACTIVATION,
                    // so without a dispose each open leaks one disposer that
                    // also retains the chain's last result — a whole notes blob
                    // when that result is a 409. But flush() is asynchronous:
                    // disposing in the same tick kills the batch it was about to
                    // send, which is the close-time save this cleanup exists for.
                    // So it hangs off the promise, on both settle paths.
                    win.cleanups.push(() => {
                        try { captureActive(); } catch (_) {}
                        const done = () => {
                            try { chain.dispose(); } catch (_) {}
                        };
                        try {
                            const p = chain.flush();
                            if (p && typeof p.then === 'function') {
                                p.then(done, done);
                            } else {
                                done();
                            }
                        } catch (_) { done(); }
                    });

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
                        // Persist immediately as a NEW rev. A pending debounced
                        // autosave no longer has to be cancelled by hand: it is
                        // already queued on the chain, so this flush coalesces it
                        // into the SAME write — and both reducers read the
                        // restored tabs at send time, so there is one PUT and it
                        // carries the restored content.
                        runSave();
                    }

                    // ---- hydrate (async): fetch content, load CM, mount --------
                    async function hydrate() {
                        let got = null;
                        try { got = await ctx.serverStore.get(); } catch (_) {}
                        if (win.disposed || !windows.has(id)) return;
                        // No rev to remember: every send re-reads the live rev
                        // through ctx.serverStore.update's CAS (#196), so a
                        // hydrate that raced a write cannot leave a stale base.
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
                        // #199/#210: the SERVICE first, the hoisted name as
                        // the mods-off path. Consumed PER USE, so an editor mod
                        // that is absent, not yet loaded, disabled or
                        // mid-teardown is simply undefined here -- and then the
                        // hoisted loadCodeMirror still answers, because the
                        // shared build is deliberately reachable with the
                        // editor mod off (core builds a text-editor window
                        // through that same hoisted path when its kind is not
                        // registered). So this buys the revocable contract
                        // without buying a scratchpad that loses syntax
                        // highlighting whenever the editor is switched off.
                        try {
                            const cm = (typeof ctx.consume === 'function')
                                ? ctx.consume('editor', 'codemirror') : null;
                            if (cm && typeof cm.load === 'function') {
                                CM = await cm.load();
                            }
                        } catch (_) { CM = null; }
                        if (!CM) {
                            try { CM = await loadCodeMirror(); } catch (_) {}
                        }
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

                    // Persist the view-state record so a reload restores the
                    // window (content then re-hydrates from the server).
                    // finishWindowPlacement is the factory's now.
                    saveAppWindow(win);
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
                    // #186: opts into the window context menu's "Delete note" item
                    // (78_js_keybindings.js reads this off the registry entry).
                    deleteLabel: 'note',
                    menu: {
                        label: 'Scratchpad',
                        iconKey: 'scratchpad',   // #119 SVG notepad in the (+) menu
                        launch: function () {
                            return openAppWindow({
                                id: SCRATCH_WIN_ID, appKind: 'scratchpad' });
                        },
                    },
                });

                // #194/#219: NO TEARDOWN HERE. This used to iterate the CORE
                // windows Map for records carrying its own appKind, registered
                // AFTER registerWindowKind so LIFO closed them while the kind
                // was still registered (so saveAppWindow saw the serialize).
                // The loader STAGES the factory-owned close ahead of the first
                // onUnload, which is to say while every kind this mod
                // registered is still registered -- so the ordering is core's
                // and cannot be got wrong here again.
            },
        });
