        // ---- single-active view lifecycle (F-ACTIVECLIENT) ---------------
        // Boot defers the heavy restore (app windows, deep-link, restore queue,
        // session polling) into bootActiveView(), which runs ONCE on the first
        // HOME {active:true}. A torn-down browser (lost the lease) reaches
        // teardownView(); reactivating rebuilds from the untouched /state via
        // rebuildView(). _stateReadyPromise gates the restore on the initial
        // /state adopt so the ADOPTED openTerms/layout is used (the control WS
        // status and the /state pull race on boot).
        let _markStateReady;
        const _stateReadyPromise = new Promise(r => { _markStateReady = r; });

        function startSlowPoll() {
            if (_slowPollTimer) return;
            _slowPollTimer = setInterval(refreshTaskbar, POLL_MS);
        }

        // Re-create persisted client-only app windows (sticky notes / text
        // editors). They have no server session, so they are created directly
        // rather than awaited from /sessions. Extracted from boot so a rebuild
        // re-runs the exact same path.
        function restoreAppWindows() {
            for (const appId of Object.keys(appStore)) {
                const rec = appStore[appId];
                // Skip docs the user closed (open:false): they live in the store
                // and reopen from the launch (+) menu's "Closed" list. Legacy
                // records predate the flag (open === undefined) — auto-open
                // those so nothing silently vanishes on upgrade.
                if (rec && rec.open === false) continue;
                // Ephemeral kinds (task manager / control panel / help: registered
                // without a serialize) are never persisted by saveAppWindow, but a
                // hand-edited/corrupt store could carry a stale record — never auto-
                // recreate those (#80/S7). An UNKNOWN/unregistered kind still goes
                // through openAppWindow (-> the note/editor default), exactly as
                // before. A kind may supply its own restore; else openAppWindow.
                const kind = lookupWindowKind(rec && rec.appKind);
                if (kind && !kind.serialize) continue;
                try { (kind && kind.restore ? kind.restore : openAppWindow)(rec); }
                catch (e) { console.warn('app window restore failed', appId, e); }
            }
        }

        // Restore-on-refresh: seed the terminals that were open before, so the
        // normal openWindow→attach path reattaches them (the broker replays
        // each PTY's snapshot/backscroll). Fold any terminal already opened
        // (a ?session= deep link) back into the set first. Extracted from boot.
        function seedRestoreQueue() {
            if (!getSettings().restoreOnRefresh) return;
            for (const [key, w] of windows) {
                if (!w.disposed && w.type !== 'app') addOpenTerm(key);
            }
            const deadline = Date.now() + AUTO_OPEN_TIMEOUT_MS;
            let seeded = false;
            for (const key of getOpenTerms()) {
                if (windows.has(key) || pendingOpens.has(key)) continue;
                pendingOpens.set(key, deadline);
                restoreKeys.add(key);
                seeded = true;
            }
            if (seeded) startFastPoll();
        }

        // First HOME activation: the heavy restore the boot path used to run
        // unconditionally. Guarded by _booted; awaits the /state adopt so the
        // adopted openTerms/layout drives the restore.
        async function bootActiveView() {
            if (_booted) return;
            _booted = true;
            _deactivated = false;
            const epoch = ++_viewEpoch;
            hideBecomeActiveOverlay();
            const taskbarEl = document.getElementById('taskbar');
            if (taskbarEl) taskbarEl.style.display = '';
            await _stateReadyPromise;
            // Superseded (deactivated, or a later boot/rebuild) mid-boot — abort.
            if (_deactivated || epoch !== _viewEpoch) return;
            restoreAppWindows();
            // ?session=<id> deep link: auto-open once it appears in /sessions.
            // A bare numeric id means the local host (links predate multi-host).
            const deepLink = params.get('session');
            if (deepLink) {
                const key = String(deepLink).indexOf(':') === -1
                    ? 'local:' + deepLink : String(deepLink);
                pendingOpens.set(key, Date.now() + AUTO_OPEN_TIMEOUT_MS);
                startFastPoll();
            }
            seedRestoreQueue();
            refreshTaskbar();
            startSlowPoll();
        }

        // Lost the HOME lease: dispose THIS browser's entire view WITHOUT
        // calling closeWindow (which mutates openTerms/layout/savePrefs). This
        // reimplements only the *disposal* half of closeWindow's teardown
        // (see closeWindow): openTerms + layout live solely in /state and are
        // never touched here, so rebuildView restores the view identically.
        function teardownView() {
            _deactivated = true;             // gate every shared-state mutation
            _viewEpoch++;                    // supersede any in-flight boot/rebuild
            stopFastPoll();
            if (_slowPollTimer) { clearInterval(_slowPollTimer); _slowPollTimer = null; }
            if (_statePushTimer) { clearTimeout(_statePushTimer); _statePushTimer = null; }
            pendingOpens.clear();
            restoreKeys.clear();
            for (const win of windows.values()) {
                win.disposed = true;
                // App docs (sticky note / editor) autosave to appStore on a
                // debounce that the cleanups below clear. Flush the live buffer
                // NOW (capturing a multi-tab editor's active doc first) so a
                // lease loss within the debounce window never loses edits across
                // the rebuild. Do NOT mark open:false — rebuild reopens them.
                if (win.type === 'app') {
                    try { if (win._captureActiveDoc) win._captureActiveDoc(); } catch (_) {}
                    try { saveAppWindow(win); } catch (_) {}
                }
                if (win.resizeTimer) { clearTimeout(win.resizeTimer); win.resizeTimer = null; }
                for (const fn of (win.cleanups || [])) { try { fn(); } catch (_) {} }
                win.cleanups = [];
                if (win.ws) {
                    try {
                        win.ws.onopen = win.ws.onmessage = null;
                        win.ws.onclose = win.ws.onerror = null;
                    } catch (_) {}
                    try { win.ws.close(); } catch (_) {}
                    win.ws = null;
                }
                if (win.term) { try { win.term.dispose(); } catch (_) {} }
                try { win.dom.remove(); } catch (_) {}
            }
            windows.clear();
            frontId = null;
            try { sessions.clear(); } catch (_) {}
            const taskbarEl = document.getElementById('taskbar');
            if (taskbarEl) taskbarEl.style.display = 'none';
            const stripEl = document.getElementById('strip');
            if (stripEl) stripEl.textContent = '';
            // The custom scrollbar is a sibling of #strip, so emptying #strip
            // leaves it; hide it explicitly (_deactivated is already true, so
            // updateStripScrollbar resolves to hidden).
            updateStripScrollbar();
            const itemsEl = document.getElementById('taskbar-items');
            if (itemsEl) itemsEl.textContent = '';
            const desktopEl = document.getElementById('desktop');
            if (desktopEl) desktopEl.classList.add('empty');
            showBecomeActiveOverlay();
        }

        // Reactivated: rebuild from the untouched server state. Re-adopt /state
        // (openTerms/layout), then re-run the same restore path as boot. A
        // deactivation that races in mid-rebuild is caught by the _deactivated
        // guards after each await.
        async function rebuildView() {
            _deactivated = false;
            const epoch = ++_viewEpoch;
            hideBecomeActiveOverlay();
            const taskbarEl = document.getElementById('taskbar');
            if (taskbarEl) taskbarEl.style.display = '';
            try { await pullState(true); } catch (_) {}
            // Superseded mid-rebuild (re-deactivated, or a newer rebuild raced
            // its pullState): bail so we never double-restore or apply an
            // out-of-order /state.
            if (_deactivated || epoch !== _viewEpoch) return;
            _stateReady = true;
            try { applyDisplaySettings(); } catch (_) {}
            try { renderWorkspaces(); } catch (_) {}
            restoreAppWindows();
            seedRestoreQueue();
            refreshTaskbar();
            startSlowPoll();
        }

        // Remote-host lease status. The HOME lease drives the page (above); a
        // remote broker's lease only masks that host's windows + flips its
        // taskbar chip. Implemented in the multi-host section.
        function onRemoteControlStatus(hostId, active, msg) {
            applyRemoteLease(hostId, active);
        }
        // A REMOTE broker reported our lease status. Unlike the HOME lease this
        // never tears down the page — it only masks that host's windows + chips
        // (reusing the focus-mode hostHidden/applyHostVisibilityAll path) and
        // flips its taskbar chip to the click-to-take-over 'lease' state. The
        // PTYs/agents keep running on that broker; reactivating unmasks them.
        function applyRemoteLease(hostId, active) {
            const st = pollStateFor(hostId);
            const next = !active;                  // inactive -> masked
            if (!!st.leaseInactive === next) return;   // no change
            st.leaseInactive = next;
            applyHostVisibilityAll();
            updateTaskbarActive();
            renderHostStatus();
        }

