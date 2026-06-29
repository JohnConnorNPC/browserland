        async function openAgentDocsWindow(opts) {
            const cwd = String(opts.cwd || '');
            const fileHostId = opts.fileHostId || 'local';
            const host = hostById(fileHostId) || localHost();
            const agentsPath = joinNative(cwd, 'AGENTS.md');
            const claudePath = joinNative(cwd, 'CLAUDE.md');
            const [aRes, cRes] = await Promise.all([
                fileApiPost('/file/read', { path: agentsPath }, host),
                fileApiPost('/file/read', { path: claudePath }, host),
            ]);
            // A real read error (not just "doesn't exist yet") aborts.
            if (aRes && !aRes.ok && aRes.error !== 'not_found') {
                showNotice('open AGENTS.md failed: ' + ((aRes && aRes.error) || '?'));
                return;
            }
            const agentsContent = (aRes && aRes.ok) ? (aRes.content || '') : '';
            const claudeContent = (cRes && cRes.ok) ? (cRes.content || '') : '';
            const mkDoc = (name, filePath, content, isAgents) => ({
                name, filePath, content,
                wrap: true, lineNums: true, isAgents,
            });
            const docs = [
                mkDoc('AGENTS.md', agentsPath, agentsContent, true),
                mkDoc('CLAUDE.md', claudePath, claudeContent, false),
            ];
            // Label remote windows with the host so two docs windows sharing a
            // cwd string stay distinguishable on the taskbar.
            const title = (fileHostId === 'local')
                ? ('Agent docs — ' + cwd)
                : ('Agent docs — ' + (host.label || fileHostId) + ':' + cwd);
            openAppWindow({
                id: opts.id,
                appKind: 'text-editor',
                title,
                docs,
                activeTab: opts.activeTab || 0,
                agentsMdCwd: cwd,
                fileHostId,
                geom: opts.geom,
                color: opts.color,
                locked: opts.locked,
                floatGeom: opts.floatGeom,
            });
        }

        // Open (or focus) the tabbed Agent-docs editor (AGENTS.md + CLAUDE.md +
        // Sections) for a terminal's working dir. Keyed by host:cwd so re-clicking
        // the titlebar 📋 button reuses one window.
        async function openAgentsMdEditor(termId) {
            const sess = sessions.get(termId);
            const cwd = sess && sess.cwd;
            if (!cwd) {
                showNotice('working directory unknown for this session');
                return;
            }
            // The terminal's cwd is an absolute path on ITS host (local OR
            // remote). Dial that broker for every /file/* op so a remote
            // terminal edits the remote host's docs, not a local one.
            const fileHostId = (sess && sess.hostId) || 'local';
            // Host-qualify the window id: local and remote terminals can share
            // a cwd string (e.g. both rooted at /home/user), so a bare cwd key
            // would collide and reuse the wrong broker's window.
            const aid = 'app:agents:' + fileHostId + ':' + cwd;
            if (windows.has(aid)) {
                const w = windows.get(aid);
                if (w.minimized) restoreWindow(aid);
                else bringToFront(aid);
                return;
            }
            await openAgentDocsWindow({ id: aid, cwd, fileHostId });
            // Land the fresh docs window as a tab in the terminal it was opened
            // from (a [terminal│AGENTS] tab group) instead of floating.
            // openAgentDocsWindow can bail before creating the window (sandbox /
            // read error), so guard on the window actually existing. Guard on the
            // terminal living in the ACTIVE workspace, not merely existing in the
            // layout: the file-read await above can interleave a workspace switch,
            // and tabbing into an inactive-workspace tile would move the
            // freshly-mounted docs DOM across workspaces (orphaning it until that
            // workspace is revisited). A floating terminal (findKeyInLayout null)
            // or one the user navigated away from mid-open leaves the docs
            // floating, as before.
            const docsWin = windows.get(aid);
            const termLoc = findKeyInLayout(termId);
            if (docsWin && termLoc && termLoc.wsIndex === getLayout().activeWs) {
                placeWindowTiled(docsWin);        // get the docs into the layout first
                tabWindowIntoTile(aid, termId);   // relocate it as a tab beside the term
            }
        }

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
            // window AND its taskbar chip + synthetic session. Issue #11: closing
            // retains ONLY a non-empty sticky note; every other kind is discarded.
            // saveAppWindow above refreshed the record's content, so the trim
            // sees the latest keystrokes. A retained sticky note is stored with
            // its content trimmed and open:false (it reopens from the launch
            // menu's "Closed notes" list). An empty sticky note, a text editor,
            // a file manager, or any other kind is deleted outright — editors
            // back their real content with server files (the × dirty-save prompt
            // offers to write them first), and a kept-but-hidden record would be
            // unreachable dead storage anyway. destroyAppWindow remains the
            // explicit per-doc discard for a live note.
            if (isApp) {
                const rec = appStore[id];
                if (rec) {
                    const content = String(rec.content == null ? '' : rec.content).trim();
                    if (rec.appKind === 'sticky-note' && content) {
                        rec.content = content;
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

