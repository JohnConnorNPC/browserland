        // ---- open-terminals set (restore-on-refresh) ----------------------
        // A mode-agnostic list of host-qualified keys ('<hostId>:<windowId>')
        // for every open TERMINAL window. Lives on the layout object so it
        // syncs via /state and survives a browser refresh; placement/mode is
        // recovered separately (per-session prefs.tiled + layout columns), so
        // this is just "which terminals were open". App windows are NOT tracked
        // here — they restore via appStore. reconcileLayout leaves unknown
        // top-level fields untouched, so this array rides along; getOpenTerms
        // self-heals a missing/garbage value.
        function getOpenTerms() {
            const L = getLayout();
            if (!Array.isArray(L.openTerms)) L.openTerms = [];
            return L.openTerms;
        }
        function addOpenTerm(key) {
            key = String(key);
            const arr = getOpenTerms();
            if (arr.indexOf(key) !== -1) return;   // set semantics; no churn
            arr.push(key);
            savePrefs();
        }
        function removeOpenTerm(key) {
            key = String(key);
            const arr = getOpenTerms();
            const i = arr.indexOf(key);
            if (i === -1) return;                  // not present; no churn
            arr.splice(i, 1);
            savePrefs();
        }
        function activeWorkspace() {
            const L = getLayout();
            return L.workspaces[L.activeWs];
        }
        // Find the tile (in any workspace) holding a given window key, or null.
        // Returns { ws, wsIndex, col, colIndex, row, rowIndex, cell, cellIndex,
        // tabIndex } where `row` is ALWAYS the top-level Row OBJECT (∈ col.rows)
        // and `rowIndex` its index in col.rows. For a SPLIT row `cell` is the
        // holding cell, `cellIndex` its index in row.cells, and `tabIndex` the
        // key's index WITHIN that cell. For every other row (and a not-yet-
        // migrated legacy split) `cell`=null, `cellIndex`=-1, and `tabIndex` is
        // the key's index in row.keys (back-compat). NEVER returns a nested row.
        function findKeyInLayout(key) {
            const L = getLayout();
            key = String(key);
            for (let wi = 0; wi < L.workspaces.length; wi++) {
                const ws = L.workspaces[wi];
                for (let ci = 0; ci < ws.columns.length; ci++) {
                    const col = ws.columns[ci];
                    const rows = col.rows || [];
                    for (let ri = 0; ri < rows.length; ri++) {
                        const row = rows[ri];
                        // A split row carries keys inside cells: locate the cell and
                        // set cell/cellIndex with tabIndex = index WITHIN that cell.
                        // A non-split row (and a not-yet-migrated legacy split that
                        // still has flat row.keys) keeps the back-compat contract:
                        // cell=null, cellIndex=-1, tabIndex = index in row.keys.
                        // loc.row is ALWAYS the top-level col.rows member.
                        if (row && row.mode === 'split' && Array.isArray(row.cells)) {
                            for (let cx = 0; cx < row.cells.length; cx++) {
                                const cell = row.cells[cx];
                                const ti = (cell && Array.isArray(cell.keys))
                                    ? cell.keys.indexOf(key) : -1;
                                if (ti !== -1) {
                                    return { ws, wsIndex: wi, col, colIndex: ci,
                                             row, rowIndex: ri,
                                             cell, cellIndex: cx, tabIndex: ti };
                                }
                            }
                        } else if (row && Array.isArray(row.keys)) {
                            const ti = row.keys.indexOf(key);
                            if (ti !== -1) {
                                return { ws, wsIndex: wi, col, colIndex: ci,
                                         row, rowIndex: ri,
                                         cell: null, cellIndex: -1, tabIndex: ti };
                            }
                        }
                    }
                }
            }
            return null;
        }
        function isTilingMode() { return getLayout().mode === 'tiling'; }

        // ---- _layout mutators (always followed by a relayout) -------------
        // Default width for a freshly-tiled column. niri appends a NEW column
        // and never resizes existing ones, so opening more windows just grows
        // the strip rightward.
        const DEFAULT_NEW_PRESET = '1/2';
        function layoutAddColumn(key, preset, atIndex) {
            const ws = activeWorkspace();
            const col = newColumn();
            col.widthPreset = (WIDTH_PRESETS.indexOf(preset) !== -1)
                ? preset : DEFAULT_NEW_PRESET;
            col.rows = [newRow([key], 1)];
            const i = Number.isInteger(atIndex)
                ? Math.max(0, Math.min(atIndex, ws.columns.length))
                : ws.columns.length;
            ws.columns.splice(i, 0, col);
            ws.focusedCol = i;
            savePrefs();
            return col;
        }
        function layoutRemoveKey(key) {
            // Canonical splice + collapse + activeTab heal lives in
            // removeKeyFromLayout; this is the savePrefs-bearing public remover
            // for close/minimize. Focus on a surviving column is left unchanged.
            if (removeKeyFromLayout(key)) savePrefs();
        }
        // Should a window with this key open tiled? Membership wins; else the
        // remembered per-window role; else follow the global desktop mode.
        function decideTiled(key) {
            if (findKeyInLayout(key)) return true;
            const role = getPref(key).tiled;
            if (role === true) return true;
            if (role === false) return false;
            return isTilingMode();
        }

        // ---- consume / expel (P3 stacking; menu-driven — P4 adds drag) -----
        // Consume: move a window OUT of its column and into the adjacent
        // column (dir -1 left / +1 right) as a new bottom row. Source column
        // collapses if it was the row's last. Target captured by reference so a
        // collapsing source can't shift the index out from under us.
        function consumeIntoAdjacentColumn(win, dir) {
            if (!win || win.disposed) return;
            const key = win.id;
            const loc = findKeyInLayout(key);
            if (!loc) return;
            const ws = loc.ws;
            const tgtIndex = loc.colIndex + dir;
            if (tgtIndex < 0 || tgtIndex >= ws.columns.length) return;
            const tgtCol = ws.columns[tgtIndex];     // captured by reference
            removeKeyFromLayout(key);
            // Land as a new bottom row (tile) of the target, equal-split among
            // its tiles so the consumed window gets a fair share.
            tgtCol.rows.push(newRow([key], 1));
            normalizeRowHeights(tgtCol.rows);
            ws.focusedCol = ws.columns.indexOf(tgtCol);
            savePrefs();
            requestRelayout();
            bringToFront(key);
        }
        // Expel: move a window out of its (multi-window) column into a brand-new
        // column inserted right after it. No-op if it is alone in its column
        // (the only key in the only row).
        function expelToNewColumn(win) {
            if (!win || win.disposed) return;
            const key = win.id;
            const loc = findKeyInLayout(key);
            if (!loc) return;
            if (loc.col.rows.length === 1 && rowKeys(loc.row).length === 1) return;
            const ws = loc.ws;
            const srcCol = loc.col;             // survives (>=1 other key remains)
            const preset = srcCol.widthPreset;
            removeKeyFromLayout(key);
            const newCol = newColumn();
            newCol.widthPreset = preset;
            newCol.rows = [newRow([key], 1)];
            const insertAt = ws.columns.indexOf(srcCol) + 1;
            ws.columns.splice(insertAt, 0, newCol);
            ws.focusedCol = insertAt;
            savePrefs();
            requestRelayout();
            bringToFront(key);
        }
        // After a focused tiled window is removed (close/minimize), move focus
        // to a sibling row in the same column first (nearest below, then
        // above), else to the window now at the focused column. `precap` is
        // captured BEFORE the removal mutates _layout.
        function captureTiledFocusContext(key) {
            const loc = findKeyInLayout(key);
            if (!loc) return null;
            const flat = columnKeys(loc.col);     // in-order keys across rows
            const i = flat.indexOf(key);
            return {
                below: flat.slice(i + 1),
                above: flat.slice(0, i).reverse(),
            };
        }
        function reconcileTiledFocus(precap) {
            if (!precap) return;
            const isLive = k => {
                const w = windows.get(k);
                return w && !w.disposed && !w.minimized && w.tiled;
            };
            let target = precap.below.find(isLive) || precap.above.find(isLive);
            if (!target) {
                const ws = activeWorkspace();
                const col = ws.columns[ws.focusedCol];
                if (col) target = columnKeys(col).find(isLive);
            }
            if (target) bringToFront(target);
        }
        // ---- drag-drop layout mutators (P4) -------------------------------
        // Drop a tiled window into an existing column (append as bottom row).
        // Same-column drop is a no-op. Source column collapses if emptied.
        function dragDropConsume(key, colId) {
            const loc = findKeyInLayout(key);
            if (!loc) return;
            const ws = loc.ws;
            const tgtCol = ws.columns.find(c => c.id === colId);
            if (!tgtCol || tgtCol === loc.col) return;   // already there
            removeKeyFromLayout(key);                    // captured tgtCol survives
            tgtCol.rows.push(newRow([key], 1));          // new bottom tile
            normalizeRowHeights(tgtCol.rows);            // equal split
            ws.focusedCol = ws.columns.indexOf(tgtCol);
            savePrefs();
            requestRelayout();
            bringToFront(key);
        }
        // ---- per-tile tab groups (i3/sway-style) --------------------------
        // Tab `dragKey`'s window into the SAME TILE as `targetKey`. Self-tab
        // (drag===target) flips just that one row tabbed — seeding a 1-tab strip
        // for a lone window. Otherwise the drag key is removed from wherever it
        // lives (collapsing its old row, then column, if emptied) and pushed
        // into the target's row, leaving every OTHER row in the column untouched.
        function tabWindowIntoTile(dragKey, targetKey) {
            dragKey = String(dragKey); targetKey = String(targetKey);
            const tloc = findKeyInLayout(targetKey);
            if (!tloc) return;
            // Self-tab (dragKey===targetKey): a top-level tile becomes a 1-tab
            // group; a lone split LEAF cell can't tab into itself (needs a second
            // window) so it stays a leaf — no-op.
            if (dragKey === targetKey) {
                if (tloc.row.mode === 'split') return;
                setRowMode(tloc.row, 'tabbed');
                tloc.row.activeTab = dragKey;
                tloc.ws.focusedCol = tloc.colIndex;
                savePrefs();
                requestRelayout();
                requestAnimationFrame(() => bringToFront(dragKey));
                return;
            }
            // Bail if dragKey wasn't actually in the layout — never fabricate a
            // key into the target tile (e.g. a stale drag / failed attach).
            if (!removeKeyFromLayout(dragKey)) return;
            // Re-locate the target AFTER removal: a collapsed source row/col (and,
            // for a same-row drag, a demoted/dropped cell) may have shifted indices
            // or even flipped the target's row mode. Tab/promote based on what it
            // IS now.
            const t2 = findKeyInLayout(targetKey);
            if (!t2) return;
            if (t2.row.mode === 'split' && t2.cell) {
                // (F-NESTSPLIT) Promote the target split CELL into a tab GROUP (or
                // extend an existing group): push dragKey into the cell and make it
                // the active tab. Same cell.id -> width preserved, no drag jump.
                const cell = t2.cell;
                if (cell.keys.indexOf(dragKey) === -1) cell.keys.push(dragKey);
                cell.activeTab = dragKey;
                delete t2.row.keys;             // split rows carry no keys
                normalizeSplitWidths(t2.row);   // cell count unchanged; keep sane
            } else {
                // Top-level tile -> make/keep it tabbed and append the newcomer.
                setRowMode(t2.row, 'tabbed');
                if (t2.row.keys.indexOf(dragKey) === -1) t2.row.keys.push(dragKey);
                t2.row.activeTab = dragKey;
            }
            t2.ws.focusedCol = t2.colIndex;
            savePrefs();
            requestRelayout();
            requestAnimationFrame(() => bringToFront(dragKey));
        }
        // Tab `key` into a column by id: targets the column's nearest live tile
        // (falls back to its first key). Used by the menu's adjacent-column tab.
        function tabWindowIntoColumn(key, colId) {
            key = String(key);
            const L = getLayout();
            let tgtCol = null;
            for (const ws of L.workspaces) {
                const c = ws.columns.find(cc => cc.id === colId);
                if (c) { tgtCol = c; break; }
            }
            if (!tgtCol) return;
            const target = firstLiveKeyInColumn(tgtCol) || columnKeys(tgtCol)[0];
            if (!target) return;
            tabWindowIntoTile(key, target);
        }
        // Unifying helper for the FLOATING tab path: dock dragWin as a tab in
        // targetWin's tile. Either window may currently be floating: a floating
        // target is attached to the strip first (gets its own column), a
        // floating drag window is attached too so its key can be moved. No-op on
        // the same window or a missing target.
        function tabWindowInto(dragWin, targetWin) {
            if (!dragWin || !targetWin || dragWin === targetWin) return;
            if (dragWin.disposed || targetWin.disposed) return;
            if (!targetWin.tiled) attachToStrip(targetWin);
            if (!dragWin.tiled) attachToStrip(dragWin);
            if (!findKeyInLayout(targetWin.id)) return;
            tabWindowIntoTile(dragWin.id, targetWin.id);
        }
        // Drop a tiled window as a new 1-row column at ws.columns index
        // `insertIndex` (measured against the layout BEFORE this move). If the
        // source column collapses and sat left of the target slot, the slot
        // shifts left by one.
        function dragDropNewColumn(key, insertIndex) {
            const loc = findKeyInLayout(key);
            if (!loc) return;
            const ws = loc.ws;
            const srcIndex = loc.colIndex;
            const preset = loc.col.widthPreset;
            // "Alone" = the only key in the only row of its column.
            const srcAlone = (loc.col.rows.length === 1 && rowKeys(loc.row).length === 1);
            // Alone window dropped into the slot immediately before/after its
            // own column = it would land exactly where it is: no-op.
            if (srcAlone && (insertIndex === srcIndex || insertIndex === srcIndex + 1)) {
                return;
            }
            const info = removeKeyFromLayout(key);
            // If the source column collapsed and sat left of the target slot,
            // the slot shifts left by one.
            const removedAt = (info && info.colCollapsed) ? srcIndex : -1;
            let idx = insertIndex;
            if (removedAt !== -1 && removedAt < idx) idx -= 1;
            idx = Math.max(0, Math.min(idx, ws.columns.length));
            const col = newColumn();
            col.widthPreset = preset;
            col.rows = [newRow([key], 1)];
            ws.columns.splice(idx, 0, col);
            ws.focusedCol = idx;
            savePrefs();
            requestRelayout();
            bringToFront(key);
        }
        // ---- directional window splits (task 1) ---------------------------
        // Insert `key`'s window as a new single-window ROW (tile) directly
        // above/below `targetKey`'s tile within the target's column. The drag
        // key is removed via removeKeyFromLayout (collapsing its old row/col if
        // emptied); the target ROW object is captured first and its index
        // recomputed AFTER the removal so a same-column drag lands correctly.
        function dragDropSplitRow(key, targetKey, after) {
            key = String(key); targetKey = String(targetKey);
            if (key === targetKey) return;
            const loc = findKeyInLayout(key);
            const tloc = findKeyInLayout(targetKey);
            if (!loc || !tloc || tloc.ws !== loc.ws) return;
            const ws = loc.ws;
            const tgtCol = tloc.col;            // survives (holds targetKey != key)
            const tgtRow = tloc.row;            // split around this tile
            removeKeyFromLayout(key);
            let ri = tgtCol.rows.indexOf(tgtRow);
            if (ri === -1) ri = tgtCol.rows.length - 1;
            const insertRow = after ? ri + 1 : ri;
            tgtCol.rows.splice(insertRow, 0, newRow([key], 1));
            normalizeRowHeights(tgtCol.rows);   // equal split
            ws.focusedCol = ws.columns.indexOf(tgtCol);
            savePrefs();
            requestRelayout();
            bringToFront(key);
        }
        // ---- horizontal (side-by-side) splits inside a tile (F-HSPLIT) ----
        // Split `targetKey`'s tile band horizontally so `dragKey`'s window sits
        // beside it (the horizontal mirror of dragDropSplitRow). `after` puts the
        // newcomer to the right of the target cell. The newcomer steals HALF the
        // TARGET cell's width (honoring which cell/side was dropped on), leaving
        // every other cell untouched.
        function dragDropSplitHoriz(dragKey, targetKey, after) {
            dragKey = String(dragKey); targetKey = String(targetKey);
            if (dragKey === targetKey) return;
            const dloc = findKeyInLayout(dragKey);
            const tloc = findKeyInLayout(targetKey);
            if (!dloc || !tloc || tloc.ws !== dloc.ws) return;
            // (F-NESTSPLIT) Same-row LEAF reorder: move the drag cell next to the
            // target cell, preserving every cell.id so widths follow. A drag tab OUT
            // of a GROUP cell (dloc.cell.keys.length>1) is NOT a reorder — it
            // extracts, so it falls through to the remove+insert path below.
            if (dloc.row === tloc.row && tloc.row.mode === 'split'
                && Array.isArray(tloc.row.cells)
                && dloc.cell && dloc.cell.keys.length === 1 && tloc.cell) {
                const row = tloc.row;
                const di = row.cells.indexOf(dloc.cell);
                if (di !== -1) row.cells.splice(di, 1);
                let ti = row.cells.indexOf(tloc.cell);
                if (ti === -1) ti = row.cells.length;
                row.cells.splice(ti + (after ? 1 : 0), 0, dloc.cell);
                delete row.keys;
                normalizeSplitWidths(row);          // same cell set -> widths preserved
                savePrefs();
                requestRelayout();
                bringToFront(dragKey);
                return;
            }
            // Otherwise: remove the drag window (cell-aware), re-locate the target by
            // key (mode/indices may have shifted — incl. a same-row group extract),
            // and build a CELLS split.
            removeKeyFromLayout(dragKey);
            const t2 = findKeyInLayout(targetKey);
            if (!t2) {
                // Target vanished (shouldn't in the sync flow) — never lose dragKey.
                const ncol = newColumn();
                ncol.rows = [newRow([dragKey], 1)];
                dloc.ws.columns.push(ncol);
                dloc.ws.focusedCol = dloc.ws.columns.length - 1;
                savePrefs();
                requestRelayout();
                bringToFront(dragKey);
                return;
            }
            const ws = t2.ws, tgtCol = t2.col, trow = t2.row;
            if (trow.mode === 'split' && t2.cell) {
                // Insert a new LEAF cell beside the target cell, stealing half its
                // width (every other cell keeps its width; renormalized after).
                const tcell = t2.cell, ci = t2.cellIndex;
                if (!trow.widths || typeof trow.widths !== 'object') trow.widths = {};
                const tw = (Number.isFinite(trow.widths[tcell.id])
                    && trow.widths[tcell.id] > 0)
                    ? trow.widths[tcell.id] : (1 / Math.max(1, trow.cells.length));
                const leaf = makeCell([dragKey]);
                trow.cells.splice(ci + (after ? 1 : 0), 0, leaf);
                trow.widths[tcell.id] = tw / 2;
                trow.widths[leaf.id] = tw / 2;
                delete trow.keys;
                normalizeSplitWidths(trow);
            } else if (trow.mode === 'tabbed') {
                // WRAP the whole tab group into [group | newcomer] (the chosen UX) —
                // the tabbed tile becomes one GROUP cell beside the newcomer leaf.
                const groupCell = makeCell(rowKeys(trow), trow.activeTab);
                const leaf = makeCell([dragKey]);
                setRowMode(trow, 'split');
                trow.cells = after ? [groupCell, leaf] : [leaf, groupCell];
                trow.widths = { [groupCell.id]: 0.5, [leaf.id]: 0.5 };
                delete trow.keys;
                normalizeSplitWidths(trow);
            } else if (trow.mode === 'single') {
                // SEED a 2-leaf split from the single target + the newcomer.
                const tcell = makeCell([targetKey]);
                const leaf = makeCell([dragKey]);
                setRowMode(trow, 'split');
                trow.cells = after ? [tcell, leaf] : [leaf, tcell];
                trow.widths = { [tcell.id]: 0.5, [leaf.id]: 0.5 };
                delete trow.keys;
                normalizeSplitWidths(trow);
            } else {
                // Unexpected (e.g. a legacy split lacking cells) — never lose dragKey.
                const ncol = newColumn();
                ncol.rows = [newRow([dragKey], 1)];
                const at = ws.columns.indexOf(tgtCol) + 1;
                ws.columns.splice(at, 0, ncol);
                ws.focusedCol = at;
                savePrefs();
                requestRelayout();
                bringToFront(dragKey);
                return;
            }
            ws.focusedCol = ws.columns.indexOf(tgtCol);
            savePrefs();
            requestRelayout();
            bringToFront(dragKey);
        }
        // Drop `dragKey` onto a quadrant of `targetKey`'s window: left/right split
        // the target tile's band horizontally (F-HSPLIT, side-by-side); top/bottom
        // make a new stacked row in the target's column. (A new full-height column
        // is reached via the column edge zones / the title-bar menu instead.)
        function splitAtWindow(dragKey, targetKey, dir) {
            if (dir === 'top' || dir === 'bottom') {
                dragDropSplitRow(dragKey, targetKey, dir === 'bottom');
                return;
            }
            dragDropSplitHoriz(dragKey, targetKey, dir === 'right');
        }
        // Reorder the focused/given column left or right by one slot.
        function moveColumn(colIndex, dir) {
            const ws = activeWorkspace();
            const j = colIndex + dir;
            if (colIndex < 0 || colIndex >= ws.columns.length) return;
            if (j < 0 || j >= ws.columns.length) return;
            const [c] = ws.columns.splice(colIndex, 1);
            ws.columns.splice(j, 0, c);
            ws.focusedCol = j;
            savePrefs();
            requestRelayout();
            scrollColumnIntoView(j, true);
        }

