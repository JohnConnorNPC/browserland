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
        // #167: the records THIS view generation's restore had to skip because
        // their appKind was not registered yet — the mod loader is an independent
        // async chain (see restoreAppWindowsAfterMods). Shape: { epoch, ids:Set },
        // published by restoreAppWindows for exactly ONE _viewEpoch, so a
        // superseded or torn-down generation's leftovers can never be replayed
        // into the next one. null until the first restore completes, which is also
        // what lets the retry tell "this generation is fully restored" from
        // "boot/rebuild is still mid-flight": _booted cannot, because
        // bootActiveView sets it BEFORE its `await _stateReadyPromise`, and
        // neither can _stateReady, which latches true for the whole page life on
        // the first /state adopt and so says nothing about a rebuild in flight.
        let _deferredRestore = null;
        // Re-entrancy latch for that retry, plus its wakeup bit. A window factory
        // is MOD code running synchronously inside the loop, and it can reach back
        // into the loader (setModEnabled -> _bringUp -> retry). Without the latch a
        // nested pass could start building a record whose outer build has not
        // reached windows.set yet — openAppWindow's dedup-by-id only fires once the
        // window EXISTS. But swallowing the nested request outright would trade the
        // duplicate for a LOST WAKEUP: the nested call may be the only notice that
        // an id the outer loop has already walked past is now buildable. So a
        // request that arrives while busy sets the bit and the outer loop drains
        // again (codex).
        let _restoreRetrying = false;
        let _restoreRetryAgain = false;

        function startSlowPoll() {
            if (_slowPollTimer) return;
            _slowPollTimer = setInterval(refreshTaskbar, POLL_MS);
        }

        // Re-create persisted client-only app windows (sticky notes / text
        // editors). They have no server session, so they are created directly
        // rather than awaited from /sessions. Extracted from boot so a rebuild
        // re-runs the exact same path.
        function restoreAppWindows() {
            // Capture the generation UP FRONT (#167). A factory / restore hook is
            // mod code running synchronously in this loop and could re-enter the
            // lifecycle — rebuildView bumps _viewEpoch and then parks on its
            // pullState — so reading _viewEpoch at the END could certify a
            // generation this pass never actually restored.
            const epoch = _viewEpoch;
            const deferred = new Set();
            for (const appId of Object.keys(appStore)) {
                _restoreOneAppWindow(appId, deferred);
            }
            // Publish the skipped set for this generation only, and only if we are
            // still it — a superseded pass leaves the newer one's record alone.
            if (!_deactivated && epoch === _viewEpoch) {
                _deferredRestore = { epoch: epoch, ids: deferred };
                // ...then drain it once, right now. The set does not exist until
                // this line, so a kind that became registered DURING the loop above
                // (mod code in a factory enabling another mod) raced past the
                // cursor with nothing left to trigger it — the same back-edge the
                // retry solves within itself. Costs one extra failed attempt per
                // still-unregistered record at boot, which is a null return with no
                // side effects (codex).
                restoreAppWindowsAfterMods();
            }
        }

        // Restore ONE persisted record. Split out (#167) so the generation restore
        // above and the post-loader retry below can never drift apart. `deferred`
        // collects the ids that the mod-loader race — and ONLY that race — skipped.
        function _restoreOneAppWindow(appId, deferred) {
            const rec = appStore[appId];
            // Skip docs the user closed (open:false): they live in the store
            // and reopen from the launch (+) menu's "Closed" list. Legacy
            // records predate the flag (open === undefined) — auto-open
            // those so nothing silently vanishes on upgrade.
            if (!rec || rec.open === false) return;
            // Ephemeral kinds (task manager / control panel / help: registered
            // without a serialize) are never persisted by saveAppWindow, but a
            // hand-edited/corrupt store could carry a stale record — never auto-
            // recreate those (#80/S7). An UNKNOWN/unregistered kind still goes
            // through openAppWindow (-> the note/editor default), exactly as
            // before. A kind may supply its own restore; else openAppWindow.
            const kind = lookupWindowKind(rec.appKind);
            if (kind && !kind.serialize) return;
            // restoring: this is the ONE automatic creation path, so each
            // window keeps the workspace it was on rather than being re-homed
            // to whichever workspace the page happens to boot on (#152).
            let win = null;
            let threw = false;
            try {
                win = (kind && kind.restore ? kind.restore : openAppWindow)(
                    rec, { restoring: true });
            } catch (e) { threw = true; console.warn('app window restore failed', appId, e); }
            // #167: the kind was UNREGISTERED and the build returned null WITHOUT
            // throwing. That is the loader race, not a failure — buildAppWindow
            // deliberately returns null for a persisted non note/editor kind it
            // does not know, leaving the record intact rather than coercing it into
            // a note. Remember exactly these, so the retry below re-attempts them
            // and NOTHING else: a REGISTERED kind is attempted once per generation,
            // which keeps a custom `restore` hook (only a registered kind can have
            // one) on its existing once-per-restore contract instead of silently
            // widening it. `threw` has to be tracked separately or a note/editor
            // fallback that THREW would be indistinguishable from that null and
            // would be re-attempted on every later mod enable, replaying whatever
            // partial side effects it managed before throwing (codex).
            if (!kind && !win && !threw && deferred) deferred.add(appId);
        }

        // #167: the retry, run once the mod loader has settled (and again after a
        // mid-session mod enable — see 86_js_mod_loader).
        //
        // Restore and the mod loader are two INDEPENDENT async chains: restore
        // waits on the control-WS lease + the /state adopt, loadMods() waits on
        // GET /info, and nothing sequences them. Whichever loses, a persisted
        // record whose mod-owned kind is not registered YET was dropped for the
        // whole page load. The exposed kinds are the persisted mod-owned ones:
        // file-manager and scratchpad.
        //
        // Deliberately NOT the other direction. Gating bootActiveView on mod
        // readiness would put /info on the critical path of window restore, the
        // ?session= deep link, seedRestoreQueue AND startSlowPoll — hostFetch
        // bounds /info's HEADERS at FETCH_TIMEOUT_MS but its body read is
        // unbounded, so a half-dead broker would trade a missing window for a
        // dead desktop. Nor restore-on-registerWindowKind: that registry is
        // re-entrant (lookupWindowKind -> _windowKindRegistry -> registerBuiltin
        // WindowKinds -> registerWindowKind) and would fire mid-init(ctx), before
        // a throw could roll the mod back.
        //
        // Safe to skip, so every caller may treat it as best-effort: if the guards
        // reject it because boot has not restored yet, boot's own restore then runs
        // with the mod kinds already registered — and boot drains this once itself
        // afterwards, so a kind registered mid-loop is not stranded either. It
        // cannot recover a loadMods
        // that NEVER settles (an /info whose body read hangs — hostFetch bounds
        // only the headers), but neither could anything else: the kinds never
        // register either. A later host login re-runs the policy apply, which
        // calls back in here.
        function restoreAppWindowsAfterMods() {
            // Torn down (another browser holds the lease): never build windows
            // behind the "Become active" overlay.
            if (_deactivated) return;
            // The initial /state adopt must have landed — restoring against a
            // layout the adopt has not applied is the trap in keying this on
            // _booted, which bootActiveView sets BEFORE it awaits that adopt.
            // Implied by a published _deferredRestore below; kept explicit because
            // it is the invariant, not a side effect of the bookkeeping.
            if (!_stateReady) return;
            // Already draining: record the wakeup instead of nesting, so an id the
            // running loop has walked past is revisited rather than stranded.
            if (_restoreRetrying) { _restoreRetryAgain = true; return; }
            // ...and the skipped set must belong to the CURRENT generation. That
            // rules out both halves of the same trap: a boot still parked on
            // `await _stateReadyPromise`, and a rebuildView still parked on its
            // pullState (where _stateReady is stale-true from the boot adopt).
            const pending = _deferredRestore;
            if (!pending || pending.epoch !== _viewEpoch || !pending.ids.size) return;
            const epoch = pending.epoch;
            _restoreRetrying = true;
            try {
                // Bounded drain: only a re-entrant request (mod code) can ask for
                // another round, and the cap keeps a pathological mod from spinning
                // here forever rather than trusting it to settle.
                for (let round = 0; round < 8; round++) {
                    _restoreRetryAgain = false;
                    for (const appId of Array.from(pending.ids)) {
                        // Re-check the generation EVERY record, not just up front:
                        // a factory can synchronously tear this view down or start a
                        // rebuild, and building the rest of an old generation's
                        // records after teardownView cleared `windows` would leave
                        // them stranded behind the inactive overlay (codex).
                        if (_deactivated || _viewEpoch !== epoch) return;
                        // Drop BEFORE building, re-add only if it is STILL blocked on
                        // an unregistered kind: an id must never be reachable while
                        // its own build is in flight, and a kind that never registers
                        // (a renamed/removed mod) must stay retryable without ever
                        // duplicating a window. A throw from anything OUTSIDE the
                        // builder (a registry lookup) must put the id back too, or
                        // delete-before-build turns into delete-and-lose.
                        pending.ids.delete(appId);
                        try { _restoreOneAppWindow(appId, pending.ids); }
                        catch (e) {
                            pending.ids.add(appId);
                            console.warn('app window restore retry failed', appId, e);
                        }
                    }
                    if (!_restoreRetryAgain) break;
                }
            } finally { _restoreRetrying = false; _restoreRetryAgain = false; }
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

