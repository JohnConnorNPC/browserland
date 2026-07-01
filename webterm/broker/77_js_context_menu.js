        // ---- desktop context menu ----------------------------------------
        // Snapshot of every window's geom + minimized state captured right
        // before a layout action. Single-level undo: most recent snapshot
        // wins, prior snapshots are discarded.
        let lastLayoutSnapshot = null; // { label: string, states: Map<id,{geom,minimized}> }

        function snapshotAllWindows(label) {
            const states = new Map();
            for (const [id, win] of windows) {
                if (win.disposed || win.tiled) continue;   // floating-only undo
                states.set(id, {
                    geom: Object.assign({}, win.geom),
                    minimized: !!win.minimized,
                });
            }
            lastLayoutSnapshot = { label, states };
        }

        function applyGeomToWindow(win, geom) {
            // Floating-only writer of pixel geometry. Tiled windows are sized
            // by flex via relayoutStrip; writing absolute geom would fight it.
            // Callers filter tiled windows out; this is the backstop invariant.
            if (win.tiled) return;
            if (isSizeLocked()) {
                const ls = lockedSize();
                geom = Object.assign({}, geom, { width: ls.width, height: ls.height });
            }
            const g = clampGeom(geom);
            win.dom.style.left = g.left + 'px';
            win.dom.style.top = g.top + 'px';
            // .term-window has 2px borders all sides; style.* is content-box,
            // so offsetWidth/Height = style + 4. Subtract 4 so the window's
            // outer box fills its slot exactly (no overlap or overflow off
            // the bottom of the desktop on stacked layouts).
            win.dom.style.width = (g.width - 4) + 'px';
            win.dom.style.height = (g.height - 4) + 'px';
            win.geom = { left: g.left, top: g.top, width: g.width, height: g.height };
            if (win.type === 'app') saveAppWindow(win);
            else getPref(win.id).geom = Object.assign({}, win.geom);
        }

        function setMinimized(win, mini) {
            if (mini) {
                if (win.minimized) return;
                win.minimized = true;
                win.dom.classList.add('minimized');
                if (frontId === win.id) frontId = null;
            } else {
                if (!win.minimized) return;
                win.minimized = false;
                win.dom.classList.remove('minimized');
            }
        }

        function liveWindowsOrdered() {
            // Stable ordering: numeric window id ascending.
            const arr = [];
            for (const win of windows.values()) {
                if (!win.disposed && !hostHidden(win.hostId)) arr.push(win);
            }
            arr.sort((a, b) => {
                const na = Number(a.sid), nb = Number(b.sid);
                if (Number.isFinite(na) && Number.isFinite(nb)) return na - nb;
                return String(a.sid).localeCompare(String(b.sid));
            });
            return arr;
        }
        // Floating subset, same order. The one-shot floating arrangements
        // (Cascade / Tile*) operate only on floating windows; tiled windows are
        // governed by the strip engine and must be left untouched.
        function floatingWindowsOrdered() {
            return liveWindowsOrdered().filter(w => !w.tiled);
        }

        function swapWindows(a, b, aOriginalGeom) {
            // aOriginalGeom is captured at drag-start because by the time we
            // get here a.dom may have been moved by the live drag. b's geom
            // is still authoritative on b.dom.
            const bGeom = {
                left: b.dom.offsetLeft,
                top: b.dom.offsetTop,
                width: b.dom.offsetWidth,
                height: b.dom.offsetHeight,
            };
            applyGeomToWindow(a, bGeom);
            applyGeomToWindow(b, aOriginalGeom);
            refitSoon(a);
            refitSoon(b);
            bringToFront(a.id);
        }

        function doCascade() {
            const wins = floatingWindowsOrdered();
            if (!wins.length) return;
            snapshotAllWindows('Cascade');
            const desktop = document.getElementById('desktop');
            const dw = desktop.clientWidth || 1024;
            const dh = desktop.clientHeight || 700;
            const lk = isSizeLocked();
            const ds = defaultPixelSize();
            const w = lk ? ds.width : Math.min(ds.width, Math.max(MIN_W, dw - 40));
            const h = lk ? ds.height : Math.min(ds.height, Math.max(MIN_H, dh - 40));
            const slackX = Math.max(1, dw - w - 60);
            const slackY = Math.max(1, dh - h - 60);
            wins.forEach((win, i) => {
                setMinimized(win, false);
                const left = 30 + (i * CASCADE_DX) % slackX;
                const top = 20 + (i * CASCADE_DY) % slackY;
                applyGeomToWindow(win, { left, top, width: w, height: h });
                refitSoon(win);
                win.dom.style.zIndex = String(floatZIndex(win));
            });
            const top = wins[wins.length - 1];
            frontId = top.id;
            updateTaskbarActive();
            try { top.term && top.term.focus(); } catch (_) {}
            savePrefs();
        }

        function doTileHorizontal() {
            // Each window gets a full-width row, equal-height stacked rows.
            const wins = floatingWindowsOrdered();
            if (!wins.length) return;
            snapshotAllWindows('Tile Horizontally');
            const desktop = document.getElementById('desktop');
            const dw = desktop.clientWidth || 1024;
            const dh = desktop.clientHeight || 700;
            const n = wins.length;
            const lk = isSizeLocked();
            const ds = defaultPixelSize();
            const rowH = Math.max(MIN_H, Math.floor(dh / n));
            wins.forEach((win, i) => {
                setMinimized(win, false);
                applyGeomToWindow(win, {
                    left: 0,
                    top: i * rowH,
                    width: lk ? ds.width : dw,
                    height: lk ? ds.height
                                : ((i === n - 1) ? (dh - i * rowH) : rowH),
                });
                refitSoon(win);
            });
            savePrefs();
            updateTaskbarActive();
        }

        function doTileVertical() {
            // Each window gets a full-height column, side-by-side.
            const wins = floatingWindowsOrdered();
            if (!wins.length) return;
            snapshotAllWindows('Tile Vertically');
            const desktop = document.getElementById('desktop');
            const dw = desktop.clientWidth || 1024;
            const dh = desktop.clientHeight || 700;
            const n = wins.length;
            const lk = isSizeLocked();
            const ds = defaultPixelSize();
            const colW = Math.max(MIN_W, Math.floor(dw / n));
            wins.forEach((win, i) => {
                setMinimized(win, false);
                applyGeomToWindow(win, {
                    left: i * colW,
                    top: 0,
                    width: lk ? ds.width
                              : ((i === n - 1) ? (dw - i * colW) : colW),
                    height: lk ? ds.height : dh,
                });
                refitSoon(win);
            });
            savePrefs();
            updateTaskbarActive();
        }

        function doTileGrid() {
            // Square-ish grid: ceil(sqrt(N)) columns, ceil(N/cols) rows. The
            // last row absorbs the remainder so there's never a gap.
            const wins = floatingWindowsOrdered();
            if (!wins.length) return;
            snapshotAllWindows('Tile H + V');
            const desktop = document.getElementById('desktop');
            const dw = desktop.clientWidth || 1024;
            const dh = desktop.clientHeight || 700;
            const n = wins.length;
            const cols = Math.max(1, Math.ceil(Math.sqrt(n)));
            const rows = Math.max(1, Math.ceil(n / cols));
            const cellW = Math.floor(dw / cols);
            const cellH = Math.floor(dh / rows);
            const lk = isSizeLocked();
            const ds = defaultPixelSize();
            wins.forEach((win, i) => {
                const r = Math.floor(i / cols);
                const c = i % cols;
                // Items in the (possibly short) last row span the leftover
                // width so nothing stays empty on the right.
                const rowItems = Math.min(cols, n - r * cols);
                const isLastInRow = (c === rowItems - 1);
                const isLastRow = (r === rows - 1);
                const w = lk ? ds.width : (isLastInRow ? (dw - c * cellW) : cellW);
                const h = lk ? ds.height : (isLastRow ? (dh - r * cellH) : cellH);
                setMinimized(win, false);
                applyGeomToWindow(win, {
                    left: c * cellW,
                    top: r * cellH,
                    width: w,
                    height: h,
                });
                refitSoon(win);
            });
            savePrefs();
            updateTaskbarActive();
        }

        function doMinimizeAll() {
            const wins = floatingWindowsOrdered();
            if (!wins.length) return;
            // Skip snapshot if everyone is already minimized — undo would be a no-op.
            if (wins.every(w => w.minimized)) return;
            snapshotAllWindows('Minimize All Windows');
            for (const win of wins) setMinimized(win, true);
            updateTaskbarActive();
        }

        function doUndoLayout() {
            if (!lastLayoutSnapshot) return;
            const snap = lastLayoutSnapshot;
            lastLayoutSnapshot = null;
            for (const [id, st] of snap.states) {
                const win = windows.get(id);
                if (!win || win.disposed) continue;
                applyGeomToWindow(win, st.geom);
                setMinimized(win, st.minimized);
                if (!st.minimized) refitSoon(win);
            }
            savePrefs();
            updateTaskbarActive();
        }

        const ctxMenu = document.getElementById('ctx-menu');
        // Shared menu renderer — used by the desktop/taskbar layout menu and
        // the launch button's profile picker.
        function renderMenu(items, x, y) {
            ctxMenu.innerHTML = '';
            for (const it of items) {
                if (it.sep) {
                    const s = document.createElement('div');
                    s.className = 'ctx-sep';
                    ctxMenu.appendChild(s);
                    continue;
                }
                const el = document.createElement('div');
                el.className = 'ctx-item' + (it.enabled ? '' : ' disabled');
                // #119: app-menu items carry an iconKey (a mod id). renderMenu
                // resolves it HERE to the trusted, hardcoded SVG via appIconSvg —
                // which returns '' for anything not in the APP_ICON_SVG registry —
                // so the ONLY value ever injected as innerHTML is our own markup;
                // no caller can route arbitrary/user text through this menu. The
                // label stays textContent in its own span (the "labels are
                // textContent only" rule, see showHostPicker). Every other menu
                // (layout, host/profile pickers) leaves iconKey unset, so those
                // items stay a bare textContent label.
                const iconSvg = it.iconKey ? appIconSvg(it.iconKey) : '';
                if (iconSvg) {
                    const ic = document.createElement('span');
                    ic.className = 'ctx-icon';
                    ic.setAttribute('aria-hidden', 'true');
                    ic.innerHTML = iconSvg;
                    el.appendChild(ic);
                    const lab = document.createElement('span');
                    lab.className = 'ctx-label';
                    lab.textContent = it.label;
                    el.appendChild(lab);
                } else {
                    el.textContent = it.label;
                }
                if (it.enabled) {
                    el.addEventListener('click', () => {
                        hideCtxMenu();
                        it.action();
                    });
                }
                ctxMenu.appendChild(el);
            }
            // Show off-screen first to measure, then clamp into viewport.
            ctxMenu.style.left = '-9999px';
            ctxMenu.style.top = '-9999px';
            ctxMenu.classList.add('open');
            const rect = ctxMenu.getBoundingClientRect();
            const vw = window.innerWidth;
            const vh = window.innerHeight;
            const left = Math.min(x, vw - rect.width - 4);
            const top = Math.min(y, vh - rect.height - 4);
            ctxMenu.style.left = Math.max(0, left) + 'px';
            ctxMenu.style.top = Math.max(0, top) + 'px';
        }

        // ---- host pickers for the file tools (#46) ------------------------
        // One reusable menu of every configured host, shared by the text
        // editor's window-level picker and each file-manager pane's picker.
        // Labels are user input — textContent only (renderMenu already does),
        // never innerHTML (codex review).
        function hostPickerLabel(h) {
            if (!h) return 'host';
            return h.id === 'local' ? 'this broker'
                : (h.label || h.url || h.id);
        }
        // currentId is marked (●) + disabled; picking another calls onPick(h).
        function showHostPicker(currentId, x, y, onPick) {
            const items = getHosts().map(h => ({
                label: (h.id === currentId ? '● ' : '    ')
                    + hostPickerLabel(h),
                enabled: h.id !== currentId,
                action: () => onPick(h),
            }));
            renderMenu(items, x, y);
        }
        // Prompt a host's login WITHOUT stealing one already in progress for a
        // DIFFERENT host. force=true bypasses "auto-pop once per host", but a
        // closed overlay is the only thing it's safe to force open — if some
        // other host's form is live, fall back to the non-forcing path (which
        // showAuthOverlay no-ops while that form is open). (#46 / codex review.)
        function promptFileHostAuth(host) {
            if (!host) return;
            showAuthOverlay(host, !authOverlay.classList.contains('open'));
        }
        // ---- WM actions (P2: presets / float<->tile / mode switch) --------
        const PRESET_LABELS = { '1/3': '⅓', '1/2': '½',
                                '2/3': '⅔', 'max': 'max' };
        function setColumnPreset(win, preset) {
            const loc = findKeyInLayout(win.id);
            if (!loc) return;
            loc.col.widthPreset = (WIDTH_PRESETS.indexOf(preset) !== -1)
                ? preset : DEFAULT_NEW_PRESET;
            // Picking a preset discards any custom drag-resized width (task 9).
            delete loc.col.widthFrac;
            savePrefs();
            requestRelayout();
        }
        function toggleFloating(win) {
            if (!win || win.disposed) return;
            if (win.tiled) detachToFloat(win);   // restores floatGeom
            else attachToStrip(win);             // snapshots floatGeom
        }
        // Switch the desktop into tiling mode AND tile the current floating
        // windows (each snapshots its float geom via attachToStrip, so the
        // switch is fully reversible by enterFloatingMode). niri rule: each
        // becomes its own appended column.
        function enterTilingMode() {
            getLayout().mode = 'tiling';
            savePrefs();
            for (const win of floatingWindowsOrdered()) attachToStrip(win);
            renderWorkspaces();
            requestRelayout();
        }
        // Reverse: float every tiled window back to its snapshotted geom and
        // leave tiling mode. Never silently destroys a hand-arranged layout —
        // detachToFloat restores each window's floatGeom.
        function enterFloatingMode() {
            getLayout().mode = 'floating';
            savePrefs();
            for (const win of liveWindowsOrdered()) {
                if (win.tiled) detachToFloat(win);
            }
            renderWorkspaces();
            requestRelayout();
        }

