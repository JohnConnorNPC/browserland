        // ---- #59: the Control Panel as a moveable floating window -----------
        // Was a centered modal (#settings-overlay / #settings-modal); now a normal
        // floating app window (appKind 'control-panel'), ephemeral like Help (it
        // edits global / per-host settings that persist via /state, so the window
        // has nothing of its own to save). To reuse every settings renderer and the
        // careful local-vs-remote routing UNCHANGED, the window BORROWS the existing
        // #settings-modal node (moving it out of the hidden overlay into the window
        // body) and hands it back on close. Single-instance: there is only one
        // #settings-modal to host.
        function findControlPanelWindow() {
            for (const w of windows.values()) {
                if (w && !w.disposed && w.appKind === 'control-panel') return w;
            }
            return null;
        }
        function isControlPanelOpen() { return !!findControlPanelWindow(); }
        function closeControlPanelWindow() {
            const ex = findControlPanelWindow();
            if (ex) closeWindow(ex.id);   // teardown returns #settings-modal to the overlay
        }
        function controlPanelDefaultGeom() {
            const d = document.getElementById('desktop').getBoundingClientRect();
            const width = Math.min(560, Math.max(340, Math.round(d.width - 48)));
            const height = Math.min(620, Math.max(360, Math.round(d.height - 64)));
            const left = Math.max(12, Math.round((d.width - width) / 2));
            const top = Math.max(12, Math.round((d.height - height) / 2 - 20));
            return { left, top, width, height };
        }
        function openControlPanelWindow(appData) {
            appData = appData || {};
            const id = String(appData.id || newAppId('cp'));
            const existing = windows.get(id);
            if (existing) {
                if (existing.minimized) restoreWindow(id); else bringToFront(id);
                return existing;
            }
            // Single-instance: focus the live Control Panel rather than spawn a
            // second (there is only one #settings-modal node to host).
            const live = findControlPanelWindow();
            if (live) {
                if (live.minimized) restoreWindow(live.id);
                bringToFront(live.id);
                return live;
            }
            const modal = document.getElementById('settings-modal');
            if (!modal) return null;   // markup missing — nothing to host

            const title = appData.title || 'Control Panel';
            const geom = clampGeom(appData.geom || controlPanelDefaultGeom());
            const color = normalizeHex(appData.color || defaultColor(id));

            const dom = document.createElement('div');
            dom.className = 'term-window app-window app-control-panel';
            dom.dataset.sessionId = id;
            dom.style.left = geom.left + 'px';
            dom.style.top = geom.top + 'px';
            dom.style.width = (geom.width - 4) + 'px';
            dom.style.height = (geom.height - 4) + 'px';
            dom.style.setProperty('--accent', color);
            dom.classList.toggle('dark-accent', isDarkAccent(color));

            const titleBar = document.createElement('div');
            titleBar.className = 'title-bar';
            const idBadge = document.createElement('span');
            idBadge.className = 'ti-id-badge';
            idBadge.textContent = '#cp';
            const titleText = document.createElement('span');
            titleText.className = 'title-text';
            titleText.textContent = title;
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

            const win = {
                id, sid: 'control-panel', hostId: 'app',
                type: 'app', appKind: 'control-panel',
                dom, body: null, titleText,
                term: null, fitAddon: null,
                ws: null, wsOpen: false, termReady: false,
                minimized: false, disposed: false,
                geom, name: title, color,
                resizeTimer: null, lastSentDims: null,
                cleanups: [],
                tiled: false,
                floatGeom: appData.floatGeom
                    ? Object.assign({}, appData.floatGeom) : null,
                locked: false,
                dirty: false,
            };

            // Borrow the settings panel: move #settings-modal out of the hidden
            // overlay into the window body and neutralize its modal-box chrome.
            // The cleanup hands it back BEFORE closeWindow removes the window DOM,
            // so getElementById('settings-modal') keeps working after close.
            modal.classList.add('cp-windowed');
            dom.appendChild(modal);
            win.body = modal;
            win.cleanups.push(() => {
                flushSettingsEdits();
                modal.classList.remove('cp-windowed');
                const overlay = document.getElementById('settings-overlay');
                if (overlay) overlay.appendChild(modal);
                if (prefs[id]) { delete prefs[id]; savePrefs(); }
            });

            for (const dir of ['n','s','e','w','nw','ne','sw','se']) {
                const hnd = document.createElement('div');
                hnd.className = 'rh rh-' + dir;
                hnd.dataset.dir = dir;
                dom.appendChild(hnd);
            }

            document.getElementById('desktop').appendChild(dom);
            document.getElementById('desktop').classList.remove('empty');
            windows.set(id, win);

            const stopProp = (e) => e.stopPropagation();
            const onMouseDown = () => bringToFront(id);
            dom.addEventListener('mousedown', onMouseDown);
            win.cleanups.push(() => dom.removeEventListener('mousedown', onMouseDown));

            const onMinClick = (e) => { e.stopPropagation(); minimizeWindow(id); };
            const onCloseClick = (e) => { e.stopPropagation(); closeWindow(id); };
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
            for (const handle of dom.querySelectorAll('.rh')) {
                wireResize(win, handle, handle.dataset.dir);
            }

            const appSess = { key: id, sid: 'control-panel', id, title, stale: false,
                              kind: 'app', hostId: 'app' };
            sessions.set(id, appSess);
            const itemsHost = document.getElementById('taskbar-items');
            if (!itemsHost.querySelector(
                    '.taskbar-item[data-session-id="' + cssEscape(id) + '"]')) {
                itemsHost.appendChild(buildTaskbarItem(appSess));
            }
            updateTaskbarColor(id);
            updateTaskbarLabel(id);
            const emptyMsg = document.getElementById('taskbar-empty');
            if (emptyMsg) emptyMsg.remove();

            // Render the form into the borrowed panel — always land on the local
            // tab (its settings are the live ones), exactly as the modal
            // openSettings() did. renderSettings owns local-vs-remote routing, so
            // appearance edits still target THIS browser even after visiting a
            // remote host tab.
            currentSettingsTab = 'local';
            settingsOpenHostId = null;
            settingsTarget = makeLocalTarget();
            renderSettingsTabs();
            showSettingsPane('local');
            renderSettings();

            if (findKeyInLayout(id)) placeWindowTiled(win);
            else bringToFront(id);
            return win;
        }
        function toggleControlPanelWindow() {
            const ex = findControlPanelWindow();
            if (ex && !ex.minimized && frontId === ex.id) { closeWindow(ex.id); return; }
            openControlPanelWindow({});
        }
        function focusOrOpenHelp() {
            markHelpSeen();   // any deliberate open is the strongest "seen" signal
            const ex = findHelpWindow();
            if (ex) {
                if (ex.minimized) restoreWindow(ex.id);
                bringToFront(ex.id);
                setTimeout(() => { try { ex._help.searchEl.focus(); } catch (_) {} }, 0);
                return ex;
            }
            return openHelpWindow({});
        }
        function launchHelp() { return focusOrOpenHelp(); }
        // The hotkey toggles: if Help is the focused, non-minimized front window,
        // close it; otherwise open/focus it.
        function toggleHelpWindow() {
            const ex = findHelpWindow();
            if (ex && !ex.minimized && frontId === ex.id) { closeWindow(ex.id); return; }
            focusOrOpenHelp();
        }
        (function wireHelpChip() {
            const chip = document.getElementById('help-chip');
            if (!chip) return;
            chip.addEventListener('click', focusOrOpenHelp);
            chip.addEventListener('keydown', (e) => {
                if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); focusOrOpenHelp(); }
            });
        })();
        // First-run nudge: once, point new users at the "?" chip (only if it's
        // visible). Deferred until the initial /state has been ADOPTED so it
        // reflects the synced helpHintSeen/showHelpButton rather than pre-pull
        // defaults (codex); capped (~10s) so an offline/auth-blocked broker
        // still nudges from local prefs instead of hanging forever.
        let _helpHintTries = 0;
        function maybeShowHelpHint() {
            try {
                if (!_stateReady && _helpHintTries++ < 20) {
                    setTimeout(maybeShowHelpHint, 500);
                    return;
                }
                const s = getSettings();
                if (s.helpHintSeen || !s.showHelpButton) return;
                showNotice('Tip: click the "?" on the taskbar for the interface guide.', 7000);
                s.helpHintSeen = true;
                savePrefs();
            } catch (_) {}
        }
        setTimeout(maybeShowHelpHint, 1800);

        // Build the tab bar: a "Browser" tab (connection list) + one tab per
        // configured host. Active tab highlighted.
        function renderSettingsTabs() {
            settingsTabsEl.textContent = '';
            const addTab = (id, label, title) => {
                const b = document.createElement('button');
                b.type = 'button';
                b.className = 'set-tab' + (id === currentSettingsTab ? ' active' : '');
                b.textContent = label;
                if (title) b.title = title;
                b.addEventListener('click', () => selectSettingsTab(id));
                settingsTabsEl.appendChild(b);
            };
            for (const host of getHosts()) {
                addTab(host.id,
                    host.id === 'local' ? 'this broker' : host.label,
                    host.id === 'local' ? 'this broker' : host.url);
            }
            addTab('browser', 'Browser', 'where to connect (browser-local)');
        }
        // Show the host-form pane or the browser (hosts) pane for a tab id.
        function showSettingsPane(tabId) {
            const browser = (tabId === 'browser');
            setPaneBrowser.classList.toggle('active', browser);
            setPaneHost.classList.toggle('active', !browser);
        }
        // Browser-global sections (theme/pattern/clock/start-button/restore/
        // taskbar-filter) edit THIS browser, not a host — show them only on the
        // local tab. Toggled SYNCHRONOUSLY on tab select (not just in
        // renderSettings) so they can't linger visible-and-editing-local while a
        // remote /state load is in flight or has failed (#17).
        function applyBrowserGlobalVisibility(show) {
            for (const el of document.querySelectorAll('.set-browser-global')) {
                el.style.display = show ? '' : 'none';
            }
        }
        // Switch tabs. Browser + local are synchronous; a remote host awaits a
        // fresh /state GET (with a "loading…" placeholder) before its form is
        // populated and editable.
        async function selectSettingsTab(tabId) {
            currentSettingsTab = tabId;
            _kbRecording = null;
            renderSettingsTabs();
            showSettingsPane(tabId);
            if (tabId === 'browser') {
                settingsOpenHostId = null;
                resetHostForm();
                renderHostsList();
                return;
            }
            if (tabId === 'local') {
                settingsOpenHostId = null;
                settingsTarget = makeLocalTarget();
                renderSettings();
                return;
            }
            // Remote host: fetch its state, then point the form at the cache.
            // Null the target while loading so a stray change handler during the
            // await early-returns instead of writing to the previous tab's blob.
            settingsTarget = null;
            settingsOpenHostId = tabId;     // pause prefetch for this host
            applyBrowserGlobalVisibility(false);  // hide local-only controls now
            setHostLoadingEl.style.display = '';
            setPaneHost.classList.add('loading');
            const entry = await fetchHostState(tabId);
            // The user may have switched tabs again while awaiting.
            if (currentSettingsTab !== tabId) return;
            setHostLoadingEl.style.display = 'none';
            setPaneHost.classList.remove('loading');
            if (!entry) {
                settingsTarget = null;
                setHostLoadingEl.style.display = '';
                setHostLoadingEl.textContent =
                    'could not load this broker’s settings';
                return;
            }
            setHostLoadingEl.textContent = 'loading settings…';
            settingsTarget = makeRemoteTarget(tabId, entry);
            renderSettings();
        }

        // Populate the host-form fields from settingsTarget.s. The Tiling
        // checkbox reflects the LOCAL live mode for the local tab, and the
        // remote's stored layout.mode for a remote tab.
        function renderSettings() {
            if (currentSettingsTab === 'browser') {
                resetHostForm();
                renderHostsList();
                return;
            }
            const t = settingsTarget;
            if (!t) return;
            const s = t.s;
            applyBrowserGlobalVisibility(t.isLocal);
            setColsEl.value = s.size ? s.size.cols : '';
            setRowsEl.value = s.size ? s.size.rows : '';
            setShowId.checked = !!s.show.id;
            setShowPid.checked = !!s.show.pid;
            setShowHost.checked = !!s.show.host;
            setTiling.checked = t.isLocal
                ? isTilingMode()
                : (remoteTilingMode(t) === 'tiling');
            setStripScrollbar.checked = !!s.stripScrollbar;
            // #38: per-host dwell delay. s is already normalized (0 or a clamped
            // number), so reflect it verbatim; 0 shows as "0" (= disabled).
            setSnapHold.value = (typeof s.snapHoldMs === 'number') ? s.snapHoldMs : 3000;

            // Restore-on-refresh governs THIS browser's startup (not a remote
            // host), so it always reflects the LOCAL setting on every host tab.
            setRestore.checked = !!getSettings().restoreOnRefresh;
            setHideOtherWs.checked = !!getSettings().hideTaskbarOtherWs;

            // Appearance is browser-global — reflect the LIVE local settings
            // (these controls are hidden on remote tabs, shown only on local).
            // #75/#76: the color-scheme radio and the background-pattern select
            // reflect themselves through their mods (renderModSettingsToggles
            // below); core only reflects the terminal font.
            const ls = getSettings();
            setTermFontEl.value = ls.termFont || '';   // #18 (browser-global)
            renderModSettingsToggles(t.isLocal);   // #71: reflect mod toggles (clock, …)
            setHelpButtonEl.checked = !!ls.showHelpButton;   // #40 (browser-global)
            setStartLabelEl.value = (ls.startLabel === '+' ? '' : ls.startLabel);
            // Default start path is PER-HOST (#17): read the target host's own
            // settings (local = live getSettings(); remote = its cached blob),
            // resolving any legacy per-OS map against THAT host's OS.
            const tgtHost = hostById(t.hostId);
            setStartPathEl.value = startPathForDisplay(s, tgtHost);
            // A legacy per-OS startPaths blob (#2 migration artifact, which an
            // upgraded REMOTE broker can also carry) can't resolve to this host's
            // OS value until that host's /profiles lands — refresh the box once it
            // does (unless the user has since focused it). Guards a tab switch /
            // target swap mid-await, like renderDefaultProfile below.
            if (!(s.startPath || '').trim() && s.startPaths
                    && tgtHost && !profilesCache.get(tgtHost.id)) {
                const wantTab = currentSettingsTab;
                const tgt = t;
                fetchProfiles(tgtHost).then(() => {
                    if (currentSettingsTab === wantTab && settingsTarget === tgt
                            && document.activeElement !== setStartPathEl) {
                        setStartPathEl.value = startPathForDisplay(tgt.s, tgtHost);
                    }
                }).catch(() => {});
            }
            renderDefaultProfile();
            renderMcpConfig();
            renderKeybindings();
        }
        // Task 8: populate the "Default terminal profile" <select> from this
        // host's /profiles (cached) and reflect the stored defaultProfile.
        // Always shows the blank "(broker default)" option; a stored value that
        // is not (yet) in the fetched list is preserved as its own option so
        // switching tabs never silently drops it. Re-run after the async
        // /profiles fetch lands.
        function renderDefaultProfile() {
            const t = settingsTarget;
            if (!t || !setDefaultProfile) return;
            const cur = (t.s.defaultProfile || '');
            const host = hostById(t.hostId);
            const d = host ? profilesCache.get(host.id) : null;
            const names = (d && Array.isArray(d.profiles)) ? d.profiles : [];
            setDefaultProfile.innerHTML = '';
            const blank = document.createElement('option');
            blank.value = '';
            blank.textContent = '(broker default)';
            setDefaultProfile.appendChild(blank);
            const seen = {};
            for (const name of names) {
                if (seen[name]) continue;
                seen[name] = true;
                const o = document.createElement('option');
                o.value = name;
                o.textContent = (d && name === d.default)
                    ? name + ' (broker default)' : name;
                setDefaultProfile.appendChild(o);
            }
            if (cur && !seen[cur]) {
                const o = document.createElement('option');
                o.value = cur;
                o.textContent = cur;
                setDefaultProfile.appendChild(o);
            }
            setDefaultProfile.value = cur;
            // Fetch the profile list if we haven't yet, then re-render this one
            // control so the names appear. Guard against a tab switch mid-await.
            if (host && !profilesCache.has(host.id)) {
                const wantTab = currentSettingsTab;
                fetchProfiles(host).then(() => {
                    if (currentSettingsTab === wantTab && settingsTarget === t) {
                        renderDefaultProfile();
                    }
                }).catch(() => {});
            }
        }
        // ---- MCP access section --------------------------------------------
        // The connect URL an external MCP server dials (the host's base + /mcp).
        // Local host has no stored url -> use this page's origin.
        function mcpConnectUrl(host) {
            const base = (host && host.url) ? host.url : window.location.origin;
            return base.replace(/\/+$/, '') + '/mcp';
        }
        function fetchMcpConfig(host) {
            return fetch(hostHttpUrl(host, '/mcp/config'))
                .then(r => (r.ok ? r.json() : null))
                .then(j => { if (j && j.ok) mcpConfigCache.set(host.id, j); })
                .catch(() => {});
        }
        // Populate the MCP section from the settings-target host's cached
        // /mcp/config (fetching it once if needed, like renderDefaultProfile).
        function renderMcpConfig() {
            const t = settingsTarget;
            if (!t || !setMcpEnabled) return;
            const host = hostById(t.hostId);
            setMcpUrlEl.textContent = host ? mcpConnectUrl(host) : '—';
            const cfg = host ? mcpConfigCache.get(host.id) : null;
            if (!cfg) {
                // Not loaded yet: show neutral defaults, fetch, then re-render.
                setMcpEnabled.checked = false;
                setMcpToken.value = '';
                setMcpDefaultMode.value = 'off';
                setMcpAllowLaunch.checked = false;
                if (host && !mcpConfigFetching.has(host.id)) {
                    mcpConfigFetching.add(host.id);
                    const wantTab = currentSettingsTab;
                    fetchMcpConfig(host).then(() => {
                        mcpConfigFetching.delete(host.id);
                        if (currentSettingsTab === wantTab && settingsTarget === t)
                            renderMcpConfig();
                    }).catch(() => { mcpConfigFetching.delete(host.id); });
                }
                return;
            }
            setMcpEnabled.checked = !!cfg.enabled;
            // Don't clobber the token the user is mid-edit on (a late GET could
            // otherwise overwrite their typing before the change event fires).
            if (document.activeElement !== setMcpToken)
                setMcpToken.value = cfg.token || '';
            setMcpDefaultMode.value = cfg.default_mode || 'off';
            setMcpAllowLaunch.checked = !!cfg.allow_launch;
            // Env-pinned token: the broker won't accept UI token changes, so
            // disable the field + Generate and say why.
            const pinned = !!cfg.token_env_pinned;
            setMcpToken.disabled = pinned;
            setMcpGenerate.disabled = pinned;
            setMcpToken.placeholder = pinned
                ? 'set by WEB_TERMINAL_MCP_TOKEN (env)'
                : '(none — MCP disabled)';
        }
        // POST a patch to /mcp/config on the settings-target host; on success
        // refresh the cache + re-render (so a server-minted token appears).
        function saveMcpConfig(patch) {
            const t = settingsTarget;
            if (!t) return;
            const host = hostById(t.hostId);
            if (!host) return;
            fetch(hostHttpUrl(host, '/mcp/config'), {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(patch || {}),
            }).then(r => (r.ok ? r.json() : null)).then(j => {
                if (j && j.ok) {
                    mcpConfigCache.set(host.id, j);
                    if (settingsTarget === t) renderMcpConfig();
                }
            }).catch(() => {});
        }
        if (setMcpEnabled) {
            setMcpEnabled.addEventListener('change', () =>
                saveMcpConfig({ enabled: setMcpEnabled.checked }));
            setMcpAllowLaunch.addEventListener('change', () =>
                saveMcpConfig({ allow_launch: setMcpAllowLaunch.checked }));
            setMcpDefaultMode.addEventListener('change', () =>
                saveMcpConfig({ default_mode: setMcpDefaultMode.value }));
            setMcpToken.addEventListener('change', () =>
                saveMcpConfig({ token: setMcpToken.value }));
            setMcpGenerate.addEventListener('click', () =>
                saveMcpConfig({ generate: true }));
        }

        // The mode of a remote tab's cached layout ('tiling' default), so its
        // Tiling toggle reflects/persists that broker's own layout mode.
        function remoteTilingMode(t) {
            const entry = hostStateCache.get(t.hostId);
            const m = entry && entry.layout && entry.layout.mode;
            return (m === 'floating') ? 'floating' : 'tiling';
        }

        function commitSizeInputs() {
            const t = settingsTarget;
            if (!t) return;
            const s = t.s;
            const cols = parseInt(setColsEl.value, 10);
            const rows = parseInt(setRowsEl.value, 10);
            if (Number.isFinite(cols) && Number.isFinite(rows)) {
                // Floors keep the cols×rows promise honest: below ~35×8 the
                // MIN_W/MIN_H pixel floor would silently override the grid.
                s.size = { cols: Math.max(35, Math.min(500, cols)),
                           rows: Math.max(8, Math.min(200, rows)) };
                setColsEl.value = s.size.cols;
                setRowsEl.value = s.size.rows;
            } else if (!setColsEl.value && !setRowsEl.value) {
                s.size = null;   // back to legacy 720×480 px
            } else {
                return;          // half-filled — wait for the other field
            }
            t.save();
            // Changing the size while locked re-snaps every live window — local
            // tab only (remote sizes affect that broker's viewers, not us).
            if (t.isLocal && isSizeLocked()) applyLockedSizeToAll();
        }
        setColsEl.addEventListener('change', commitSizeInputs);
        setRowsEl.addEventListener('change', commitSizeInputs);
        document.getElementById('set-size-clear').addEventListener('click', () => {
            const t = settingsTarget;
            if (!t) return;
            setColsEl.value = '';
            setRowsEl.value = '';
            t.s.size = null;
            t.save();
            if (t.isLocal && isSizeLocked()) applyLockedSizeToAll();
        });

        for (const [cb, field] of [[setShowId, 'id'], [setShowPid, 'pid'],
                                   [setShowHost, 'host']]) {
            cb.addEventListener('change', () => {
                const t = settingsTarget;
                if (!t) return;
                t.s.show[field] = cb.checked;
                t.save();
                if (t.isLocal) applyDisplaySettings();
            });
        }

        // Workspace scrollbar: shared per-host display toggle (same pattern as
        // show.* above). Persist to the target's blob; the visual bar is a LOCAL
        // effect (applyDisplaySettings → updateStripScrollbar), so reapply only
        // for the local tab.
        setStripScrollbar.addEventListener('change', () => {
            const t = settingsTarget;
            if (!t) return;
            t.s.stripScrollbar = setStripScrollbar.checked;
            t.save();
            if (t.isLocal) applyDisplaySettings();
        });

        // #38: per-host dwell delay (ms) for the snap / pop-out gestures. There's
        // no live side effect — snapHoldMsFor reads it fresh on each drag — so we
        // just normalize (0/negative disables; else clamp [250,20000]; blank/NaN
        // -> the 3000 default), persist to the target's blob, and echo the
        // clamped value back so the field shows what was actually stored.
        setSnapHold.addEventListener('change', () => {
            const t = settingsTarget;
            if (!t) return;
            const raw = setSnapHold.value.trim();
            let v;
            if (raw === '') {
                v = 3000;
            } else {
                v = Math.round(Number(raw));
                if (!isFinite(v)) v = 3000;
                else if (v <= 0) v = 0;                     // explicit disable
                else v = Math.max(250, Math.min(20000, v));
            }
            t.s.snapHoldMs = v;
            setSnapHold.value = v;                          // reflect normalization
            t.save();
        });

        // Window mode. LOCAL: enter/leave functions re-tile/re-float live
        // windows and savePrefs() themselves. REMOTE: just persist that
        // broker's stored layout.mode (no local enter/exit).
        setTiling.addEventListener('change', () => {
            const t = settingsTarget;
            if (!t) return;
            if (t.isLocal) {
                if (setTiling.checked) enterTilingMode();
                else enterFloatingMode();
            } else {
                const entry = hostStateCache.get(t.hostId);
                if (!entry) return;
                // Only persist the mode once the host's layout is loaded — never
                // fabricate a bare {} that putHostState would then refuse (or,
                // worse, that would wipe the remote layout). Revert the checkbox
                // so it keeps reflecting the real stored mode.
                if (!entry.layoutLoaded || !entry.layout
                    || typeof entry.layout !== 'object'
                    || Array.isArray(entry.layout)) {
                    setTiling.checked = (remoteTilingMode(t) === 'tiling');
                    showNotice('cannot change mode — this broker’s layout '
                        + 'has not loaded yet');
                    return;
                }
                entry.layout.mode = setTiling.checked ? 'tiling' : 'floating';
                t.save();
            }
        });

        // Restore-on-refresh: bound to the LOCAL settings (governs this
        // browser's startup, not a remote host), so it ignores settingsTarget.
        setRestore.addEventListener('change', () => {
            getSettings().restoreOnRefresh = setRestore.checked;
            savePrefs();
        });
        // Taskbar workspace filter: also LOCAL (this browser's chrome). Reapply
        // immediately so the taskbar updates without a workspace switch.
        setHideOtherWs.addEventListener('change', () => {
            getSettings().hideTaskbarOtherWs = setHideOtherWs.checked;
            savePrefs();
            applyTaskbarWorkspace();
        });

        // Appearance (terminal font / start label): browser-local like restore-
        // on-refresh — write the LIVE local getSettings() directly (NOT
        // settingsTarget, which may point at a remote host), persist, then apply
        // to this browser immediately. This is why the Control Panel window (#59)
        // can host a remote host's tab without appearance edits leaking to it.
        // #75/#76: the color-scheme radio's and background-pattern select's change
        // handlers now live in their mods (ctx.settings.radio / ctx.settings.
        // select wire #set-mods -> savePrefs + applyTheme / applyPattern; the
        // theme mod's apply also re-applies the theme-var-aware pattern).
        setTermFontEl.addEventListener('change', () => {   // #18: terminal font
            getSettings().termFont = setTermFontEl.value;
            savePrefs();
            applyTerminalFont();
        });
        // #71: the clock toggle's change handler now lives in the clock mod
        // (ctx.settings.boolean wires the #set-mods checkbox to savePrefs +
        // re-apply). Core only reflects it on render (renderModSettingsToggles).
        setHelpButtonEl.addEventListener('change', () => {   // #40
            getSettings().showHelpButton = !!setHelpButtonEl.checked;
            savePrefs();
            applyHelpButton(setHelpButtonEl.checked);
        });
        const commitStartLabel = () => {
            const v = (setStartLabelEl.value || '').trim().slice(0, 24) || '+';
            getSettings().startLabel = v;
            savePrefs();
            applyStartButton();
        };
        setStartLabelEl.addEventListener('input', commitStartLabel);
        setStartLabelEl.addEventListener('change', commitStartLabel);
        // Issue #10/#17: the default start path is PER-HOST. Write the target
        // host's settings blob then t.save() (savePrefs for local; the host
        // /state PUT for remote) — same pattern as the default-profile control.
        // No live effect (it only governs the NEXT launch's cwd on that host),
        // and saving retires the legacy per-OS startPaths map (#2) it supersedes.
        let _startPathTimer = null;
        const commitStartPath = () => {
            if (_startPathTimer) { clearTimeout(_startPathTimer); _startPathTimer = null; }
            const t = settingsTarget;
            if (!t) return;
            t.s.startPath = (setStartPathEl.value || '').trim();
            if (t.s.startPaths) delete t.s.startPaths;
            t.save();
        };
        // Debounced on 'input' so typing persists without a remote /state PUT per
        // keystroke; 'change' (blur/Enter) and closeSettings() flush immediately
        // so an edit is never lost to Escape / a programmatic close.
        setStartPathEl.addEventListener('input', () => {
            if (_startPathTimer) clearTimeout(_startPathTimer);
            _startPathTimer = setTimeout(commitStartPath, 400);
        });
        setStartPathEl.addEventListener('change', commitStartPath);

        // Task 8: default terminal profile. Persists the same way every other
        // control in this form does — mutate the target's settings blob then
        // t.save() (savePrefs for local, the host /state PUT for remote). No
        // live local effect: it only governs the NEXT launch on that host.
        if (setDefaultProfile) {
            setDefaultProfile.addEventListener('change', () => {
                const t = settingsTarget;
                if (!t) return;
                t.s.defaultProfile = setDefaultProfile.value || '';
                t.save();
            });
        }

