        // ---- shared server state (/state) ---------------------------------
        // The same-origin broker's /state is the source of truth for the two
        // synced slices, prefs._settings and prefs._layout; localStorage is the
        // offline cache/fallback. Optimistic concurrency on an integer rev:
        // pushes carry the last-seen baseRev and a 409 means another browser
        // won — we adopt its state and converge (a simultaneous edit's loser
        // yields, which is exactly the task-2 "both viewers reflect the same
        // tiling" behaviour). prefs._hosts (where to connect) and per-session
        // pixel geometry are deliberately excluded — they are per-browser.
        // ---- single-active-browser lease (F-ACTIVECLIENT) -----------------
        // _deactivated is the linchpin: while this browser is torn down
        // (another browser holds the HOME lease) it gates EVERY shared-state
        // mutation — no /state push, no taskbar tick / pull, no relayout — so a
        // background view can never clobber the active one. _homeActive mirrors
        // the HOME broker's lease (the only lease that drives the full-page
        // overlay/teardown). _booted guards the one-time heavy boot;
        // _slowPollTimer holds the slow refreshTaskbar interval so a teardown
        // can stop it. savePrefsLocal (localStorage only) is intentionally NOT
        // gated — the offline cache may always update.
        let _deactivated = false;
        let _homeActive = false;
        let _booted = false;
        let _slowPollTimer = null;
        // Monotonic lifecycle token: bumped on every teardown / boot / rebuild
        // so a stale async rebuild (active->inactive->active flips race its
        // pullState) bails after its await instead of double-restoring or
        // applying an out-of-order /state.
        let _viewEpoch = 0;
        let _stateRev = 0;
        let _stateReady = false;       // initial pull done; gates pushes
        let _statePendingPush = false; // a save fired before the initial pull
        let _statePushTimer = null;
        let _statePushInFlight = false;
        let _statePushAgain = false;
        let _stateApplying = false;    // suppress echo while applying server state
        let _stateLastSerialized = null;
        let _lastStatePoll = 0;        // throttle /state polling to the slow tick
        let _lastHostStatePoll = 0;    // throttle REMOTE-host settings prefetch
        const _EMPTY_STATE_JSON = JSON.stringify({ settings: {}, layout: {} });

        function _stateBlob() {
            return {
                settings: (prefs._settings && typeof prefs._settings === 'object'
                    && !Array.isArray(prefs._settings)) ? prefs._settings : {},
                layout: (prefs._layout && typeof prefs._layout === 'object'
                    && !Array.isArray(prefs._layout)) ? prefs._layout : {},
            };
        }
        function _stateSerialize() { return JSON.stringify(_stateBlob()); }

        function schedulePush() {
            if (_deactivated) return;            // torn down — never push state
            if (_stateApplying) return;          // don't echo server state back
            if (!_stateReady) { _statePendingPush = true; return; }
            if (_statePushTimer) return;
            _statePushTimer = setTimeout(() => {
                _statePushTimer = null;
                pushState();
            }, 600);
        }

        async function pushState() {
            if (_deactivated) return;            // torn down — never push state
            if (_statePushInFlight) { _statePushAgain = true; return; }
            const serialized = _stateSerialize();
            if (serialized === _stateLastSerialized) return;  // nothing changed
            _statePushInFlight = true;
            try {
                const blob = _stateBlob();
                const r = await hostFetch(localHost(), '/state', {
                    method: 'PUT',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        baseRev: _stateRev,
                        settings: blob.settings,
                        layout: blob.layout,
                        clientId: CLIENT_ID,   // lease: only the active browser
                    }),                        // may write (else 409 not_active)
                });
                // Torn down while this PUT was in flight (lost the lease): drop
                // the result so a 409 not_active never re-renders into the
                // overlay the teardown just put up.
                if (_deactivated) return;
                if (r.status === 409) {
                    // Another browser won the race — its state is inlined so we
                    // converge in one round trip (no second GET). Adopt it ONLY
                    // if we have no NEWER local edit than the payload we just
                    // sent; otherwise keep the local edit and rebase onto the
                    // winner's rev so the queued re-push re-sends it (the active
                    // editor wins; an idle loser yields). _stateSerialize() now
                    // differs from `serialized` exactly when the user edited
                    // during the in-flight PUT (the Codex lost-update case).
                    const cur = await r.json();
                    if (cur && typeof cur === 'object') {
                        _stateRev = (typeof cur.rev === 'number') ? cur.rev : _stateRev;
                        if (!_statePushAgain && _stateSerialize() === serialized) {
                            _applyServerState(cur);          // no local edit pending — yield
                        } else {
                            _stateLastSerialized = null;     // keep local; force the re-push
                        }
                    }
                } else if (r.ok) {
                    const resp = await r.json();
                    if (resp && typeof resp.rev === 'number') _stateRev = resp.rev;
                    _stateLastSerialized = serialized;
                }
                // 401 (local broker needs auth) / other: keep localStorage; the
                // existing /sessions auth flow handles login, next save retries.
            } catch (e) {
                // Offline — localStorage already holds it; retry on next save.
            } finally {
                _statePushInFlight = false;
                if (_statePushAgain) { _statePushAgain = false; schedulePush(); }
            }
        }

        // Adopt server-provided settings+layout into prefs and re-render. Guards
        // against re-pushing what we just adopted (_stateApplying).
        function _applyServerState(srv) {
            _stateApplying = true;
            try {
                prefs._settings = (srv.settings && typeof srv.settings === 'object'
                    && !Array.isArray(srv.settings)) ? srv.settings : {};
                prefs._layout = (srv.layout && typeof srv.layout === 'object'
                    && !Array.isArray(srv.layout)) ? srv.layout : {};
                getSettings();                  // self-heal the adopted blob
                getLayout();                    // reconcile the adopted layout
                savePrefsLocal();               // cache, do NOT push
                _stateLastSerialized = _stateSerialize();
                document.body.classList.toggle('size-locked', isSizeLocked());
                applyDisplaySettings();
                try { renderWorkspaces(); } catch (_) {}
                requestRelayout();
                // An adopted layout can change activeWs / membership, so the
                // floating-window workspace mask and the taskbar ws badges must
                // be reconciled now, not left stale until the next 2s poll
                // (Codex review). Track A helpers; guarded for load order.
                try { applyWorkspaceVisibility(); } catch (_) {}
                try { applyTaskbarWorkspace(); } catch (_) {}
                // A remote browser may have edited the section library; re-render
                // any open Sections panel + AGENTS.md checklist (last-writer-wins).
                try { refreshOpenSectionUIs(); } catch (_) {}
            } finally {
                _stateApplying = false;
            }
        }

        // Pull /state. initial=true on boot: seed _stateRev, adopt server state
        // when it has any (rev>0), or seed the server from localStorage when the
        // broker is fresh (rev 0). Later polls only re-apply when rev changed
        // (another browser wrote) — our own pushes advance _stateRev so they
        // never re-trigger.
        async function pullState(initial) {
            let r;
            try {
                const ctrl = new AbortController();
                const timer = setTimeout(() => ctrl.abort(), FETCH_TIMEOUT_MS);
                try {
                    r = await hostFetch(localHost(), '/state',
                        { cache: 'no-store', signal: ctrl.signal });
                } finally { clearTimeout(timer); }
            } catch (e) { return; }              // offline — keep localStorage
            if (!r.ok) return;                   // 401/5xx — handled elsewhere
            let srv;
            try { srv = await r.json(); } catch (e) { return; }
            if (!srv || typeof srv !== 'object') return;
            const srvRev = (typeof srv.rev === 'number') ? srv.rev : 0;
            if (srvRev === 0) {
                if (initial) {
                    _stateRev = 0;
                    const localSerialized = _stateSerialize();
                    if (localSerialized !== _EMPTY_STATE_JSON) {
                        _stateLastSerialized = null;   // force a seeding push
                    } else {
                        _stateLastSerialized = localSerialized;
                    }
                }
                return;
            }
            if (!initial && srvRev === _stateRev) return;   // unchanged
            const srvSerialized = JSON.stringify({
                settings: srv.settings || {}, layout: srv.layout || {} });
            _stateRev = srvRev;                  // take the new rev either way
            if (!initial && srvSerialized === _stateLastSerialized) return;
            // A background pull must NOT clobber an in-progress local edit
            // (Codex lost-update case). With unpushed local changes, keep them:
            // _stateRev now points at the server's rev, so the pending debounced
            // push rebases onto it and wins (active editor wins; convergence
            // happens once this browser goes idle and a later pull finds it
            // clean). Never skips on the initial boot sync.
            if (!initial && _stateSerialize() !== _stateLastSerialized) return;
            _applyServerState(srv);
        }

