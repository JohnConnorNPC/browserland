        // ---- strip engine -------------------------------------------------
        // relayoutStrip() is the SINGLE writer of #strip DOM. Everything that
        // changes tiled placement mutates _layout then asks for a relayout;
        // relayout renders _layout (active workspace) into .strip-col columns,
        // reparents the live windows, sizes the columns, and routes every
        // tiled window back through scheduleResize (no new cell math).
        let _relayoutPending = false;
        function requestRelayout() {
            if (_deactivated) return;            // torn down — no live windows
            if (_relayoutPending) return;
            _relayoutPending = true;
            requestAnimationFrame(() => {
                _relayoutPending = false;
                if (_deactivated) return;        // torn down before the rAF ran
                relayoutStrip();
            });
        }
        // Per-column tile layout: each live ROW draws one TILE — a single-window
        // row places its window directly as a flexed child of the column; a
        // tabbed row gets a .strip-tile wrapper holding a .strip-tabs bar + its
        // windows (one visible, rest .tab-hidden); a split row gets a
        // .strip-tile.hsplit wrapper holding its windows side-by-side with
        // .strip-hgutter bars between them. Engine-owned row gutters sit between
        // consecutive tiles. row.height is the per-tile flex fraction,
        // re-normalized among LIVE rows so a dormant (minimized) tile leaves no
        // gap. The tile wrapper for a tabbed/split row is reused across relayouts
        // via the tileEls WeakMap so its live terminals are never torn down.
        const tileEls = new WeakMap();      // Row object -> its .strip-tile el
        // (F-NESTSPLIT) A split row's GROUP cell renders a nested .strip-tile
        // (tab strip) inside the .strip-tile.hsplit. Keyed by cell.id (cells are
        // plain data, not Row objects, so a WeakMap on object identity won't do);
        // reaped per-parent in layoutRowSplit (the top-level colEl orphan reap
        // only reaches direct colEl children, not inside the split wrapper).
        const cellTileEls = new Map();      // cell.id -> its nested .strip-tile el
        function liveRowHeights(liveRows) {
            const n = liveRows.length;
            if (n === 0) return [];
            const hs = liveRows.map(lr => (lr.row.height > 0 ? lr.row.height : 1 / n));
            const sum = hs.reduce((a, b) => a + b, 0);
            return sum > 0 ? hs.map(x => x / sum) : liveRows.map(() => 1 / n);
        }
        // Keyed mirror of liveRowHeights for a split row: the width fraction for
        // each LIVE cell in render order, looked up BY cell.id in row.widths (a
        // dormant/minimized middle cell therefore doesn't shift the live cells'
        // widths), positive-finite else 1/n, renormalized over the live subset.
        // Legacy (pre-migration) split rows render single-window pseudo-cells
        // whose id IS the window key, so this reproduces the old by-key widths.
        function liveCellWidths(row, liveCells) {
            const n = liveCells.length;
            if (n === 0) return [];
            const src = (row.widths && typeof row.widths === 'object'
                && !Array.isArray(row.widths)) ? row.widths : {};
            const w = liveCells.map(lc => {
                const v = Number(src[lc.id]);
                return (Number.isFinite(v) && v > 0) ? v : (1 / n);
            });
            const sum = w.reduce((a, b) => a + b, 0);
            return sum > 0 ? w.map(x => x / sum) : liveCells.map(() => 1 / n);
        }
        // A representative LIVE key for a row, used to stamp the stacked-row
        // gutters (onGutterDown resolves the top-level row back via rowOfKey): a
        // split row's first live cell's rep (always a live key — the render set
        // picks it from the cell's live subset), a tabbed row's active tab if
        // live, else the first live key.
        function repLiveKey(lr) {
            if (lr.row.mode === 'split' && lr.liveCells && lr.liveCells.length)
                return lr.liveCells[0].repKey;
            if (lr.row.mode === 'tabbed' && lr.liveKeys.indexOf(lr.row.activeTab) !== -1)
                return lr.row.activeTab;
            return lr.liveKeys[0];
        }
        function layoutColumnRows(col, colEl, liveRows, deferReparent) {
            const heights = liveRowHeights(liveRows);
            if (deferReparent) {
                // Selection/IME live: set classes/flex but NEVER move nodes,
                // build/destroy .strip-tile, or rebuild .strip-tabs. The queued
                // retry relayout rebuilds structurally once selection ends.
                liveRows.forEach((lr, ri) => {
                    const { row, liveKeys } = lr;
                    if (row.mode === 'tabbed') {
                        const tileEl = tileEls.get(row);
                        if (tileEl) {
                            tileEl.style.flex = heights[ri] + ' 1 0';
                            tileEl.classList.remove('hsplit');   // mode may have flipped
                        }
                        let active = (liveKeys.indexOf(row.activeTab) !== -1)
                            ? row.activeTab : liveKeys[0];
                        row.activeTab = active;
                        for (const k of liveKeys) {
                            const win = windows.get(k);
                            if (!win) continue;
                            win.tiled = true;
                            win.dom.classList.add('tiled');
                            if (k === active) {
                                win.dom.classList.remove('tab-hidden');
                                win.dom.style.flex = '1 1 0';
                            } else {
                                win.dom.classList.add('tab-hidden');
                            }
                        }
                    } else if (row.mode === 'split') {
                        // No reparent/teardown mid-selection: size each live cell
                        // from its keyed width and ensure none is left hidden. A
                        // just-dropped cell not yet inside the wrapper self-heals
                        // on the relayout queued for when selection/IME ends.
                        const tileEl = tileEls.get(row);
                        if (tileEl) {
                            tileEl.style.flex = heights[ri] + ' 1 0';
                            tileEl.classList.add('hsplit');      // mode may have flipped
                        }
                        // Cell-native: one flex item per LIVE cell, sized by
                        // widths[cell.id]. No reparent/teardown mid-selection: a
                        // LEAF flexes its window; a GROUP flexes its nested wrapper
                        // and just toggles tab-hidden (the wrapper/strip already
                        // exist from a prior structural pass).
                        const cells = lr.liveCells || [];
                        const sw = liveCellWidths(row, cells);
                        cells.forEach((lc, i) => {
                            const isGroup = lc.cell && Array.isArray(lc.cell.keys)
                                && lc.cell.keys.length >= 2;
                            if (isGroup) {
                                const cellEl = cellTileEls.get(lc.id);
                                if (cellEl) cellEl.style.flex = sw[i] + ' 1 0';
                                let act = (lc.cell.activeTab
                                    && lc.liveKeys.indexOf(lc.cell.activeTab) !== -1)
                                    ? lc.cell.activeTab : lc.liveKeys[0];
                                lc.cell.activeTab = act;
                                for (const k of lc.liveKeys) {
                                    const w = windows.get(k);
                                    if (!w) continue;
                                    w.tiled = true;
                                    w.dom.classList.add('tiled');
                                    if (k === act) {
                                        w.dom.classList.remove('tab-hidden');
                                        w.dom.style.flex = '1 1 0';
                                    } else {
                                        w.dom.classList.add('tab-hidden');
                                    }
                                }
                            } else {
                                const win = windows.get(lc.repKey);
                                if (!win) return;
                                win.tiled = true;
                                win.dom.classList.add('tiled');
                                win.dom.classList.remove('tab-hidden');
                                win.dom.style.flex = sw[i] + ' 1 0';
                            }
                        });
                    } else {
                        const win = windows.get(liveKeys[0]);
                        if (win) {
                            win.tiled = true;
                            win.dom.classList.add('tiled');
                            win.dom.classList.remove('tab-hidden');
                            win.dom.style.flex = heights[ri] + ' 1 0';
                        }
                    }
                });
                return;
            }
            // --- structural (reparenting) path ---
            // Drop stale row gutters; rebuilt fresh below.
            for (const g of Array.from(colEl.children)) {
                if (g.classList && g.classList.contains('strip-gutter')) g.remove();
            }
            const usedTiles = new Set();
            const topEls = [];          // colEl-level flex item per live row
            let cursor = null;
            liveRows.forEach((lr, ri) => {
                const { row, liveKeys } = lr;
                let topEl;
                if (row.mode === 'tabbed' || row.mode === 'split') {
                    let tileEl = tileEls.get(row);
                    if (!tileEl) {
                        tileEl = document.createElement('div');
                        tileEls.set(row, tileEl);
                    }
                    usedTiles.add(tileEl);
                    // Re-key the (possibly reused) wrapper for this mode BEFORE
                    // dispatching, so a wrapper that was tabbed/split last pass
                    // never carries a foreign tab strip / .hsplit class / stale
                    // gutters into the other builder.
                    resetTileWrapper(tileEl, row.mode);
                    const want = cursor ? cursor.nextSibling : colEl.firstChild;
                    if (tileEl.parentElement !== colEl || tileEl !== want) {
                        colEl.insertBefore(tileEl, want);
                    }
                    tileEl.style.flex = heights[ri] + ' 1 0';
                    if (row.mode === 'tabbed') layoutRowTabs(col, row, tileEl, liveKeys);
                    else layoutRowSplit(col, row, tileEl, lr.liveCells || []);
                    topEl = tileEl;
                } else {
                    const win = windows.get(liveKeys[0]);
                    win.dom.classList.remove('tab-hidden');
                    const want = cursor ? cursor.nextSibling : colEl.firstChild;
                    reparentTiled(win, colEl, want);
                    win.dom.style.flex = heights[ri] + ' 1 0';
                    topEl = win.dom;
                }
                topEls.push(topEl);
                cursor = topEl;
            });
            // Orphan .strip-tile cleanup: drop tile wrappers not used this pass
            // (their windows were reparented out above). Mirrors the .strip-col
            // cleanup; SAFE here only because reparenting ran (the deferReparent
            // path returned early and never reaches this).
            for (const el of Array.from(colEl.children)) {
                if (el.classList && el.classList.contains('strip-tile')
                    && !usedTiles.has(el)) el.remove();
            }
            // Gutter between each pair of stacked tiles, keyed by a
            // representative live key of the above/below tile (onGutterDown
            // resolves the Row objects from those keys).
            for (let ri = 1; ri < liveRows.length; ri++) {
                const g = buildGutter(col.id,
                    repLiveKey(liveRows[ri - 1]), repLiveKey(liveRows[ri]));
                colEl.insertBefore(g, topEls[ri]);
            }
        }
        // Lay out a tabbed TILE inside its .strip-tile wrapper: a .strip-tabs
        // bar on top, then exactly one visible window (the active tab) flexed to
        // fill; the rest carry .tab-hidden. Mirrors the single-window path's
        // invariants (win.tiled / .tiled / flex) so close/minimize/focus is
        // unchanged. Only ever called on the structural (non-defer) path.
        // `host` is the object that OWNS activeTab and the tab membership: a
        // top-level tabbed ROW, or (nested, ctx.nested) a split-row CELL. ctx for
        // a nested call = { nested:true, parentSplitRow, cellIndex } so the ⊟
        // button drops the GROUP's tabs as sibling leaf cells instead of exploding
        // the row into stacked rows. Reparents `liveKeys`' windows into `tileEl`.
        function layoutRowTabs(col, host, tileEl, liveKeys, ctx) {
            ctx = ctx || {};
            const nested = !!ctx.nested;
            const row = host;   // legacy local name; host===row for a top-level tile
            // Heal the active tab to a live key (write back so /state persists).
            let active = (typeof host.activeTab === 'string'
                && liveKeys.indexOf(host.activeTab) !== -1)
                ? host.activeTab : liveKeys[0];
            host.activeTab = active;
            // Build/reuse the tab strip as the FIRST child of the tile.
            let tabsEl = tileEl.querySelector(':scope > .strip-tabs');
            if (!tabsEl) {
                tabsEl = document.createElement('div');
                tabsEl.className = 'strip-tabs';
                // (F-NESTSPLIT) Dragging the tab-strip BACKGROUND of a NESTED group
                // cell moves the whole group as a unit. The tab/⊟ buttons
                // stopPropagation, so this only fires on the empty strip area. The
                // live drag context is read from tabsEl._groupDrag (refreshed each
                // pass below) so the once-added listener never closes over a stale
                // cell. Added once (the element is reused across relayouts).
                tabsEl.addEventListener('mousedown', (e) => {
                    if (e.button !== 0) return;
                    if (e.target.closest('.strip-tab')) return;   // a button handles itself
                    const gd = tabsEl._groupDrag;
                    if (!gd || !gd.cell || !Array.isArray(gd.cell.keys)
                        || gd.cell.keys.length < 2) return;        // only a real group
                    const ks = gd.cell.keys;
                    const active = (typeof gd.cell.activeTab === 'string'
                        && ks.indexOf(gd.cell.activeTab) !== -1)
                        ? gd.cell.activeTab : ks[0];
                    const aw = windows.get(active);
                    if (!aw) return;
                    startTiledDrag(aw, e,
                        { keys: ks.slice(), activeTab: gd.cell.activeTab });
                });
            }
            // Refresh the group-drag context: a nested call carries the cell, a
            // top-level tabbed tile carries none (its strip background is inert).
            tabsEl._groupDrag = nested ? { cell: host } : null;
            if (tileEl.firstChild !== tabsEl) tileEl.insertBefore(tabsEl, tileEl.firstChild);
            tabsEl.replaceChildren();
            for (const k of liveKeys) {
                const win = windows.get(k);
                const tab = document.createElement('button');
                tab.type = 'button';
                tab.className = 'strip-tab' + (k === active ? ' active' : '');
                tab.textContent = (win && win.name) ? win.name : k;
                tab.title = tab.textContent;
                tab.dataset.tabKey = k;
                tab.addEventListener('mousedown', (e) => e.stopPropagation());
                tab.addEventListener('click', (e) => {
                    e.stopPropagation();
                    row.activeTab = k;
                    savePrefs();
                    requestRelayout();
                    // Focus after the relayout frame: a display:none tab can't
                    // take focus before it's revealed.
                    requestAnimationFrame(() => bringToFront(k));
                });
                tabsEl.appendChild(tab);
            }
            // The ⊟ button: a TOP-LEVEL tabbed tile explodes into stacked single
            // rows; a NESTED group cell drops its tabs as sibling LEAF cells in
            // the same band (stays horizontal — the chosen UX).
            const untab = document.createElement('button');
            untab.type = 'button';
            untab.className = 'strip-tab strip-tab-untab';
            untab.textContent = '⊟';
            untab.title = nested ? 'Drop tabs side by side' : 'Split into rows';
            untab.addEventListener('mousedown', (e) => e.stopPropagation());
            untab.addEventListener('click', (e) => {
                e.stopPropagation();
                if (nested) nestedUntabCell(ctx.parentSplitRow, ctx.cell);
                else untabTile(col, col.rows.indexOf(host));
            });
            tabsEl.appendChild(untab);
            // Reparent every live window into the tile after the tab bar; only
            // the active one is visible + flexed, the rest hidden (display:none).
            let cursor = tabsEl;
            for (const k of liveKeys) {
                const win = windows.get(k);
                reparentTiled(win, tileEl, cursor.nextSibling);
                if (k === active) win.dom.classList.remove('tab-hidden');
                else win.dom.classList.add('tab-hidden');
                win.dom.style.flex = '1 1 0';
                cursor = win.dom;
            }
        }
        // Re-key a reused .strip-tile wrapper for `mode` before its builder runs:
        // toggle the .hsplit class (split lays cells out in a row; tabbed/single
        // stack) and strip the chrome the new mode does NOT own — a stale
        // .strip-tabs bar when becoming split, leftover .strip-hgutter bars when
        // becoming tabbed — so a wrapper reused across tabbed<->split never
        // carries foreign chrome. The live windows inside are untouched (the
        // builder reparents/flexes them). Only called for tabbed/split tiles.
        function resetTileWrapper(tileEl, mode) {
            // Toggle only the mode class (don't hard-reset className — that would
            // silently drop any future runtime state class on the wrapper).
            tileEl.classList.add('strip-tile');
            tileEl.classList.toggle('hsplit', mode === 'split');
            if (mode !== 'tabbed') {
                const t = tileEl.querySelector(':scope > .strip-tabs');
                if (t) t.remove();
            }
            if (mode !== 'split') {
                for (const g of Array.from(
                        tileEl.querySelectorAll(':scope > .strip-hgutter')))
                    g.remove();
                // A non-split tile holds no nested group-cell wrappers; drop any
                // stale one (DOM + its cellTileEls entry) left when a split row
                // collapses to tabbed/single and reuses this same wrapper (codex).
                for (const cellEl of Array.from(
                        tileEl.querySelectorAll(':scope > .strip-tile'))) {
                    const cid = cellEl.dataset && cellEl.dataset.cellId;
                    if (cid && cellTileEls.get(cid) === cellEl) cellTileEls.delete(cid);
                    cellEl.remove();
                }
            }
        }
        // Lay out a horizontally-split TILE inside its .strip-tile.hsplit wrapper
        // (F-HSPLIT): each live window reparented in liveKeys (left→right) order,
        // flexed to its keyed width fraction, with a .strip-hgutter between
        // consecutive cells. Mirrors layoutRowTabs' invariants (win.tiled /
        // .tiled / inline flex). Only ever called on the structural (non-defer)
        // path. n===1 (dormant siblings) renders the lone live cell, no gutter.
        // Lay out a split TILE inside its .strip-tile.hsplit wrapper: one flex
        // item per LIVE cell, flexed by widths[cell.id], with a vertical gutter
        // between consecutive cells. P1 is LEAF-ONLY — each cell reparents its
        // single rep window; a group cell's nested tab strip (a .strip-tile child
        // built via layoutRowTabs) is P3. Legacy (pre-migration) split rows pass
        // single-window pseudo-cells whose id IS the window key, so gutters are
        // stamped with window keys exactly as before (onHGutterDown unchanged).
        function layoutRowSplit(col, row, tileEl, liveCells) {
            const ws = liveCellWidths(row, liveCells);
            // Full gutter teardown each pass — unlike the tab strip there is no
            // single sub-container; the gutters interleave the windows.
            for (const g of Array.from(
                    tileEl.querySelectorAll(':scope > .strip-hgutter')))
                g.remove();
            // Each LIVE cell becomes one flex item in order. A LEAF cell reparents
            // its lone window directly; a GROUP cell (cell.keys.length>=2) renders
            // a nested .strip-tile tab strip via layoutRowTabs. `rendered` tracks
            // {el,id} per drawn cell so gutters (and the reap) align even if a
            // cell's rep window is momentarily missing.
            const usedCellEls = new Set();
            const rendered = [];
            let cursor = null;
            for (let i = 0; i < liveCells.length; i++) {
                const lc = liveCells[i];
                const isGroup = lc.cell && Array.isArray(lc.cell.keys)
                    && lc.cell.keys.length >= 2;
                let flexEl;
                if (isGroup) {
                    let cellEl = cellTileEls.get(lc.id);
                    if (!cellEl) {
                        cellEl = document.createElement('div');
                        cellTileEls.set(lc.id, cellEl);
                    }
                    cellEl.dataset.cellId = lc.id;
                    usedCellEls.add(cellEl);
                    resetTileWrapper(cellEl, 'tabbed');     // nested strip, not hsplit
                    const want = cursor ? cursor.nextSibling : tileEl.firstChild;
                    if (cellEl.parentElement !== tileEl || cellEl !== want)
                        tileEl.insertBefore(cellEl, want);
                    layoutRowTabs(col, lc.cell, cellEl, lc.liveKeys,
                        { nested: true, parentSplitRow: row, cell: lc.cell });
                    flexEl = cellEl;
                } else {
                    const win = windows.get(lc.repKey);
                    if (!win) continue;
                    const want = cursor ? cursor.nextSibling : tileEl.firstChild;
                    reparentTiled(win, tileEl, want);
                    win.dom.classList.remove('tab-hidden');
                    flexEl = win.dom;
                }
                flexEl.style.flex = ws[i] + ' 1 0';
                rendered.push({ el: flexEl, id: lc.id });
                cursor = flexEl;
            }
            // Reap nested group wrappers not used this pass (a former group now a
            // leaf, or gone) — the top-level colEl orphan reap can't reach inside
            // this split wrapper (codex #7). Drop the DOM node AND its map entry.
            for (const child of Array.from(tileEl.children)) {
                if (child.classList && child.classList.contains('strip-tile')
                    && !usedCellEls.has(child)) {
                    const cid = child.dataset && child.dataset.cellId;
                    if (cid && cellTileEls.get(cid) === child) cellTileEls.delete(cid);
                    child.remove();
                }
            }
            // Vertical-bar gutter between each pair of consecutive RENDERED cells,
            // stamped with their two cell ids (onHGutterDown resolves the row +
            // cells from them), inserted before the right cell's flex element.
            for (let i = 1; i < rendered.length; i++) {
                const g = buildHGutter(col.id, rendered[i - 1].id, rendered[i].id);
                tileEl.insertBefore(g, rendered[i].el);
            }
        }
        function buildGutter(colId, aboveKey, belowKey) {
            const g = document.createElement('div');
            g.className = 'strip-gutter';
            g.dataset.colId = colId;
            g.dataset.aboveKey = aboveKey;
            g.dataset.belowKey = belowKey;
            g.addEventListener('mousedown', onGutterDown);
            return g;
        }
        // The colEl-level flex element for a row: the .strip-tile wrapper for a
        // tabbed OR split tile, else the row's single live window dom (resolved
        // via its gutter-stamped representative key).
        function rowFlexEl(row, repKey) {
            if (row.mode === 'tabbed' || row.mode === 'split')
                return tileEls.get(row) || null;
            const w = windows.get(repKey);
            return w ? w.dom : null;
        }
        function onGutterDown(e) {
            if (e.button !== 0) return;
            e.preventDefault();
            e.stopPropagation();
            const g = e.currentTarget;
            const ws = activeWorkspace();
            const col = ws.columns.find(c => c.id === g.dataset.colId);
            if (!col) return;
            const aboveRow = rowOfKey(col, g.dataset.aboveKey);
            const belowRow = rowOfKey(col, g.dataset.belowKey);
            if (!aboveRow || !belowRow || aboveRow === belowRow) return;
            const aboveEl = rowFlexEl(aboveRow, g.dataset.aboveKey);
            const belowEl = rowFlexEl(belowRow, g.dataset.belowKey);
            // Both flex items must resolve to a live DOM node, else the drag
            // would write heights with no visual feedback (e.g. a tile whose
            // wrapper isn't mapped). Bail rather than start a dead drag.
            if (!aboveEl || !belowEl) return;
            const colEl = g.parentElement;
            const colH = colEl.clientHeight || 1;
            const startY = e.clientY;
            const startA = aboveRow.height, startB = belowRow.height;
            const pairSum = startA + startB;
            // Per-tile floor, but never more than half the pair — otherwise the
            // clamp range inverts when the pair is already tiny (many tiles /
            // skewed heights) and a tile could go negative.
            const minF = Math.min(0.08, pairSum / 2);
            const onMove = (ev) => {
                const frac = (ev.clientY - startY) / colH;
                const na = Math.max(minF, Math.min(pairSum - minF, startA + frac));
                const nb = pairSum - na;
                aboveRow.height = na; belowRow.height = nb;
                // Live-apply only the two affected tiles (pair-sum constant, so
                // sibling tiles keep their height). flex-grow is scale-invariant
                // so raw fractions match relayout's normalized values visually.
                if (aboveEl) aboveEl.style.flex = na + ' 1 0';
                if (belowEl) belowEl.style.flex = nb + ' 1 0';
            };
            const onUp = () => {
                document.removeEventListener('mousemove', onMove);
                document.removeEventListener('mouseup', onUp);
                savePrefs();
                // Relayout re-applies live-normalized flex (canonical even with
                // dormant rows present) and its double-RAF resizes the rows.
                requestRelayout();
            };
            document.addEventListener('mousemove', onMove);
            document.addEventListener('mouseup', onUp);
        }
        // Per-cell width gutter (F-HSPLIT): the horizontal analogue of buildGutter,
        // stamped with the two ADJACENT LIVE cell keys it sits between. Lives
        // inside the .strip-tile.hsplit wrapper (torn down/rebuilt each relayout).
        function buildHGutter(colId, leftKey, rightKey) {
            const g = document.createElement('div');
            g.className = 'strip-hgutter';
            g.dataset.colId = colId;
            g.dataset.leftKey = leftKey;
            g.dataset.rightKey = rightKey;
            g.addEventListener('mousedown', onHGutterDown);
            return g;
        }
        function onHGutterDown(e) {
            if (e.button !== 0) return;
            e.preventDefault();
            e.stopPropagation();
            const g = e.currentTarget;
            const ws = activeWorkspace();
            const col = ws.columns.find(c => c.id === g.dataset.colId);
            if (!col) return;
            // (F-NESTSPLIT) The gutter is stamped with two CELL IDs. Find the split
            // row whose cells include the left id (top-level), and both adjacent cells.
            const leftId = g.dataset.leftKey, rightId = g.dataset.rightKey;
            let row = null, leftCell = null, rightCell = null;
            for (const r of col.rows) {
                if (r.mode !== 'split' || !Array.isArray(r.cells)) continue;
                const lc = r.cells.find(c => c.id === leftId);
                const rc = r.cells.find(c => c.id === rightId);
                // Require BOTH cells in the SAME row (a gutter always sits between
                // two cells of one row) — robust against a cell.id colliding across
                // two split rows in the column (codex hardening).
                if (lc && rc) { row = r; leftCell = lc; rightCell = rc; break; }
            }
            if (!row) return;
            // The gutter must still sit between two LIVE-ADJACENT cells (a cell is live
            // if any of its keys is live, so a dormant MIDDLE cell joins its live
            // neighbours; a gutter left stale by a reorder is rejected). Touch .dom
            // only after this. P1 is leaf-only — a cell's flex element is its rep
            // window's dom (a P3 group cell flexes its Map<cellId,el> wrapper instead).
            const liveOK = (k) => {
                const w = windows.get(k);
                return w && !w.disposed && !w.minimized && !hostHidden(w.hostId);
            };
            const liveCells = row.cells.filter(c =>
                Array.isArray(c.keys) && c.keys.some(liveOK));
            const li = liveCells.indexOf(leftCell);
            if (li < 0 || liveCells[li + 1] !== rightCell) return;
            // The cell's FLEX item: a GROUP cell flexes its nested .strip-tile
            // wrapper, a LEAF cell flexes its lone window's dom. Live-applying flex
            // to the window inside a group wrapper would do nothing visible (codex).
            const flexElOf = (cell) => {
                if (Array.isArray(cell.keys) && cell.keys.length >= 2) {
                    const el = cellTileEls.get(cell.id);
                    if (el) return el;
                }
                const k = cell.keys.find(liveOK);
                const w = k ? windows.get(k) : null;
                return w ? w.dom : null;
            };
            const leftEl = flexElOf(leftCell), rightEl = flexElOf(rightCell);
            if (!leftEl || !rightEl) return;
            const wrapper = g.parentElement;
            const wrapW = (wrapper && wrapper.clientWidth) || 1;   // never divide by 0
            const startX = e.clientX;
            // Sanitize the keyed pair before reading; widths are kept summing to 1
            // over present cells (normalized after every mutation), so the raw pair
            // ratio matches the painted (live-normalized) ratio -> no first-move jump.
            const okPair = row.widths
                && Number.isFinite(row.widths[leftId]) && row.widths[leftId] > 0
                && Number.isFinite(row.widths[rightId]) && row.widths[rightId] > 0;
            if (!okPair) normalizeSplitWidths(row);
            const startA = row.widths[leftId], startB = row.widths[rightId];
            const pairSum = startA + startB;
            const minF = Math.min(0.08, pairSum / 2);
            document.body.classList.add('col-resizing');
            const onMove = (ev) => {
                const frac = (ev.clientX - startX) / wrapW;
                const na = Math.max(minF, Math.min(pairSum - minF, startA + frac));
                const nb = pairSum - na;
                row.widths[leftId] = na; row.widths[rightId] = nb;
                // Live-apply only the two affected cells (pair-sum constant, so the
                // other cells keep their width). flex-grow is scale-invariant.
                leftEl.style.flex = na + ' 1 0';
                rightEl.style.flex = nb + ' 1 0';
            };
            const onUp = () => {
                document.removeEventListener('mousemove', onMove);
                document.removeEventListener('mouseup', onUp);
                document.body.classList.remove('col-resizing');
                normalizeSplitWidths(row);   // keep persisted widths summing to 1
                savePrefs();
                requestRelayout();
            };
            document.addEventListener('mousemove', onMove);
            document.addEventListener('mouseup', onUp);
        }

