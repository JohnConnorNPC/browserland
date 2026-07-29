        // ---- workspaces (#148, extracted from core 62_js_workspaces.js) ----
        // Vertical workspaces as a first-class mod. Default ON: with it enabled
        // the desktop behaves exactly as it did when this lived in core.
        //
        // THE WHOLE DESIGN IN ONE LINE: workspaces are PRESENTATION over the one
        // desktop core owns. Every tiled column lives in prefs._layout.columns
        // exactly once, always; this mod stamps a `wsId` on each column and
        // registers ctx.desktop.columnFilter, so the strip draws one group at a
        // time. Nothing is ever moved between stores, which is why disabling is
        // non-destructive AND reversible: drop the filter and every window is
        // back on one desktop, re-enable and the groups return. There is no
        // flatten, and no way for a browser without this mod to destroy the
        // workspaces of a browser that has it.
        //
        // Store (rides the /state-synced layout blob, like _layout.openTerms --
        // core passes unknown fields through untouched, so it is shared across
        // every browser on this broker with no broker or wire change):
        //   _layout.mods.workspaces = { version:1, activeId,
        //                               list:[ {id, name?, focusedCol:int} ] }
        //   _layout.columns[i].wsId = '<workspace id>'   (mod-owned; core
        //                               preserves it and never reads it)
        // An UNSTAMPED column -- one core made while this was off, or one a peer
        // browser without the mod created -- is adopted by the ACTIVE workspace
        // on sight, so nothing is ever orphaned.
        //
        // Floating windows are a separate axis: their membership lives in the
        // browser-LOCAL prefs._floatWs (key -> wsId | null = all workspaces),
        // because per-window placement is per-browser like floating geometry.
        //
        // Direct core access is idiomatic here (the loader is explicit that the
        // ctx families are review hygiene, not a boundary): windows, prefs,
        // getLayout, visibleColumns, relayoutStrip, parkWindow, savePrefs,
        // renderMenu, openTextPrompt/openConfirmDialog are all in the same
        // scope. ctx is used where a real seam exists -- the column filter, the
        // menus, the key actions, the taskbar hooks and the two settings.
        registerMod({
            id: 'workspaces',
            version: '1.0.0',
            ctxVersion: 1,
            // window: masks/parks windows and adds title-bar menu items;
            // taskbar: badges + dims chips and intercepts activation;
            // settings: owns wsLabelMode + hideTaskbarOtherWs.
            tiers: ['window', 'taskbar', 'settings'],
            init: function (ctx) {
                // The two settings this mod owns, read through onto the SAME
                // synced blob they always lived in (no new schema field, the
                // #126 termfont pattern) -- an upgrading user's stored value is
                // preserved and neither key is written by a mere read.
                const labelMode = ctx.settings.select('wsLabelMode', [
                    { value: 'number', label: 'Numbers' },
                    { value: 'name', label: 'Names' },
                ], { label: 'Workspace labels', def: 'number' });
                const hideOther = ctx.settings.boolean('hideTaskbarOtherWs', false, {
                    label: 'Hide taskbar items from other workspaces',
                });
                function wsLabelMode() { return labelMode.get(); }
                function setWsLabelModeValue(v) { labelMode.set(v); }
                function hideTaskbarOtherWs() { return hideOther.get(); }
                labelMode.onChange(function () {
                    renderWorkspaces(); applyTaskbarWorkspace();
                });
                hideOther.onChange(function () { applyTaskbarWorkspace(); });

                // The pager lives in the taskbar between the chips and the host
                // status, exactly where the core markup used to put it. Created
                // here and removed on unload -- the #clock-chip pattern.
                const pager = document.createElement('div');
                pager.id = 'ws-pager';
                const taskbar = document.getElementById('taskbar');
                const hostStatus = document.getElementById('host-status');
                if (taskbar) {
                    if (hostStatus) taskbar.insertBefore(pager, hostStatus);
                    else taskbar.appendChild(pager);
                }
                ctx.onUnload(function () { pager.remove(); });
            // Set by any wsStore()/adopt heal that mutated the store without
            // saving; adoptUnstampedColumns() is the single place that turns it
            // into one savePrefs().
            let _wsDirty = false;
            function wsStore() {
                const L = getLayout();
                if (!L.mods || typeof L.mods !== 'object' || Array.isArray(L.mods)) {
                    L.mods = {};
                }
                let st = L.mods.workspaces;
                if (!st || typeof st !== 'object' || Array.isArray(st)) {
                    st = L.mods.workspaces = {};
                }
                st.version = 1;
                if (!Array.isArray(st.list)) st.list = [];
                // Heal the entries, dropping garbage and duplicate ids. IN PLACE:
                // a caller may hold st.list across something that re-reads the
                // store, and a swapped-out array would orphan its mutation --
                // the same hazard reconcileLayout has with L.columns.
                const seen = new Set();
                const kept = st.list.filter(function (w) {
                    if (!w || typeof w !== 'object' || Array.isArray(w)) return false;
                    if (typeof w.id !== 'string' || !w.id || seen.has(w.id)) return false;
                    seen.add(w.id);
                    if (typeof w.name !== 'string' || !w.name.trim()) delete w.name;
                    else w.name = w.name.slice(0, 40);
                    if (!Number.isInteger(w.focusedCol) || w.focusedCol < 0) w.focusedCol = 0;
                    return true;
                });
                if (kept.length !== st.list.length) _wsDirty = true;
                st.list.length = 0;
                for (const w of kept) st.list.push(w);
                // #148 hand-off: core's one-time legacy migration left the old
                // grouping (ids only) under L.wsLegacy. Adopt it once, then delete.
                if (L.wsLegacy && typeof L.wsLegacy === 'object') {
                    adoptLegacyWorkspaces(L, st);
                    delete L.wsLegacy;
                    // The adopt rebuilt the list AND stamped columns, none of it
                    // saved yet. Flag it: without this the whole migration lives
                    // in memory only and is redone from the server's blob on
                    // every load, losing any stamp made since.
                    _wsDirty = true;
                }
                if (!st.list.length) {
                    st.list.push({ id: mintLayoutId(), focusedCol: 0 });
                    // Freshly seeded. Flag it so it is persisted once: an id that
                    // only ever lived in memory is re-minted on the next load, and
                    // every column stamped with the old one would read as dangling
                    // and lose its grouping.
                    _wsDirty = true;
                }
                if (!st.list.some(function (w) { return w.id === st.activeId; })) {
                    st.activeId = st.list[0].id;
                    _wsDirty = true;
                }
                return st;
            }
            // Rebuild the workspace list from the pre-#148 blob core flattened. The
            // columns are already all in L.columns; this only re-stamps the grouping.
            function adoptLegacyWorkspaces(L, st) {
                const legacy = L.wsLegacy;
                const list = Array.isArray(legacy.list) ? legacy.list : [];
                const byId = new Map();
                for (const col of L.columns) if (col && col.id) byId.set(col.id, col);
                for (const entry of list) {
                    if (!entry || typeof entry !== 'object') continue;
                    const id = (typeof entry.id === 'string' && entry.id)
                        ? entry.id : mintLayoutId();
                    // An entry we already hold (a partly-adopted blob, or a peer
                    // that got here first) is NOT skipped wholesale: only the list
                    // entry is deduped. Its columnIds must still be stamped below,
                    // or those columns stay unstamped and the active workspace
                    // swallows them.
                    if (!st.list.some(function (w) { return w.id === id; })) {
                        const w = { id: id,
                            focusedCol: Number.isInteger(entry.focusedCol)
                                ? entry.focusedCol : 0 };
                        if (typeof entry.name === 'string' && entry.name.trim()) {
                            w.name = entry.name.slice(0, 40);
                        }
                        st.list.push(w);
                    }
                    for (const cid of (Array.isArray(entry.columnIds) ? entry.columnIds : [])) {
                        const col = byId.get(cid);
                        if (col && typeof col.wsId !== 'string') col.wsId = id;
                    }
                }
                if (st.list.length) {
                    const i = Number.isInteger(legacy.activeWs) ? legacy.activeWs : 0;
                    st.activeId = (st.list[Math.max(0, Math.min(i, st.list.length - 1))]
                        || st.list[0]).id;
                }
            }
            // Claim every column that has no workspace, or points at one that no
            // longer exists, for the ACTIVE workspace -- and PERSIST it.
            //
            // Without this, "unstamped means the active workspace" is only ever a
            // read-time default, so a column created by a browser WITHOUT this mod
            // (or by core while it was disabled) would keep reading as unstamped
            // and show up on every workspace at once. The dangling case is the
            // same story as applyWorkspaceVisibility's heal for floats: a peer can
            // remove a workspace this browser still has columns pointing at.
            // Idempotent, and saves only when something actually changed, so it is
            // safe on the every-relayout path.
            function adoptUnstampedColumns() {
                // A torn-down browser (another one holds the lease) must not write:
                // its push is refused or 409s, the adopt replaces the layout with
                // the peer's still-unstamped one, and the next relayout stamps and
                // saves again -- churn that never converges. Reading is unaffected:
                // columnWsId() already treats an unstamped column as the active
                // workspace, so nothing is mis-drawn while we wait.
                if (_deactivated) return false;
                const st = wsStore();
                const live = new Set(st.list.map(function (w) { return w.id; }));
                let changed = _wsDirty;
                _wsDirty = false;
                for (const col of getLayout().columns) {
                    if (typeof col.wsId === 'string' && live.has(col.wsId)) continue;
                    col.wsId = st.activeId;
                    changed = true;
                }
                if (changed) savePrefs();
                return changed;
            }
            function wsList() { return wsStore().list; }
            function activeWorkspaceId() { return wsStore().activeId; }
            function activeWorkspaceIndex() {
                const st = wsStore();
                const i = st.list.findIndex(function (w) { return w.id === st.activeId; });
                return i === -1 ? 0 : i;
            }
            function activeWorkspaceEntry() { return wsList()[activeWorkspaceIndex()]; }
            // A column's workspace. Unstamped => the active one (adopted on sight),
            // and so is a column stamped for a workspace that no longer exists.
            function columnWsId(col, st) {
                st = st || wsStore();
                const id = col && col.wsId;
                if (typeof id !== 'string') return st.activeId;
                return st.list.some(function (w) { return w.id === id; }) ? id : st.activeId;
            }
            function workspaceColumns(wsId) {
                const L = getLayout();
                const st = wsStore();
                return L.columns.filter(function (c) { return columnWsId(c, st) === wsId; });
            }
            function workspaceIndexById(id) {
                return wsList().findIndex(function (w) { return w.id === id; });
            }

            // ---- workspace labels + the pager dots ----------------------------
            function wsLabel(ws, i) {
                if (wsLabelMode() === 'name') {
                    return (ws && ws.name) ? ws.name : ('WS' + (i + 1));
                }
                return String(i + 1);
            }
            // Build the workspace dots (1..n + '+') into a container. The
            // churn-guard sig lives on the container itself, so two containers keep
            // INDEPENDENT guards (one rendering never short-circuits the other).
            function renderWsDots(container) {
                if (!container) return;
                const st = wsStore();
                const list = st.list;
                const active = activeWorkspaceIndex();
                const mode = wsLabelMode();
                // Names + mode are part of the signature so a rename / mode toggle
                // forces a rebuild (not just the active-class fast path).
                // Length-prefix each name: a plain join lets ['ab','c'] and
                // ['a','bc'] produce the same signature, so a real rename would
                // hit the churn guard and leave a stale label on a live dot.
                const sig = mode + '|' + active + '|' + list.length + '|'
                    + list.map(w => (w.name || '').length + ':' + (w.name || '')).join('|');
                if (container.dataset.sig === sig) {
                    container.querySelectorAll('.ws-dot:not(.add)').forEach((d, i) => {
                        d.classList.toggle('active', i === active);
                    });
                    return;
                }
                container.dataset.sig = sig;
                // The rebuild below drops the dot the pointer may be resting on
                // WITHOUT firing its mouseleave, so its body-level preview would
                // hang around until the next hover or a reload. Same stranding
                // the pager's hide path has to avoid (#162).
                hideWsPreview();
                container.innerHTML = '';
                list.forEach((ws, i) => {
                    const d = document.createElement('div');
                    d.className = 'ws-dot' + (i === active ? ' active' : '')
                        + (mode === 'name' ? ' named' : '');
                    d.textContent = wsLabel(ws, i);
                    d.title = (ws.name ? ws.name + ' — ' : '') + 'workspace ' + (i + 1)
                        + '  (right-click: rename / remove)';
                    d.addEventListener('click', () => switchWorkspace(i));
                    d.addEventListener('contextmenu', (e) => {
                        e.preventDefault();
                        e.stopPropagation();
                        buildWorkspaceMenu(i, e.clientX, e.clientY);
                    });
                    d.addEventListener('mouseenter', () => showWsPreview(i, d));
                    d.addEventListener('mouseleave', hideWsPreview);
                    container.appendChild(d);
                });
                const add = document.createElement('div');
                add.className = 'ws-dot add';
                add.textContent = '+';
                add.title = 'new workspace';
                add.addEventListener('click', () => addWorkspace());
                container.appendChild(add);
            }

            // ---- workspace rename / remove / label-mode (tasks 18, 19) --------
            function buildWorkspaceMenu(i, x, y) {
                const list = wsList();
                const mode = wsLabelMode();
                const items = [
                    { label: 'Workspace ' + (i + 1)
                        + (list[i] && list[i].name ? ' — ' + list[i].name : ''),
                      enabled: false },
                    { label: 'Rename…', enabled: true,
                      action: () => renameWorkspace(i) },
                    { label: 'Remove workspace', enabled: list.length > 1,
                      action: () => removeWorkspace(i) },
                    { sep: true },
                    { label: (mode === 'name' ? '✓ ' : '   ') + 'Show names',
                      enabled: true, action: () => setWsLabelMode('name') },
                    { label: (mode === 'number' ? '✓ ' : '   ') + 'Show numbers',
                      enabled: true, action: () => setWsLabelMode('number') },
                    { sep: true },
                    { label: 'New workspace', enabled: true, action: addWorkspace },
                ];
                renderMenu(items, x, y);
            }
            function setWsLabelMode(mode) {
                setWsLabelModeValue((mode === 'name') ? 'name' : 'number');
                renderWorkspaces();
                applyTaskbarWorkspace();
            }
            async function renameWorkspace(i) {
                const ws = wsList()[i];
                if (!ws) return;
                const name = await openTextPrompt({
                    title: 'Rename workspace',
                    label: 'Name',
                    value: ws.name || '',
                    placeholder: 'blank = use the number',
                    okLabel: 'Rename',
                });
                if (name === null) return;          // cancelled
                // A state-sync poll-adopt during the await can swap prefs._layout
                // wholesale, so re-find the live workspace by id before mutating it.
                const ws2 = wsList().find(w => w.id === ws.id);
                if (!ws2) return;
                const t = name.trim();
                if (t) {
                    ws2.name = t.slice(0, 40);
                    setWsLabelModeValue('name');          // naming implies showing names
                } else {
                    delete ws2.name;
                }
                savePrefs();
                renderWorkspaces();
                applyTaskbarWorkspace();
            }
            async function removeWorkspace(i) {
                if (wsList().length <= 1) {
                    showNotice('cannot remove the last workspace');
                    return;
                }
                const victim = wsList()[i];
                if (!victim) return;
                const hasContent = workspaceColumns(victim.id).length
                    || anyFloatingOnWs(victim.id);
                if (hasContent) {
                    const ok = await openConfirmDialog({
                        title: 'Remove workspace',
                        message: 'Remove workspace ' + (i + 1)
                            + '? Its windows move to an adjacent workspace.',
                        okLabel: 'Remove',
                        danger: true,
                    });
                    if (!ok) return;
                }
                // A state-sync poll-adopt during the await can replace prefs._layout
                // wholesale, so the captured list / index go stale. Re-acquire and
                // re-locate the victim by id before mutating.
                const st = wsStore();
                if (st.list.length <= 1) return;
                const vi = workspaceIndexById(victim.id);
                if (vi < 0) return;
                const neighbor = st.list[(vi > 0) ? vi - 1 : 1];
                if (!neighbor) return;
                // Re-stamp the victim's columns onto the neighbour — the columns
                // themselves never move, so no window can be lost here.
                for (const col of workspaceColumns(victim.id)) col.wsId = neighbor.id;
                reassignFloatingWs(victim.id, neighbor.id);
                const wasActive = (st.activeId === victim.id);
                st.list.splice(vi, 1);
                if (wasActive) {
                    st.activeId = neighbor.id;
                    applyWorkspaceView();
                }
                savePrefs();
                renderWorkspaces();
                relayoutStrip();
                applyWorkspaceVisibility();
                applyTaskbarWorkspace();
                updateTaskbarActive();
            }
            // ---- workspace preview popover (task 19) --------------------------
            let _wsPreviewEl = null;
            function hideWsPreview() {
                if (_wsPreviewEl) { _wsPreviewEl.remove(); _wsPreviewEl = null; }
            }
            function showWsPreview(i, anchor) {
                hideWsPreview();
                const ws = wsList()[i];
                if (!ws) return;
                const box = document.createElement('div');
                box.className = 'ws-preview';
                const title = document.createElement('div');
                title.className = 'wsp-title';
                title.textContent = (ws.name ? ws.name + '  ' : '')
                    + '(workspace ' + (i + 1) + ')';
                box.appendChild(title);
                const map = document.createElement('div');
                map.className = 'wsp-map';
                // Mirror relayoutStrip's live set (61) so the preview never shows a
                // minimized/dormant/phantom ghost the workspace itself doesn't display
                // (#147). Same skeleton as the strip's render set — per column the live
                // rows, and per split row the live cells — each keyed off the SHARED
                // isLiveKey. Dormant keys stay in _layout (reconcile keeps them) but
                // are dropped here exactly as the strip drops them.
                const cols = workspaceColumns(ws.id);
                const renderCols = [];
                for (const col of cols) {
                    const liveRows = [];
                    for (const row of (col.rows || [])) {
                        const liveKeys = rowKeys(row).filter(isLiveKey);
                        if (!liveKeys.length) continue;                 // dormant row -> skip
                        let liveCells = null;
                        if (row.mode === 'split') {
                            // Mirror relayoutStrip (61) exactly: a migrated split uses its
                            // real cells; a legacy cells-less split synthesizes one single-
                            // window pseudo-cell per live key (id === the key, so the by-key
                            // widths lookup still matches) until reconcile migrates it.
                            if (Array.isArray(row.cells)) {
                                liveCells = [];
                                for (const cell of row.cells) {
                                    const ck = (Array.isArray(cell.keys) ? cell.keys : [])
                                        .filter(isLiveKey);
                                    if (!ck.length) continue;           // dead cell -> skip
                                    const at = cell.activeTab;
                                    liveCells.push({ cell, id: cell.id, liveKeys: ck,
                                        repKey: (typeof at === 'string' && ck.indexOf(at) !== -1) ? at : ck[0] });
                                }
                                if (!liveCells.length) continue;
                            } else {
                                liveCells = liveKeys.map(k =>
                                    ({ cell: null, id: k, liveKeys: [k], repKey: k }));
                            }
                        }
                        liveRows.push({ row, liveKeys, liveCells });
                    }
                    if (liveRows.length) renderCols.push({ col, liveRows });
                }
                // Lay the LIVE columns out left-to-right by width fraction, rows top-to-
                // bottom by height fraction — a faithful mini schematic of what shows.
                let xoff = 0;
                const totalFrac = renderCols.reduce((a, rc) => a + colCurrentFrac(rc.col), 0) || 1;
                renderCols.forEach((rc) => {
                    const col = rc.col;
                    const wfrac = colCurrentFrac(col) / Math.max(1, totalFrac);
                    const rowsArr = rc.liveRows;
                    const hsum = rowsArr.reduce((a, lr) => a + (lr.row.height > 0 ? lr.row.height : 0), 0) || 1;
                    let yoff = 0;
                    rowsArr.forEach((lr) => {
                        const row = lr.row;
                        const hfrac = (row.height > 0 ? row.height : (1 / rowsArr.length)) / hsum;
                        // Draw one labeled rect; a tabbed/group tile gets a "(+N)" badge
                        // counting only its HIDDEN LIVE tabs (minimized tabs aren't shown).
                        const mkRect = (left, width, labelKey, plusN) => {
                            const r = document.createElement('div');
                            r.className = 'wsp-rect';
                            r.style.left = (left * 100) + '%';
                            r.style.width = (width * 100) + '%';
                            r.style.top = (yoff * 100) + '%';
                            r.style.height = (hfrac * 100) + '%';
                            const lab = document.createElement('span');
                            const sess = sessions.get(labelKey);
                            let txt = sess ? (sess.title || ('#' + sess.sid))
                                : (windows.has(labelKey)
                                    ? (windows.get(labelKey).name || labelKey) : labelKey);
                            if (plusN > 0) txt += ' (+' + plusN + ')';
                            lab.textContent = txt;
                            r.appendChild(lab);
                            map.appendChild(r);
                        };
                        if (row.mode === 'split' && lr.liveCells) {
                            // One sub-rect per LIVE cell, sized by widths[cell.id] within
                            // the row band but renormalized over the LIVE cells (a dead
                            // cell surrenders its width); a group cell labels its rep live
                            // tab + "(+N)" of its remaining live tabs.
                            const cells = lr.liveCells;
                            const wsum = cells.reduce((a, lc) => {
                                const v = row.widths && Number(row.widths[lc.id]);
                                return a + ((Number.isFinite(v) && v > 0) ? v : 0);
                            }, 0);
                            let cxoff = 0;
                            cells.forEach((lc) => {
                                const v = row.widths && Number(row.widths[lc.id]);
                                const cf = wsum > 0
                                    ? ((Number.isFinite(v) && v > 0) ? v : 0) / wsum
                                    : (1 / cells.length);
                                mkRect(xoff + cxoff * wfrac, cf * wfrac, lc.repKey,
                                    lc.liveKeys.length - 1);
                                cxoff += cf;
                            });
                        } else {
                            // single/tabbed (or a legacy cells-less split): label the
                            // active tab if it's live, else the first live key; badge the
                            // count of the row's remaining LIVE tabs.
                            const at = row.activeTab;
                            const labelKey = (typeof at === 'string'
                                && lr.liveKeys.indexOf(at) !== -1) ? at : lr.liveKeys[0];
                            mkRect(xoff, wfrac, labelKey, lr.liveKeys.length - 1);
                        }
                        yoff += hfrac;
                    });
                    xoff += wfrac;
                });
                const floatN = countFloatingOnWs(ws.id);
                if (!renderCols.length && !floatN) {
                    const e = document.createElement('div');
                    e.className = 'wsp-empty';
                    e.textContent = 'empty';
                    map.appendChild(e);
                }
                box.appendChild(map);
                if (floatN) {
                    const f = document.createElement('div');
                    f.className = 'wsp-title';
                    f.style.marginTop = '4px';
                    f.style.marginBottom = '0';
                    f.textContent = '+ ' + floatN + ' floating';
                    box.appendChild(f);
                }
                document.body.appendChild(box);
                // Position above the anchoring dot, clamped to the viewport.
                const ar = anchor.getBoundingClientRect();
                const bw = box.offsetWidth, bh = box.offsetHeight;
                let left = ar.left + ar.width / 2 - bw / 2;
                left = Math.max(6, Math.min(left, window.innerWidth - bw - 6));
                let top = ar.top - bh - 8;
                if (top < 6) top = ar.bottom + 8;
                box.style.left = left + 'px';
                box.style.top = top + 'px';
                _wsPreviewEl = box;
            }
            // ---- floating windows locked to a workspace (task 8) -------------
            // Membership lives in a single reserved map prefs._floatWs (key -> ws
            // id, or null = "all workspaces"). A reserved (_-prefixed) key survives
            // the prefs GC, works for both terminals and app docs without touching
            // their creation paths, and stays browser-local (not pushed to /state —
            // per-window placement is per-browser, like floating pixel geometry).
            // Tiled windows are NOT covered: their membership IS their column.
            function floatWsMap() {
                if (!prefs._floatWs || typeof prefs._floatWs !== 'object'
                    || Array.isArray(prefs._floatWs)) prefs._floatWs = {};
                return prefs._floatWs;
            }
            function keyWsId(key) {
                const v = floatWsMap()[key];
                if (v === null) return null;            // explicit all-workspaces
                return (typeof v === 'string') ? v : undefined;
            }
            function windowWsId(win) { return win ? keyWsId(win.id) : undefined; }
            function setWindowWs(win, wsId, render) {
                if (!win) return;
                floatWsMap()[win.id] = wsId;
                savePrefsLocal();                        // per-window, browser-local
                if (render) { applyWorkspaceVisibility(); applyTaskbarWorkspace(); }
            }
            function setWindowAllWorkspaces(win, all) {
                setWindowWs(win, all ? null : activeWorkspaceId(), true);
            }
            function floatingOnWs(wsId) {
                const out = [];
                for (const win of windows.values()) {
                    if (win.disposed || win.tiled) continue;
                    if (windowWsId(win) === wsId) out.push(win);
                }
                return out;
            }
            function anyFloatingOnWs(wsId) { return floatingOnWs(wsId).length > 0; }
            function countFloatingOnWs(wsId) { return floatingOnWs(wsId).length; }
            function reassignFloatingWs(fromWsId, toWsId) {
                const m = floatWsMap();
                for (const k of Object.keys(m)) if (m[k] === fromWsId) m[k] = toWsId;
                savePrefsLocal();
            }
            // ---- placement / activation helpers (#152) -----------------------
            // Does this float belong to a workspace OTHER than the active one?
            // Answered from MEMBERSHIP, never from the ws-hidden class: the class is
            // a presentation echo that a setWindowWs(render:false) can leave stale in
            // either direction. Tiled (membership IS the column), all-workspaces
            // (null) and unassigned (undefined — adopted on sight) are all "not off".
            function windowOffActiveWs(win) {
                if (!win || win.tiled) return false;
                const wsId = windowWsId(win);
                return (typeof wsId === 'string') && wsId !== activeWorkspaceId();
            }
            // Stamp a floating window's workspace at CREATION, and mask it in the
            // same frame. Without this the stamp only lands later — from
            // applyWorkspaceVisibility on the 2 s taskbar poll, on /state adopt, or
            // inside switchWorkspace — with two consequences: a window that belongs
            // to another workspace paints where you are and flicks away a tick later,
            // and an unassigned window born on ws A just before a switch to B is
            // stamped B (switchWorkspace sets activeId BEFORE it applies visibility).
            function adoptFloatWorkspace(win) {
                if (!win || win.disposed || win.tiled) return;
                const wsId = windowWsId(win);
                // Reconcile membership exactly as applyWorkspaceVisibility does, or
                // the creation stamp and the poll disagree for two seconds: an
                // unassigned window adopts the active ws, and one pointing at a
                // workspace that no longer exists (removed while this key had no
                // window) is HEALED here. Without the heal, a dangling ref would
                // mask the window against a workspace nothing can switch to — the
                // poll would fix it a tick later, turning "paints then vanishes"
                // into "does nothing, then appears", which is worse in a throttled
                // background tab.
                const liveWsIds = new Set(wsList().map(w => w.id));
                if (wsId === undefined
                    || (typeof wsId === 'string' && !liveWsIds.has(wsId))) {
                    setWindowWs(win, activeWorkspaceId(), false);   // masked below
                }
                const hide = windowOffActiveWs(win);
                win.dom.classList.toggle('ws-hidden', hide);
                // Same invariant applyWorkspaceVisibility holds: a masked window is
                // never the front window. A window this fresh can't already be front,
                // but this is the only other place the mask is applied, so it owns
                // the rule rather than assuming its callers do.
                if (hide && frontId === win.id) { frontId = null; updateTaskbarActive(); }
                applyTaskbarWorkspace();   // else the chip's ws badge lags a poll
            }
            // Mask floating windows that don't belong to the active workspace;
            // lazily lock an unassigned (new / pre-feature) window to the current
            // workspace. Idempotent — safe to call every poll tick.
            function applyWorkspaceVisibility() {
                const activeId = activeWorkspaceId();
                // Membership ids live in browser-local prefs._floatWs while the
                // workspace SET lives in the /state-synced layout; an adopt that
                // rebuilds the layout (minted fresh ws id) can leave a float
                // pointing at a ws that no longer exists. Treat such a dangling
                // ref like an unassigned window: re-home to the active ws so it
                // shows, and persist the heal so it can't recur. Snapshot the live
                // ws ids once — this runs every poll tick — and note setWindowWs
                // below never mutates the layout, so the set stays valid.
                const liveWsIds = new Set(wsList().map(w => w.id));
                let frontCleared = false;
                for (const win of windows.values()) {
                    if (win.disposed || win.tiled) continue;
                    let wsId = windowWsId(win);
                    if (wsId === undefined) { setWindowWs(win, activeId, false); wsId = activeId; }
                    else if (typeof wsId === 'string' && !liveWsIds.has(wsId)) {  // dangling ref
                        setWindowWs(win, activeId, false); wsId = activeId;  // re-home + heal
                    }
                    const show = (wsId === null) || (wsId === activeId);
                    if (win.dom.classList.contains('ws-hidden') !== !show) {
                        win.dom.classList.toggle('ws-hidden', !show);
                        if (!show && frontId === win.id) { frontId = null; frontCleared = true; }
                        // Masked -> visible: .ws-hidden is display:none, so the
                        // window had no box while parked and sendResize bailed on
                        // its 0x0 rect. Re-measure now it has one, else the PTY
                        // keeps whatever cols/rows it last saw — for a window
                        // created already-masked (#152), that is none at all.
                        else if (show) refitSoon(win);
                    }
                }
                if (frontCleared) updateTaskbarActive();
            }
            // ---- taskbar workspace indicator (task 16) -----------------------
            // Each taskbar item carries a small ws badge and dims when its window
            // lives on another workspace, so the bar indicates the active ws.
            function workspaceIndexForKey(key) {
                const loc = findKeyInLayout(key);
                if (loc) return workspaceIndexById(columnWsId(loc.col));  // tiled membership
                const wsId = keyWsId(key);
                if (typeof wsId === 'string') {
                    const idx = workspaceIndexById(wsId);
                    if (idx >= 0) return idx;
                }
                return null;                            // all-workspaces / unknown
            }
            function applyTaskbarWorkspace() {
                const list = wsList();
                const active = activeWorkspaceIndex();
                const hideOther = !!hideTaskbarOtherWs();
                document.querySelectorAll('#taskbar-items .taskbar-item').forEach(el => {
                    const key = el.dataset.sessionId || '';
                    const wsi = workspaceIndexForKey(key);
                    let badge = el.querySelector('.ti-ws');
                    if (wsi === null || wsi < 0) {
                        if (badge) badge.remove();
                        el.classList.remove('other-ws');
                        el.classList.remove('ws-hidden');   // all-ws items never hidden
                        return;
                    }
                    if (!badge) {
                        badge = document.createElement('span');
                        badge.className = 'ti-ws';
                        el.appendChild(badge);
                    }
                    const ws = list[wsi];
                    badge.textContent = (ws && ws.name) ? ws.name : ('ws' + (wsi + 1));
                    el.classList.toggle('other-ws', wsi !== active);
                    el.classList.toggle('ws-hidden', hideOther && wsi !== active);
                });
                reorderTaskbarItems();
            }

            // Render the taskbar pager. Called from every workspace-state change,
            // since the pager shows in floating mode too.
            function renderWorkspaces() {
                renderWsDots(document.getElementById('ws-pager'));
            }
            // Push the active workspace's remembered focused column back onto the
            // layout. The strip's filter does the actual showing/hiding.
            function applyWorkspaceView() {
                const L = getLayout();
                const entry = activeWorkspaceEntry();
                const n = workspaceColumns(activeWorkspaceId()).length;
                L.focusedCol = Math.max(0,
                    Math.min(entry ? entry.focusedCol : 0, Math.max(0, n - 1)));
            }
            // focusedCol means different things to us and to a browser WITHOUT
            // this mod: it writes an index over ALL columns, we read one over the
            // visible subset, and core's reconcile clamps only against the storage
            // count. Clamp it to what is actually on screen (an out-of-range value
            // focuses nothing at all), then mirror it into the active workspace
            // entry so switching away and back returns to the right column.
            function syncFocusedCol() {
                const L = getLayout();
                const n = visibleColumns().length;
                const clamped = Math.max(0, Math.min(L.focusedCol | 0, Math.max(0, n - 1)));
                if (L.focusedCol !== clamped) L.focusedCol = clamped;
                const entry = activeWorkspaceEntry();
                if (entry) entry.focusedCol = clamped;
            }
            function switchWorkspace(index) {
                const st = wsStore();
                if (index < 0 || index >= st.list.length) return;
                const target = st.list[index];
                if (!target || target.id === st.activeId) return;
                // Park the currently-visible windows first, so the relayout's
                // unused-colEl cleanup never removes a live window's dom.
                for (const col of visibleColumns()) {
                    for (const k of columnKeys(col)) parkWindow(windows.get(k));
                }
                const cur = activeWorkspaceEntry();
                if (cur) cur.focusedCol = getLayout().focusedCol;   // remember where we were
                st.activeId = target.id;
                applyWorkspaceView();
                savePrefs();
                renderWorkspaces();               // pager reflects the switch now
                applyWorkspaceVisibility();       // show only this ws's floating wins
                relayoutStrip();                  // mounts the new workspace
                applyTaskbarWorkspace();          // taskbar dims off-ws items
                // Focus the new workspace's focused column.
                const fcol = visibleColumns()[getLayout().focusedCol];
                if (fcol) {
                    // Prefer the focused column's active (visible) tab.
                    const fk = firstLiveKeyInColumn(fcol);
                    if (fk) bringToFront(fk);
                }
            }
            function addWorkspace() {
                const st = wsStore();
                st.list.push({ id: mintLayoutId(), focusedCol: 0 });
                savePrefs();
                renderWorkspaces();               // new dot shows even before switch
                switchWorkspace(st.list.length - 1);
            }
            // Move a tiled window to another workspace. Its column is RE-STAMPED
            // (and moved beside that workspace's other columns so the strip and the
            // taskbar keep a sensible left-to-right order) — never copied, so no
            // window can be lost. If the window shared a column, it is expelled into
            // its own column first, exactly as "Move to own column" would.
            function sendWindowToWorkspace(win, targetIndex) {
                if (!win || win.disposed) return;
                const st = wsStore();
                const target = st.list[targetIndex];
                if (!target) return;
                let loc = findKeyInLayout(win.id);
                if (!loc || columnWsId(loc.col, st) === target.id) return;
                const focusPrecap = (frontId === win.id)
                    ? captureTiledFocusContext(win.id) : null;
                const L = getLayout();
                const shared = (loc.col.rows.length > 1 || rowKeys(loc.row).length > 1);
                if (shared) {
                    expelToNewColumn(win);        // its own column, adjacent to the old
                    loc = findKeyInLayout(win.id);
                    if (!loc) return;
                }
                const col = loc.col;
                col.wsId = target.id;
                // Re-seat it next to the target workspace's last column so storage
                // order stays grouped (the taskbar's spatial order reads it).
                const peers = workspaceColumns(target.id).filter(c => c !== col);
                const at = peers.length
                    ? L.columns.indexOf(peers[peers.length - 1]) + 1 : L.columns.length;
                const from = L.columns.indexOf(col);
                if (from !== -1 && at !== from) {
                    L.columns.splice(from, 1);
                    L.columns.splice(at > from ? at - 1 : at, 0, col);
                }
                target.focusedCol = Math.max(0,
                    workspaceColumns(target.id).indexOf(col));
                savePrefs();
                parkWindow(win);                  // belongs to a hidden column now
                // The moved window is no longer visible, so it must not stay the
                // active/front window — drop frontId; reconcile may set a new one.
                if (frontId === win.id) frontId = null;
                applyWorkspaceView();
                requestRelayout();
                renderWorkspaces();
                applyTaskbarWorkspace();
                updateTaskbarActive();
                if (focusPrecap) {
                    requestAnimationFrame(() => reconcileTiledFocus(focusPrecap));
                }
            }
            // Send to a brand-new workspace (the title-bar menu's last entry).
            function sendWindowToNewWorkspace(win) {
                const st = wsStore();
                st.list.push({ id: mintLayoutId(), focusedCol: 0 });
                savePrefs();
                sendWindowToWorkspace(win, st.list.length - 1);
            }

                // ---- seams ------------------------------------------------
                // The one decision everything else hangs off: the strip draws
                // only the active workspace's columns.
                ctx.desktop.columnFilter(function (col, L) {
                    const st = (L.mods && L.mods.workspaces) || null;
                    if (!st || !Array.isArray(st.list) || !st.list.length) return true;
                    const id = col && col.wsId;
                    if (typeof id !== 'string') return true;   // unstamped -> adopted here
                    if (!st.list.some(function (w) { return w.id === id; })) return true;
                    return id === st.activeId;
                });
                // A column core just made belongs to the workspace you can see.
                ctx.desktop.onColumnCreated(function (col) {
                    if (col && typeof col.wsId !== 'string') col.wsId = activeWorkspaceId();
                });
                ctx.desktop.onPlaced(adoptFloatWorkspace);
                ctx.desktop.onForgotten(function (key) {
                    delete floatWsMap()[key];
                    savePrefsLocal();
                });
                // Invoking a window parked elsewhere must never no-op:
                //   tiled elsewhere -> go THERE (a tiled window's membership IS
                //                      its column, so it cannot be re-homed
                //                      without restructuring the layout)
                //   float elsewhere -> re-home it HERE (the #152 decision)
                //   masked but ours -> repair the stale mask
                // An all-workspaces float (windowWsId === null) is never
                // re-homed: it is not hidden by design.
                ctx.desktop.onReveal(function (win) {
                    if (win.tiled) {
                        const loc = findKeyInLayout(win.id);
                        if (loc && visibleColIndex(loc.col) === -1) {
                            const wi = workspaceIndexById(columnWsId(loc.col));
                            if (wi >= 0) switchWorkspace(wi);
                        }
                    } else if (windowOffActiveWs(win)) {
                        setWindowWs(win, activeWorkspaceId(), true);   // re-home + render
                    } else if (win.dom.classList.contains('ws-hidden')) {
                        applyWorkspaceVisibility();                    // repair the mask
                    }
                });
                // Repaint on every relayout: a /state adopt, the active-view
                // rebuild, a layout undo and the float<->tile switch all
                // converge there. All three calls are idempotent.
                ctx.desktop.onLayoutRender(function () {
                    adoptUnstampedColumns();
                    syncFocusedCol();
                    renderWorkspaces();
                    applyWorkspaceVisibility();
                    applyTaskbarWorkspace();
                });
                // ...and on every taskbar rebuild, which re-creates the chips
                // the ws badges hang off.
                ctx.taskbar.onItemsRendered(function () {
                    applyWorkspaceVisibility();
                    applyTaskbarWorkspace();
                });
                // A chip for a window on another workspace switches there first,
                // whether or not that window is currently open.
                ctx.taskbar.interceptActivate(function (key) {
                    const targetWs = workspaceIndexForKey(key);
                    if (targetWs === null || targetWs < 0
                        || targetWs === activeWorkspaceIndex()) return false;
                    switchWorkspace(targetWs);
                    return true;
                });
                // Title-bar menu: "Send to <workspace>" for a tiled window, the
                // all-workspaces toggle for a floating one.
                ctx.registerWindowMenuItems(function (win) {
                    if (!win) return [];
                    if (!win.tiled) {
                        // Workspace membership (task 8): locked to this ws, or
                        // shown on all. windowWsId null = all workspaces.
                        return [{
                            label: (windowWsId(win) === null)
                                ? '\u2713 On all workspaces'
                                : 'Show on all workspaces',
                            enabled: true,
                            action: () => setWindowAllWorkspaces(win, windowWsId(win) !== null),
                        }];
                    }
                    const items = [{ sep: true }];
                    const here = activeWorkspaceIndex();
                    wsList().forEach((ws, wi) => {
                        if (wi === here) return;
                        items.push({ label: 'Send to '
                                         + (ws.name ? ws.name : 'workspace ' + (wi + 1)),
                                     enabled: true,
                                     action: () => sendWindowToWorkspace(win, wi) });
                    });
                    items.push({ label: 'Send to new workspace', enabled: true,
                                 action: () => sendWindowToNewWorkspace(win) });
                    return items;
                });
                // Empty-desktop / strip menu (tiling mode): switcher + New.
                ctx.registerDesktopMenuItems(function () {
                    const items = [];
                    const cur = activeWorkspaceIndex();
                    wsList().forEach((ws, wi) => {
                        items.push({
                            label: (wi === cur ? '\u2713 ' : '   ')
                                + (ws.name ? ws.name : 'Workspace ' + (wi + 1))
                                + ' (' + workspaceColumns(ws.id).length + ')',
                            enabled: true,
                            action: () => switchWorkspace(wi),
                        });
                    });
                    items.push({ label: '   New workspace', enabled: true,
                                 action: addWorkspace });
                    return items;
                });
                // The seven shortcuts. Their ids are VERBATIM the pre-#148 ones:
                // user rebindings are stored by id, and DEFAULT_KEYBINDINGS (54)
                // still carries the same defaults, so nothing about a user's
                // keyboard changes when this becomes a mod.
                ctx.registerKeyActions([
                    { id: 'workspace-prev',  label: 'Previous workspace',
                      run: () => switchWorkspace(activeWorkspaceIndex() - 1) },
                    { id: 'workspace-next',  label: 'Next workspace',
                      run: () => switchWorkspace(activeWorkspaceIndex() + 1) },
                    { id: 'workspace-1',     label: 'Go to workspace 1',
                      run: () => switchWorkspace(0) },
                    { id: 'workspace-2',     label: 'Go to workspace 2',
                      run: () => switchWorkspace(1) },
                    { id: 'workspace-3',     label: 'Go to workspace 3',
                      run: () => switchWorkspace(2) },
                    { id: 'workspace-4',     label: 'Go to workspace 4',
                      run: () => switchWorkspace(3) },
                    { id: 'workspace-5',     label: 'Go to workspace 5',
                      run: () => switchWorkspace(4) },
                ]);

                // ---- teardown ---------------------------------------------
                // Dropping the filter is what un-hides everything (ctx's
                // disposer relayouts), so all that is left is clearing the
                // presentation echoes this mod applied: the float masks and the
                // taskbar badges/dimming. NOTHING in the store is touched --
                // re-enabling restores the same workspaces.
                ctx.onUnload(function () {
                    for (const win of windows.values()) {
                        if (win.disposed || !win.dom) continue;
                        if (!win.dom.classList.contains('ws-hidden')) continue;
                        win.dom.classList.remove('ws-hidden');
                        // .ws-hidden is display:none, so this window had a 0x0 box
                        // and sendResize bailed on it. Re-measure now it has one,
                        // else its PTY keeps whatever cols/rows it last saw.
                        refitSoon(win);
                    }
                    document.querySelectorAll('#taskbar-items .taskbar-item')
                        .forEach(function (el) {
                            el.classList.remove('other-ws');
                            el.classList.remove('ws-hidden');
                            const badge = el.querySelector('.ti-ws');
                            if (badge) badge.remove();
                        });
                    hideWsPreview();
                });

                adoptUnstampedColumns();
                renderWorkspaces();      // pager populated from the first paint
                applyWorkspaceVisibility();
            },
        });
