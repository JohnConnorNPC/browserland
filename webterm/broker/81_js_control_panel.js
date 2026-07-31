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
        // #181: wider and shorter than the old 340-560 x 360-620. The panel used
        // to be one tall scroll, so height was the scarce dimension; it is now an
        // applet grid, and a grid wants width — at 340px the tiles fall into a
        // three-column stack before anything has been filtered. The clamps stay
        // clamps: a small desktop still gets a panel that fits inside it, and the
        // grid reflows on its own container width either way.
        function controlPanelDefaultGeom() {
            const d = document.getElementById('desktop').getBoundingClientRect();
            const width = Math.min(640, Math.max(340, Math.round(d.width - 48)));
            const height = Math.min(540, Math.max(340, Math.round(d.height - 64)));
            const left = Math.max(12, Math.round((d.width - width) / 2));
            const top = Math.max(12, Math.round((d.height - height) / 2 - 20));
            return { left, top, width, height };
        }
        function openControlPanelWindow(appData) {
            appData = appData || {};
            const id = String(appData.id || newAppId('cp'));
            const existing = windows.get(id);
            if (existing) {
                revealAndFocusWindow(id);
                return existing;
            }
            // Single-instance: focus the live Control Panel rather than spawn a
            // second (there is only one #settings-modal node to host). Parked on
            // another workspace it must come HERE, not silently no-op (#152) —
            // Ctrl+Alt+P is the whole reason that symptom was reported.
            const live = findControlPanelWindow();
            if (live) {
                revealAndFocusWindow(live.id);
                return live;
            }
            const modal = document.getElementById('settings-modal');
            if (!modal) return null;   // markup missing — nothing to host

            const title = appData.title || 'Control Panel';
            const geom = clampGeom(appData.geom || controlPanelDefaultGeom());
            const color = normalizeHex(appData.color || defaultColor(id));

            // Shared chrome (#79): .term-window shell + title bar (_ / ×) + the
            // eight resize handles, built + wired by the window-runtime factory.
            // The Control Panel is locked:false (no scroll-lock) and its id badge
            // ('#cp') differs from its sid ('control-panel').
            const chrome = buildAppChrome({
                id, appClass: 'app-control-panel', badge: '#cp',
                geom, color, locked: false, title,
            });
            const { dom, titleText } = chrome;

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

            addResizeHandles(dom);   // last children: edge/corner hit zones on top

            document.getElementById('desktop').appendChild(dom);
            document.getElementById('desktop').classList.remove('empty');
            windows.set(id, win);

            // Raise / minimize / close / drag / 8-way resize / WM context menu.
            wireAppChrome(win, chrome);

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
            // The panel BORROWS a singleton that is never destroyed, so nothing
            // about a reopen is fresh — including a remote tab's loading state
            // left mid-flight by the previous close. That fetch's tab guard
            // returns without clearing either the placeholder or the .loading
            // class (it is answering for a tab that is no longer open), so a
            // reopen would otherwise land on the local tab wearing "loading
            // settings…". Clear both here, where we know the tab is local.
            setHostLoadingEl.style.display = 'none';
            setHostLoadingEl.textContent = 'loading settings…';
            setPaneHost.classList.remove('loading');
            renderSettingsTabs();
            showSettingsPane('local');
            renderSettings();
            // #181: land on the applet grid, with no filter and no applet held
            // over from the last time this node was open. AFTER renderSettings,
            // which repaints every section (and reconciles through
            // applyBrowserGlobalVisibility) before we decide what to show.
            cpResetControlPanelView();

            finishWindowPlacement(win);
            return win;
        }
        function toggleControlPanelWindow() {
            const ex = findControlPanelWindow();
            if (ex && !ex.minimized && frontId === ex.id) { closeWindow(ex.id); return; }
            openControlPanelWindow({});
        }
        // #78: focusOrOpenHelp / launchHelp / toggleHelpWindow, the "?" chip
        // wiring, and the first-run Help nudge moved to mods/help/help.js. They
        // stay top-level hoisted functions in that mod script, so the core call
        // sites (the (+) menu launchHelp in 76, the toggle-help keybinding in 78)
        // still resolve them across the one concatenated <script>.

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
            // #181: this is the ONE function that knows browser-global
            // visibility just moved, and the applet grid is built from what is
            // actually available on the open tab — an applet whose whole
            // membership is browser-global must not be drawn on a remote tab
            // only to open onto blank space. Reconcile reads the inline display
            // we just wrote, so it must run AFTER the loop, never before.
            reconcileControlPanel();
        }
        // Switch tabs. Browser + local are synchronous; a remote host awaits a
        // fresh /state GET (with a "loading…" placeholder) before its form is
        // populated and editable.
        async function selectSettingsTab(tabId) {
            currentSettingsTab = tabId;
            _kbRecording = null;
            renderSettingsTabs();
            showSettingsPane(tabId);
            // #157: the mod-policy section keys off currentSettingsTab alone (not
            // settingsTarget / this host's /state), so paint it NOW — before any
            // await, on every tab including 'browser'. If the remote /state fetch
            // below is slow or fails, renderSettings never runs, and rows left from
            // the previous tab would show another broker's policy under this host's
            // name — the defect #153 fixed for the OSC 52 box.
            renderModPolicy();
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
            // #153: the clipboard grant is BROWSER-LOCAL, so it neither needs
            // nor waits for the remote's /state. Rendered here, synchronously,
            // for the same reason: if the fetch below fails (host down, no
            // token) renderSettings never runs, and the box would otherwise sit
            // there showing the PREVIOUS tab's host — a grant switch displaying
            // another machine's answer. The change handler resolves its host
            // from currentSettingsTab, so it stays usable on that tab too.
            setOsc52.checked = isOsc52Allowed(tabId);
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
            renderLabelOrder(t);   // #123: label id/pid/host/title toggles + order
            setTiling.checked = t.isLocal
                ? isTilingMode()
                : (remoteTilingMode(t) === 'tiling');
            setStripScrollbar.checked = !!s.stripScrollbar;
            // #88: terminal × terminate toggle + its confirm companion. Confirm
            // is greyed while the terminate toggle is off (it only applies then).
            setCloseTerminates.checked = !!s.terminalCloseTerminates;
            setConfirmTerminate.checked = !!s.terminalCloseConfirm;
            setConfirmTerminate.disabled = !s.terminalCloseTerminates;
            // #38: per-host dwell delay. s is already normalized (0 or a clamped
            // number), so reflect it verbatim; 0 shows as "0" (= disabled).
            setSnapHold.value = (typeof s.snapHoldMs === 'number') ? s.snapHoldMs : 3000;
            // #125: per-broker viewport-slide rate (ms/screen). s is normalized
            // (0 or a clamped number), so reflect verbatim; 0 shows as "0".
            setSlideMs.value = (typeof s.slideScreenMs === 'number') ? s.slideScreenMs : 350;

            // #153: clipboard authorization for the host this tab belongs to,
            // read from the browser-local map — never t.s, which syncs.
            setOsc52.checked = isOsc52Allowed(t.hostId);

            // Restore-on-refresh governs THIS browser's startup (not a remote
            // host), so it always reflects the LOCAL setting on every host tab.
            setRestore.checked = !!getSettings().restoreOnRefresh;

            // Appearance is browser-global — reflect the LIVE local settings
            // (these controls are hidden on remote tabs, shown only on local).
            // #75/#76/#126: the color-scheme radio, the background-pattern select,
            // and the terminal-font select all reflect themselves through their mods
            // (renderModSettingsToggles below); core reflects none of them.
            const ls = getSettings();
            renderModSettingsToggles(t.isLocal);   // #71/#78: reflect mod toggles (clock, help, …)
            setStartLabelEl.value = (ls.startLabel === '+' ? '' : ls.startLabel);
            setSwapLaunchEl.checked = !!ls.swapLaunchButtons;   // #114 (browser-global)
            // #178: normalizeSettings guarantees one of the three modes, so the
            // select always has a matching option to land on.
            setHostStatusChipEl.value = ls.hostStatusChip;
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
            renderProfilesEditor();
            renderMcpConfig();
            renderModPolicy();   // #157 (also painted synchronously on tab switch)
            renderKeybindings();
        }
        // #123: taskbar/title label editor. Renders one reorderable row per key
        // in t.s.show.order (checkbox = t.s.show[key] visibility, ↑/↓ = swap
        // adjacent, drag = splice before/after a target). All three mechanisms
        // AND the toggle funnel through set(): mutate t.s, persist via t.save(),
        // reapply live for the local target, then re-render. Reflects/persists
        // through settingsTarget, so it drives both the local target (savePrefs →
        // localStorage + /state push) and a remote-host target (/state PUT); a
        // remote host's own viewers re-apply on their /state pull. Only the LAST
        // remaining checked component can't be unchecked (≥1 stays on).
        const LABEL_ORDER_NAMES = { id: 'window id', host: 'host',
                                    pid: 'pid', title: 'title' };
        let _labelDragKey = null;
        function renderLabelOrder(t) {
            const host = document.getElementById('set-label-order');
            if (!host) return;
            host.textContent = '';
            const order = normalizeLabelOrder(t.s.show.order);   // defensive
            const set = (fn) => {
                fn();
                t.save();
                if (t.isLocal) applyDisplaySettings();
                renderLabelOrder(t);
            };
            const commitOrder = (a) =>
                set(() => { t.s.show.order = normalizeLabelOrder(a); });
            const checkedCount = () =>
                order.reduce((n, k) => n + (t.s.show[k] ? 1 : 0), 0);
            order.forEach((key, i) => {
                const row = document.createElement('div');
                row.className = 'set-label-row';
                row.draggable = true;
                row.dataset.labelKey = key;

                const cb = document.createElement('input');
                cb.type = 'checkbox';
                cb.checked = !!t.s.show[key];
                cb.addEventListener('change', () => {
                    // Never turn OFF the last remaining component — revert the
                    // box so ≥1 datum-bearing part always stays selectable.
                    if (!cb.checked && checkedCount() <= 1) { cb.checked = true; return; }
                    set(() => { t.s.show[key] = cb.checked; });
                });

                const name = document.createElement('span');
                name.className = 'set-label-name';
                name.textContent = LABEL_ORDER_NAMES[key] || key;

                const up = document.createElement('button');
                up.type = 'button';
                up.textContent = '↑';
                up.title = 'move up';
                up.disabled = (i === 0);
                up.addEventListener('click', () => {
                    const a = order.slice();
                    const tmp = a[i - 1]; a[i - 1] = a[i]; a[i] = tmp;
                    commitOrder(a);
                });

                const down = document.createElement('button');
                down.type = 'button';
                down.textContent = '↓';
                down.title = 'move down';
                down.disabled = (i === order.length - 1);
                down.addEventListener('click', () => {
                    const a = order.slice();
                    const tmp = a[i + 1]; a[i + 1] = a[i]; a[i] = tmp;
                    commitOrder(a);
                });

                // Drag-to-reorder (file-manager row mechanics). The drop lands
                // the dragged key BEFORE or AFTER this target row depending on
                // which half of the row the pointer is over, so the very ends
                // stay reachable. Dropping onto itself is a no-op.
                row.addEventListener('dragstart', (e) => {
                    _labelDragKey = key;
                    if (e.dataTransfer) e.dataTransfer.effectAllowed = 'move';
                    row.classList.add('dragging');
                });
                row.addEventListener('dragend', () => {
                    _labelDragKey = null;
                    row.classList.remove('dragging', 'drop-before', 'drop-after');
                });
                row.addEventListener('dragover', (e) => {
                    if (!_labelDragKey || _labelDragKey === key) return;
                    e.preventDefault();
                    if (e.dataTransfer) e.dataTransfer.dropEffect = 'move';
                    const r = row.getBoundingClientRect();
                    const after = (e.clientY - r.top) > r.height / 2;
                    row.classList.toggle('drop-after', after);
                    row.classList.toggle('drop-before', !after);
                });
                row.addEventListener('dragleave', () => {
                    row.classList.remove('drop-before', 'drop-after');
                });
                row.addEventListener('drop', (e) => {
                    e.preventDefault();
                    const drag = _labelDragKey;
                    row.classList.remove('drop-before', 'drop-after');
                    if (!drag || drag === key) return;
                    const r = row.getBoundingClientRect();
                    const after = (e.clientY - r.top) > r.height / 2;
                    const a = order.filter(k => k !== drag);
                    let ti = a.indexOf(key);
                    if (ti === -1) return;
                    if (after) ti += 1;
                    a.splice(ti, 0, drag);
                    commitOrder(a);
                });

                row.appendChild(cb);
                row.appendChild(name);
                row.appendChild(up);
                row.appendChild(down);
                host.appendChild(row);
            });
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
            // The re-render is conditional on the fetch having actually
            // POPULATED the cache: against a dead host the GET fast-fails, and
            // an unconditional re-render would re-enter here, re-fetch, fail
            // again — a hot loop hammering a broker that is already down. A
            // failed fetch simply leaves the "(broker default)" option standing.
            if (host && !profilesCache.has(host.id)) {
                const wantTab = currentSettingsTab;
                fetchProfiles(host).then(() => {
                    if (profilesCache.has(host.id)
                            && currentSettingsTab === wantTab
                            && settingsTarget === t) {
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
            return hostFetch(host, '/mcp/config')
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
                // Re-render ONLY if the GET actually populated the cache. An
                // unconditional re-render re-enters this branch and re-fetches,
                // so a host that fast-fails (broker down, connection refused)
                // turns the section into a hot retry loop. Failing quietly here
                // leaves the neutral defaults on screen; the next deliberate
                // render (reopening the tab / switching back to this host) is
                // the retry, and it is a manual one.
                if (host && !mcpConfigFetching.has(host.id)) {
                    mcpConfigFetching.add(host.id);
                    const wantTab = currentSettingsTab;
                    fetchMcpConfig(host).then(() => {
                        mcpConfigFetching.delete(host.id);
                        if (mcpConfigCache.has(host.id)
                                && currentSettingsTab === wantTab
                                && settingsTarget === t)
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
            hostFetch(host, '/mcp/config', {
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

        // ---- Launch profiles editor (#70) ----------------------------------
        // Per-host, same pattern as the MCP section: fetch the FULL objects from
        // this host's /profiles/config (browser realm — commands never travel via
        // /profiles or /mcp/*), edit them, POST the whole set back (replace
        // semantics), and live-swap without a restart. After every save we also
        // invalidate the names-only profilesCache (76) so the right-click + launch
        // picker and the Default-profile <select> refetch the live set.
        function fetchProfilesConfig(host) {
            return hostFetch(host, '/profiles/config')
                .then(r => (r.ok ? r.json() : null))
                .then(j => { if (j && j.ok) profilesConfigCache.set(host.id, j); })
                .catch(() => {});
        }
        function commandPreview(cmd) {
            const s = (Array.isArray(cmd) ? cmd : []).join(' ');
            return s.length > 80 ? s.slice(0, 79) + '…' : s;
        }
        // The host the editor acts on = the settings-target host (local or the
        // open remote tab), so every write lands on the right broker.
        function profilesEditorHost() {
            return settingsTarget ? hostById(settingsTarget.hostId) : null;
        }
        // #115: per-profile color pickers reuse attachColorPicker, which appends a
        // hidden <input type=color> to document.body per picker (cleaned via
        // target.cleanups). renderProfilesEditor wipes + rebuilds its rows on every
        // render (tab open, poll refresh, a save, a color pick), so hold the live
        // pickers' cleanups + their shim targets module-side and flush them at the
        // top of each render: mark stale targets disposed (a late OS color-dialog
        // resolve then no-ops) and run every cleanup (removes the listeners + the
        // body inputs) BEFORE the fresh pickers register. Exact mirror of
        // flushHostPickers (#103, 83_js_broker_identity.js). Bounded to one input-
        // per-profile-row on document.body at a time.
        let profilePickerCleanups = [];
        let profilePickerTargets = [];
        function flushProfilePickers() {
            for (const t of profilePickerTargets) t.disposed = true;
            for (const fn of profilePickerCleanups) { try { fn(); } catch (_) {} }
            profilePickerCleanups = [];
            profilePickerTargets = [];
        }
        // #115: set/clear one profile's default color. Mutates the cached (full)
        // profile set and re-POSTs it (REPLACE semantics, like the Default button),
        // which persists to the sidecar, live-swaps the launcher, drops the names-
        // only /profiles cache, and re-renders the editor; refreshTaskbar so any
        // rebuilt chips pick up the new color map. val=null clears the default.
        function applyProfileColor(host, name, val) {
            const cfg = profilesConfigCache.get(host.id);
            if (!cfg || !cfg.profiles || !cfg.profiles[name]) return;
            const np = Object.assign({}, cfg.profiles);
            np[name] = Object.assign({}, np[name], { color: val || null });
            return saveProfilesConfig(host, np, cfg.default_profile)
                .then(() => refreshTaskbar());
        }
        // ---- "Mods on this broker" section (#157) ---------------------------
        // Views and edits the MOD POLICY of the broker whose tab is open: the
        // on/off that broker PINS for every browser that loads its page. Three
        // states per mod — Default (unpinned, so each browser's own Mods list
        // decides), On, Off — which is exactly {absent, true, false} in that
        // broker's policy.
        //
        // What this is NOT: it does not scope YOUR mods per host, and on a remote
        // tab it changes nothing about your current session. It edits state that
        // takes effect when a browser next loads THAT broker's page.
        //
        // Read and write are both that broker's own admin surface — GET /info for
        // the catalog + pins, POST /mods/policy to change one. Deliberately NOT
        // the settingsTarget /state path every other control on this pane uses: a
        // /state PUT is lease-gated (409 not_active unless you are that broker's
        // single active browser), so the pins of any broker with a live viewer of
        // its own would be permanently unwritable — the exact broker an operator
        // reaches for this section to administer. Nothing here reads or writes
        // `t.s`, so the section also stays usable on a tab whose /state never
        // loaded.
        //
        // Cached per host WITH the failure recorded, mirroring profilesConfigFailed
        // below: a render must never re-fetch after a failed fetch, or a sleeping
        // broker turns the section into a hot retry loop. Re-selecting the tab is
        // the retry, and it is a deliberate one.
        const modCatalogCache = new Map();      // hostId -> {state, mods, modsEnabled, policy}
        const modCatalogFetching = new Set();   // hostIds with an in-flight GET
        // Fetch one host's catalog + pins. Resolves to nothing; the outcome lands
        // in modCatalogCache ALWAYS (a failure is a cached outcome, not an
        // absence), so every state has copy of its own and none re-fetches:
        //   ok           - a current broker: `mods` is its served catalog
        //   headless     - serve_ui false: it serves no page, so it serves no mods
        //   unsupported  - answered /info without `mods`: predates this feature
        //   unauthorized - 401/403: no token stored for that host, or a stale one
        //   unreachable  - down, black-holed, or CORS-blocked
        async function fetchModCatalog(host) {
            const rec = { state: 'unreachable', mods: [], modsEnabled: true,
                          policy: {} };
            try {
                const r = await hostFetch(host, '/info', { cache: 'no-store' });
                if (r.status === 401 || r.status === 403) {
                    rec.state = 'unauthorized';
                } else if (r.ok) {
                    let j = null;
                    try { j = await r.json(); } catch (_) { j = null; }
                    if (j && typeof j === 'object' && Array.isArray(j.mods)) {
                        rec.mods = j.mods;
                        rec.modsEnabled = (j.mods_enabled !== false);
                        rec.policy = sanitizeModPolicy(j.mod_policy);
                        rec.state = (j.serve_ui === false) ? 'headless' : 'ok';
                    } else {
                        // A pre-#157 broker answers /info fine and simply lacks the
                        // keys. This is WHY the catalog rides /info: a brand-new
                        // path would have failed its cross-origin OPTIONS preflight
                        // on that broker and arrived here as an opaque network
                        // error — reported as "asleep" for a machine that is up.
                        rec.state = 'unsupported';
                    }
                }
            } catch (_) { /* leave 'unreachable' */ }
            modCatalogCache.set(host.id, rec);
        }
        // The well-shaped {modId: bool} subset of whatever a peer sent — the twin
        // of the broker's own _sanitize_mod_policy. A peer is not trusted to bound
        // our DOM: only real booleans on mod-id-shaped keys survive, capped.
        function sanitizeModPolicy(raw) {
            const out = {};
            if (!raw || typeof raw !== 'object' || Array.isArray(raw)) return out;
            for (const k of Object.keys(raw).sort()) {
                if (Object.keys(out).length >= MAX_MOD_POLICY_KEYS) break;
                if (typeof raw[k] === 'boolean' && MOD_ID_RE.test(k)) out[k] = raw[k];
            }
            return out;
        }
        // The pins a pinned-ON mod IMPLIES: its declared `requires`, transitively.
        // The enforcing loader does the same thing (_resolvePins, 86) because a
        // "pinned on" mod whose dependency is off never actually runs; this mirror
        // exists so the editor can SAY so on the dependency's row instead of
        // showing it as unpinned while it is in fact forced on. Walks the catalog
        // backwards: it is in served order, where a dependency always precedes its
        // dependents, so one pass closes a chain. An EXPLICIT pin is never
        // overridden — pinning `editor:false` under `scratchpad:true` leaves editor
        // off and scratchpad blocked, which is what the loader enforces.
        function modPolicyImplied(mods, policy) {
            // Object.create(null), NOT {}: the ids here come off a PEER's /info
            // and are not filtered by MOD_ID_RE on this path. On a plain object
            // an id of "__proto__" would make `implied[id] = m.id` a silent
            // no-op and `implied[id] !== undefined` read Object.prototype's own
            // __proto__ accessor — i.e. every mod would look implied-on. A
            // null-prototype map makes that unrepresentable rather than
            // defended against (#163).
            const implied = Object.create(null);  // id -> the mod that requires it
            const byId = new Set(mods.map((m) => m.id));
            for (let i = mods.length - 1; i >= 0; i--) {
                const m = mods[i];
                const on = (policy[m.id] === true)
                    || (policy[m.id] === undefined && implied[m.id] !== undefined);
                if (!on) continue;
                for (const dep of (Array.isArray(m.requires) ? m.requires : [])) {
                    if (byId.has(dep) && policy[dep] === undefined
                            && implied[dep] === undefined) {
                        implied[dep] = m.id;
                    }
                }
            }
            return implied;
        }
        // Change pins on `host`. `set` is {modId: true | false | null (= clear)}.
        // PATCH-shaped ({set:{…}}) so two operators editing different mods cannot
        // clobber each other, and the broker answers with the AUTHORITATIVE
        // policy — which is what the row repaints from, so a refused or partial
        // write can never leave the select showing a value that never landed.
        //
        // #158: takes the whole map, not one id, so a bulk apply (the sync-mods
        // mod pushing this broker's setup to a peer) is ONE round trip landing
        // under the broker's own lock, rather than N writes leaving N partial
        // states behind a failure. The pin editor's saveModPin below is the
        // one-key wrapper, so both callers share this single write path — #158
        // asked for exactly that rather than a second way to set the same field.
        // opts.quiet suppresses the notices: a fan-out reports per broker in its
        // own result rows, and one toast per failing host is a storm.
        // Resolves {ok, error?} and NEVER rejects.
        async function saveModPins(host, set, opts) {
            const quiet = !!(opts && opts.quiet);
            const ids = Object.keys(set || {});
            // hostFetch(null, …) silently resolves against OUR OWN origin, so a
            // caller that lost its host must never reach it — that would write
            // this broker's policy under a peer's name.
            if (!host) return { ok: false, error: 'no_host' };
            if (!ids.length) return { ok: true };        // nothing to write
            if (ids.length > MAX_MOD_POLICY_KEYS) {
                return { ok: false, error: 'too_many' };
            }
            let r;
            try {
                r = await hostFetch(host, '/mods/policy', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ set: set }),
                });
            } catch (e) {
                if (!quiet) {
                    showNotice('could not reach ' + (host.label || host.url)
                        + ' to change its mod policy');
                }
                return { ok: false, error: 'unreachable' };
            }
            if (!r.ok) {
                if (!quiet) {
                    showNotice('could not change the mod policy on '
                        + (host.label || host.url) + ' (HTTP ' + r.status + ')');
                }
                let e = null;
                try { e = await r.json(); } catch (_) { e = null; }
                return { ok: false,
                         error: (e && e.error) ? e.error : ('HTTP ' + r.status) };
            }
            let j = null;
            try { j = await r.json(); } catch (_) { j = null; }
            const rec = modCatalogCache.get(host.id);
            if (rec && j && j.ok) rec.policy = sanitizeModPolicy(j.policy);
            return { ok: true };
        }
        // #163: a mod's provenance as reported by the broker that serves it.
        //
        // SELF-ASSERTED, and rendered as such. `source` is a string that broker
        // chose to put in its own /info; there is no way from here to check it,
        // and this pane exists precisely to administer a broker somebody else
        // controls. So the label QUOTES the claim ("peer reports: installed")
        // and anything we do not recognise — a missing key, a non-string, a
        // value from a future build, a value a hostile broker made up — renders
        // `unknown`. It must never fall back to the more-trusted-looking label:
        // "unknown => shipped" would let a broker present a third-party package
        // as part of our own bundle by omitting one field.
        //
        // ONE label for every tab, including the local one. The local broker's
        // rows arrive down the same /info fetch as everyone else's, and a second
        // rendering path that trusts more is a second path to get wrong.
        function peerModSource(source) {
            if (source === 'installed') return 'peer reports: installed';
            if (source === 'shipped') return 'peer reports: shipped';
            return 'unknown';
        }
        // One pin, for the per-host editor's select. Keeps the boolean contract
        // its change handler was written against.
        function saveModPin(host, id, pin) {
            const set = {};
            set[id] = pin;
            return saveModPins(host, set).then(function (res) {
                return !!(res && res.ok);
            });
        }
        // Paint the section for the CURRENT tab. Safe to call with no settings
        // target and before/without any /state — it keys off currentSettingsTab
        // alone. That is what lets selectSettingsTab call it SYNCHRONOUSLY on
        // every tab switch: a remote /state GET can take seconds and can fail
        // outright, and rows left over from the previous tab would be another
        // machine's policy under this host's name (the #153 lesson).
        function renderModPolicy() {
            const list = document.getElementById('set-mod-policy-list');
            const hint = document.getElementById('set-mod-policy-hint');
            if (!list || !hint) return;
            const tabId = currentSettingsTab;
            list.textContent = '';
            if (tabId === 'browser') { hint.textContent = ''; return; }
            const host = hostById(tabId);
            if (!host) {
                // The host was removed while its tab was open. hostFetch(null, …)
                // silently resolves against OUR OWN origin, so a null host must
                // never reach it — bail instead of reading our own policy under
                // someone else's name.
                hint.textContent = 'this broker is no longer configured.';
                return;
            }
            const rec = modCatalogCache.get(host.id);
            if (!rec) {
                hint.textContent = 'reading this broker’s mods…';
                if (!modCatalogFetching.has(host.id)) {
                    modCatalogFetching.add(host.id);
                    const wantTab = tabId;
                    fetchModCatalog(host).then(() => {
                        modCatalogFetching.delete(host.id);
                        // Repaint ONLY if this is still the open tab, so a late
                        // answer can never paint one broker's mods under another's.
                        if (currentSettingsTab === wantTab) renderModPolicy();
                    }).catch(() => { modCatalogFetching.delete(host.id); });
                }
                return;
            }
            const isLocal = (host.id === 'local');
            if (rec.state !== 'ok') {
                // Degrade with copy per cause, and offer NO edit: writing a pin to
                // a broker that cannot enforce it (an older build) would plant
                // state that silently activates when it is next upgraded.
                hint.textContent = {
                    headless: 'this broker serves no desktop page, so it serves no '
                        + 'mods — there is nothing to pin here.',
                    unsupported: 'this broker’s build does not report its mods, so '
                        + 'its mod policy cannot be read or changed from here. '
                        + 'Update it to manage its mods remotely.',
                    unauthorized: 'this broker did not accept our password, so its '
                        + 'mods cannot be read. Set its password on the Browser tab.',
                    unreachable: 'could not reach this broker to read its mods. '
                        + 'Select this tab again to retry.',
                }[rec.state] || 'this broker’s mods are unavailable.';
                return;
            }
            const implied = modPolicyImplied(rec.mods, rec.policy);
            // Union of served mods and whatever the policy names, so a pin for a
            // mod this broker does not ship is VISIBLE (and clearable) instead of
            // silently governing nothing. Capped: the row count must never be a
            // function of what a peer chose to send us.
            // UNIQUE ids, enforced here rather than assumed: a peer's /info is
            // a JSON array it composed, and nothing stops it repeating an id.
            // Two rows for one id would give one mod two selects whose change
            // handlers write the same key, so the visible value would depend on
            // which row was touched last. The `served` index is a Set (and
            // `implied` a null-prototype map, above) so no id — "__proto__"
            // included — can corrupt the lookup (#163).
            const served = new Set();
            const rows = [];
            for (const m of rec.mods) {
                if (rows.length >= MAX_MOD_POLICY_KEYS) break;
                if (!m || typeof m.id !== 'string' || !MOD_ID_RE.test(m.id)) continue;
                if (served.has(m.id)) continue;
                served.add(m.id);
                // `missing` is OUR sentinel for "the policy names it but the
                // broker does not serve it", built below. It must never be read
                // off the wire: a peer that sent `missing:true` on a row it DID
                // serve would suppress its own provenance badge AND get the
                // "not installed on this broker" note — a served mod dressed as
                // an absent one. Rebuilt here so a peer field cannot survive.
                rows.push({ id: m.id, title: m.title,
                            description: m.description,
                            source: m.source,
                            default_enabled: m.default_enabled,
                            missing: false });
            }
            for (const id of Object.keys(rec.policy)) {
                if (rows.length >= MAX_MOD_POLICY_KEYS) break;
                if (!served.has(id)) rows.push({ id: id, missing: true });
            }
            for (const m of rows) {
                const id = m.id;
                const row = document.createElement('div');
                row.className = 'set-mod-policy-row';
                row.dataset.modId = id;
                const name = document.createElement('span');
                name.className = 'set-mod-policy-name';
                name.textContent = id;
                // A peer's own strings — text nodes only, and length-capped so a
                // long or hostile title/description cannot blow out the panel.
                const desc = [m.title, m.description].filter(
                    (s) => typeof s === 'string' && s).join(' — ');
                if (desc) name.title = desc.slice(0, 400);
                row.appendChild(name);
                // #163: where that broker says the mod came from. A row with no
                // peer claim at all (a pin naming a mod the broker does not
                // serve) gets no badge — the "not installed on this broker"
                // note below is that row's whole story.
                if (!m.missing) {
                    const src = document.createElement('span');
                    src.className = 'set-mod-policy-source';
                    src.textContent = peerModSource(m.source);
                    src.title = 'this broker reports where it got the mod; '
                        + 'nothing here verifies that claim';
                    row.appendChild(src);
                }
                const sel = document.createElement('select');
                sel.className = 'set-mod-policy-pin';
                // "Default" has to say what the default IS: four shipped mods are
                // default-off, and an unlabelled "Default" reads as "on" for them.
                const dflt = m.missing ? 'Default'
                    : ((m.default_enabled === false) ? 'Default (off)'
                                                     : 'Default (on)');
                for (const opt of [['', dflt], ['on', 'On'], ['off', 'Off']]) {
                    const o = document.createElement('option');
                    o.value = opt[0];
                    o.textContent = opt[1];
                    sel.appendChild(o);
                }
                sel.value = (typeof rec.policy[id] === 'boolean')
                    ? (rec.policy[id] ? 'on' : 'off') : '';
                sel.disabled = !rec.modsEnabled;
                sel.addEventListener('change', () => {
                    // Resolve the host at EVENT time, never from a closure: these
                    // rows outlive a tab switch by however long the change event
                    // takes to arrive, and a captured host would write this pin
                    // into the PREVIOUS broker's policy.
                    const h = hostById(currentSettingsTab);
                    if (!h || h.id !== host.id) { renderModPolicy(); return; }
                    const pin = (sel.value === '') ? null : (sel.value === 'on');
                    sel.disabled = true;
                    const wantTab = currentSettingsTab;
                    saveModPin(h, id, pin).then(() => {
                        if (currentSettingsTab === wantTab) renderModPolicy();
                        else sel.disabled = false;
                    }).catch(() => { sel.disabled = false; });
                });
                row.appendChild(sel);
                const note = document.createElement('span');
                note.className = 'set-mod-policy-note';
                if (m.missing) {
                    note.textContent = 'not installed on this broker';
                } else if (rec.policy[id] === undefined && implied[id]) {
                    note.textContent = 'on — required by ' + implied[id];
                }
                row.appendChild(note);
                list.appendChild(row);
            }
            if (!rec.modsEnabled) {
                hint.textContent = 'this broker’s master mod switch is off '
                    + '(mods_enabled=false), so none of its mods run — pins are '
                    + 'shown but cannot be changed from here.';
                return;
            }
            hint.textContent = 'What this broker pins for every browser that loads '
                + (isLocal ? 'this' : 'that') + ' page. “Default” leaves the choice '
                + 'to each browser (its own Mods list); On and Off take the choice '
                + 'away and lock it — including for mods that ship off, so pinning '
                + 'one on turns it on for everyone. Applies the next time a browser '
                + 'loads that page'
                + (isLocal ? ', including this one — so a change here is not live.'
                           : '.');
        }
        // Hosts whose last /profiles/config GET failed. While a host is in here
        // the editor shows a manual "retry" control instead of re-fetching on
        // every render: the old code re-rendered unconditionally after a failed
        // fetch, and since a failed fetch leaves the cache empty that re-render
        // immediately re-fetched — a hot loop against a broker that is down.
        const profilesConfigFailed = new Set();
        function renderProfilesEditor() {
            flushProfilePickers();
            const t = settingsTarget;
            if (!t || !setProfilesListEl) return;
            const host = hostById(t.hostId);
            const cfg = host ? profilesConfigCache.get(host.id) : null;
            setProfilesListEl.innerHTML = '';
            // Add/Detect both end in a whole-set REPLACE POST built on top of
            // the cached set, so neither is safe while that set is unknown —
            // see openProfileDialog. Gate them on a loaded config.
            if (setProfileAddBtn) setProfileAddBtn.disabled = !cfg;
            if (setProfileDetectBtn) setProfileDetectBtn.disabled = !cfg;
            if (!cfg) {
                if (host && profilesConfigFailed.has(host.id)) {
                    setProfilesListEl.textContent = 'could not load — ';
                    const again = document.createElement('button');
                    again.type = 'button';
                    again.textContent = 'retry';
                    again.addEventListener('click', () => {
                        profilesConfigFailed.delete(host.id);
                        renderProfilesEditor();
                    });
                    setProfilesListEl.appendChild(again);
                    return;
                }
                setProfilesListEl.textContent = 'loading…';
                if (host && !profilesConfigFetching.has(host.id)) {
                    profilesConfigFetching.add(host.id);
                    const wantTab = currentSettingsTab;
                    fetchProfilesConfig(host).then(() => {
                        profilesConfigFetching.delete(host.id);
                        // Only recurse when the GET actually populated the
                        // cache; otherwise mark the host failed so the next
                        // render offers retry rather than another request.
                        if (!profilesConfigCache.has(host.id)) {
                            profilesConfigFailed.add(host.id);
                        }
                        if (currentSettingsTab === wantTab && settingsTarget === t)
                            renderProfilesEditor();
                    }).catch(() => {
                        // fetchProfilesConfig swallows its own errors, so this
                        // is belt-and-braces — but if it ever stops doing so,
                        // an unmarked failure would put the loop straight back.
                        profilesConfigFetching.delete(host.id);
                        profilesConfigFailed.add(host.id);
                    });
                }
                return;
            }
            profilesConfigFailed.delete(host.id);
            const names = Object.keys(cfg.profiles || {}).sort();
            if (!names.length) { setProfilesListEl.textContent = 'no profiles'; return; }
            for (const name of names) {
                const p = cfg.profiles[name] || {};
                const row = document.createElement('div');
                row.className = 'set-profile-row';
                const info = document.createElement('div');
                info.className = 'set-profile-info';
                const nm = document.createElement('div');
                nm.className = 'set-profile-name';
                nm.textContent = name;
                if (name === cfg.default_profile) {
                    const badge = document.createElement('span');
                    badge.className = 'set-profile-badge';
                    badge.textContent = 'default';
                    nm.appendChild(badge);
                }
                if (cfg.exists && cfg.exists[name] === false) {
                    const warn = document.createElement('span');
                    warn.className = 'set-profile-warn';
                    warn.textContent = '⚠ not found';
                    warn.title = 'the command was not found on this host’s PATH';
                    nm.appendChild(warn);
                }
                info.appendChild(nm);
                const cmd = document.createElement('div');
                cmd.className = 'set-profile-cmd';
                cmd.textContent = commandPreview(p.command)
                    + (p.title ? '  ·  ' + p.title : '');
                info.appendChild(cmd);
                row.appendChild(info);
                const acts = document.createElement('div');
                acts.className = 'set-profile-acts';
                const mk = document.createElement('button');
                mk.type = 'button'; mk.textContent = 'Default';
                mk.disabled = (name === cfg.default_profile);
                mk.addEventListener('click', () =>
                    saveProfilesConfig(host, cfg.profiles, name));
                const ed = document.createElement('button');
                ed.type = 'button'; ed.textContent = 'Edit';
                ed.addEventListener('click', () => openProfileDialog(host, name, null));
                const del = document.createElement('button');
                del.type = 'button'; del.textContent = 'Delete';
                del.className = 'danger';
                del.addEventListener('click', () => deleteProfile(host, name));
                acts.appendChild(mk); acts.appendChild(ed); acts.appendChild(del);
                // #115: optional per-profile DEFAULT color — the reused swatch
                // picker on a lightweight shim target (a `color` getter reading the
                // live profile entry, `disposed`, and the shared module cleanups
                // array flushed above). A pick writes the profile's color server-
                // side via applyProfileColor; the ✕ clears it back to the host/auto
                // default. The popover anchors to the row (position:relative).
                const colorTarget = {
                    get color() { return p.color || ''; },
                    disposed: false,
                    cleanups: profilePickerCleanups,
                };
                profilePickerTargets.push(colorTarget);
                const colorBtn = attachColorPicker(
                    colorTarget, row, PALETTE.map((c) => ({ color: c })),
                    (sw) => applyProfileColor(host, name, normalizeHex(sw.color)));
                colorBtn.title = 'default color for this profile';
                // #122: dot previews the color a terminal launched from this profile
                // would inherit — the per-profile color, else the host DEFAULT (#103),
                // else the base accent (the auto palette pick at launch). Mirrors the
                // resolution chain in 67_js_window_lifecycle.js.
                row.style.setProperty('--accent',
                    p.color || hostDefaultColor(host.id) || 'var(--accent-default)');
                acts.appendChild(colorBtn);
                if (p.color) {
                    const clr = document.createElement('button');
                    clr.type = 'button';
                    clr.textContent = '✕';
                    clr.title = 'clear default color (revert to host / auto '
                        + 'per-window colors)';
                    clr.addEventListener('click',
                        () => applyProfileColor(host, name, null));
                    acts.appendChild(clr);
                }
                row.appendChild(acts);
                setProfilesListEl.appendChild(row);
            }
        }
        function deleteProfile(host, name) {
            const cfg = profilesConfigCache.get(host.id);
            if (!cfg) return;
            const np = Object.assign({}, cfg.profiles);
            delete np[name];
            // The broker rejects an empty set (it would brick /launch); guard
            // here so the user gets a clear message instead of a 400.
            if (!Object.keys(np).length) {
                showNotice('cannot delete the last profile');
                return;
            }
            const def = (cfg.default_profile === name) ? '' : cfg.default_profile;
            saveProfilesConfig(host, np, def);
        }
        function saveProfilesConfig(host, profiles, defaultProfile) {
            if (!host) return;
            return hostFetch(host, '/profiles/config', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ profiles: profiles,
                                       default_profile: defaultProfile || '' }),
                // Well past the default deadline: the broker validates the whole
                // set, then does an ATOMIC sidecar write (temp + fsync + replace,
                // off the event loop) behind profiles_lock and live-swaps the
                // launcher. That is disk work under a lock, not a lookup — and a
                // client-side abort here would report "save failed" on a write
                // that in fact landed.
                timeoutMs: 8000,
            }).then(r => r.json().catch(() => null)).then(j => {
                if (j && j.ok) {
                    profilesConfigCache.set(host.id, j);
                    // Drop the names-only cache (76) so the launch picker + the
                    // Default-profile <select> refetch the live set — no restart.
                    profilesCache.delete(host.id);
                    const cur = profilesEditorHost();
                    if (cur && cur.id === host.id) {
                        renderProfilesEditor();
                        renderDefaultProfile();
                    }
                } else {
                    showNotice('profile save failed: ' + ((j && j.error) || 'error'));
                }
            }).catch(e => showNotice('profile save failed: ' + e));
        }
        // Add / Edit dialog. `editName` != null edits an existing profile (rename
        // allowed); `preset` (from Detect) pre-fills a fresh Add. The command is a
        // textarea, ONE argv token per line — avoids fragile quote-splitting: the
        // array joins to lines and splits back on newlines.
        function openProfileDialog(host, editName, preset) {
            // NEVER fall back to a phantom {profiles:{}}. saveProfilesConfig
            // POSTs the WHOLE set and the broker persists exactly what it is
            // sent (app.py _profiles_config_post writes `to_persist` verbatim),
            // so a Save built on an empty stand-in would REPLACE every real
            // profile on that broker with just the one being typed. If this
            // host's config never loaded we cannot edit it — say so.
            const cfg = profilesConfigCache.get(host.id);
            if (!cfg) {
                showNotice('profiles for this host aren’t loaded — retry first');
                return;
            }
            const base = preset || (editName ? (cfg.profiles[editName] || {}) : {});
            const initName = preset ? (preset.name || '') : (editName || '');
            let nameInput, titleInput, cwdInput, cmdArea;
            openDialog({
                title: editName ? 'Edit profile' : 'Add profile',
                body: function (c) {
                    const mkRow = (labelText, el) => {
                        const row = document.createElement('div');
                        row.className = 'set-row app-dialog-field';
                        const lab = document.createElement('label');
                        lab.textContent = labelText;
                        row.appendChild(lab); row.appendChild(el);
                        c.appendChild(row);
                    };
                    nameInput = document.createElement('input');
                    nameInput.type = 'text'; nameInput.value = initName;
                    nameInput.placeholder = 'name (letters, digits, . _ + -)';
                    mkRow('name', nameInput);
                    titleInput = document.createElement('input');
                    titleInput.type = 'text'; titleInput.value = base.title || '';
                    titleInput.placeholder = '(optional) window title';
                    mkRow('title', titleInput);
                    cwdInput = document.createElement('input');
                    cwdInput.type = 'text'; cwdInput.value = base.cwd || '';
                    cwdInput.placeholder = '(optional) start directory';
                    mkRow('cwd', cwdInput);
                    const cmdRow = document.createElement('div');
                    cmdRow.className = 'app-dialog-field set-profile-cmdfield';
                    const cmdLab = document.createElement('label');
                    cmdLab.textContent = 'command (one argument per line)';
                    cmdArea = document.createElement('textarea');
                    cmdArea.className = 'set-profile-cmdarea';
                    cmdArea.rows = 6;
                    cmdArea.value = (base.command || []).join('\n');
                    cmdArea.placeholder = 'wsl.exe\n-d\nUbuntu\n--\nbash\n-l';
                    cmdRow.appendChild(cmdLab); cmdRow.appendChild(cmdArea);
                    c.appendChild(cmdRow);
                },
                buttons: [
                    { label: 'Save', value: 'save', primary: true },
                    { label: 'Cancel', value: false },
                ],
            }).then(function (r) {
                if (!r || r.value !== 'save') return;
                const name = (nameInput.value || '').trim();
                const title = (titleInput.value || '').trim();
                const cwd = (cwdInput.value || '').trim();
                const command = (cmdArea.value || '').split('\n')
                    .map(s => s.trim()).filter(Boolean);
                if (!name) { showNotice('profile name is required'); return; }
                if (!command.length) { showNotice('command cannot be empty'); return; }
                const live = profilesConfigCache.get(host.id) || cfg;
                const np = Object.assign({}, live.profiles);
                if (editName && editName !== name) delete np[editName];
                // #115: preserve an existing per-profile default color across an
                // Edit (the dialog doesn't expose it — the color dot on the row
                // does), so editing the command/title/cwd never silently drops it.
                const prevColor = editName
                    ? ((live.profiles[editName] || {}).color || null) : null;
                np[name] = { command: command, title: title || null,
                             cwd: cwd || null, color: prevColor };
                // Renaming the default profile carries the default to the new name.
                let def = live.default_profile || '';
                if (editName && def === editName) def = name;
                saveProfilesConfig(host, np, def);
            });
        }
        // Detect: list the broker's environment scan; clicking a suggestion opens
        // the Add dialog pre-filled (openDialog's singleton cancels this one), so
        // nothing is saved until the user confirms.
        function detectProfiles(host) {
            // The scan shells out — on Windows `wsl -l -q` with its own timeout=8
            // (app.py), plus PATH probing — so the server's own worst case already
            // exceeds the default deadline. Give it room rather than aborting a
            // scan that was about to answer.
            hostFetch(host, '/profiles/detect', { timeoutMs: 15000 })
                .then(r => (r.ok ? r.json() : null))
                .then(j => {
                    const list = (j && j.ok && Array.isArray(j.suggestions))
                        ? j.suggestions : [];
                    openDialog({
                        title: 'Detected shells',
                        body: function (c) {
                            if (!list.length) {
                                const m = document.createElement('div');
                                m.className = 'app-dialog-msg';
                                m.textContent = 'No shells detected on this host.';
                                c.appendChild(m);
                                return;
                            }
                            const wrap = document.createElement('div');
                            wrap.className = 'set-detect-list';
                            for (const s of list) {
                                const b = document.createElement('button');
                                b.type = 'button'; b.className = 'set-detect-item';
                                const nm = document.createElement('div');
                                nm.className = 'set-profile-name';
                                nm.textContent = s.title || s.name;
                                const cm = document.createElement('div');
                                cm.className = 'set-profile-cmd';
                                cm.textContent = commandPreview(s.command);
                                b.appendChild(nm); b.appendChild(cm);
                                b.addEventListener('click', () =>
                                    openProfileDialog(host, null, s));
                                wrap.appendChild(b);
                            }
                            c.appendChild(wrap);
                        },
                        buttons: [{ label: 'Close', value: false }],
                    });
                })
                .catch(e => showNotice('detect failed: ' + e));
        }
        if (setProfileAddBtn) {
            setProfileAddBtn.addEventListener('click', () => {
                const host = profilesEditorHost();
                if (host) openProfileDialog(host, null, null);
            });
        }
        if (setProfileDetectBtn) {
            setProfileDetectBtn.addEventListener('click', () => {
                const host = profilesEditorHost();
                if (host) detectProfiles(host);
            });
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

        // #88: terminal × terminate toggle (per-host display setting, same shape
        // as stripScrollbar above). The × behavior + its affordance read the LOCAL
        // getSettings() — like stripScrollbar's effect — so reapply only for the
        // local tab. The companion confirm checkbox follows the toggle's grey/live
        // state immediately; its stored value is kept (re-enabling restores it).
        setCloseTerminates.addEventListener('change', () => {
            const t = settingsTarget;
            if (!t) return;
            t.s.terminalCloseTerminates = setCloseTerminates.checked;
            t.save();
            setConfirmTerminate.disabled = !setCloseTerminates.checked;
            if (t.isLocal) applyDisplaySettings();   // live re-title open terminal × buttons
        });
        // #153: authorize OSC 52 clipboard writes for the host this tab belongs
        // to. Writes the browser-local map keyed by t.hostId — deliberately NOT
        // t.s/t.save(), so the switch that lets a broker write your clipboard
        // is never itself reachable from a broker. Takes effect on the next
        // sequence; nothing to reapply.
        setOsc52.addEventListener('change', () => {
            // currentSettingsTab, NOT settingsTarget: the target is null while a
            // remote's /state is in flight and stays null if that fetch fails,
            // and a browser-local grant has no business being ungrantable
            // because the far end is unreachable.
            const hostId = currentSettingsTab;
            if (!hostId || hostId === 'browser') return;
            setOsc52Allowed(hostId, setOsc52.checked);
            setOsc52.checked = isOsc52Allowed(hostId);   // reflect what stuck
        });

        setConfirmTerminate.addEventListener('change', () => {
            const t = settingsTarget;
            if (!t) return;
            t.s.terminalCloseConfirm = setConfirmTerminate.checked;
            t.save();
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

        // #125: per-broker viewport-slide rate (ms per screen-width). Like snapHold
        // there's no live side effect — scrollColumnIntoView reads getSettings()
        // fresh on each switch — so we just normalize (0 = instant; else clamp
        // [120,2000]; blank/NaN -> the 350 default), persist to the target's blob,
        // and echo the clamped value back so the field shows what was stored.
        setSlideMs.addEventListener('change', () => {
            const t = settingsTarget;
            if (!t) return;
            const raw = setSlideMs.value.trim();
            let v;
            if (raw === '') {
                v = 350;
            } else {
                v = Math.round(Number(raw));
                if (!isFinite(v)) v = 350;
                else if (v <= 0) v = 0;                     // explicit instant
                else v = Math.max(120, Math.min(2000, v));
            }
            t.s.slideScreenMs = v;
            setSlideMs.value = v;                           // reflect normalization
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

        // Appearance (start label): browser-local like restore-on-refresh — write
        // the LIVE local getSettings() directly (NOT settingsTarget, which may point
        // at a remote host), persist, then apply to this browser immediately. This is
        // why the Control Panel window (#59) can host a remote host's tab without
        // appearance edits leaking to it.
        // #75/#76/#126: the color-scheme radio's, background-pattern select's, and
        // terminal-font select's change handlers now live in their mods (ctx.settings.
        // radio / ctx.settings.select wire #set-mods -> savePrefs + applyTheme /
        // applyPattern / applyTerminalFont; the theme mod's apply also re-applies the
        // theme-var-aware pattern).
        // #71/#78: the clock and Help-button toggle change handlers now live in
        // their mods (ctx.settings.boolean wires each #set-mods checkbox to
        // savePrefs + re-apply). Core only reflects them on render
        // (renderModSettingsToggles).
        const commitStartLabel = () => {
            const v = (setStartLabelEl.value || '').trim().slice(0, 24) || '+';
            getSettings().startLabel = v;
            savePrefs();
            applyStartButton();
        };
        setStartLabelEl.addEventListener('input', commitStartLabel);
        setStartLabelEl.addEventListener('change', commitStartLabel);
        // #114: swap the START (+) button's left/right-click gestures. Browser-
        // local like the start label — write LIVE local getSettings(), persist,
        // then re-apply so the tooltip's right-click hint tracks the new mapping.
        setSwapLaunchEl.addEventListener('change', () => {
            getSettings().swapLaunchButtons = setSwapLaunchEl.checked;
            savePrefs();
            applyStartButton();
        });
        // #178: the broker-status chip's visibility mode. Write LIVE local
        // getSettings() like the two above, persist, then converge through
        // applyDisplaySettings — the same door a peer browser's /state adopt
        // uses, so both routes end at exactly one applyHostStatusVisibility.
        setHostStatusChipEl.addEventListener('change', () => {
            getSettings().hostStatusChip = setHostStatusChipEl.value;
            savePrefs();
            applyDisplaySettings();
        });
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

