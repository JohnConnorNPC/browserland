        // ---- tiling layout (niri-style WM) --------------------------------
        // prefs._layout is the SINGLE source of truth for tiled placement.
        // Shape:
        //   { mode:'floating'|'tiling', activeWs:int,
        //     workspaces:[ { id, focusedCol:int,
        //                    columns:[ { id, widthPreset:'1/3'|'1/2'|'2/3'|'max',
        //                                keys:[winKey...], heights:[frac...] } ] } ] }
        // A window's remembered tiled-vs-floating role lives in its per-session
        // pref (pref.tiled) and is only a FALLBACK used when _layout has no
        // membership for its key. getLayout() self-heals like
        // getSettings()/getHosts(): a hand-edited or stale blob can never break
        // startup — reconcileLayout() always returns a structurally valid
        // object. An absent/invalid/missing mode defaults to TILING (the new
        // default); an existing saved 'floating' is left untouched, so users
        // already on floating stay there until they flip the Control Panel toggle.
        const WIDTH_PRESETS = ['1/3', '1/2', '2/3', 'max'];
        function presetFraction(p) {
            switch (p) {
                case '1/3': return 1 / 3;
                case '2/3': return 2 / 3;
                case 'max': return 1;
                case '1/2':
                default: return 1 / 2;
            }
        }
        // Monotonic id minter for workspaces/columns. Ids are cosmetic (DOM
        // keying / debugging) — never load-bearing for correctness, which
        // keys on window keys and object identity — so a counter that resets
        // each reload (possibly re-minting a stored id) is harmless. Seeded
        // past any stored numeric suffix to avoid in-session collisions.
        let _layoutIdSeq = 0;
        function mintLayoutId() {
            _layoutIdSeq += 1;
            return 'L' + _layoutIdSeq;
        }
        function seedLayoutIdSeq(L) {
            let max = 0;
            const scan = (id) => {
                if (typeof id === 'string' && id.charAt(0) === 'L') {
                    const n = parseInt(id.slice(1), 10);
                    if (Number.isFinite(n) && n > max) max = n;
                }
            };
            for (const ws of (L.workspaces || [])) {
                scan(ws && ws.id);
                for (const col of ((ws && ws.columns) || [])) {
                    scan(col && col.id);
                    // Split-cell ids (F-NESTSPLIT) also share the 'L'+n space and
                    // KEY row.widths, so a freshly minted col/cell id must never
                    // collide with a stored cell.id. (No-op until P1 builds cells.)
                    for (const row of ((col && col.rows) || []))
                        for (const cell of ((row && row.cells) || []))
                            scan(cell && cell.id);
                }
            }
            if (max > _layoutIdSeq) _layoutIdSeq = max;
        }
        function newColumn() {
            return { id: mintLayoutId(), widthPreset: '1/2', rows: [] };
        }
        // ---- nested-rows model (per-tile tab groups / horizontal splits) --
        // A column is a vertical stack of Rows; each Row is one TILE rendered in
        // one of three mutually-exclusive MODES:
        //   'single' — one window fills the tile.
        //   'tabbed' — many windows, one visible behind a tab strip (activeTab).
        //   'split'  — many windows side-by-side, each a width fraction (widths),
        //              sharing the tile's vertical band (F-HSPLIT).
        //   Row = { height:number, keys:[k,...], mode:'single'|'tabbed'|'split',
        //           activeTab?:string,            // 'tabbed' only
        //           widths?:{ [key]:fraction } }  // 'split' only, keyed, sum 1;
        //                                          // left→right order from keys[]
        // row.height is the per-tile flex fraction (gutters resize it). row.keys
        // holds membership for ALL modes. The old whole-column col.tabbed maps to
        // one full-height tabbed row, so F-WINTAB is a degenerate single tile.
        // INVARIANTS (self-healed in reconcileLayout / removeKeyFromLayout):
        // exactly one mode; single ⇒ keys.length===1; split ⇒ keys.length>=2 with
        // one finite widths[key] per key; mode-foreign fields deleted (setRowMode).
        function newRow(keys, height) {
            return {
                height: (typeof height === 'number' && height > 0) ? height : 1,
                keys: (Array.isArray(keys) ? keys : [keys]).map(String),
                mode: 'single',
            };
        }
        // Single writer for a row's MODE: sets row.mode and deletes the fields the
        // new mode does not own (widths unless split, activeTab unless tabbed) so a
        // row never carries split-brain state. ALL mode transitions go through here.
        function setRowMode(row, mode) {
            if (mode !== 'single' && mode !== 'tabbed' && mode !== 'split')
                mode = 'single';     // never let a typo'd mode reach the renderer
            row.mode = mode;
            // cells/widths are split-only so a row never split-brains across a
            // mode flip. CAUTION (P1 reconcile): this deletes row.cells when
            // leaving 'split', so any collapse path MUST copy a surviving cell's
            // keys/activeTab into locals BEFORE calling setRowMode to demote.
            if (mode !== 'split') { delete row.widths; delete row.cells; }
            if (mode !== 'tabbed') delete row.activeTab;
            return row;
        }
        // Keyed mirror of normalizeRowHeights for a split row: ensure row.widths has
        // one positive finite fraction per key in row.keys, drop entries for absent
        // keys, renormalize to sum 1 (equal split when absent/garbage). Mutates in
        // place; rejects non-finite (never emits NaN/0/Infinity).
        function normalizeRowWidths(row) {
            const keys = Array.isArray(row.keys) ? row.keys : [];
            const n = keys.length;
            if (n === 0) { row.widths = {}; return row; }
            const src = (row.widths && typeof row.widths === 'object'
                && !Array.isArray(row.widths)) ? row.widths : {};
            const w = {};
            let sum = 0;
            for (const k of keys) {
                const v = Number(src[k]);
                w[k] = (Number.isFinite(v) && v > 0) ? v : (1 / n);
                sum += w[k];
            }
            if (sum > 0) for (const k of keys) w[k] = w[k] / sum;
            else for (const k of keys) w[k] = 1 / n;
            row.widths = w;
            return row;
        }
        // ---- split CELL helpers (F-NESTSPLIT) -----------------------------
        // A split row's cells are PLAIN DATA, one shape:
        //   Cell = { id, keys:[k,…], activeTab? }
        //     leaf      = keys.length === 1   (activeTab absent/ignored)
        //     tab group = keys.length >= 2    (activeTab is one of keys)
        // Promote leaf→group / demote group→leaf KEEP the same cell.id so the
        // column width (row.widths[cell.id]) and DOM wrapper survive the flip.
        // For a split row `cells` is the SINGLE source of truth and carries NO
        // row.keys (reconcile/setRowMode enforce this); row.widths is keyed by
        // cell.id. single/tabbed rows are UNCHANGED (row.keys authoritative).
        function makeCell(keys, activeTab) {
            const ks = (Array.isArray(keys) ? keys : [keys]).map(String);
            const cell = { id: mintLayoutId(), keys: ks };
            // activeTab belongs to a group only; a leaf drops it. Heal a missing
            // or out-of-group activeTab to the first key. (Callers pass >=1 real
            // key; reconcile is the gate that drops any empty cell.)
            if (ks.length > 1) {
                cell.activeTab = (typeof activeTab === 'string'
                    && ks.indexOf(activeTab) !== -1) ? activeTab : ks[0];
            }
            return cell;
        }
        // A cell's PREFERRED (stored) representative key: the active tab for a
        // group when it passes the optional isLive predicate, else the first
        // key; the lone key for a leaf. NOTE: when isLive is given and the active
        // tab is dead this returns keys[0] even if a LATER tab is live — it is a
        // stable preferred key, NOT a guaranteed-live one. First-live selection
        // belongs to repLiveKey / firstLiveKeyInColumn (P3).
        function cellRepKey(cell, isLive) {
            const keys = (cell && Array.isArray(cell.keys)) ? cell.keys : [];
            if (keys.length <= 1) return keys[0];
            const at = cell.activeTab;
            if (typeof at === 'string' && keys.indexOf(at) !== -1
                && (typeof isLive !== 'function' || isLive(at))) return at;
            return keys[0];
        }
        // Flatten one cell to a fresh array of its window keys.
        function cellKeys(cell) {
            return (cell && Array.isArray(cell.keys)) ? cell.keys.slice() : [];
        }
        // A leaf cell holds exactly one window; a group holds >=2.
        function isLeaf(cell) {
            return !!cell && Array.isArray(cell.keys) && cell.keys.length === 1;
        }
        // Read-path accessor that replaces flat `row.keys` reads where a row may
        // be split: a split row's keys live in its cells, every other mode keeps
        // row.keys authoritative. Always returns a FRESH array (READ-ONLY — never
        // mutate the result to write membership; that goes through the split
        // mutators / removeKeyFromLayout). Fresh-for-both avoids the trap of a
        // mutation silently no-op'ing on split rows.
        function rowKeys(row) {
            const out = [];
            if (row && row.mode === 'split' && Array.isArray(row.cells)) {
                for (const c of row.cells) {
                    if (c && Array.isArray(c.keys)) for (const k of c.keys) out.push(k);
                }
            } else if (row && Array.isArray(row.keys)) {
                for (const k of row.keys) out.push(k);
            }
            return out;
        }
        // Keyed mirror of normalizeRowWidths for a split row: one positive finite
        // fraction per cell.id in row.cells, drop entries for absent cells,
        // renormalize to sum 1 (equal split when absent/garbage). Mutates in
        // place. The ONLY sanctioned writer of a split row's widths. (Assumes
        // unique cell.ids — reconcile remints any duplicate before calling here.)
        function normalizeSplitWidths(row) {
            const cells = (row && Array.isArray(row.cells)) ? row.cells : [];
            const n = cells.length;
            if (n === 0) { row.widths = {}; return row; }
            const src = (row.widths && typeof row.widths === 'object'
                && !Array.isArray(row.widths)) ? row.widths : {};
            const w = {};
            let sum = 0;
            for (const c of cells) {
                const v = Number(src[c.id]);
                w[c.id] = (Number.isFinite(v) && v > 0) ? v : (1 / n);
                sum += w[c.id];
            }
            if (sum > 0) for (const c of cells) w[c.id] = w[c.id] / sum;
            else for (const c of cells) w[c.id] = 1 / n;
            row.widths = w;
            return row;
        }
        // Deepest key lookup for DROP TARGETING: the cell (and tab within it)
        // holding `key`. Returns { row, rowIndex, cell, cellIndex, tabIndex } with
        // `row` ALWAYS a top-level col.rows member, or null. For a non-split row
        // (and a not-yet-migrated legacy split with row.keys but no cells)
        // cell=null / cellIndex=-1 and tabIndex indexes row.keys — back-compat
        // with findKeyInLayout. Distinct from rowOfKey, which stays top-level.
        function cellOfKey(col, key) {
            if (!col || !Array.isArray(col.rows)) return null;
            key = String(key);
            for (let ri = 0; ri < col.rows.length; ri++) {
                const row = col.rows[ri];
                if (!row) continue;
                if (row.mode === 'split' && Array.isArray(row.cells)) {
                    for (let ci = 0; ci < row.cells.length; ci++) {
                        const cell = row.cells[ci];
                        const ti = (cell && Array.isArray(cell.keys))
                            ? cell.keys.indexOf(key) : -1;
                        if (ti !== -1)
                            return { row, rowIndex: ri, cell, cellIndex: ci, tabIndex: ti };
                    }
                } else if (Array.isArray(row.keys)) {
                    const ti = row.keys.indexOf(key);
                    if (ti !== -1)
                        return { row, rowIndex: ri, cell: null, cellIndex: -1, tabIndex: ti };
                }
            }
            return null;
        }
        // Clamp/normalize each row's height to a positive fraction summing to 1
        // (equal split when absent/garbage). Mutates in place; returns rows.
        function normalizeRowHeights(rows) {
            const n = rows.length;
            if (n === 0) return rows;
            let sum = 0;
            for (const r of rows) {
                if (typeof r.height !== 'number' || !(r.height > 0)) r.height = 1 / n;
                sum += r.height;
            }
            if (sum > 0) for (const r of rows) r.height = r.height / sum;
            else for (const r of rows) r.height = 1 / n;
            return rows;
        }
        // Flatten a column's rows into the in-order list of window keys (incl.
        // dormant/phantom). The nested-rows analogue of the old flat col.keys.
        function columnKeys(col) {
            const out = [];
            if (col && Array.isArray(col.rows)) {
                // rowKeys descends into a split row's cells (and falls back to flat
                // row.keys for single/tabbed/legacy-split), so this stays correct
                // whether or not the row has been migrated to the cells shape.
                for (const r of col.rows) for (const k of rowKeys(r)) out.push(k);
            }
            return out;
        }
        // The top-level Row object in `col` that holds `key`, or null. Matches
        // through rowKeys so a nested (split-cell) key still resolves, but ALWAYS
        // returns the top-level row — NEVER a cell (rowOfKey answers "is this tile
        // tabbed?"; cell-level lookup is cellOfKey / findKeyInLayout).
        function rowOfKey(col, key) {
            if (!col || !Array.isArray(col.rows)) return null;
            key = String(key);
            for (const r of col.rows) {
                if (rowKeys(r).indexOf(key) !== -1) return r;
            }
            return null;
        }
        // Canonical key remover: splice `key` out of its row, collapse an empty
        // row (then an empty column), and self-heal the survivor row's mode (a
        // split row that drops to one cell becomes single; else its widths/active
        // tab are healed). Lives INSIDE so all callers inherit the repair. Does
        // NOT savePrefs/relayout — callers own that. Returns collapse/index info
        // for bookkeeping (e.g. dragDropNewColumn's slot shift), or null if absent.
        function removeKeyFromLayout(key) {
            key = String(key);
            const loc = findKeyInLayout(key);
            if (!loc) return null;
            const { ws, col, colIndex, row, rowIndex } = loc;
            let rowCollapsed = false, colCollapsed = false;
            if (loc.cell) {
                // (F-NESTSPLIT cells path) Drop the key from its cell, then collapse
                // the cell and the parent split row. DEAD until migration (loc.cell is
                // null for every legacy/non-split location). Group cell -> heal/ demote;
                // emptied cell -> drop it + its widths[cell.id]; <2 cells -> single/
                // tabbed (capture keys BEFORE setRowMode); else renormalize cell widths.
                const cell = loc.cell;
                cell.keys.splice(loc.tabIndex, 1);
                if (cell.keys.length === 0) {
                    const ci = row.cells.indexOf(cell);
                    if (ci !== -1) row.cells.splice(ci, 1);
                    if (row.widths) delete row.widths[cell.id];
                } else if (cell.keys.length === 1) {
                    delete cell.activeTab;                 // group -> leaf, same id
                } else if (cell.keys.indexOf(cell.activeTab) === -1) {
                    cell.activeTab = cell.keys[0];         // heal group active tab
                }
                if (row.cells.length === 0) {
                    col.rows.splice(rowIndex, 1);
                    rowCollapsed = true;
                    normalizeRowHeights(col.rows);
                } else if (row.cells.length === 1) {
                    const only = row.cells[0];
                    if (only.keys.length === 1) {
                        const lk = only.keys[0];
                        setRowMode(row, 'single');
                        row.keys = [lk];
                    } else {
                        const gk = only.keys.slice();
                        const at = only.activeTab;
                        setRowMode(row, 'tabbed');
                        row.keys = gk;
                        row.activeTab = (typeof at === 'string' && gk.indexOf(at) !== -1)
                            ? at : gk[0];
                    }
                } else {
                    normalizeSplitWidths(row);
                }
            } else {
                // (legacy / non-split path) unchanged: splice the flat row.keys.
                row.keys.splice(loc.tabIndex, 1);
                if (row.widths) delete row.widths[key];
                if (row.keys.length === 0) {
                    col.rows.splice(rowIndex, 1);
                    rowCollapsed = true;
                    normalizeRowHeights(col.rows);
                } else if (row.mode === 'split') {
                    // A legacy split row that lost a key: demote to single when one
                    // remains, else renormalize the survivors' widths to sum 1.
                    if (row.keys.length === 1) setRowMode(row, 'single');
                    else normalizeRowWidths(row);
                } else if (row.mode === 'tabbed') {
                    // A tabbed tile that lost a tab and now holds ONE window: drop
                    // the tab strip and demote to single (mirrors the split-cell
                    // collapse above). Otherwise heal a stale activeTab to a
                    // present key.
                    if (row.keys.length === 1) setRowMode(row, 'single');
                    else if (row.keys.indexOf(row.activeTab) === -1)
                        row.activeTab = row.keys[0];
                }
            }
            if (col.rows.length === 0) {
                ws.columns.splice(colIndex, 1);
                colCollapsed = true;
                ws.focusedCol = Math.max(0,
                    Math.min(ws.focusedCol, ws.columns.length - 1));
            }
            return { ws, wsIndex: loc.wsIndex, colIndex, rowIndex,
                     rowCollapsed, colCollapsed };
        }
        // Untab a tabbed tile: explode it into one single-window row per key,
        // splitting the tile's height evenly among them.
        function untabTile(col, rowIndex) {
            if (!col || !Array.isArray(col.rows)) return;
            const row = col.rows[rowIndex];
            if (!row || row.mode !== 'tabbed') return;
            const each = (row.height > 0 ? row.height : 1) / Math.max(1, row.keys.length);
            const split = row.keys.map(k => newRow([k], each));
            col.rows.splice(rowIndex, 1, ...split);
            normalizeRowHeights(col.rows);
            savePrefs();
            requestRelayout();
        }
        // Un-split a horizontally-split tile (F-HSPLIT): explode it into one
        // single-window row per cell, splitting the tile's height evenly among
        // them (mirror of untabTile). The fresh single rows carry no widths.
        function unsplitRow(col, rowIndex) {
            if (!col || !Array.isArray(col.rows)) return;
            const row = col.rows[rowIndex];
            if (!row || row.mode !== 'split') return;
            const h = (row.height > 0 ? row.height : 1);
            const cells = Array.isArray(row.cells) ? row.cells : null;
            let newRows;
            if (cells && cells.length) {
                // Explode CELLS into stacked rows: a LEAF -> a single row; a GROUP
                // -> a TABBED row preserving its tabs + active tab (mirror of the
                // nested ⊟, but vertical instead of side-by-side). Heights even.
                const each = h / Math.max(1, cells.length);
                newRows = cells.map(c => {
                    const ks = Array.isArray(c.keys) ? c.keys.slice() : [];
                    const r = newRow(ks, each);
                    if (ks.length >= 2) {
                        setRowMode(r, 'tabbed');
                        r.activeTab = (typeof c.activeTab === 'string'
                            && ks.indexOf(c.activeTab) !== -1) ? c.activeTab : ks[0];
                    }
                    return r;
                });
            } else {
                // Legacy split (no cells) — flatten keys to single rows.
                const ks = rowKeys(row);
                const each = h / Math.max(1, ks.length);
                newRows = ks.map(k => newRow([k], each));
            }
            col.rows.splice(rowIndex, 1, ...newRows);
            normalizeRowHeights(col.rows);
            savePrefs();
            requestRelayout();
        }
        // (F-NESTSPLIT) Nested untab (the ⊟ on a nested tab strip): drop a GROUP
        // cell's tabs as sibling LEAF cells in the SAME split band (stays
        // horizontal — the chosen UX; row-level "Un-split row" still stacks rows).
        // Each new leaf gets a minted id and an equal share of the group's width.
        // Takes the CELL OBJECT (not an index) and resolves its persisted index at
        // call time — a render-time live-cell index can drift from row.cells when an
        // earlier cell is dormant/minimized/host-hidden (codex).
        function nestedUntabCell(row, cell) {
            if (!row || row.mode !== 'split' || !Array.isArray(row.cells)) return;
            const cellIndex = row.cells.indexOf(cell);
            if (cellIndex === -1 || !cell || !Array.isArray(cell.keys)
                || cell.keys.length < 2) return;
            if (!row.widths || typeof row.widths !== 'object') row.widths = {};
            const oldW = (Number.isFinite(row.widths[cell.id]) && row.widths[cell.id] > 0)
                ? row.widths[cell.id] : (1 / Math.max(1, row.cells.length));
            const each = oldW / cell.keys.length;
            const leaves = cell.keys.map(k => makeCell([k]));
            row.cells.splice(cellIndex, 1, ...leaves);
            delete row.widths[cell.id];
            for (const nc of leaves) row.widths[nc.id] = each;
            normalizeSplitWidths(row);
            savePrefs();
            requestRelayout();
        }
        function newWorkspace() {
            return { id: mintLayoutId(), focusedCol: 0, columns: [] };
        }
        // Clamp/normalize a heights[] array to exactly n positive fractions
        // summing to 1 (equal split when absent/garbage).
        function normalizeHeights(raw, n) {
            if (n <= 0) return [];
            const h = [];
            const src = Array.isArray(raw) ? raw : [];
            for (let i = 0; i < n; i++) {
                const v = Number(src[i]);
                h[i] = (Number.isFinite(v) && v > 0) ? v : (1 / n);
            }
            const sum = h.reduce((a, b) => a + b, 0);
            return (sum > 0) ? h.map(x => x / sum) : new Array(n).fill(1 / n);
        }
        // (F-NESTSPLIT) Validate a row already in the split CELLS shape, idempotently.
        // Per cell: String()+global-`seen` dedupe its keys (dropping an emptied cell
        // and its width entry), demote a 1-key group to a leaf (drop activeTab, same
        // id), heal a group's activeTab to a present key; remint any DUPLICATE cell.id
        // within the row (cell.id keys row.widths, so a dup would collapse the map).
        // Then collapse: 0 survivors -> drop the row (return null); 1 leaf -> single;
        // 1 group -> tabbed (BOTH capture keys/activeTab into locals BEFORE setRowMode,
        // which deletes row.cells); >=2 -> stays split, normalizeSplitWidths + delete
        // row.keys (cells are the sole source of truth). Returns the row or null.
        function cleanSplitCellsRow(row, seen, seenCellIds) {
            delete row.tabbed;                       // never trust a legacy boolean here
            const survivors = [];
            // cell.id must be unique LAYOUT-WIDE (it keys row.widths and the gutter
            // resolves a cell by id). reconcile passes a shared set so a cross-row
            // duplicate is reminted; a direct call falls back to within-row dedupe.
            const idsUsed = (seenCellIds instanceof Set) ? seenCellIds : new Set();
            for (const cell of (Array.isArray(row.cells) ? row.cells : [])) {
                if (!cell || typeof cell !== 'object' || Array.isArray(cell)) continue;
                const nk = [];
                for (const k of (Array.isArray(cell.keys) ? cell.keys : [])) {
                    const key = String(k);
                    if (!key || seen.has(key)) continue;     // global dedupe; phantoms kept
                    seen.add(key);
                    nk.push(key);
                }
                if (nk.length === 0) {                        // emptied -> drop cell + width
                    if (row.widths && cell.id != null) delete row.widths[cell.id];
                    continue;
                }
                cell.keys = nk;
                // id: mint when missing/garbage OR a duplicate of an earlier survivor.
                if (typeof cell.id !== 'string' || !cell.id || idsUsed.has(cell.id))
                    cell.id = mintLayoutId();
                idsUsed.add(cell.id);
                if (nk.length === 1) delete cell.activeTab;          // leaf carries none
                else if (typeof cell.activeTab !== 'string'
                         || nk.indexOf(cell.activeTab) === -1)
                    cell.activeTab = nk[0];                          // heal group active
                survivors.push(cell);
            }
            row.height = (typeof row.height === 'number' && row.height > 0)
                ? row.height : 1;
            if (survivors.length === 0) return null;                 // structurally empty
            if (survivors.length === 1) {
                const only = survivors[0];
                if (only.keys.length === 1) {
                    const lk = only.keys[0];            // capture BEFORE setRowMode
                    setRowMode(row, 'single');          // deletes row.cells/widths/activeTab
                    row.keys = [lk];
                } else {
                    const gk = only.keys.slice();       // capture BEFORE setRowMode
                    const at = only.activeTab;
                    setRowMode(row, 'tabbed');          // deletes row.cells/widths
                    row.keys = gk;
                    row.activeTab = (typeof at === 'string' && gk.indexOf(at) !== -1)
                        ? at : gk[0];
                }
                return row;
            }
            row.cells = survivors;
            setRowMode(row, 'split');     // keeps cells/widths; drops any stray activeTab
            normalizeSplitWidths(row);    // one positive frac per cell.id, sum 1
            delete row.keys;              // a split row carries NO keys (cells authoritative)
            return row;
        }
        function reconcileLayout(L) {
            if (!L || typeof L !== 'object' || Array.isArray(L)) L = {};
            // Default to tiling: only fills an invalid/unset/missing mode, so a
            // saved 'floating' layout is preserved (the Control Panel toggle owns it
            // thereafter). New users and malformed/old blobs open tiled.
            if (L.mode !== 'tiling' && L.mode !== 'floating') L.mode = 'tiling';
            if (!Array.isArray(L.workspaces) || L.workspaces.length === 0) {
                L.workspaces = [newWorkspace()];
            }
            seedLayoutIdSeq(L);
            const seen = new Set();             // global key dedupe across all ws/cols
            const seenCellIds = new Set();      // global cell.id dedupe (keys row.widths)
            const cleanWs = [];
            for (const ws of L.workspaces) {
                if (!ws || typeof ws !== 'object' || Array.isArray(ws)) continue;
                if (typeof ws.id !== 'string' || !ws.id) ws.id = mintLayoutId();
                // Optional workspace name (task 19); a non-string is dropped so
                // it renders as its number.
                if (typeof ws.name !== 'string' || !ws.name.trim()) delete ws.name;
                else ws.name = ws.name.slice(0, 40);
                const cleanCols = [];
                const rawCols = Array.isArray(ws.columns) ? ws.columns : [];
                for (const col of rawCols) {
                    if (!col || typeof col !== 'object' || Array.isArray(col)) continue;
                    if (typeof col.id !== 'string' || !col.id) col.id = mintLayoutId();
                    if (WIDTH_PRESETS.indexOf(col.widthPreset) === -1) col.widthPreset = '1/2';
                    // Free horizontal resize (task 9): a custom width fraction
                    // overrides the preset. Drop a garbage/out-of-range value.
                    if (typeof col.widthFrac !== 'number'
                        || !(col.widthFrac > 0) || col.widthFrac > 1) {
                        delete col.widthFrac;
                    }
                    // Nested-rows migration (per-tile tab groups). Idempotent
                    // over three shapes; the legacy fields are dropped after so
                    // a re-run is a no-op and the blob rides /state as col.rows.
                    let rawRows;
                    if (Array.isArray(col.rows) && col.rows.length) {
                        // (a) already nested rows -> re-validate in place (row
                        // OBJECT identity is preserved so the tile WeakMap holds).
                        // A non-empty col.rows is authoritative; an EMPTY one
                        // falls through so a partial {rows:[],keys:[...]} blob
                        // still migrates its legacy keys instead of dropping them.
                        rawRows = col.rows;
                    } else if (Array.isArray(col.keys) && col.tabbed === true) {
                        // (b) legacy whole-column tabbed -> one full-height tabbed
                        // row (F-WINTAB becomes a degenerate single tabbed tile).
                        rawRows = [{
                            height: 1,
                            keys: col.keys.slice(),
                            mode: 'tabbed',
                            activeTab: (typeof col.activeTab === 'string')
                                ? col.activeTab : undefined,
                        }];
                    } else if (Array.isArray(col.keys)) {
                        // (c) legacy stacked rows -> one single-window row per key,
                        // seeding each row's height from the old per-key heights.
                        const hs = normalizeHeights(col.heights, col.keys.length);
                        rawRows = col.keys.map((k, i) => ({
                            height: hs[i], keys: [k], mode: 'single',
                        }));
                    } else {
                        rawRows = [];
                    }
                    delete col.keys; delete col.heights;
                    delete col.tabbed; delete col.activeTab;
                    const cleanRows = [];
                    for (const row of rawRows) {
                        if (!row || typeof row !== 'object' || Array.isArray(row)) continue;
                        // (F-NESTSPLIT) A split row already carrying cells is validated
                        // by its own idempotent cleaner (cells are authoritative ONLY
                        // for split rows — the invariant); the legacy keys-based path
                        // below never sees it. DEAD until the migration commit makes
                        // legacy splits synthesize cells.
                        if (row.mode === 'split' && Array.isArray(row.cells)
                            && row.cells.some(c => c && Array.isArray(c.keys)
                                                   && c.keys.length)) {
                            // At least one cell holds real keys -> cells authoritative.
                            // (Cells that are all keyless garbage fall through so the
                            // keys-path can still migrate any legacy row.keys — codex.)
                            const cleaned = cleanSplitCellsRow(row, seen, seenCellIds);
                            if (cleaned) cleanRows.push(cleaned);
                            continue;
                        }
                        // Keys-authoritative path: purge any stray/empty cells so a
                        // non-split row (or an empty-cells split) can never afterwards
                        // look like a cells-row to findKeyInLayout (codex).
                        delete row.cells;
                        const rkeys = [];
                        const rawKeys = Array.isArray(row.keys) ? row.keys : [];
                        for (const k of rawKeys) {
                            const key = String(k);
                            // Global dedupe across all ws/cols/rows; dormant and
                            // multi-host phantom keys are kept, dupes dropped.
                            if (!key || seen.has(key)) continue;
                            seen.add(key);
                            rkeys.push(key);
                        }
                        if (rkeys.length === 0) continue;       // drop empty row
                        const h = (typeof row.height === 'number' && row.height > 0)
                            ? row.height : 1;
                        // Canonicalize the row MODE: prefer a valid new enum
                        // (so a saved {mode:'split'} survives, and a corrupt
                        // {tabbed:true,mode:'split'} resolves to the explicit mode),
                        // else migrate the legacy `tabbed` boolean from old blobs.
                        let mode = (row.mode === 'single' || row.mode === 'tabbed'
                            || row.mode === 'split')
                            ? row.mode : (row.tabbed === true ? 'tabbed' : 'single');
                        // Cardinality repair: split needs >=2 keys (a key dropped
                        // by the global dedupe can leave a 1-key split → single).
                        if (mode === 'split' && rkeys.length < 2) mode = 'single';
                        if (mode !== 'tabbed' && mode !== 'split' && rkeys.length > 1) {
                            // Invariant: a SINGLE row holds exactly one window.
                            // Split a corrupt multi-key single row into one
                            // single-window row per key (each window stays visible
                            // as its own tile instead of being orphaned).
                            const each = h / rkeys.length;
                            for (const k of rkeys) cleanRows.push(newRow([k], each));
                            continue;
                        }
                        row.keys = rkeys;
                        row.height = h;
                        delete row.tabbed;          // drop the legacy boolean
                        setRowMode(row, mode);      // single writer: clears foreign fields
                        if (mode === 'tabbed') {
                            if (typeof row.activeTab !== 'string'
                                || rkeys.indexOf(row.activeTab) === -1)
                                row.activeTab = rkeys[0];   // heal to a present key
                        } else if (mode === 'split') {
                            // MIGRATE a legacy split (flat keys + window-keyed widths)
                            // to the cells shape: one LEAF cell per surviving key,
                            // carrying the old window-keyed width onto the new cell.id.
                            // rkeys are ALREADY deduped via `seen`, so build inline —
                            // do NOT route through cleanSplitCellsRow (it would re-dedupe
                            // every key away). Idempotent: a 2nd reconcile finds cells
                            // and takes the validator branch instead.
                            const oldW = (row.widths && typeof row.widths === 'object'
                                && !Array.isArray(row.widths)) ? row.widths : {};
                            const cells = [];
                            const widths = {};
                            for (const k of rkeys) {
                                const cell = makeCell([k]);     // leaf: {id, keys:[k]}
                                seenCellIds.add(cell.id);       // freshly minted, but keep the set complete
                                cells.push(cell);
                                const v = Number(oldW[k]);
                                if (Number.isFinite(v) && v > 0) widths[cell.id] = v;
                            }
                            row.cells = cells;
                            row.widths = widths;
                            normalizeSplitWidths(row);      // one frac per cell.id, sum 1
                            delete row.keys;                // split rows carry NO keys
                        }
                        cleanRows.push(row);
                    }
                    if (cleanRows.length === 0) continue;       // collapse empty column
                    col.rows = normalizeRowHeights(cleanRows);
                    cleanCols.push(col);
                }
                ws.columns = cleanCols;
                if (!Number.isInteger(ws.focusedCol)) ws.focusedCol = 0;
                ws.focusedCol = Math.max(0,
                    Math.min(ws.focusedCol, Math.max(0, cleanCols.length - 1)));
                cleanWs.push(ws);
            }
            if (cleanWs.length === 0) cleanWs.push(newWorkspace());
            L.workspaces = cleanWs;
            if (!Number.isInteger(L.activeWs)) L.activeWs = 0;
            L.activeWs = Math.max(0, Math.min(L.activeWs, L.workspaces.length - 1));
            return L;
        }
        function getLayout() {
            const L = reconcileLayout(prefs._layout);
            prefs._layout = L;
            return L;
        }
