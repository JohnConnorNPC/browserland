        // ---- custom workspace scrollbar (Part A) --------------------------
        // A slim themed overlay bar (#strip-scrollbar, a sibling of #strip so it
        // survives teardownView) shown whenever the tiling strip can scroll. It
        // never resizes the strip (overlay → no terminal reflow). Two passes:
        //   updateStripScrollbar() — the metrics pass (reads scrollWidth, caches
        //     {maxScroll,thumbW,maxThumbX}, sets thumb width, shows/hides). Run
        //     on layout/option/resize changes, NOT on every scroll frame.
        //   positionStripThumb()  — the cheap per-scroll pass: from the cached
        //     metrics + live scrollLeft, set the thumb's left. Called from
        //     onStripScroll so a scroll burst never re-reads scrollWidth (which
        //     would force a per-frame reflow against onStripScroll's writes).
        let _sbMetrics = null;
        let _sbEls = null;       // {strip,bar,thumb} resolved once (static nodes)
        function sbEls() {
            if (_sbEls) return _sbEls;
            const strip = document.getElementById('strip');
            const bar = document.getElementById('strip-scrollbar');
            if (!strip || !bar) return null;
            const thumb = bar.querySelector('.sb-thumb');
            if (!thumb) return null;
            _sbEls = { strip, bar, thumb };
            return _sbEls;
        }
        function updateStripScrollbar() {
            const els = sbEls();
            if (!els) return;
            const { strip, bar, thumb } = els;
            const cw = strip.clientWidth;
            const sw = strip.scrollWidth;
            const on = getSettings().stripScrollbar && isTilingMode()
                && !_deactivated;
            const canScroll = (sw - cw) > 1;
            if (!on || !canScroll) {
                bar.style.display = 'none';
                _sbMetrics = null;
                return;
            }
            const maxScroll = sw - cw;
            // Clamp to cw so a very narrow viewport (cw < 24) can't make the
            // thumb overflow the track (and maxThumbX stays >= 0).
            const thumbW = Math.min(cw, Math.max(24, Math.round((cw * cw) / sw)));
            const maxThumbX = Math.max(0, cw - thumbW);
            _sbMetrics = { maxScroll, thumbW, maxThumbX };
            thumb.style.width = thumbW + 'px';
            bar.style.display = 'block';
            positionStripThumb();
        }
        function positionStripThumb() {
            const m = _sbMetrics;
            if (!m) return;                       // bar hidden → nothing to do
            const els = sbEls();
            if (!els) return;
            const frac = (m.maxScroll > 0)
                ? Math.max(0, Math.min(1, els.strip.scrollLeft / m.maxScroll)) : 0;
            // transform (compositor-only) instead of `left` so a scroll burst
            // never triggers layout for the thumb.
            els.thumb.style.transform =
                'translateX(' + Math.round(frac * m.maxThumbX) + 'px)';
        }
        // Thumb drag: map the pointer delta to strip.scrollLeft; setting it fires
        // the native scroll event → onStripScroll (floating drag) +
        // positionStripThumb, so we never move the thumb directly here. Wired
        // once at startup (the bar element is static). Document-level move/up
        // listeners mirror the other drag patterns (wireDrag).
        (function wireStripScrollbar() {
            const els = sbEls();
            if (!els) return;
            const { strip, thumb } = els;
            let dragging = false, startX = 0, startScroll = 0;
            function onMove(e) {
                if (!dragging) return;
                const m = _sbMetrics;
                if (!m || m.maxThumbX <= 0) return;
                const frac = (e.clientX - startX) / m.maxThumbX;
                strip.scrollLeft = Math.max(0, Math.min(m.maxScroll,
                    startScroll + frac * m.maxScroll));
                e.preventDefault();
            }
            function onUp() {
                if (!dragging) return;
                dragging = false;
                thumb.classList.remove('dragging');
                document.removeEventListener('mousemove', onMove, true);
                document.removeEventListener('mouseup', onUp, true);
                window.removeEventListener('blur', onUp, true);
            }
            thumb.addEventListener('mousedown', (e) => {
                if (e.button !== 0 || !_sbMetrics) return;
                dragging = true;
                startX = e.clientX;
                startScroll = strip.scrollLeft;
                thumb.classList.add('dragging');
                document.addEventListener('mousemove', onMove, true);
                document.addEventListener('mouseup', onUp, true);
                // Lost focus mid-drag (alt-tab, etc.) never delivers mouseup →
                // release on blur so the thumb can't stick in 'grabbing'.
                window.addEventListener('blur', onUp, true);
                e.preventDefault();
                e.stopPropagation();
            });
        })();

        // ---- floating scroll-lock -----------------------------------------
        // Default-unlocked floating windows travel with the strip: on every
        // strip scroll we shift each unlocked, non-minimized floating window's
        // geom.left (kept screen-relative, so drag/resize/clamp stay unchanged)
        // by the scroll delta, gluing it to the columns underneath. Locked
        // windows stay pinned to the screen. geom.left is updated in memory
        // only — pref.geom persists at the last drag/resize, so a reload
        // restores the window near where the user last placed it.
        let _lastStripScroll = 0;
        function onStripScroll() {
            const strip = document.getElementById('strip');
            if (!strip) return;
            const sl = strip.scrollLeft;
            const delta = sl - _lastStripScroll;
            _lastStripScroll = sl;
            if (!delta) return;
            for (const win of windows.values()) {
                if (win.disposed || win.tiled || win.minimized || win.locked) continue;
                win.geom.left -= delta;
                win.dom.style.left = win.geom.left + 'px';
            }
            positionStripThumb();   // cheap: cached metrics + live scrollLeft
        }
        function setWindowLocked(win, locked) {
            if (!win || win.disposed) return;
            win.locked = !!locked;
            getPref(win.id).locked = win.locked;
            win.dom.classList.toggle('scroll-locked', win.locked);
            savePrefs();
        }
        function toggleWindowLock(win) {
            if (!win || win.disposed) return;
            // App windows persist lock state in the app store — the prefs-backed
            // setWindowLocked path would write an 'app:' key the prefs GC then
            // clobbers, silently losing the pin/unpin choice across a poll.
            if (win.type === 'app') {
                win.locked = !win.locked;
                win.dom.classList.toggle('scroll-locked', win.locked);
                saveAppWindow(win);
                return;
            }
            setWindowLocked(win, !win.locked);
        }

        // ---- layer moves (float <-> tile) ---------------------------------
        // placeWindowTiled: make a window tiled NOW (synchronous relayout so a
        // freshly-opened window measures its final tiled box before the term
        // first renders). Ensures membership without resizing existing columns.
        function placeWindowTiled(win) {
            win.tiled = true;
            getPref(win.id).tiled = true;
            if (!win.floatGeom) {
                const pf = getPref(win.id).floatGeom;
                win.floatGeom = pf
                    ? Object.assign({}, pf)
                    : currentFloatGeom(win);
            }
            if (!findKeyInLayout(win.id)) {
                layoutAddColumn(win.id, DEFAULT_NEW_PRESET);
            }
            win.dom.classList.add('tiled');
            win.dom.style.left = '';
            win.dom.style.top = '';
            win.dom.style.width = '';
            win.dom.style.height = '';
            win.dom.style.zIndex = '';
            // A window whose membership is in an INACTIVE workspace (e.g. a
            // reattach after reload into a parked workspace) is parked, not
            // measured — it mounts and resizes when its workspace is shown.
            const loc = findKeyInLayout(win.id);
            if (loc && loc.wsIndex !== getLayout().activeWs) {
                parkWindow(win);
                return;
            }
            relayoutStrip();
        }
        // The window's current floating box. Uses the live laid-out rect when
        // visible; falls back to the tracked geom for a minimized/display:none
        // window (whose offset* are all 0 — capturing those would break a
        // later un-tile's geometry restore).
        function currentFloatGeom(win) {
            if (!win.minimized) {
                const w = win.dom.offsetWidth, h = win.dom.offsetHeight;
                if (w > 0 && h > 0) {
                    return { left: win.dom.offsetLeft, top: win.dom.offsetTop,
                             width: w, height: h };
                }
            }
            const g = win.geom || getPref(win.id).geom || defaultGeom();
            return { left: g.left | 0, top: g.top | 0,
                     width: g.width | 0, height: g.height | 0 };
        }
        // attachToStrip: float -> tile, snapshotting the floating geom so an
        // un-tile can restore the hand-arranged box.
        function attachToStrip(win, atIndex) {
            if (!win || win.disposed) return;
            win.floatGeom = currentFloatGeom(win);
            getPref(win.id).floatGeom = Object.assign({}, win.floatGeom);
            if (!findKeyInLayout(win.id)) {
                layoutAddColumn(win.id, DEFAULT_NEW_PRESET, atIndex);
            }
            placeWindowTiled(win);
            bringToFront(win.id);
            // App windows: snapshot the float-box to the app store (their
            // prefs 'app:' key is junk the GC drops; appStore is authoritative
            // for content/geom/lock). Tiling membership lives in _layout.
            if (win.type === 'app') saveAppWindow(win);
        }
        // detachToFloat: tile -> float, restoring the snapshotted geom (or a
        // fresh default), reparenting back above the strip.
        function detachToFloat(win) {
            if (!win || win.disposed) return;
            layoutRemoveKey(win.id);
            getPref(win.id).tiled = false;
            win.tiled = false;
            win.dom.classList.remove('tiled');
            // A window floated straight out of a tabbed column (e.g. an
            // inactive tab) must not stay display:none (task 10).
            win.dom.classList.remove('tab-hidden');
            win.dom.style.flex = '';
            const desktop = document.getElementById('desktop');
            desktop.appendChild(win.dom);
            const geom = clampGeom(win.floatGeom
                || getPref(win.id).floatGeom || defaultGeom());
            applyGeomToWindow(win, geom);
            savePrefs();
            bringToFront(win.id);
            requestRelayout();
            refitSoon(win);
        }
        // removeFromStrip: drop a key from the strip model (close/minimize),
        // collapsing its column and reflowing. The window teardown itself is
        // the caller's job.
        function removeFromStrip(key) {
            layoutRemoveKey(key);
            requestRelayout();
        }

        // ---- vertical workspaces (P5) -------------------------------------
        // Only the active workspace is mounted in #strip; other workspaces'
        // windows are parked in #park (display:none) — their xterm/WebSocket
        // stay alive but the isResizable() guard blocks any resize while parked
        // (zero rect). On activation: mount via relayout, then resize once the
        // boxes have laid out (relayout's own double-RAF tail).
        function parkWindow(win) {
            const park = document.getElementById('park');
            if (park && win && !win.disposed && win.dom.parentElement !== park) {
                park.appendChild(win.dom);
            }
        }
        // Build the workspace dots (1..n + '+') into a container. The
        // churn-guard sig lives on the container itself, so the right-edge
        // rail and the taskbar pager keep INDEPENDENT guards (one rendering
        // never short-circuits the other).
        function wsLabel(ws, i) {
            if (getSettings().wsLabelMode === 'name') {
                return (ws && ws.name) ? ws.name : ('WS' + (i + 1));
            }
            return String(i + 1);
        }
        function renderWsDots(container) {
            if (!container) return;
            const L = getLayout();
            const mode = getSettings().wsLabelMode;
            // Names + mode are part of the signature so a rename / mode toggle
            // forces a rebuild (not just the active-class fast path).
            const sig = mode + '|' + L.activeWs + '|' + L.workspaces.length + '|'
                + L.workspaces.map(w => w.name || '').join('');
            if (container.dataset.sig === sig) {
                container.querySelectorAll('.ws-dot:not(.add)').forEach((d, i) => {
                    d.classList.toggle('active', String(i) === String(L.activeWs));
                });
                return;
            }
            container.dataset.sig = sig;
            container.innerHTML = '';
            L.workspaces.forEach((ws, i) => {
                const d = document.createElement('div');
                d.className = 'ws-dot' + (i === L.activeWs ? ' active' : '')
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
            const L = getLayout();
            const mode = getSettings().wsLabelMode;
            const items = [
                { label: 'Workspace ' + (i + 1)
                    + (L.workspaces[i] && L.workspaces[i].name
                        ? ' — ' + L.workspaces[i].name : ''),
                  enabled: false },
                { label: 'Rename…', enabled: true,
                  action: () => renameWorkspace(i) },
                { label: 'Remove workspace', enabled: L.workspaces.length > 1,
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
            getSettings().wsLabelMode = (mode === 'name') ? 'name' : 'number';
            savePrefs();
            renderWorkspaces();
        }
        function renameWorkspace(i) {
            const L = getLayout();
            const ws = L.workspaces[i];
            if (!ws) return;
            const name = prompt('Workspace ' + (i + 1)
                + ' name (blank = use the number):', ws.name || '');
            if (name === null) return;          // cancelled
            const t = name.trim();
            if (t) {
                ws.name = t.slice(0, 40);
                getSettings().wsLabelMode = 'name';   // naming implies showing names
            } else {
                delete ws.name;
            }
            savePrefs();
            renderWorkspaces();
            applyTaskbarWorkspace();
        }
        function removeWorkspace(i) {
            const L = getLayout();
            if (L.workspaces.length <= 1) {
                showNotice('cannot remove the last workspace');
                return;
            }
            if (i < 0 || i >= L.workspaces.length) return;
            const victim = L.workspaces[i];
            const hasContent = (victim.columns && victim.columns.length)
                || anyFloatingOnWs(victim.id);
            if (hasContent && !confirm('Remove workspace ' + (i + 1)
                + '? Its windows move to an adjacent workspace.')) return;
            const neighborIdx = (i > 0) ? i - 1 : 1;
            const neighbor = L.workspaces[neighborIdx];
            // Move tiled columns into the neighbor (keeps their windows).
            for (const col of victim.columns) neighbor.columns.push(col);
            // Reassign floating windows locked to the victim -> neighbor (by id,
            // so no index bookkeeping is needed across the splice).
            reassignFloatingWs(victim.id, neighbor.id);
            // Park the victim's tiled windows first so the relayout below never
            // tries to mount them under the now-removed workspace.
            for (const col of victim.columns) {
                for (const k of columnKeys(col)) parkWindow(windows.get(k));
            }
            L.workspaces.splice(i, 1);
            // Fix the active index.
            let active = L.activeWs;
            if (active === i) active = Math.max(0, i - 1);
            else if (active > i) active -= 1;
            L.activeWs = Math.max(0, Math.min(active, L.workspaces.length - 1));
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
            const L = getLayout();
            const ws = L.workspaces[i];
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
            // Lay the columns out left-to-right by width fraction, rows top-to-
            // bottom by height fraction — a faithful mini schematic.
            const cols = ws.columns || [];
            let xoff = 0;
            const totalFrac = cols.reduce((a, c) => a + colCurrentFrac(c), 0) || 1;
            cols.forEach((col) => {
                const wfrac = colCurrentFrac(col) / Math.max(1, totalFrac);
                const rowsArr = col.rows || [];
                const hsum = rowsArr.reduce((a, r) => a + (r.height > 0 ? r.height : 0), 0) || 1;
                let yoff = 0;
                rowsArr.forEach((row) => {
                    const hfrac = (row.height > 0 ? row.height : (1 / rowsArr.length)) / hsum;
                    // Draw one labeled rect; a tabbed/group tile gets a "(+N)" badge.
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
                    if (row.mode === 'split' && Array.isArray(row.cells)
                        && row.cells.length) {
                        // One sub-rect per CELL, sized by widths[cell.id] within the
                        // row band; a group cell labels its active tab + "(+N)".
                        const cells = row.cells;
                        const wsum = cells.reduce((a, c) => {
                            const v = row.widths && Number(row.widths[c.id]);
                            return a + ((Number.isFinite(v) && v > 0) ? v : 0);
                        }, 0);
                        let cxoff = 0;
                        cells.forEach((c) => {
                            const v = row.widths && Number(row.widths[c.id]);
                            const cf = wsum > 0
                                ? ((Number.isFinite(v) && v > 0) ? v : 0) / wsum
                                : (1 / cells.length);
                            const isGroup = Array.isArray(c.keys) && c.keys.length >= 2;
                            const labelKey = isGroup
                                ? ((typeof c.activeTab === 'string'
                                    && c.keys.indexOf(c.activeTab) !== -1)
                                    ? c.activeTab : c.keys[0])
                                : (c.keys ? c.keys[0] : undefined);
                            mkRect(xoff + cxoff * wfrac, cf * wfrac, labelKey,
                                isGroup ? (c.keys.length - 1) : 0);
                            cxoff += cf;
                        });
                    } else {
                        const k = (row.mode === 'tabbed'
                            && row.keys.indexOf(row.activeTab) !== -1)
                            ? row.activeTab : rowKeys(row)[0];
                        mkRect(xoff, wfrac, k,
                            (row.mode === 'tabbed' && row.keys.length > 1)
                                ? (row.keys.length - 1) : 0);
                    }
                    yoff += hfrac;
                });
                xoff += wfrac;
            });
            const floatN = countFloatingOnWs(ws.id);
            if (!cols.length && !floatN) {
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
            setWindowWs(win, all ? null : activeWorkspace().id, true);
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
        // Mask floating windows that don't belong to the active workspace;
        // lazily lock an unassigned (new / pre-feature) window to the current
        // workspace. Idempotent — safe to call every poll tick.
        function applyWorkspaceVisibility() {
            const activeId = activeWorkspace().id;
            // Membership ids live in browser-local prefs._floatWs while the
            // workspace SET lives in the /state-synced layout; an adopt that
            // rebuilds the layout (minted fresh ws id) can leave a float
            // pointing at a ws that no longer exists. Treat such a dangling
            // ref like an unassigned window: re-home to the active ws so it
            // shows, and persist the heal so it can't recur. Snapshot the live
            // ws ids once — this runs every poll tick — and note setWindowWs
            // below never mutates the layout, so the set stays valid.
            const liveWsIds = new Set(getLayout().workspaces.map(w => w.id));
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
                }
            }
            if (frontCleared) updateTaskbarActive();
        }
        // ---- taskbar workspace indicator (task 16) -----------------------
        // Each taskbar item carries a small ws badge and dims when its window
        // lives on another workspace, so the bar indicates the active ws.
        function workspaceIndexForKey(key) {
            const loc = findKeyInLayout(key);
            if (loc) return loc.wsIndex;            // tiled membership
            const wsId = keyWsId(key);
            if (typeof wsId === 'string') {
                const idx = getLayout().workspaces.findIndex(w => w.id === wsId);
                if (idx >= 0) return idx;
            }
            return null;                            // all-workspaces / unknown
        }
        function applyTaskbarWorkspace() {
            const L = getLayout();
            const active = L.activeWs;
            const hideOther = !!getSettings().hideTaskbarOtherWs;
            document.querySelectorAll('#taskbar-items .taskbar-item').forEach(el => {
                const key = el.dataset.sessionId || '';
                const wsi = workspaceIndexForKey(key);
                let badge = el.querySelector('.ti-ws');
                if (wsi === null) {
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
                const ws = L.workspaces[wsi];
                badge.textContent = (ws && ws.name) ? ws.name : ('ws' + (wsi + 1));
                el.classList.toggle('other-ws', wsi !== active);
                el.classList.toggle('ws-hidden', hideOther && wsi !== active);
            });
            reorderTaskbarItems();
        }

        // Spatial rank per window key: a Map<key -> rank> built by walking the
        // layout in the SAME render order the strip uses, so taskbar order can
        // never disagree with the screen. For each workspace (in index order):
        // its tiled columns L->R — columnKeys descends rows/cells/tabs in
        // on-screen order, so hidden tabs of a tab group land adjacent at the
        // group's slot — then that workspace's floating windows sorted top->left.
        // Keys absent here (closed / remote-only sessions, all-workspaces floats
        // whose windowWsId is null) trail as positionless in reorderTaskbarItems.
        // O(layout + floats); reuses columnKeys/rowKeys — no per-chip scan.
        function spatialKeyOrder() {
            const order = new Map();
            let rank = 0;
            const num = (v) => { const n = Number(v); return Number.isFinite(n) ? n : 0; };
            const L = getLayout();
            for (const ws of L.workspaces) {
                if (Array.isArray(ws.columns)) {
                    for (const col of ws.columns) {
                        for (const k of columnKeys(col)) {
                            const key = String(k);
                            if (!order.has(key)) order.set(key, rank++);
                        }
                    }
                }
                // Floating windows belonging to THIS workspace, in reading order
                // (geom shape is {left,top,width,height}). all-workspaces floats
                // (windowWsId === null) match no ws.id, so they stay positionless.
                const floats = floatingOnWs(ws.id).slice().sort((a, b) => {
                    const ga = a.geom || {}, gb = b.geom || {};
                    return (num(ga.top) - num(gb.top)) || (num(ga.left) - num(gb.left));
                });
                for (const win of floats) {
                    const key = String(win.id);
                    if (!order.has(key)) order.set(key, rank++);
                }
            }
            return order;
        }

        // Reorder #taskbar-items chips top-left -> bottom-right to match the
        // on-screen tiling layout (incl. tab-group members, which are each their
        // own chip). Stable-sort the live chips by (spatial rank, current DOM
        // index): positionless chips get a finite sentinel (NOT Infinity, whose
        // difference is NaN and corrupts the comparator) so they cluster at the
        // end keeping their existing relative order. Idempotent — bails without
        // touching the DOM when already ordered, so the 2s poll never churns the
        // bar. Scoped to .taskbar-item, so #taskbar-empty is never moved.
        function reorderTaskbarItems() {
            const host = document.getElementById('taskbar-items');
            if (!host) return;
            const nodes = Array.from(host.querySelectorAll('.taskbar-item'));
            if (nodes.length < 2) return;
            const order = spatialKeyOrder();
            const SENTINEL = Number.MAX_SAFE_INTEGER;
            const decorated = nodes.map((el, i) => {
                const r = order.get(el.dataset.sessionId || '');
                return { el, rank: (typeof r === 'number') ? r : SENTINEL, i };
            });
            decorated.sort((a, b) => (a.rank - b.rank) || (a.i - b.i));
            let changed = false;
            for (let i = 0; i < decorated.length; i++) {
                if (decorated[i].el !== nodes[i]) { changed = true; break; }
            }
            if (!changed) return;                 // already ordered — no churn
            // appendChild MOVES an existing node (click closures survive).
            for (const d of decorated) host.appendChild(d.el);
            // Keep the "no sessions" placeholder trailing if it happens to
            // coexist with chips (e.g. only floating app docs, no server
            // sessions) — re-appending chips above would otherwise strand it.
            const empty = document.getElementById('taskbar-empty');
            if (empty && empty.parentElement === host) host.appendChild(empty);
        }

        // Render both switchers: the right-edge rail (CSS-gated to tiling mode)
        // and the always-visible taskbar pager. Called from every
        // workspace-state change, since the pager shows in floating mode too.
        function renderWorkspaces() {
            renderWsDots(document.getElementById('ws-pager'));
        }
        function switchWorkspace(index) {
            const L = getLayout();
            if (index < 0 || index >= L.workspaces.length || index === L.activeWs) return;
            // Park the currently-active workspace's live windows first, so the
            // relayout's unused-colEl cleanup never removes a live window's dom.
            const cur = L.workspaces[L.activeWs];
            for (const col of cur.columns) {
                for (const k of columnKeys(col)) parkWindow(windows.get(k));
            }
            L.activeWs = index;
            savePrefs();
            renderWorkspaces();               // pager/rail reflect the switch now
            applyWorkspaceVisibility();       // show only this ws's floating wins
            relayoutStrip();                  // mounts the new workspace
            applyTaskbarWorkspace();          // taskbar dims off-ws items
            // Focus the new workspace's focused column.
            const nws = L.workspaces[index];
            const fcol = nws.columns[nws.focusedCol];
            if (fcol) {
                // Prefer the focused column's active (visible) tab.
                const fk = firstLiveKeyInColumn(fcol);
                if (fk) bringToFront(fk);
            }
        }
        function addWorkspace() {
            const L = getLayout();
            L.workspaces.push(newWorkspace());
            savePrefs();
            renderWorkspaces();               // new dot shows even before switch
            switchWorkspace(L.workspaces.length - 1);
        }
        // Move a tiled window to another workspace as a new column. It lands in
        // an inactive workspace, so it is parked (not measured) until shown.
        function sendWindowToWorkspace(win, targetIndex) {
            if (!win || win.disposed) return;
            const L = getLayout();
            if (targetIndex < 0 || targetIndex >= L.workspaces.length) return;
            const loc = findKeyInLayout(win.id);
            if (!loc || loc.wsIndex === targetIndex) return;
            const focusPrecap = (frontId === win.id)
                ? captureTiledFocusContext(win.id) : null;
            removeKeyFromLayout(win.id);       // collapse source row/col, heal focus
            const tgt = L.workspaces[targetIndex];
            const col = newColumn();
            col.rows = [newRow([win.id], 1)];
            tgt.columns.push(col);
            tgt.focusedCol = tgt.columns.length - 1;
            savePrefs();
            parkWindow(win);                  // belongs to an inactive ws now
            // The moved window is no longer visible, so it must not stay the
            // active/front window — drop frontId; reconcile may set a new one.
            if (frontId === win.id) frontId = null;
            requestRelayout();
            updateTaskbarActive();
            if (focusPrecap) {
                requestAnimationFrame(() => reconcileTiledFocus(focusPrecap));
            }
        }

