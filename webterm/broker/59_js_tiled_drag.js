        // ---- P4: tiled title-bar drag (consume / reorder / detach) --------
        // During a drag NOTHING in _layout changes and the window is never
        // reparented — only viewport-fixed overlays move. Exactly one mutator
        // runs on drop (= one relayout = one reparent). rAF-throttled.
        const DRAG_THRESHOLD = 5;
        const FLOAT_BAND = 90;        // bottom desktop band -> detach to float
        const EDGE_SCROLL = 44;       // edge zone width for auto-scroll
        const EDGE_SCROLL_PX = 26;    // px per tick
        const TAB_BAND = 36;          // top band of a WINDOW -> tab into its tile
        // Hold-to-snap (dwell a FLOATING window into the tiling grid): while
        // dragging a floating window, parking it still for the dwell delay
        // switches the drag into snap mode (same drop overlays the tiled drag
        // uses); release commits, the top-left zone / Escape cancels back to the
        // pre-drag box. The MIRROR gesture (tile -> float pop-out) reuses the
        // same dwell in startTiledDrag. HOLD_MS is only the DEFAULT — the live
        // delay is per-host and resolved by snapHoldMsFor(win) (0 disables).
        const HOLD_MS = 3000;         // default dwell before a drag offers snap / pop-out
        const DWELL_MOVE = 6;         // px; moving more than this resets the dwell timer
        const SNAP_CANCEL = 96;       // px; top-left "return to floating" safe zone
        // #38: the dwell delay (ms) a still hold must hold before a drag offers
        // to snap (float -> tile) or pop out (tile -> float), resolved per WINDOW
        // so a remote window honors ITS broker's configured delay:
        //   - window.__HOLD_MS__ (a positive test override) always wins;
        //   - a local window reads the live local settings;
        //   - a remote window reads its host's cached settings blob;
        //   - 0 disables the gesture; anything missing/invalid falls to HOLD_MS.
        // normalizeSettings clamps stored values, so this only re-guards the
        // remote/override paths.
        function snapHoldMsFor(win) {
            const ov = (window.__HOLD_MS__ | 0);
            if (ov > 0) return ov;
            let ms;
            const hid = win && win.hostId;
            if (!hid || hid === 'local') {
                ms = getSettings().snapHoldMs;
            } else {
                const entry = hostStateCache.get(hid);
                ms = entry && entry.settings && entry.settings.snapHoldMs;
            }
            if (ms === 0) return 0;                       // explicit disable
            return (typeof ms === 'number' && ms > 0) ? ms : HOLD_MS;
        }
        function dropEls() {
            return {
                vbar: document.getElementById('drop-vbar'),
                col: document.getElementById('drop-col'),
                flt: document.getElementById('drop-float'),
            };
        }
        function hideDropEls() {
            const { vbar, col, flt } = dropEls();
            vbar.classList.remove('show');
            col.classList.remove('show');
            flt.classList.remove('show');
        }
        // ---- hold-to-snap cancel zone (top-left "return to floating") -------
        // Positioned at the #desktop top-left so it never overlaps the taskbar;
        // sized SNAP_CANCEL square. Hit-test mirrors the placement exactly.
        function snapCancelRect() {
            const d = document.getElementById('desktop').getBoundingClientRect();
            return { left: d.left, top: d.top, w: SNAP_CANCEL, h: SNAP_CANCEL };
        }
        function showSnapCancel() {
            const el = document.getElementById('snap-cancel');
            const r = snapCancelRect();
            el.style.left = r.left + 'px';
            el.style.top = r.top + 'px';
            el.style.width = r.w + 'px';
            el.style.height = r.h + 'px';
            el.classList.add('show');
        }
        function hideSnapCancel() {
            document.getElementById('snap-cancel').classList.remove('show');
        }
        function inSnapCancel(cx, cy) {
            const r = snapCancelRect();
            return cx >= r.left && cx < r.left + r.w &&
                   cy >= r.top && cy < r.top + r.h;
        }
        // Which live tiled window row inside `col` is under (cx,cy)? Returns
        // {key, rect} or null. Used for per-window directional splits (task 1).
        function windowRowAt(col, cx, cy) {
            for (const k of columnKeys(col)) {
                const w = windows.get(k);
                if (!w || w.disposed || w.minimized || !w.tiled) continue;
                if (hostHidden(w.hostId)) continue;
                const wr = w.dom.getBoundingClientRect();
                if (wr.width <= 0 || wr.height <= 0) continue;
                if (cx >= wr.left && cx < wr.right &&
                    cy >= wr.top && cy < wr.bottom) {
                    return { key: k, rect: wr };
                }
            }
            return null;
        }
        // allowFloat=false suppresses the bottom-band "detach to float" target so
        // a low drop still tiles — snap mode uses this (its only cancels are the
        // top-left zone + Escape, not a second float band). Default true keeps the
        // tiled title-bar drag's existing detach-to-float behavior.
        function computeDropTarget(cx, cy, allowFloat = true) {
            const strip = document.getElementById('strip');
            const desktop = document.getElementById('desktop');
            const dRect = desktop.getBoundingClientRect();
            const ws = activeWorkspace();
            if (allowFloat && cy > dRect.bottom - FLOAT_BAND) return { kind: 'float' };
            const colEls = Array.from(strip.querySelectorAll('.strip-col'));
            if (colEls.length === 0) return { kind: 'newcol', index: 0 };
            for (const colEl of colEls) {
                const r = colEl.getBoundingClientRect();
                if (cx >= r.left && cx < r.right) {
                    const idx = ws.columns.findIndex(c => c.id === colEl.dataset.colId);
                    if (idx === -1) return { kind: 'newcol', index: ws.columns.length };
                    const edge = Math.min(60, r.width * 0.25);
                    if (cx < r.left + edge)
                        return { kind: 'newcol', index: idx, x: r.left };
                    if (cx > r.right - edge)
                        return { kind: 'newcol', index: idx + 1, x: r.right };
                    // Resolve the specific window under the cursor.
                    const col = ws.columns.find(c => c.id === colEl.dataset.colId);
                    const row = col ? windowRowAt(col, cx, cy) : null;
                    if (row) {
                        const wr = row.rect;
                        // Top band of THIS window -> tab into its tile (per-tile
                        // tab groups). Targets the window, not the whole column,
                        // so only that tile becomes a tab strip.
                        if (cy < wr.top + TAB_BAND)
                            return { kind: 'tab', targetKey: row.key, rect: wr };
                        // Otherwise a directional split (task 1): subdivide the
                        // hovered window into L/R/T/B triangular quadrants
                        // (nearest-edge wins, like editor split-drop UIs):
                        // left/right => split the tile band horizontally beside
                        // the target (F-HSPLIT); top/bottom => new stacked row.
                        const fx = (cx - wr.left) / (wr.width || 1);
                        const fy = (cy - wr.top) / (wr.height || 1);
                        const dL = fx, dR = 1 - fx, dT = fy, dB = 1 - fy;
                        const m = Math.min(dL, dR, dT, dB);
                        const dir = (m === dT) ? 'top' : (m === dB) ? 'bottom'
                                  : (m === dL) ? 'left' : 'right';
                        // (F-NESTSPLIT) A TABBED tile's L/R interior now WRAPS into a
                        // split `[tabgroup │ newcomer]` in-band (the chosen UX), so it
                        // returns a 'split' target like any other tile — no longer the
                        // old tabbed-L/R→new-column special case.
                        return { kind: 'split', dir, targetKey: row.key, rect: wr };
                    }
                    // Column body with no resolvable window row (e.g. over a tab
                    // strip or padding) -> stack as a new bottom row.
                    return { kind: 'consume', colId: colEl.dataset.colId, rect: r };
                }
            }
            // Left of the first / right of the last rendered column.
            const firstR = colEls[0].getBoundingClientRect();
            if (cx < firstR.left) return { kind: 'newcol', index: 0, x: firstR.left };
            const lastEl = colEls[colEls.length - 1];
            const lastR = lastEl.getBoundingClientRect();
            const li = ws.columns.findIndex(c => c.id === lastEl.dataset.colId);
            return { kind: 'newcol',
                     index: (li === -1 ? ws.columns.length : li + 1), x: lastR.right };
        }
        function showDrop(target) {
            const { vbar, col, flt } = dropEls();
            const strip = document.getElementById('strip');
            const sRect = strip.getBoundingClientRect();
            hideDropEls();
            if (!target) return;
            if (target.kind === 'float') {
                const d = document.getElementById('desktop').getBoundingClientRect();
                flt.style.top = (d.bottom - FLOAT_BAND) + 'px';
                flt.style.height = FLOAT_BAND + 'px';
                flt.classList.add('show');
            } else if (target.kind === 'consume') {
                const r = target.rect;
                col.style.left = r.left + 'px';
                col.style.top = r.top + 'px';
                col.style.width = r.width + 'px';
                col.style.height = r.height + 'px';
                col.classList.add('show');
            } else if (target.kind === 'tab') {
                // Highlight just the top band so the drop reads as "tab here".
                const r = target.rect;
                col.style.left = r.left + 'px';
                col.style.top = r.top + 'px';
                col.style.width = r.width + 'px';
                col.style.height = TAB_BAND + 'px';
                col.classList.add('show');
            } else if (target.kind === 'split') {
                // Shade the half of the target window the split would fill.
                const r = target.rect;
                let left = r.left, top = r.top, w = r.width, h = r.height;
                if (target.dir === 'left') { w = r.width / 2; }
                else if (target.dir === 'right') { left = r.left + r.width / 2; w = r.width / 2; }
                else if (target.dir === 'top') { h = r.height / 2; }
                else if (target.dir === 'bottom') { top = r.top + r.height / 2; h = r.height / 2; }
                col.style.left = left + 'px';
                col.style.top = top + 'px';
                col.style.width = w + 'px';
                col.style.height = h + 'px';
                col.classList.add('show');
            } else {
                const x = (typeof target.x === 'number') ? target.x : sRect.left;
                vbar.style.left = (x - 2) + 'px';
                vbar.style.top = sRect.top + 'px';
                vbar.style.height = sRect.height + 'px';
                vbar.classList.add('show');
            }
        }
        function floatAtCursor(win, cx, cy) {
            if (!win || win.disposed) return;
            const dRect = document.getElementById('desktop').getBoundingClientRect();
            const base = win.floatGeom || getPref(win.id).floatGeom || defaultGeom();
            const w = base.width || DEFAULT_W, h = base.height || DEFAULT_H;
            const left = Math.max(0, Math.min(cx - dRect.left - w / 2, dRect.width - 80));
            const top = Math.max(0, Math.min(cy - dRect.top - 12, dRect.height - 30));
            win.floatGeom = { left, top, width: w, height: h };
            detachToFloat(win);
        }
        function commitDrop(win, target, ev) {
            if (!target || !win || win.disposed) return;
            if (target.kind === 'float') {
                floatAtCursor(win, ev.clientX, ev.clientY);
            } else if (target.kind === 'consume') {
                dragDropConsume(win.id, target.colId);
            } else if (target.kind === 'tab') {
                tabWindowIntoTile(win.id, target.targetKey);
            } else if (target.kind === 'split') {
                splitAtWindow(win.id, target.targetKey, target.dir);
            } else if (target.kind === 'newcol') {
                dragDropNewColumn(win.id, target.index);
            }
        }
        // (F-NESTSPLIT) Move a whole tab-group CELL as a unit (dragging the group's
        // tab-strip background). Reuses the tested single-window drop path: drop the
        // ACTIVE tab via commitDrop, then pull each remaining tab into the active
        // window's NEW tile, so the group reassembles at the destination — a new
        // column -> tabbed column; onto a tile -> merged group; beside a window -> a
        // group cell. Float degrades to floating the active tab (a floating window
        // can't host a tab group). `keys`/`activeTab` are captured at drag start.
        function dragDropMoveGroup(keys, activeTab, target, ev) {
            if (!target) return;
            keys = (Array.isArray(keys) ? keys : []).map(String);
            if (keys.length === 0) return;
            const active = (typeof activeTab === 'string' && keys.indexOf(activeTab) !== -1)
                ? activeTab : keys[0];
            const activeWin = windows.get(active);
            if (!activeWin) return;
            // Dropping onto one of the group's OWN windows is a no-op (move to self).
            if (target.targetKey && keys.indexOf(String(target.targetKey)) !== -1) return;
            // Float can't host a tab group -> dissolve it: float each member (they
            // can't stay grouped while floating). Honors "move the whole group".
            if (target.kind === 'float') {
                for (const k of keys) {
                    const w = windows.get(k);
                    if (w) floatAtCursor(w, ev.clientX, ev.clientY);
                }
                requestAnimationFrame(() => bringToFront(active));
                return;
            }
            const others = keys.filter(k => k !== active);
            // 1) Place the active window at the target via the normal single path.
            const beforeLoc = findKeyInLayout(active);
            commitDrop(activeWin, target, ev);
            const afterLoc = findKeyInLayout(active);
            // If commitDrop was effectively a NO-OP (active stayed in the same row +
            // cell — e.g. a same-column consume, or an alone drop next to itself),
            // skip the re-tab: pulling the others would needlessly reorder the group
            // in place (codex).
            const sameSpot = beforeLoc && afterLoc
                && afterLoc.row === beforeLoc.row && afterLoc.cell === beforeLoc.cell;
            if (afterLoc && !sameSpot) {
                // 2) Reassemble the rest of the group around the moved active
                // window. Move EVERY member, not just live ones — tabWindowIntoTile
                // is LAYOUT-key based (a dormant/phantom multi-host key must travel
                // with its group; bringToFront just no-ops for a windowless key),
                // so guarding on windows.get(k) would strand non-live tabs (codex).
                for (const k of others) tabWindowIntoTile(k, active);
                // Each re-tab made the newcomer active; restore the original tab.
                const loc = findKeyInLayout(active);
                if (loc) {
                    if (loc.cell && loc.cell.keys.indexOf(active) !== -1)
                        loc.cell.activeTab = active;
                    else if (loc.row.mode === 'tabbed'
                             && loc.row.keys.indexOf(active) !== -1)
                        loc.row.activeTab = active;
                    savePrefs();
                    requestRelayout();
                }
            }
            requestAnimationFrame(() => bringToFront(active));
        }
        function startTiledDrag(win, e, groupCtx) {
            // NB: don't preventDefault on mousedown — that would suppress a
            // plain click's native behavior even when no drag starts. It's
            // deferred to the threshold crossing in onMove.
            bringToFront(win.id);
            const strip = document.getElementById('strip');
            const startX = e.clientX, startY = e.clientY;
            let dragging = false, lastX = startX, lastY = startY;
            let target = null, rafId = 0, scrollTimer = 0;
            // #38: tile -> float pop-out (the mirror of the floating-window snap
            // dwell), SINGLE-window only. Holding a tiled drag STILL for the
            // per-host dwell arms a "release to float" mode; moving on again
            // disarms it (so an accidental pause is fully recoverable) and
            // re-arms the clock, so float engages only on a deliberate park.
            // Escape cancels. Skipped for a GROUP drag (a group still floats only
            // via the explicit bottom float-band drop, unchanged) and when the
            // delay is 0 (disabled). The delay is captured ONCE at grab so a
            // mid-drag settings/prefetch change can't make the gesture
            // nondeterministic (codex).
            const holdMs = snapHoldMsFor(win);
            let dwellX = startX, dwellY = startY, holdTimer = 0, floatMode = false;
            const clearHold = () => {
                if (holdTimer) { clearTimeout(holdTimer); holdTimer = 0; }
            };
            const setFloatMode = (on) => {
                if (floatMode === on) return;
                floatMode = on;
                win.dom.classList.toggle('pop-out-arm', on);   // "release to float" cue
            };
            const enterFloat = () => {
                holdTimer = 0;
                if (win.disposed) { finish(false, null); return; }
                if (!dragging) return;          // only after a real drag started
                setFloatMode(true);
                target = { kind: 'float' };
                showDrop(target);
            };
            const armDwell = () => {
                clearHold();
                setFloatMode(false);            // moving on un-floats; re-decide on park
                dwellX = lastX; dwellY = lastY;
                if (groupCtx || holdMs <= 0) return;   // single-window only; 0 disables
                holdTimer = setTimeout(enterFloat, holdMs);
            };
            const frame = () => {
                rafId = 0;
                if (win.disposed) { finish(false, null); return; }
                // While popped out (parked still past the dwell), show the float
                // overlay; a meaningful move re-arms and clears floatMode, so the
                // next frame falls through to normal grid targeting again.
                if (floatMode) { target = { kind: 'float' }; showDrop(target); return; }
                target = computeDropTarget(lastX, lastY);
                showDrop(target);
            };
            const onMove = (ev) => {
                lastX = ev.clientX; lastY = ev.clientY;
                if (!dragging) {
                    if (Math.hypot(ev.clientX - startX, ev.clientY - startY) < DRAG_THRESHOLD)
                        return;
                    dragging = true;
                    ev.preventDefault();   // suppress native selection now, not on click
                    document.body.classList.add('tiled-dragging');
                    win.dom.classList.add('drag-source');
                    armDwell();            // start the pop-out dwell clock
                } else if (Math.hypot(ev.clientX - dwellX, ev.clientY - dwellY) > DWELL_MOVE) {
                    // Meaningful movement re-arms the dwell so pop-out engages
                    // only on a deliberate park — and disarms an already-engaged
                    // floatMode (armDwell -> setFloatMode(false)) so a paused-by-
                    // accident drag recovers by simply moving on.
                    armDwell();
                }
                if (!rafId) rafId = requestAnimationFrame(frame);
            };
            const onUp = (ev) => finish(true, ev);
            const onCtx = (ev) => {
                // Right-click while dragging -> detach to floating at cursor.
                ev.preventDefault();
                ev.stopPropagation();
                ev.stopImmediatePropagation();
                const cancelled = dragging;
                finish(false, null);
                if (cancelled) floatAtCursor(win, lastX, lastY);
            };
            const onKey = (ev) => {
                if (ev.key === 'Escape') { ev.preventDefault(); finish(false, null); }
            };
            // A lost mouseup (window blur / alt-tab mid-drag) must not leave
            // listeners, the interval, or body.tiled-dragging stuck.
            const onBlur = () => finish(false, null);
            function finish(commit, ev) {
                clearHold();
                document.removeEventListener('mousemove', onMove);
                document.removeEventListener('mouseup', onUp);
                document.removeEventListener('contextmenu', onCtx, true);
                document.removeEventListener('keydown', onKey, true);
                window.removeEventListener('blur', onBlur);
                if (rafId) { cancelAnimationFrame(rafId); rafId = 0; }
                if (scrollTimer) { clearInterval(scrollTimer); scrollTimer = 0; }
                document.body.classList.remove('tiled-dragging');
                win.dom.classList.remove('drag-source');
                win.dom.classList.remove('pop-out-arm');
                hideDropEls();
                if (commit && dragging && !win.disposed) {
                    // Recompute on drop: a fast drag-and-release can fire
                    // mouseup before the rAF that would have set `target`.
                    if (!target) target = computeDropTarget(lastX, lastY);
                    // groupCtx -> move the whole tab group as a unit (the active
                    // window `win` is the group's anchor); else the single window.
                    if (groupCtx) dragDropMoveGroup(groupCtx.keys, groupCtx.activeTab,
                                                    target, ev);
                    else commitDrop(win, target, ev);
                }
            }
            // Edge auto-scroll while dragging near the strip's left/right edge.
            scrollTimer = setInterval(() => {
                if (!dragging) return;
                const r = strip.getBoundingClientRect();
                if (lastX < r.left + EDGE_SCROLL) strip.scrollLeft -= EDGE_SCROLL_PX;
                else if (lastX > r.right - EDGE_SCROLL) strip.scrollLeft += EDGE_SCROLL_PX;
            }, 16);
            document.addEventListener('mousemove', onMove);
            document.addEventListener('mouseup', onUp);
            document.addEventListener('contextmenu', onCtx, true);
            document.addEventListener('keydown', onKey, true);
            window.addEventListener('blur', onBlur);
        }

