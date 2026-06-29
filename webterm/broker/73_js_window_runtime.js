        function refitSoon(win) {
            requestAnimationFrame(() => requestAnimationFrame(() => {
                if (!win.disposed) sendResize(win, true);
            }));
        }

        function maybeSendInitialResize(win) {
            if (win.disposed) return;
            if (win.termReady && win.wsOpen) sendResize(win, true);
        }

        function attachWebSocket(win) {
            const host = hostById(win.hostId);
            if (!host) {
                // Host removed from settings mid-flight — nothing to dial.
                closeWindow(win.id);
                return;
            }
            const url = hostWsUrl(host,
                '/ws?session=' + encodeURIComponent(win.sid)
                + '&clientId=' + encodeURIComponent(CLIENT_ID));
            const ws = new WebSocket(url);
            ws.binaryType = 'arraybuffer';
            win.ws = ws;
            win.wsOpen = false;
            win.staleSession = false;
            // Open time of THIS attempt — 0 until (unless) the socket opens,
            // so scheduleReattach can tell a stable connection's death from
            // a failed dial.
            win.lastOpenAt = 0;

            ws.onopen = () => {
                if (win.disposed) { try { ws.close(); } catch (_) {} return; }
                win.wsOpen = true;
                win.authFailed = false;
                win.lastOpenAt = Date.now();
                try { win.term.write('Connected to webterm (session ' + win.sid + ')\r\n'); } catch (_) {}
                maybeSendInitialResize(win);
            };

            ws.onmessage = (event) => {
                if (win.disposed) return;
                if (event.data instanceof ArrayBuffer) {
                    win.term.write(new Uint8Array(event.data));
                    return;
                }
                let data;
                try { data = JSON.parse(event.data); }
                catch (_) { try { win.term.write(event.data); } catch (__) {} return; }
                if (data.type === 'error' && data.reason === 'unknown_session') {
                    win.staleSession = true;
                    return;
                }
                if (data.type === 'exit') {
                    // The session's child process exited (PTY EOF). The broker
                    // forwards this the instant it happens, so tear the window
                    // down now instead of waiting out the /sessions poll grace
                    // cycle (~12 s). Ordered after the child's final output, so
                    // that output has already been written above. staleSession
                    // guards the rare case the WS races closed first; closeWindow
                    // is idempotent and disposes the socket so no reattach fires.
                    win.staleSession = true;
                    showNotice('session ' + win.sid + ' exited');
                    closeWindow(win.id);
                    return;
                }
                if (data.type === 'resized') {
                    const cols = Math.max(1, data.cols | 0);
                    const rows = Math.max(1, data.rows | 0);
                    try { win.term.resize(cols, rows); } catch (_) {}
                    // Tiled windows: flex owns the pixel box, so never snap the
                    // .term-window size to the grid — just accept the grid.
                    if (win.tiled) return;
                    // Locked clients refuse peer-driven size snaps — the
                    // local pixel area must stay at lockedSize() regardless
                    // of what an unlocked peer dragged to.
                    if (isSizeLocked()) return;
                    // Snap the .term-window pixel size to the new grid so peer
                    // browsers follow a remote drag-resize. Originator's drag
                    // produced these exact dims so this is usually identity
                    // (or a small grid-alignment snap).
                    const dims = readCellDims(win);
                    if (dims) {
                        // .term-window box-model (content-box):
                        //   borders: 2px each side (4 horiz, 4 vert)
                        //   .term-body padding: 4px each side (8 horiz, 8 vert)
                        //   title-bar: 26px (vert only)
                        // -> outer offsetWidth  = grid_w + 12
                        // -> outer offsetHeight = grid_h + 38
                        // ceil avoids fractional cellW shaving cols on the
                        // floor() round-trip in sendResize().
                        const targetW = Math.ceil(cols * dims.w) + 12;
                        const targetH = Math.ceil(rows * dims.h) + 38;
                        const curW = win.dom.offsetWidth;
                        const curH = win.dom.offsetHeight;
                        // Tolerate up to one cell of drift before snapping.
                        // readCellDims() varies a hair between fresh term
                        // instances (font/canvas measurement), so on
                        // close+reopen a sub-cell drift would otherwise drop
                        // a row/col and shrink the window each cycle. A
                        // real peer-sync from a remote drag-resize moves the
                        // size by many cells and still trips the snap.
                        const snapThresholdW = Math.max(2, dims.w);
                        const snapThresholdH = Math.max(2, dims.h);
                        if (Math.abs(targetW - curW) > snapThresholdW
                            || Math.abs(targetH - curH) > snapThresholdH) {
                            const desktop = document.getElementById('desktop');
                            const dw = desktop.clientWidth;
                            const dh = desktop.clientHeight;
                            const left = win.dom.offsetLeft;
                            const top = win.dom.offsetTop;
                            const w = Math.max(MIN_W, Math.min(targetW, dw - left));
                            const h = Math.max(MIN_H, Math.min(targetH, dh - top));
                            // style.width is content-box; subtract borders so
                            // the resulting offsetWidth matches the target.
                            win.dom.style.width = (w - 4) + 'px';
                            win.dom.style.height = (h - 4) + 'px';
                            win.geom.width = win.dom.offsetWidth;
                            win.geom.height = win.dom.offsetHeight;
                            getPref(win.id).geom = Object.assign({}, win.geom);
                            savePrefs();
                        }
                    }
                    return;
                }
                if (data.type === 'output') {
                    try { win.term.write(data.data); } catch (_) {}
                    return;
                }
                if (data.type === 'mcp_activity') {
                    // #33: an agent just read (kind:'read') or wrote (kind:'write')
                    // this window over MCP — pulse its robot icon.
                    flashMcpBtn(win, data.kind === 'write' ? 'write' : 'read');
                    return;
                }
                if (data.type === 'title') {
                    // Live title push from the producer (OSC 0/2 etc.).
                    // Mirror the polling path in refreshTaskbarInner so the
                    // title bar + taskbar entry both update without waiting.
                    const newTitle = String(data.data || '');
                    const key = String(win.id);
                    const sess = sessions.get(key)
                        || { key, id: win.sid, sid: win.sid,
                             hostId: win.hostId,
                             hostLabel: (hostById(win.hostId) || {}).label };
                    sess.title = newTitle;
                    sessions.set(key, sess);
                    const display = formatTitle(sess);
                    if (display !== win.name) {
                        win.name = display;
                        try { win.titleText.textContent = display; } catch (_) {}
                    }
                    updateTaskbarLabel(key);
                }
            };

            ws.onclose = (ev) => {
                if (win.disposed) return;
                win.wsOpen = false;
                if (ev && ev.code === 4401) {
                    // Broker rejected our token post-upgrade. Never
                    // auto-retried — the overlay (or its chip) is the only
                    // recovery, and only for THIS window's host.
                    win.authFailed = true;
                    pollStateFor(win.hostId).authNeeded = true;
                    showAuthOverlay(hostById(win.hostId));
                    renderHostStatus();
                    return;
                }
                if (ev && ev.code === 4409) {
                    // Deactivated: another browser took this broker's single-
                    // active lease. The control-WS {active:false} push drives
                    // the full teardown (HOME) or host masking (remote); do NOT
                    // reattach here — the relay drops a non-active socket's
                    // input anyway, so a reattach would just bounce on 4409.
                    return;
                }
                if (win.staleSession) {
                    showNotice('session ' + win.sid + ' no longer exists');
                    closeWindow(win.id);
                    return;
                }
                try { win.term.write('\r\n\r\n[connection closed]\r\n'); } catch (_) {}
                // Auto-reattach (taskbar poll fires it once the session is
                // confirmed alive): stable connections retry quickly, flappy
                // ones back off 2 s * 2^n up to 30 s.
                scheduleReattach(win);
            };

            ws.onerror = () => {
                if (win.disposed) return;
                try { win.term.write('\r\n[ws error]\r\n'); } catch (_) {}
            };
        }

        function scheduleReattach(win) {
            const now = Date.now();
            if (win.lastOpenAt && (now - win.lastOpenAt) >= REATTACH_STABLE_MS) {
                win.reattachAttempts = 0;
            }
            const delay = Math.min(REATTACH_BACKOFF_MAX_MS,
                2000 * Math.pow(2, win.reattachAttempts));
            win.reattachAttempts += 1;
            win.reattachAt = now + delay;
        }

        function reattachWindow(win) {
            if (win.disposed) return;
            try {
                if (win.ws) {
                    win.ws.onopen = null; win.ws.onmessage = null;
                    win.ws.onclose = null; win.ws.onerror = null;
                    win.ws.close();
                }
            } catch (_) {}
            attachWebSocket(win);
        }

        function cacheCellDims(w, h) {
            // Persist the last measured cell box so defaultPixelSize() can
            // honor a cols×rows setting before any window is open. 0.25px
            // tolerance: fresh term instances measure a hair differently
            // (see the snap-threshold comment in the `resized` handler) and
            // this sits on the resize hot path — a hairline tolerance would
            // rewrite localStorage on almost every call.
            const s = getSettings();
            const c = s.cellDims;
            if (c && Math.abs(c.w - w) < 0.25 && Math.abs(c.h - h) < 0.25) return;
            s.cellDims = { w, h };
            savePrefs();
        }

        function readCellDims(win) {
            // xterm 5.3 private API. Same accessor as the previous
            // single-session path. Guard heavily — shape can vary by version
            // and is unavailable until first render.
            try {
                const core = win.term && win.term._core;
                const rs = core && core._renderService;
                const dims = rs && rs.dimensions;
                if (!dims) return null;
                const w = dims.actualCellWidth
                    || (dims.css && dims.css.cell && dims.css.cell.width);
                const h = dims.actualCellHeight
                    || (dims.css && dims.css.cell && dims.css.cell.height);
                if (!w || !h) return null;
                cacheCellDims(w, h);
                return { w, h };
            } catch (_) {
                return null;
            }
        }

        // The single guard against hidden/off-workspace/minimized/parked
        // terminals computing a bogus 1x1 grid: every sizing path flows
        // through sendResize, which early-returns here. A window with a
        // zero/near-zero body rect (display:none, detached/parked, or laid
        // out at 0px) must never reach the cols/rows math below.
        function isResizable(win) {
            if (!win || win.disposed || win.minimized) return false;
            let r;
            try { r = win.body.getBoundingClientRect(); } catch (_) { return false; }
            return !!r && r.width >= 2 && r.height >= 2;
        }

        function sendResize(win, force) {
            if (!win || win.disposed) return;
            if (!isResizable(win)) return;
            if (!win.ws || win.ws.readyState !== WebSocket.OPEN) return;
            const dims = readCellDims(win);
            if (!dims) return;
            const rect = win.body.getBoundingClientRect();
            // .term-body has 4px padding all sides.
            const availW = rect.width - 8;
            const availH = rect.height - 8;
            if (availW < dims.w || availH < dims.h) return;
            const cols = Math.max(1, Math.floor(availW / dims.w));
            const rows = Math.max(1, Math.floor(availH / dims.h));
            if (!force && win.lastSentDims
                && win.lastSentDims.cols === cols
                && win.lastSentDims.rows === rows) {
                return;
            }
            win.lastSentDims = { cols, rows };
            try { win.ws.send(JSON.stringify({ type: 'resize', cols, rows })); }
            catch (_) {}
        }

        function scheduleResize(win) {
            if (win.resizeTimer) clearTimeout(win.resizeTimer);
            win.resizeTimer = setTimeout(() => {
                win.resizeTimer = null;
                sendResize(win, false);
            }, RESIZE_DEBOUNCE_MS);
        }

        // ---- shared app-window chrome factory (#79) -----------------------
        // Every app-window KIND -- sticky-note + text-editor (openAppWindow),
        // file-manager, task-manager, control-panel, and the help mod -- builds
        // the SAME .term-window.app-window shell: a .title-bar with an id badge,
        // a title, and minimize/close buttons; eight .rh resize handles; and the
        // identical raise-on-mousedown / drag / 8-way resize / title-bar context
        // menu wiring. These three helpers own that shared chrome so each kind
        // only supplies its own body + behavior. PURE REFACTOR (#79): the DOM
        // produced (element order, classes, attributes) and the listeners wired
        // are byte-for-byte what each kind built inline before. Terminals
        // (openWindow, .term-window WITHOUT .app-window) are deliberately out --
        // their title bar carries git/MCP controls and is left untouched.
        //
        // They are top-level `function` declarations (hoisted across the one
        // concatenated <script>, so the earlier kind fragments AND the help mod
        // script can call them regardless of fragment order) and run only at
        // window-open time, when every dependency (isDarkAccent, wireDrag,
        // wireResize, buildWindowMenu, bringToFront, minimizeWindow, closeWindow)
        // is already initialized.

        // Build the shell + title bar. Returns the element refs a kind needs to
        // hang its own extras (toolbar, body, color picker, note buttons). It does
        // NOT append the resize handles (those go AFTER the body, via
        // addResizeHandles, so they stay the last children) and does NOT insert
        // into the desktop. `spec.geom` is the already-clamped OUTER box; the -4
        // turns it into the content-box width/height exactly as openWindow /
        // applyGeomToWindow do. `spec.badge` is the literal id-badge text
        // ('#cp' for the control panel, whose badge differs from its sid).
        function buildAppChrome(spec) {
            const dom = document.createElement('div');
            dom.className = 'term-window app-window ' + spec.appClass;
            dom.dataset.sessionId = spec.id;
            dom.style.left = spec.geom.left + 'px';
            dom.style.top = spec.geom.top + 'px';
            // Same content-box -4 math as openWindow / applyGeomToWindow.
            dom.style.width = (spec.geom.width - 4) + 'px';
            dom.style.height = (spec.geom.height - 4) + 'px';
            dom.style.setProperty('--accent', spec.color);
            dom.classList.toggle('dark-accent', isDarkAccent(spec.color));
            if (spec.locked) dom.classList.add('scroll-locked');   // pinned float

            const titleBar = document.createElement('div');
            titleBar.className = 'title-bar';
            const idBadge = document.createElement('span');
            idBadge.className = 'ti-id-badge';
            idBadge.textContent = spec.badge;
            const titleText = document.createElement('span');
            titleText.className = 'title-text';
            titleText.textContent = spec.title;
            const minBtn = document.createElement('button');
            minBtn.type = 'button';
            minBtn.className = 'tb-btn btn-min';
            minBtn.textContent = '_';
            minBtn.title = 'minimize';
            const closeBtn = document.createElement('button');
            closeBtn.type = 'button';
            closeBtn.className = 'tb-btn btn-close';
            closeBtn.textContent = '×';
            closeBtn.title = 'close';
            titleBar.appendChild(idBadge);
            titleBar.appendChild(titleText);
            titleBar.appendChild(minBtn);
            titleBar.appendChild(closeBtn);
            dom.appendChild(titleBar);

            return { dom, titleBar, idBadge, titleText, minBtn, closeBtn };
        }

        // Append the eight .rh resize handles (edges n/s/e/w + corners). Called
        // AFTER a kind appends its body so the handles stay the LAST children:
        // they are absolute-positioned overlays whose edge/corner hit zones must
        // sit on top of the body.
        function addResizeHandles(dom) {
            for (const dir of ['n','s','e','w','nw','ne','sw','se']) {
                const h = document.createElement('div');
                h.className = 'rh rh-' + dir;
                h.dataset.dir = dir;
                dom.appendChild(h);
            }
        }

        // Wire the shared chrome interactions onto an app window: raise-on-
        // mousedown, the minimize + close buttons (close defaults to closeWindow;
        // the text-editor passes requestCloseAppWindow so a dirty buffer offers to
        // flush its server file first), the title-bar drag, the title-bar context
        // menu (the per-window WM menu), and the eight resize handles. Every
        // listener is registered in win.cleanups so closeWindow tears them down.
        // `chrome` carries the refs from buildAppChrome.
        function wireAppChrome(win, chrome, onClose) {
            const id = win.id;
            const titleBar = chrome.titleBar;
            const minBtn = chrome.minBtn;
            const closeBtn = chrome.closeBtn;
            const close = onClose || closeWindow;
            const stopProp = (e) => e.stopPropagation();

            const onMouseDown = () => bringToFront(id);
            win.dom.addEventListener('mousedown', onMouseDown);
            win.cleanups.push(() =>
                win.dom.removeEventListener('mousedown', onMouseDown));

            const onMinClick = (e) => { e.stopPropagation(); minimizeWindow(id); };
            const onCloseClick = (e) => { e.stopPropagation(); close(id); };
            minBtn.addEventListener('mousedown', stopProp);
            minBtn.addEventListener('click', onMinClick);
            closeBtn.addEventListener('mousedown', stopProp);
            closeBtn.addEventListener('click', onCloseClick);
            win.cleanups.push(() => {
                minBtn.removeEventListener('mousedown', stopProp);
                minBtn.removeEventListener('click', onMinClick);
                closeBtn.removeEventListener('mousedown', stopProp);
                closeBtn.removeEventListener('click', onCloseClick);
            });

            wireDrag(win, titleBar);
            const onTitleCtx = (e) => {
                e.preventDefault();
                e.stopPropagation();
                bringToFront(win.id);
                buildWindowMenu(win, e.clientX, e.clientY);
            };
            titleBar.addEventListener('contextmenu', onTitleCtx);
            win.cleanups.push(() =>
                titleBar.removeEventListener('contextmenu', onTitleCtx));
            for (const handle of win.dom.querySelectorAll('.rh')) {
                wireResize(win, handle, handle.dataset.dir);
            }
        }

        function minimizeWindow(id) {
            const win = windows.get(id);
            if (!win) return;
            // Capture sibling-focus context while the row is still in _layout.
            const focusPrecap = (win.tiled && frontId === id)
                ? captureTiledFocusContext(id) : null;
            win.minimized = true;
            win.dom.classList.add('minimized');
            if (frontId === id) frontId = null;
            updateTaskbarActive();
            // Tiled: the column drops it from the render (membership retained),
            // collapsing the column if it was the last row. No display:none
            // hole left in a flex column.
            if (win.tiled) requestRelayout();
            if (focusPrecap) {
                requestAnimationFrame(() => reconcileTiledFocus(focusPrecap));
            }
        }

        function restoreWindow(id) {
            const win = windows.get(id);
            if (!win) return;
            win.minimized = false;
            win.dom.classList.remove('minimized');
            // Tiled: reparent back into its retained column before measuring.
            if (win.tiled) requestRelayout();
            bringToFront(id);
            refitSoon(win);
        }

        function closeWindow(id) {
            const win = windows.get(id);
            if (!win) return;
            // Capture app identity up front: the teardown below removes the DOM
            // and the windows-map entry, and the cleanups clear the autosave
            // debounce. Flush one final content snapshot NOW (before cleanups
            // drop the timer) so a close right after typing never loses the
            // last keystrokes.
            const isApp = win.type === 'app';
            if (isApp) saveAppWindow(win);
            else removeOpenTerm(id);   // restore-on-refresh: no longer open
            // Tiled: drop from its column (collapse + reflow) before teardown.
            // Membership is removed so a later relayout never touches the dead
            // window; the column collapses if this was its last row. Capture
            // sibling-focus context first so a closed focused row hands focus
            // to a sibling/neighbor.
            let focusPrecap = null;
            if (win.tiled) {
                if (frontId === id) focusPrecap = captureTiledFocusContext(id);
                removeFromStrip(id);
            }
            win.disposed = true;
            if (win.resizeTimer) { clearTimeout(win.resizeTimer); win.resizeTimer = null; }
            for (const fn of win.cleanups) { try { fn(); } catch (_) {} }
            win.cleanups = [];
            if (win.ws) {
                try { win.ws.onopen = null; win.ws.onmessage = null;
                      win.ws.onclose = null; win.ws.onerror = null; } catch (_) {}
                try { win.ws.close(); } catch (_) {}
                win.ws = null;
            }
            if (win.term) { try { win.term.dispose(); } catch (_) {} }
            try { win.dom.remove(); } catch (_) {}
            windows.delete(id);
            // App windows are document-model: × close tears down the live
            // window AND its taskbar chip + synthetic session. The window kind's
            // retainOnClose decides keep-vs-discard (#80/S7): issue #11 keeps ONLY
            // a non-empty sticky note (its registry entry trims the content + says
            // keep), stored open:false so it reopens from the launch menu's "Closed
            // notes" list. saveAppWindow above refreshed the record, so the trim
            // sees the latest keystrokes. Every other kind (no retainOnClose) — an
            // empty sticky note, a text editor, a file manager, or an unknown kind —
            // is deleted outright: editors back their real content with server files
            // (the × dirty-save prompt offers to write them first) and a kept-but-
            // hidden record would be unreachable dead storage anyway. Ephemeral
            // kinds wrote no record (rec is absent), so they fall through untouched.
            // destroyAppWindow remains the explicit per-doc discard for a live note.
            if (isApp) {
                const rec = appStore[id];
                if (rec) {
                    const kind = lookupWindowKind(rec.appKind);
                    if (kind && kind.retainOnClose && kind.retainOnClose(rec)) {
                        rec.open = false;
                    } else {
                        delete appStore[id];
                    }
                    saveAppStore();
                }
                const tItem = document.querySelector(
                    '.taskbar-item[data-session-id="' + cssEscape(id) + '"]');
                if (tItem) tItem.remove();
                sessions.delete(id);
            }
            if (frontId === id) frontId = null;
            if (windows.size === 0) {
                document.getElementById('desktop').classList.add('empty');
            }
            updateTaskbarActive();
            if (focusPrecap) {
                requestAnimationFrame(() => reconcileTiledFocus(focusPrecap));
            }
        }

        // User-initiated close for an app window — the one place a save prompt
        // belongs (closeWindow itself stays non-blocking: the /sessions poll
        // reaper calls it in a loop, so it can never await/confirm). For a dirty
        // text-editor, offer to flush the SERVER file first; the buffer already
        // autosaves to appStore, so declining only skips the server-file write.
        // Then tear down via the normal closeWindow path.
        async function requestCloseAppWindow(id) {
            const win = windows.get(id);
            if (!win || win.type !== 'app') { closeWindow(id); return; }
            // Dirty = any unsaved doc (a multi-tab agent-docs window can have
            // more than one). Capture the live editor into its doc first so the
            // check sees the latest edit, then flush the document model to
            // appStore NOW — closeWindow clears the pending autosave debounce, so
            // an edit made within that window would otherwise be lost from the
            // store even when the user declines the server save (Codex review).
            if (win.tabs && win._captureActiveDoc) {
                try { win._captureActiveDoc(); } catch (_) {}
            }
            try { saveAppWindow(win); } catch (_) {}
            const anyDirty = win.tabs
                ? win.tabs.some(d => d.kind === 'file' && d.dirty)
                : win.dirty;
            if (win.appKind === 'text-editor' && anyDirty) {
                // OK = save the server file(s) before closing; Cancel = close
                // without writing them (the buffers still live in appStore, so
                // nothing is lost — they reopen dirty). If the user asked to save
                // but a write fails / Save As is cancelled, keep the window open
                // rather than silently dropping the server save.
                const what = win.tabs ? 'the open documents'
                    : (win.filePath || 'this file');
                if (confirm('Save changes to ' + what + ' before closing?')) {
                    let ok = false;
                    const saver = win._saveAllDirty || win._saveToServer;
                    if (saver) {
                        try { ok = await saver(); } catch (_) {}
                    }
                    if (!ok) return;   // save failed/cancelled -> abort close
                }
            }
            closeWindow(id);
        }

        // Permanently discard an app document. closeWindow already tears down
        // the window, drops _layout membership, and (for app docs) removes the
        // chip + synthetic session and marks the store record open:false; this
        // additionally deletes the store record so it's gone for good. The
        // chip/session removal below is redundant-but-harmless belt-and-braces.
        // This is the explicit "Delete note"/"Delete file" action — the × button
        // keeps the doc (reopen from the launch menu); only this throws it away.
        function destroyAppWindow(id) {
            closeWindow(id);
            const tItem = document.querySelector(
                '.taskbar-item[data-session-id="' + cssEscape(id) + '"]');
            if (tItem) tItem.remove();
            sessions.delete(id);
            deleteAppWindow(id);
            updateTaskbarActive();
        }

