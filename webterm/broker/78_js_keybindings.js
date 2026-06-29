        // ---- keybindings (task 4) -----------------------------------------
        // A small action registry + a capture-phase dispatcher. Each binding
        // is a canonical combo string (see comboFromEvent); the LOCAL host's
        // settings.keybindings are the active map (read live via getSettings)
        // so a remote tab edits ITS broker's bindings without hijacking this
        // browser's keys.

        // The first live (non-disposed, non-minimized) window key in a tiled
        // column, or null — used so focusing a column hands focus to a window.
        function firstLiveKeyInColumn(col) {
            if (!col || !Array.isArray(col.rows)) return null;
            const isLive = k => {
                const w = windows.get(k);
                return w && !w.disposed && !w.minimized;
            };
            for (const row of col.rows) {
                // Prefer a tabbed tile's ACTIVE (visible) tab, so focusing a
                // column never switches the active tab out from under the user.
                if (row.mode === 'tabbed' && isLive(row.activeTab)) return row.activeTab;
                // A split row prefers each CELL's visible rep (active tab if live,
                // else its first live key) so focusing a column never reveals a
                // hidden nested tab (codex #4).
                if (row.mode === 'split' && Array.isArray(row.cells)) {
                    for (const cell of row.cells) {
                        const rep = cellRepKey(cell, isLive);   // active-if-live, else keys[0]
                        if (isLive(rep)) return rep;
                        const lk = (cell.keys || []).find(isLive);
                        if (lk) return lk;
                    }
                    continue;
                }
                const k = rowKeys(row).find(isLive);
                if (k) return k;
            }
            return null;
        }
        // Move the focused column by dir (∓1), clamped, scroll it into view and
        // bring its first live window to the front. Tiling-only (no-op when
        // floating, where there are no columns to walk).
        function focusColumnBy(dir) {
            if (!isTilingMode()) return;
            const ws = activeWorkspace();
            if (!ws.columns.length) return;
            const j = Math.max(0, Math.min(ws.columns.length - 1,
                ws.focusedCol + dir));
            if (j === ws.focusedCol && firstLiveKeyInColumn(ws.columns[j])
                === null) return;
            ws.focusedCol = j;
            savePrefs();
            scrollColumnIntoView(j, true);
            const k = firstLiveKeyInColumn(ws.columns[j]);
            if (k) bringToFront(k);
        }
        // Close the front window: app docs go through the save-aware path so a
        // dirty editor still prompts; terminals close directly.
        function closeFrontWindow() {
            if (!frontId) return;
            const win = windows.get(frontId);
            if (!win) return;
            if (win.type === 'app') requestCloseAppWindow(frontId);
            else closeWindow(frontId);
        }

        const KEY_ACTIONS = [
            { id: 'focus-col-left',  label: 'Focus column left',
              run: () => focusColumnBy(-1) },
            { id: 'focus-col-right', label: 'Focus column right',
              run: () => focusColumnBy(1) },
            { id: 'move-col-left',   label: 'Move column left',
              run: () => moveColumn(activeWorkspace().focusedCol, -1) },
            { id: 'move-col-right',  label: 'Move column right',
              run: () => moveColumn(activeWorkspace().focusedCol, 1) },
            { id: 'workspace-prev',  label: 'Previous workspace',
              run: () => switchWorkspace(getLayout().activeWs - 1) },
            { id: 'workspace-next',  label: 'Next workspace',
              run: () => switchWorkspace(getLayout().activeWs + 1) },
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
            { id: 'new-terminal',    label: 'New terminal',
              run: () => launchProfile(localHost(),
                                       hostDefaultProfile(localHost())) },
            { id: 'toggle-tiling',   label: 'Toggle tiling mode',
              run: () => { if (isTilingMode()) enterFloatingMode();
                           else enterTilingMode(); } },
            { id: 'close-window',    label: 'Close focused window',
              run: () => closeFrontWindow() },
            { id: 'minimize-window', label: 'Minimize focused window',
              run: () => { if (frontId) minimizeWindow(frontId); } },
            { id: 'toggle-fullscreen', label: 'Toggle fullscreen',
              run: () => toggleFullscreen() },
            { id: 'open-control-panel', label: 'Open control panel',
              run: () => toggleControlPanelWindow() },
            // #40: unbound by default (absent from DEFAULT_KEYBINDINGS) — shows in
            // the keybindings editor as 'unset' so users can assign their own combo.
            { id: 'toggle-help',     label: 'Toggle help',
              run: () => toggleHelpWindow() },
        ];
        const KEY_ACTION_BY_ID = {};
        for (const act of KEY_ACTIONS) KEY_ACTION_BY_ID[act.id] = act;

        // Canonical combo string for a keydown: modifiers in a fixed order
        // (Ctrl, Alt, Shift, Meta) then the key. Pure modifier presses return
        // '' so a recorder ignores them; letters lower-cased, everything else
        // (digits, Arrow*, Enter, F-keys) used as-is.
        function comboFromEvent(e) {
            const k = e.key;
            if (k === 'Control' || k === 'Alt' || k === 'Shift'
                || k === 'Meta' || k === 'OS' || k === 'AltGraph') return '';
            const parts = [];
            if (e.ctrlKey) parts.push('Ctrl');
            if (e.altKey) parts.push('Alt');
            if (e.shiftKey) parts.push('Shift');
            if (e.metaKey) parts.push('Meta');
            let key = k;
            if (key.length === 1) {
                // Letters canonicalize to lowercase so Shift is the only thing
                // that distinguishes them; printable symbols pass through.
                if (/[a-zA-Z]/.test(key)) key = key.toLowerCase();
            }
            if (key === ' ') key = 'Space';
            parts.push(key);
            return parts.join('+');
        }

        // The keybinding recorder: while active, the next combo keydown is
        // captured into the open settings row instead of firing an action.
        let _kbRecording = null;   // {actionId, done(combo)} or null

        document.addEventListener('keydown', (e) => {
            // Recorder mode: capture the combo for the row being edited and
            // swallow the event (so it neither types nor triggers an action).
            if (_kbRecording) {
                const combo = comboFromEvent(e);
                if (!combo) return;          // ignore lone modifier taps
                e.preventDefault();
                e.stopPropagation();
                // Escape cancels the capture (leaves the binding unchanged).
                if (combo === 'Escape') {
                    _kbRecording = null;
                    renderKeybindings();
                    return;
                }
                // Reject a combo without a non-shift modifier — the dispatcher
                // only fires Ctrl/Alt/Meta combos, so a plain key (or Shift+key)
                // would record a binding that can never trigger. Keep recording
                // so the user can press the real combo.
                if (!(e.ctrlKey || e.altKey || e.metaKey)) return;
                const rec = _kbRecording;
                _kbRecording = null;
                try { rec.done(combo); } catch (_) {}
                return;
            }
            const combo = comboFromEvent(e);
            if (!combo) return;
            // Only honor combos carrying a non-shift modifier, so plain typing
            // (and Shift+letter) is never hijacked into the terminal.
            if (!(e.ctrlKey || e.altKey || e.metaKey)) return;
            const map = getSettings().keybindings || {};
            let actionId = null;
            for (const id of Object.keys(map)) {
                if (map[id] === combo) { actionId = id; break; }
            }
            const act = actionId && KEY_ACTION_BY_ID[actionId];
            if (!act) return;
            e.preventDefault();
            e.stopPropagation();
            try { act.run(); } catch (err) { console.warn('keybinding', actionId, err); }
        }, true);

        // Per-window right-click menu (title bar). Mode-agnostic: offers the
        // float<->tile toggle either way, plus column-width presets when tiled.
        // Set a window's per-window MCP mode (off/read/readwrite) on its own
        // broker. POSTs the BARE wire id (host-unqualified, like /session/git).
        // Optimistically updates the cached session so the menu's ✓ is correct
        // before the next poll, then refreshes.
        function setWindowMcpMode(win, mode) {
            const host = hostById(win.hostId);
            if (!host) return;
            // Persist the choice as DESIRED policy up front (durable, synced),
            // independent of the POST: the re-assert pass enforces it until the
            // broker reflects it, so a dropped POST or a failed /state PUT can
            // never leave a broker override with no recorded pin (or vice
            // versa). Add the key to _mcpAsserting so a concurrent re-assert
            // tick won't fire a competing POST for the same window.
            setMcpMode(win.id, mode);
            _mcpAsserting.add(win.id);
            const ctrl = new AbortController();
            const timer = setTimeout(() => ctrl.abort(), FETCH_TIMEOUT_MS);
            fetch(hostHttpUrl(host, '/session/mcp'), {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ id: win.sid, mode }),
                signal: ctrl.signal,
            }).then(r => r.json().catch(() => null)).then(j => {
                if (j && j.ok) {
                    const sess = sessions.get(win.id);
                    if (sess) sess.mcp = mode;
                    if (win.refreshMcpBtn) win.refreshMcpBtn();  // honest sync (#20)
                    refreshTaskbar();
                }
            }).catch(() => {}).finally(() => {
                clearTimeout(timer);
                _mcpAsserting.delete(win.id);
            });
        }

        // POST /session/kill to one host record and normalize the result. A
        // network/CORS throw becomes status 0; a 409 session_gone counts as
        // success (the kill raced the agent's ACK). Shared by terminateWindow's
        // primary attempt and its cross-host fallback (#64).
        async function killSessionOnHost(h, id, pid) {
            let res;
            try {
                res = await fetch(hostHttpUrl(h, '/session/kill'), {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ id, pid }),
                }).then(r => r.json()
                    .then(j => ({ status: r.status, json: j }))
                    .catch(() => ({ status: r.status,
                                    json: { ok: false, error: 'HTTP ' + r.status } })));
            } catch (e) {
                res = { status: 0, json: { ok: false, error: String(e) } };
            }
            const j = res.json || {};
            res.gone = res.status === 409 && j.error === 'session_gone';
            res.ok = (res.status === 200 && j.ok) || res.gone;
            return res;
        }

        // Hard-kill a terminal/agent session's shell process tree, then close
        // its window. Reusable top-level twin of the Task Manager's
        // destroySession (that one is trapped in a closure over busyOps/render
        // and Task-Manager-only state). The ARGUMENT is the session KEY
        // ('<hostId>:<windowId>'); the wire {id} is the BARE sess.id (like
        // /session/git). App docs have no pid -> a notice, not a silent no-op
        // (#64). Killing the shell tears the agent down before it can ACK, so
        // the broker may answer HTTP 409 + error:'session_gone' — that is
        // SUCCESS, same as a 200 ok:true. On success we optimistically tear down
        // the window + chip + session entry so the chip doesn't linger ~2s for
        // the next /sessions poll. A transport/auth failure (status 0/401/403)
        // against THIS URL triggers a strictly-gated fallback — retry the same
        // {id,pid} against another record that is the SAME physical broker
        // (matching brokerId) and currently lists the session; a broker answer
        // (404/409) or busy/alive (429/504) never retries. An unrecovered
        // failure logs to the console and raises a STICKY host-named error (the
        // old 4s toast vanished unread, #64). Host-aware fetch mirrors
        // setWindowMcpMode.
        async function terminateWindow(key) {
            const sess = sessions.get(key);
            if (!sess) {
                console.warn('[terminate] session not found', { key });
                showNotice('terminate: session not found');
                return;
            }
            const host = hostById(sess.hostId);
            const hostUrl = (host && host.url) ? host.url
                : (sess.hostId === 'local' || (host && host.id === 'local'))
                    ? '(local)' : (sess.hostId || '(unknown)');
            const ctx = { key, hostId: sess.hostId, url: hostUrl,
                          id: sess.id, pid: sess.pid, kind: sess.kind };
            // Console logging (#64): the single highest-leverage diagnostic for
            // "Terminate did nothing" — it was previously SILENT, so a kill
            // bound to a stale host record left no trace at all.
            console.warn('[terminate] requested', ctx);
            if (sess.kind === 'app') {
                // App docs are local-only synthetic windows with no process to
                // kill. This used to bare-return — indistinguishable from the
                // silent-failure bug; say so out loud now (#64).
                console.warn('[terminate] app window has no process', ctx);
                showNotice('cannot terminate an app window');
                return;
            }
            if (sess.pid == null) {
                console.warn('[terminate] session has no pid', ctx);
                showNotice('terminate: no pid');
                return;
            }
            if (!host) {
                console.warn('[terminate] no host record for session', ctx);
                showNotice('terminate failed: no host');
                return;
            }
            const res = await killSessionOnHost(host, sess.id, sess.pid);
            if (!res.ok) {
                const reason = (res.json || {}).error
                    || ('HTTP ' + res.status);
                console.warn('[terminate] kill failed',
                    Object.assign({ status: res.status, reason }, ctx));
                // Gated cross-host fallback (#64). ONLY when THIS URL had a
                // transport/auth failure (status 0/401/403 — host down, wrong
                // token, or CORS) AND we know its brokerId: retry the SAME
                // {id,pid} against another record that is the SAME physical
                // broker and currently lists this exact session. We never fall
                // back on a broker ANSWER (404 unknown_session / 409 — terminal)
                // or a busy/alive broker (429/504 — retry risks a double-kill),
                // and never without a matching known brokerId (an unrelated
                // broker can share an {id,pid} → wrong-machine kill).
                const transportFail = res.status === 0
                    || res.status === 401 || res.status === 403;
                let recoveredVia = null;
                if (transportFail && host.brokerId) {
                    const cands = getHosts().filter(h =>
                        h.id !== host.id
                        && h.brokerId && h.brokerId === host.brokerId
                        && pollStateFor(h.id).sessions.some(s =>
                            s.id === sess.id && s.pid === sess.pid));
                    // Prefer the local record (no network hop, always reachable).
                    cands.sort((a, b) =>
                        (a.url === '' ? -1 : 0) - (b.url === '' ? -1 : 0));
                    for (const h of cands) {
                        const via = h.url || '(local)';
                        console.warn('[terminate] retry via matching broker',
                            { via, viaHostId: h.id, brokerId: h.brokerId });
                        const r2 = await killSessionOnHost(h, sess.id, sess.pid);
                        if (r2.ok) {
                            console.warn('[terminate] fallback succeeded', { via });
                            recoveredVia = via;
                            break;
                        }
                        console.warn('[terminate] fallback attempt failed',
                            { via, status: r2.status,
                              reason: (r2.json || {}).error });
                    }
                }
                if (!recoveredVia) {
                    // Sticky, host-named error (#64): the old 4s toast vanished
                    // unread, so a failed kill looked silent. Point an unknown-id
                    // transport failure at the Troubleshooting remover.
                    const hint = (transportFail && !host.brokerId)
                        ? ' — open Settings ▸ Browser ▸ Troubleshooting to '
                          + 'remove the stale/duplicate host'
                        : '';
                    showNotice('terminate failed on ' + hostUrl + ' [' + key
                        + ']: ' + reason + hint,
                        { sticky: true, type: 'error' });
                    return;
                }
            }
            // Optimistic cleanup (copies destroyAppWindow's chip-node removal):
            // drop the window, the synthetic session entry, and the chip now so
            // nothing lingers until the next poll. Guarded by windows.has so a
            // parked (process-alive, window-closed) session still terminates.
            // Also sweep the DUPLICATE chips for the same process reached via
            // other alias records of this broker (#64) — same brokerId + same
            // {id,pid} — so the user isn't left clicking a twin that would now
            // 404 unknown_session. Same-broker only: on one broker the id keys
            // a unique session, so this can never touch another machine's.
            const victims = new Set([key]);
            if (host.brokerId) {
                for (const [k, s] of sessions) {
                    if (k === key) continue;
                    if (s.id !== sess.id || s.pid !== sess.pid) continue;
                    const sh = hostById(s.hostId);
                    if (sh && sh.brokerId === host.brokerId) victims.add(k);
                }
            }
            for (const k of victims) {
                if (windows.has(k)) closeWindow(k);
                sessions.delete(k);
                const el = document.querySelector(
                    '.taskbar-item[data-session-id="' + cssEscape(k) + '"]');
                if (el) el.remove();
            }
            updateTaskbarActive();
            showNotice('terminated ' + (sess.title || ('#' + sess.sid)));
        }

        function buildWindowMenu(win, x, y) {
            const items = [];
            if (win.tiled) {
                const loc = findKeyInLayout(win.id);
                const cur = loc ? loc.col.widthPreset : DEFAULT_NEW_PRESET;
                const ncols = loc ? loc.ws.columns.length : 0;
                const row = loc ? loc.row : null;
                // Alone = the only key in the only row of its column.
                const alone = loc
                    ? (loc.col.rows.length === 1 && rowKeys(row).length === 1) : true;
                items.push({ label: 'Column width', enabled: false });
                for (const p of WIDTH_PRESETS) {
                    items.push({
                        label: (cur === p ? '✓ ' : ' ') + PRESET_LABELS[p],
                        enabled: true,
                        action: () => setColumnPreset(win, p),
                    });
                }
                items.push({ sep: true });
                // Vertical stacking: consume into a neighbor column / expel.
                items.push({ label: 'Stack into left column',
                             enabled: !!loc && loc.colIndex > 0,
                             action: () => consumeIntoAdjacentColumn(win, -1) });
                items.push({ label: 'Stack into right column',
                             enabled: !!loc && loc.colIndex < ncols - 1,
                             action: () => consumeIntoAdjacentColumn(win, 1) });
                items.push({ label: 'Move to own column',
                             enabled: !!loc && !alone,
                             action: () => expelToNewColumn(win) });
                // Explicit new-column action (restores the capability the
                // window-interior left/right drag now spends on an in-band split).
                // Spawns a fresh column to the right; a no-op when already alone.
                items.push({ label: 'Move to new column',
                             enabled: !!loc,
                             action: () => dragDropNewColumn(win.id, loc.colIndex + 1) });
                // Per-tile tab groups: tab into the nearest live tile of a
                // neighbor column, tab this window's tile (seed/extend a tab
                // group), or untab the current tile back to single-window rows.
                const leftCol = (loc && loc.colIndex > 0)
                    ? loc.ws.columns[loc.colIndex - 1] : null;
                const rightCol = (loc && loc.colIndex < ncols - 1)
                    ? loc.ws.columns[loc.colIndex + 1] : null;
                items.push({ label: 'Tab into left column',
                             enabled: !!leftCol && !!firstLiveKeyInColumn(leftCol),
                             action: () => tabWindowIntoTile(win.id,
                                 firstLiveKeyInColumn(leftCol)) });
                items.push({ label: 'Tab into right column',
                             enabled: !!rightCol && !!firstLiveKeyInColumn(rightCol),
                             action: () => tabWindowIntoTile(win.id,
                                 firstLiveKeyInColumn(rightCol)) });
                if (loc && row && row.mode !== 'tabbed' && row.mode !== 'split') {
                    // Self-tab this window's tile: seeds a 1-tab strip for a lone
                    // window (or tabs it in place without moving).
                    items.push({ label: 'Tab this window', enabled: true,
                        action: () => tabWindowIntoTile(win.id, win.id) });
                }
                if (loc && row && row.mode === 'tabbed') {
                    items.push({ label: 'Untab tile (split to rows)',
                                 enabled: true,
                                 action: () => untabTile(loc.col, loc.rowIndex) });
                }
                if (loc && row && row.mode === 'split') {
                    // (F-NESTSPLIT) This window is inside a GROUP cell -> drop the
                    // group's tabs as sibling LEAF cells in the same band (the menu
                    // twin of the nested ⊟). Only when the cell is actually a group.
                    if (loc.cell && Array.isArray(loc.cell.keys)
                        && loc.cell.keys.length >= 2) {
                        items.push({ label: 'Untab cell (side by side)', enabled: true,
                            action: () => nestedUntabCell(loc.row, loc.cell) });
                    }
                    // Explode the whole split row into stacked rows (a leaf -> a
                    // single row, a group -> a tabbed row).
                    items.push({ label: 'Un-split row (split to rows)',
                                 enabled: true,
                                 action: () => unsplitRow(loc.col, loc.rowIndex) });
                }
                items.push({ sep: true });
                items.push({ label: 'Move column left',
                             enabled: !!loc && loc.colIndex > 0,
                             action: () => moveColumn(loc.colIndex, -1) });
                items.push({ label: 'Move column right',
                             enabled: !!loc && loc.colIndex < ncols - 1,
                             action: () => moveColumn(loc.colIndex, 1) });
                items.push({ sep: true });
                // Send to another vertical workspace.
                const L = getLayout();
                const here = loc ? loc.wsIndex : L.activeWs;
                L.workspaces.forEach((ws, wi) => {
                    if (wi === here) return;
                    items.push({ label: 'Send to '
                                     + (ws.name ? ws.name : 'workspace ' + (wi + 1)),
                                 enabled: true,
                                 action: () => sendWindowToWorkspace(win, wi) });
                });
                items.push({ label: 'Send to new workspace', enabled: true,
                             action: () => {
                                 const LL = getLayout();
                                 LL.workspaces.push(newWorkspace());
                                 savePrefs();
                                 sendWindowToWorkspace(win, LL.workspaces.length - 1);
                             } });
                items.push({ sep: true });
                items.push({ label: 'Float this window', enabled: true,
                             action: () => detachToFloat(win) });
            } else {
                items.push({ label: 'Tile this window', enabled: true,
                             action: () => attachToStrip(win) });
                items.push({
                    label: win.locked
                        ? 'Unlock (scroll with strip)'
                        : 'Lock to screen (pin)',
                    enabled: true,
                    action: () => toggleWindowLock(win),
                });
                // Workspace membership (task 8): locked to this ws, or shown on
                // all. windowWsId null = all workspaces.
                items.push({
                    label: (windowWsId(win) === null)
                        ? '✓ On all workspaces'
                        : 'Show on all workspaces',
                    enabled: true,
                    action: () => setWindowAllWorkspaces(win, windowWsId(win) !== null),
                });
            }
            // MCP access (per-window mode): only for terminal sessions — app
            // docs (notes/editor/etc.) are not server sessions. The ✓ prefers
            // the saved pin (the DESIRED mode) so it is correct on first paint,
            // before the first /sessions poll; with no pin it falls back to the
            // effective mode carried on the 2s poll (= the live broker default).
            if (win.type !== 'app') {
                const sess = sessions.get(win.id);
                const curMode = getMcpMode(win.id)
                    || ((sess && sess.mcp) ? sess.mcp : 'off');
                items.push({ sep: true });
                items.push({ label: 'MCP access', enabled: false });
                for (const [val, lab] of [['off', 'Off'], ['read', 'Read'],
                                          ['readwrite', 'Read-write']]) {
                    items.push({
                        label: (curMode === val ? '✓ ' : '   ') + lab,
                        enabled: true,
                        action: () => setWindowMcpMode(win, val),
                    });
                }
            }
            items.push({ sep: true });
            items.push({ label: win.minimized ? 'Restore' : 'Minimize',
                         enabled: true,
                         action: () => win.minimized
                            ? restoreWindow(win.id) : minimizeWindow(win.id) });
            items.push({ label: 'Close', enabled: true,
                         action: () => win.type === 'app'
                            ? requestCloseAppWindow(win.id)
                            : closeWindow(win.id) });
            // Terminate = hard kill (close is soft: the shell keeps running).
            // Terminals/agents only — app docs (type 'app') have no pid. For a
            // terminal/agent window win.id IS the session key terminateWindow
            // expects.
            if (win.type !== 'app') {
                items.push({ label: 'Terminate', enabled: true, action: () => {
                    if (confirm('Terminate this session? '
                            + 'The shell process tree will be killed.'))
                        terminateWindow(win.id);
                }});
            }
            // Persisted docs only: × Close keeps them, Delete is the one path that
            // discards them. Ephemeral windows (file manager, task manager, control
            // panel, help) have nothing saved — Close already fully tears them down
            // — so a "Delete" there would be a misleading no-op.
            if (win.type === 'app'
                && win.appKind !== 'task-manager'
                && win.appKind !== 'control-panel'
                && win.appKind !== 'help'
                && win.appKind !== 'file-manager') {
                const what = win.appKind === 'text-editor' ? 'file' : 'note';
                items.push({ sep: true });
                items.push({
                    label: 'Delete ' + what,
                    enabled: true,
                    action: () => {
                        if (confirm('Delete this ' + what + ' permanently? '
                                + 'Its saved content is removed.'))
                            destroyAppWindow(win.id);
                    },
                });
            }
            renderMenu(items, x, y);
        }

        function buildCtxMenu(x, y) {
            const tiling = isTilingMode();
            const floats = floatingWindowsOrdered();
            const hasFloats = floats.length > 0;
            const locked = isSizeLocked();
            const items = [];
            // The floating<->tiling mode switch now lives in the Control Panel, not
            // this menu. What remains is mode-appropriate arrangement.
            if (!tiling) {
                // Floating one-shot arrangements (floating windows only).
                items.push(
                    { label: 'Cascade', enabled: hasFloats, action: doCascade },
                    { label: 'Tile Horizontally', enabled: hasFloats, action: doTileHorizontal },
                    { label: 'Tile Vertically', enabled: hasFloats, action: doTileVertical },
                    { label: 'Tile H + V', enabled: hasFloats, action: doTileGrid },
                    { sep: true },
                    { label: locked ? 'Unlock Size' : 'Lock Size',
                      enabled: true, action: () => setSizeLocked(!locked) },
                    { sep: true },
                    { label: 'Minimize All Windows',
                      enabled: hasFloats && floats.some(w => !w.minimized),
                      action: doMinimizeAll });
            } else {
                // Tiling mode: vertical-workspace switcher + bulk un-tile. The
                // per-column controls live on each window's title-bar menu.
                const L = getLayout();
                L.workspaces.forEach((ws, wi) => {
                    items.push({
                        label: (wi === L.activeWs ? '✓ ' : '   ')
                            + (ws.name ? ws.name : 'Workspace ' + (wi + 1))
                            + ' (' + ws.columns.length + ')',
                        enabled: true,
                        action: () => switchWorkspace(wi),
                    });
                });
                items.push({ label: '   New workspace', enabled: true,
                             action: addWorkspace });
            }
            items.push({ sep: true });
            items.push({ label: '🎛 Control panel', enabled: true, action: launchControlPanel });
            if (lastLayoutSnapshot && !tiling) {
                items.push({ sep: true });
                items.push({
                    label: 'Undo ' + lastLayoutSnapshot.label,
                    enabled: true,
                    action: doUndoLayout,
                });
            }
            renderMenu(items, x, y);
        }

        // Per-chip taskbar menu: a fuller window-control set than the title-bar
        // menu, and one that must work even when the window is closed/parked.
        // `key` is the chip's dataset.sessionId (the session KEY). `win` may be
        // undefined (parked session whose process is still alive), so every
        // window action is gated on windows.has(key).
        function buildTaskbarItemMenu(key, x, y) {
            const win = windows.get(key);
            const sess = sessions.get(key);
            // App-doc classification from BOTH sides: a live app window always
            // carries a kind:'app' session, but cross-check win.type so Close
            // never falls through to the terminal path if the session entry is
            // momentarily absent.
            const isApp = (win && win.type === 'app')
                || (sess && sess.kind === 'app');
            const items = [];
            // Focus: always available. Reopen a closed window (app docs via
            // their store factory, terminals from /sessions), switch to its
            // workspace first, restore if minimized, else just raise it — the
            // onTaskbarClick reopen branch, minus the off-ws float reveal.
            items.push({ label: 'Focus', enabled: true, action: () => {
                const targetWs = workspaceIndexForKey(key);
                if (targetWs !== null && targetWs !== getLayout().activeWs)
                    switchWorkspace(targetWs);
                const w = windows.get(key);
                if (!w) {
                    if (appStore[key]) openAppWindow(appStore[key]);
                    else openWindow(key, sessions.get(key));
                } else if (w.minimized) {
                    restoreWindow(key);
                } else {
                    bringToFront(key);
                }
            }});
            items.push({ label: (win && win.minimized) ? 'Restore' : 'Minimize',
                         enabled: windows.has(key),
                         action: () => (win && win.minimized)
                            ? restoreWindow(key) : minimizeWindow(key) });
            items.push({ sep: true });
            items.push({ label: 'Close', enabled: windows.has(key),
                         action: () => isApp
                            ? requestCloseAppWindow(key) : closeWindow(key) });
            // Terminate: terminals/agents only (app docs have no pid). Enabled
            // whenever the session exists — works on a parked session too.
            if (!isApp) {
                items.push({ label: 'Terminate', enabled: !!sess, action: () => {
                    if (confirm('Terminate this session? '
                            + 'The shell process tree will be killed.'))
                        terminateWindow(key);
                }});
            }
            renderMenu(items, x, y);
        }
        function hideCtxMenu() {
            ctxMenu.classList.remove('open');
        }
        const desktopEl = document.getElementById('desktop');
        desktopEl.addEventListener('contextmenu', (e) => {
            // Fire on the desktop background OR the strip / strip-col gaps
            // (tiling mode), but never on a window — its title-bar menu and the
            // terminal paste handler own those.
            if (!(e.target instanceof Element)
                || e.target.closest('.term-window')) return;
            e.preventDefault();
            buildCtxMenu(e.clientX, e.clientY);
        });
        const taskbarEl = document.getElementById('taskbar');
        taskbarEl.addEventListener('contextmenu', (e) => {
            // Fire on the taskbar background or any non-interactive child
            // (e.g. taskbar items). Skip if we're on a real button/control
            // so its own handling (the + button's profile menu) can run.
            const t = e.target;
            if (t.closest('button')) return;
            // A chip gets its own per-window control menu; the taskbar
            // background still falls through to the global workspace menu.
            const item = t.closest('.taskbar-item');
            if (item && item.dataset.sessionId) {
                e.preventDefault();
                buildTaskbarItemMenu(item.dataset.sessionId, e.clientX, e.clientY);
                return;
            }
            e.preventDefault();
            buildCtxMenu(e.clientX, e.clientY);
        });
        document.addEventListener('mousedown', (e) => {
            if (!ctxMenu.classList.contains('open')) return;
            if (ctxMenu.contains(e.target)) return;
            hideCtxMenu();
        }, true);
        document.addEventListener('keydown', (e) => {
            if (e.key === 'Escape') {
                hideCtxMenu();
                closeSettings();
                // The Help window handles its own Escape (scoped to its body):
                // clear the query if any, else close — see buildHelpBody.
            }
        });
        window.addEventListener('blur', hideCtxMenu);
        window.addEventListener('resize', hideCtxMenu);
        // Browser resize: recompute preset column widths, then the relayout's
        // double-RAF resizes the visible tiled terminals once layout settles.
        window.addEventListener('resize', requestRelayout);
        // Strip clientWidth changed → re-measure the workspace scrollbar. (The
        // relayout above also re-measures, but resize may not always relayout
        // columns; this keeps the bar's metrics correct on a bare viewport size
        // change.)
        window.addEventListener('resize', updateStripScrollbar);
        // Strip scroll: drag unlocked floating windows along with the columns.
        (function () {
            const strip = document.getElementById('strip');
            if (strip) strip.addEventListener('scroll', onStripScroll, { passive: true });
        })();

