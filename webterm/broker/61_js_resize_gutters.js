        // ---- horizontal (inter-column) resize gutters (task 9) -------------
        // Companion to the vertical row gutters: drag steals width between the
        // two adjacent columns, writing a custom col.widthFrac that overrides
        // the preset. Pair-sum stays constant so the rest of the strip is still.
        function colCurrentFrac(col) {
            return (typeof col.widthFrac === 'number' && col.widthFrac > 0)
                ? col.widthFrac : presetFraction(col.widthPreset);
        }
        function buildColGutter(leftCol, rightCol) {
            const g = document.createElement('div');
            g.className = 'strip-col-gutter';
            g.dataset.leftColId = leftCol.id;
            g.dataset.rightColId = rightCol.id;
            g.addEventListener('mousedown', onColGutterDown);
            return g;
        }
        function onColGutterDown(e) {
            if (e.button !== 0) return;
            e.preventDefault();
            e.stopPropagation();
            const g = e.currentTarget;
            const ws = activeWorkspace();
            const leftCol = ws.columns.find(c => c.id === g.dataset.leftColId);
            const rightCol = ws.columns.find(c => c.id === g.dataset.rightColId);
            if (!leftCol || !rightCol) return;
            const strip = document.getElementById('strip');
            const stripW = strip.clientWidth || 1;
            const startX = e.clientX;
            const startL = colCurrentFrac(leftCol);
            const startR = colCurrentFrac(rightCol);
            const pairSum = startL + startR;
            const minF = Math.min(0.12, pairSum / 2);
            const leftEl = strip.querySelector(
                '.strip-col[data-col-id="' + cssEscape(leftCol.id) + '"]');
            const rightEl = strip.querySelector(
                '.strip-col[data-col-id="' + cssEscape(rightCol.id) + '"]');
            document.body.classList.add('col-resizing');
            const onMove = (ev) => {
                const dfrac = (ev.clientX - startX) / stripW;
                const nl = Math.max(minF, Math.min(pairSum - minF, startL + dfrac));
                const nr = pairSum - nl;
                leftCol.widthFrac = nl;
                rightCol.widthFrac = nr;
                if (leftEl) leftEl.style.width = Math.round(nl * stripW) + 'px';
                if (rightEl) rightEl.style.width = Math.round(nr * stripW) + 'px';
            };
            const onUp = () => {
                document.removeEventListener('mousemove', onMove);
                document.removeEventListener('mouseup', onUp);
                document.body.classList.remove('col-resizing');
                savePrefs();
                requestRelayout();   // resize the two columns' terminals to fit
            };
            document.addEventListener('mousemove', onMove);
            document.addEventListener('mouseup', onUp);
        }

        // Move win.dom into colEl at the given row position (or end). Clears
        // the absolute-floating inline styles so flex fully owns the box, adds
        // .tiled, and — only when the term has already rendered — nudges xterm
        // to repaint into its new box and restores focus if it had it. Never
        // called mid text-selection / IME composition (see relayout guard).
        function reparentTiled(win, colEl, beforeNode) {
            win.tiled = true;
            win.dom.classList.add('tiled');
            win.dom.classList.remove('minimized');
            // Guard: never insert the node before itself (DOM quirk that can
            // move it to the wrong slot and drop focus). Treat "before me" as
            // "before my current next sibling" = stay put.
            if (beforeNode === win.dom) beforeNode = win.dom.nextSibling;
            const target = (beforeNode && beforeNode.parentElement === colEl)
                ? beforeNode : null;
            const inPlace = win.dom.parentElement === colEl
                && win.dom.nextSibling === target;
            if (inPlace) return;
            const hadFocus = win.dom.contains(document.activeElement);
            // Clear every floating inline box style so flex owns the box. The
            // floating path only writes left/top/width/height/zIndex, but
            // clearing transform/right/bottom/min/max too is cheap insurance
            // against a stale value (e.g. a drag-swap transform) blocking flex.
            const s = win.dom.style;
            s.left = ''; s.top = ''; s.right = ''; s.bottom = '';
            s.width = ''; s.height = ''; s.zIndex = ''; s.transform = '';
            s.minWidth = ''; s.minHeight = ''; s.maxWidth = ''; s.maxHeight = '';
            if (target) colEl.insertBefore(win.dom, target);
            else colEl.appendChild(win.dom);
            if (win.termReady) {
                try { win.term.refresh(0, Math.max(0, win.term.rows - 1)); }
                catch (_) {}
            }
            // focusWin focuses the xterm for terminals, the textarea for app
            // windows (win.term is null there — a bare win.term.focus() threw).
            if (hadFocus) focusWin(win);
        }
        // True while the user is mid text-selection or IME composition inside
        // a tiled terminal — reparenting then would drop the selection / abort
        // the composition, so relayout defers (re-armed on the next request).
        let _imeComposing = false;
        function selectionActiveInStrip() {
            if (_imeComposing) return true;
            const sel = window.getSelection && window.getSelection();
            if (sel && !sel.isCollapsed && sel.rangeCount) {
                const node = sel.anchorNode;
                const strip = document.getElementById('strip');
                if (node && strip && strip.contains(
                        node.nodeType === 1 ? node : node.parentElement)) {
                    return true;
                }
            }
            return false;
        }
        function relayoutStrip() {
            const strip = document.getElementById('strip');
            const desktop = document.getElementById('desktop');
            if (!strip) return;
            // Defer reparenting while a selection/composition is live; the
            // column widths are still safe to refresh, but moving nodes is not.
            const deferReparent = selectionActiveInStrip();
            const ws = activeWorkspace();
            const stripW = strip.clientWidth || desktop.clientWidth || 1024;

            // Render set: per column, the live ROWS (a row is live if >=1 of its
            // keys maps to a live, non-minimized window). Dormant rows/columns
            // are kept in _layout but not drawn; phantom (multi-host) keys are
            // filtered out of liveKeys here.
            const isLiveKey = (k) => {
                const w = windows.get(k);
                return w && !w.disposed && !w.minimized && !hostHidden(w.hostId);
            };
            const renderCols = [];
            for (const col of ws.columns) {
                const liveRows = [];
                for (const row of col.rows) {
                    // rowKeys flattens a split row's cells (and falls back to flat
                    // row.keys for single/tabbed/legacy-split).
                    const liveKeys = rowKeys(row).filter(isLiveKey);
                    if (!liveKeys.length) continue;          // dormant row
                    // Split rows render cell-native: one flex item per LIVE cell.
                    // A migrated row uses its real cells; a legacy (pre-migration)
                    // split synthesizes one single-window pseudo-cell per live key
                    // whose id IS the window key, so widths/gutter lookups match the
                    // old by-key behavior until reconcile migrates (P1c). Each
                    // cell's repKey is drawn from its LIVE keys so it is never a
                    // dead key (leaf -> the lone key; group -> active tab if live,
                    // else the first live tab).
                    let liveCells = null;
                    if (row.mode === 'split') {
                        if (Array.isArray(row.cells)) {
                            liveCells = [];
                            for (const cell of row.cells) {
                                const ck = cell.keys.filter(isLiveKey);
                                if (!ck.length) continue;
                                const at = cell.activeTab;
                                liveCells.push({
                                    cell, id: cell.id,
                                    repKey: (typeof at === 'string'
                                        && ck.indexOf(at) !== -1) ? at : ck[0],
                                    liveKeys: ck,
                                });
                            }
                        } else {
                            liveCells = liveKeys.map(k =>
                                ({ cell: null, id: k, repKey: k, liveKeys: [k] }));
                        }
                    }
                    liveRows.push({ row, liveKeys, liveCells });
                }
                if (liveRows.length) renderCols.push({ col, liveRows });
            }
            // Width budget: the (n-1) 6px inter-column gutters (task 9) are real
            // flex items, so sizing each column as round(frac*stripW) overflowed
            // the strip by gutterCount*6px once >1 column rendered (3×⅓ left a
            // stray scrollbar). Reserve the gutter width up front so equal
            // presets tile exactly. (A custom widthFrac may still overflow ->
            // scroll, which is intended.)
            const STRIP_GUTTER_W = 6;   // keep in sync with .strip-col-gutter
            const gutterReserve =
                Math.max(0, renderCols.length - 1) * STRIP_GUTTER_W;
            const availW = Math.max(1, stripW - gutterReserve);
            // Tiling class = tiling mode OR any rendered tiled column. Keying
            // off the mode (not just the column count) keeps the strip
            // interactive on an empty active workspace (so you can switch
            // back); the OR also covers a stray tiled window while the global
            // mode is floating. The desktop contextmenu falls back to the
            // desktop menu on the empty strip background.
            desktop.classList.toggle('tiling',
                getLayout().mode === 'tiling' || renderCols.length > 0);

            // Drop stale inter-column gutters up front so the column-position
            // logic below sees only .strip-col siblings (it compares against
            // prevEl.nextSibling); they're rebuilt fresh at the end.
            for (const g of Array.from(strip.children)) {
                if (g.classList && g.classList.contains('strip-col-gutter')) g.remove();
            }
            const existing = new Map();
            for (const el of Array.from(strip.children)) {
                if (el.classList && el.classList.contains('strip-col')) {
                    existing.set(el.dataset.colId, el);
                }
            }
            const usedEls = new Set();
            const placedCols = [];          // [{col, colEl}] in render order
            let prevEl = null;
            for (const { col, liveRows } of renderCols) {
                let colEl = existing.get(col.id);
                if (!colEl) {
                    colEl = document.createElement('div');
                    colEl.className = 'strip-col';
                    colEl.dataset.colId = col.id;
                }
                usedEls.add(colEl);
                // A custom widthFrac (free horizontal resize, task 9) overrides
                // the preset; both are fractions of the strip width.
                const frac = (typeof col.widthFrac === 'number' && col.widthFrac > 0)
                    ? col.widthFrac : presetFraction(col.widthPreset);
                colEl.style.width = Math.round(frac * availW) + 'px';
                // Position colEl right after prevEl (keeps column order).
                const want = prevEl ? prevEl.nextSibling : strip.firstChild;
                if (colEl.parentElement !== strip || colEl !== want) {
                    strip.insertBefore(colEl, want);
                }
                layoutColumnRows(col, colEl, liveRows, deferReparent);
                placedCols.push({ col, colEl });
                prevEl = colEl;
            }
            // Remove now-unused .strip-col elements — but ONLY when reparenting
            // actually ran. On the deferred path windows were NOT moved, so a
            // window may still be a child of an old colEl that is no longer in
            // usedEls; removing it would delete the live window's dom. The
            // queued retry relayout cleans up once the selection/IME ends.
            if (!deferReparent) {
                for (const [, el] of existing) {
                    if (!usedEls.has(el)) el.remove();
                }
                // (F-NESTSPLIT) Catch-all reap of cellTileEls map entries whose
                // nested wrapper got detached this pass (its parent split tile or
                // column was removed, so layoutRowSplit's per-row reap never ran).
                for (const [cid, el] of cellTileEls) {
                    if (!el.isConnected) cellTileEls.delete(cid);
                }
            }
            // Inter-column resize gutters (task 9): one before each column after
            // the first; dragging steals width between the two adjacent columns.
            for (let i = 1; i < placedCols.length; i++) {
                const g = buildColGutter(placedCols[i - 1].col, placedCols[i].col);
                strip.insertBefore(g, placedCols[i].colEl);
            }

            // Resize every visible tiled terminal once the new boxes have laid
            // out (double RAF), then bring the focused column into view.
            requestAnimationFrame(() => requestAnimationFrame(() => {
                for (const { liveRows } of renderCols) {
                    for (const lr of liveRows) {
                        // A split row resizes only each live cell's VISIBLE rep
                        // (so a P3 group's hidden tabs aren't scheduled); other
                        // rows resize all their live keys as before.
                        const keys = (lr.row.mode === 'split' && lr.liveCells)
                            ? lr.liveCells.map(lc => lc.repKey) : lr.liveKeys;
                        for (const k of keys) {
                            const win = windows.get(k);
                            if (win && !win.disposed) scheduleResize(win);
                        }
                    }
                }
                scrollColumnIntoView(ws.focusedCol, false);
                updateStripScrollbar();   // widths/columns changed → re-measure
            }));
            renderWorkspaces();
            if (deferReparent) requestRelayout();   // retry once selection ends
            reorderTaskbarItems();   // taskbar tracks the tiling order
        }
        // Fast, cancelable viewport slide. Replaces native behavior:'smooth' (browser-
        // timed, sluggish over long jumps). Animates strip.scrollLeft so onStripScroll
        // keeps floating windows + the scrollbar thumb in sync every frame.
        // #125: slide speed. The cap is the per-broker `slideDurationMs` setting
        // (Control Panel; default 500, 0 = instant), passed in per call; distance
        // sets the actual duration at ~SLIDE_PX_PER_MS px/ms so short reveals stay
        // proportionally quicker, clamped to [half the cap, the cap].
        const SLIDE_PX_PER_MS = 1.5;
        let _slideRaf = null;
        let _slideExpected = 0;   // last scrollLeft we wrote; divergence => user took over
        const _reducedMotionMql =
            window.matchMedia ? window.matchMedia('(prefers-reduced-motion: reduce)') : null;
        function cancelStripSlide() {
            if (_slideRaf !== null) { cancelAnimationFrame(_slideRaf); _slideRaf = null; }
        }
        function slideStripTo(strip, to, maxMs) {
            cancelStripSlide();                       // retarget: kill any in-flight slide
            // The only caller gates on a normalized [120,2000] cap, but keep the
            // helper self-contained: a non-finite or <=0 cap means "no slide", so
            // jump instantly rather than animate with a NaN/Infinity duration.
            if (!(maxMs > 0) || !isFinite(maxMs)) { strip.scrollLeft = to; return; }
            const from = strip.scrollLeft;
            const dist = Math.abs(to - from);
            if (dist < 1) return;
            // distance-scaled + clamped: short reveals stay snappy, long ones stay
            // bounded by the user's cap. minMs = half the cap keeps the clamp well-
            // ordered (minMs <= maxMs) even when the cap is set very low.
            const minMs = maxMs * 0.5;
            const dur = Math.min(maxMs, Math.max(minMs, dist / SLIDE_PX_PER_MS));
            let t0 = null;
            _slideExpected = from;
            function step(ts) {                        // ts = rAF DOMHighResTimeStamp
                if (t0 === null) t0 = ts;
                // scrollbar-thumb drag / wheel / drag-edge-scroll moved it -> yield to user
                if (Math.abs(strip.scrollLeft - _slideExpected) > 2) { _slideRaf = null; return; }
                const t = Math.min(1, (ts - t0) / dur);
                const eased = 1 - Math.pow(1 - t, 3);  // ease-out cubic (snappy start, soft land)
                strip.scrollLeft = Math.round(from + (to - from) * eased);
                _slideExpected = strip.scrollLeft;     // read back (browser clamps to maxScroll)
                _slideRaf = (t < 1) ? requestAnimationFrame(step) : null;
            }
            _slideRaf = requestAnimationFrame(step);
        }
        // smooth=true only for explicit user focus changes; relayout-driven
        // scrolls use instant positioning so bursts (open/close) don't queue
        // janky smooth motion.
        function scrollColumnIntoView(colIndex, smooth) {
            const strip = document.getElementById('strip');
            const ws = activeWorkspace();
            const col = ws.columns[colIndex];
            if (!strip || !col) return;
            const el = strip.querySelector(
                '.strip-col[data-col-id="' + cssEscape(col.id) + '"]');
            if (!el) return;                  // dormant (unrendered) column
            // Reveal-only (scrollIntoViewIfNeeded): scroll the MINIMUM amount to
            // bring the column into view, and never when it's already visible, so
            // selecting an on-screen window can't jerk the view. No re-centering.
            const viewLeft = strip.scrollLeft;
            const viewRight = viewLeft + strip.clientWidth;
            const colLeft = el.offsetLeft;
            const colRight = colLeft + el.offsetWidth;
            let x;
            if (el.offsetWidth > strip.clientWidth) {
                // Wider than the viewport: if any part is already on-screen leave
                // it (the user may have scrolled within it); only when it's wholly
                // off-screen do we align its left edge.
                if (colRight > viewLeft && colLeft < viewRight) return;
                x = colLeft;
            } else if (colLeft < viewLeft) {
                x = colLeft;                  // off the left edge -> reveal left
            } else if (colRight > viewRight) {
                x = colRight - strip.clientWidth;  // off the right edge -> reveal right
            } else {
                return;                       // fully visible -> no scroll
            }
            x = Math.max(0, x);
            if (Math.abs(x - viewLeft) < 1) return;
            // #125: cap from the per-broker slideDurationMs setting; 0 = instant
            // (an explicit user opt-out, treated like reduced-motion).
            const slideMs = getSettings().slideDurationMs;
            if (smooth && slideMs > 0 && !(_reducedMotionMql && _reducedMotionMql.matches)) {
                slideStripTo(strip, x, slideMs);
            } else {
                cancelStripSlide();          // instant supersedes any live slide
                strip.scrollLeft = x;        // relayout / reduced-motion / slideMs=0
            }
        }
        function cssEscape(s) {
            if (window.CSS && CSS.escape) return CSS.escape(s);
            return String(s).replace(/["\\]/g, '\\$&');
        }

