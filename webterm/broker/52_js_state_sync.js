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
        // #246: the last push of a real edit failed (5xx, 401, offline, an
        // unusable 409) and nothing re-queued it. "Next save retries" -- so
        // the edit still counts as pending and a pull must not adopt it away.
        // A 400/413 is a verdict on the payload, which can never land, so it
        // clears it. Also cleared by a landed push, by any adopt, by a push or
        // pull that finds nothing left to send, and never set by a PUT that
        // outlived its view (_viewEpoch).
        let _statePushFailed = false;
        // #246 review: a rev was taken WITHOUT adopting its state (a pull
        // skipped for a pending push, or a 409 kept the local edit). If that
        // push then sends nothing (the edit undone inside the debounce, a
        // 400/413), the rev must be reconsidered: polls keep evaluating it
        // instead of returning "unchanged". Cleared once this tab and the
        // server agree (an adopt, a landed PUT, an acknowledgement).
        let _stateRevSkipped = false;
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
        // #247 item 4 / N1: every rev a response carries is a safe
        // non-negative integer, and a state body (a GET 200, a 409) also
        // carries object settings and layout -- a proxy's `409 {}` or
        // `{rev: 7}` must never be adopted as an EMPTY state.
        function _stateRevOk(v) { return Number.isSafeInteger(v) && v >= 0; }
        function _stateBodyOk(b) {
            const obj = v => !!v && typeof v === 'object' && !Array.isArray(v);
            return obj(b) && _stateRevOk(b.rev) && obj(b.settings) && obj(b.layout);
        }
        // #247 items 1/1b: a single-flight /state channel. At most ONE /state
        // request (GET or PUT) is outstanding per tab, so the broker handles
        // them in the order this tab sent them and no response can carry a
        // rev older than one already observed -- with no rev comparisons, so
        // a genuine rollback is still followed. A background poll is skipped
        // while the channel is busy; a push or an initial pull waits for it.
        // Acquire = wait, then RE-CHECK with no await in between (returning
        // from an async helper yields a tick even when the channel was free,
        // and another requester may take it in that tick):
        //   do { await _stateWaitChannel(); } while (_stateChannel);
        // then claim it synchronously. Only the owner releases it.
        let _stateChannel = null;
        async function _stateWaitChannel() {
            while (_stateChannel) { try { await _stateChannel; } catch (_) {} }
        }

        function schedulePush() {
            if (_deactivated) return;            // torn down — never push state
            if (isDetachedSurface()) return;     // #226: one writer, the desktop
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
            if (isDetachedSurface()) return;     // #226: one writer, the desktop
            if (_statePushInFlight) { _statePushAgain = true; return; }
            _statePushInFlight = true;
            const epoch = _viewEpoch;
            let mine = null;
            let release = null;
            try {
                // #247 single-flight: wait for the /state channel, then claim
                // it with no await in between (see _stateWaitChannel).
                do { await _stateWaitChannel(); } while (_stateChannel);
                if (_deactivated) return;
                const serialized = _stateSerialize();
                if (serialized === _stateLastSerialized) {        // nothing changed
                    _statePushFailed = false;    // (an undone edit owes nothing)
                    return;
                }
                mine = new Promise(r => { release = r; });
                _stateChannel = mine;
                let out = null;
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
                    const body = (r.status === 409 || r.ok) ? await r.json() : null;
                    out = { status: r.status, ok: r.ok, body: body };
                } catch (e) {
                    out = null;
                } finally {
                    if (_stateChannel === mine) _stateChannel = null;
                    release();
                    release = null;
                }
                // Everything below runs synchronously: a request waiting on the
                // channel resumes only after this response has been processed.
                if (!out) {
                    // Offline — localStorage already holds it; retry on next save.
                    // (The PUT may still have landed: pullState acknowledges that.)
                    if (epoch === _viewEpoch) _statePushFailed = true;
                    return;
                }
                // Torn down while this PUT was in flight (lost the lease): drop
                // the result so a 409 not_active never re-renders into the
                // overlay the teardown just put up.
                if (_deactivated) return;
                if (out.status === 409) {
                    // Another browser won the race — its state is inlined so we
                    // converge in one round trip (no second GET). Adopt it ONLY
                    // if we have no NEWER local edit than the payload we just
                    // sent; otherwise keep the local edit and rebase onto the
                    // winner's rev so the queued re-push re-sends it (the active
                    // editor wins; an idle loser yields). _stateSerialize() now
                    // differs from `serialized` exactly when the user edited
                    // during the in-flight PUT (the Codex lost-update case).
                    const cur = out.body;
                    // #248 A4: refused for the LEASE (not a stale rev): a hint
                    // that this page may have lost it unawares. Ask the broker;
                    // the adopt below is unchanged.
                    if (cur && cur.error === 'not_active'
                            && typeof probeControl === 'function') {
                        probeControl(homeHostId());
                    }
                    if (_stateBodyOk(cur)) {
                        _stateRev = cur.rev;
                        if (!_statePushAgain && _stateSerialize() === serialized) {
                            _applyServerState(cur);          // no local edit pending — yield
                        } else {
                            _stateLastSerialized = null;     // keep local; force the re-push
                            _stateRevSkipped = true;         // took cur.rev, did not adopt it
                        }
                    } else if (epoch === _viewEpoch) {
                        _statePushFailed = true;     // unusable body: still owed
                    }
                } else if (out.ok) {
                    const resp = out.body;
                    if (!resp || !_stateRevOk(resp.rev)) {
                        // A 200 without a usable rev taught us nothing: the PUT
                        // may or may not have landed, so it is still owed (the
                        // next pull acknowledges it if it did).
                        if (epoch === _viewEpoch) _statePushFailed = true;
                        return;
                    }
                    _stateRev = resp.rev;
                    _stateLastSerialized = serialized;
                    _statePushFailed = false;
                    _stateRevSkipped = false;
                } else if (out.status === 400 || out.status === 413) {
                    // A verdict on this payload (which carries any older
                    // failed edit too): it can never land, so nothing is owed.
                    _statePushFailed = false;
                } else if (epoch === _viewEpoch) {
                    // 401 (local broker needs auth) / other: keep localStorage;
                    // the existing /sessions auth flow handles login, next
                    // save retries.
                    _statePushFailed = true;
                }
            } finally {
                _statePushInFlight = false;
                if (release) { if (_stateChannel === mine) _stateChannel = null; release(); }
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
                // #226: the detached surface adopts SETTINGS only; its layout
                // is the fixed one-float layout 51 installed, never the
                // desktop's columns (which may well hold the key it shows).
                prefs._layout = isDetachedSurface() ? { mode: 'floating' }
                    : (srv.layout && typeof srv.layout === 'object'
                        && !Array.isArray(srv.layout)) ? srv.layout : {};
                getSettings();                  // self-heal the adopted blob
                getLayout();                    // reconcile the adopted layout
                savePrefsLocal();               // cache, do NOT push
                _stateLastSerialized = _stateSerialize();
                _statePushFailed = false;       // the adopted state supersedes it
                _stateRevSkipped = false;
                document.body.classList.toggle('size-locked', isSizeLocked());
                applyDisplaySettings();
                // An adopted layout can change what belongs where, so a mod's
                // desktop chrome (the workspaces pager, the floating mask, the
                // taskbar ws badges) must be reconciled now rather than left
                // stale until the next 2s poll. requestRelayout() lands on the
                // #148 layout-rendered seam next frame, which is where that
                // repaint now lives -- no named workspace call in core.
                requestRelayout();
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
            // ONE deadline for the whole exchange, connect THROUGH body, which
            // is why this site opts out of hostFetch's built-in one
            // (timeoutMs: 0): that one is disarmed as soon as the response
            // HEADERS arrive, and a broker that answers and then stalls its
            // body would hang this await forever — with the boot gate
            // (_markStateReady) behind it, that is a permanently blank desktop.
            // Same shape as pollHost: the json() read lives INSIDE the try, and
            // the timer is cleared in the outer finally.
            // #247 single-flight: a background poll never queues behind a busy
            // channel (the next tick asks again); an initial pull waits for it.
            if (!initial && (_stateChannel || _statePushInFlight)) return;
            if (initial) { do { await _stateWaitChannel(); } while (_stateChannel); }
            let release;
            const mine = new Promise(r => { release = r; });
            _stateChannel = mine;
            let srv;
            const ctrl = new AbortController();
            const timer = setTimeout(() => ctrl.abort(), FETCH_TIMEOUT_MS);
            try {
                const r = await hostFetch(localHost(), '/state',
                    { cache: 'no-store', signal: ctrl.signal, timeoutMs: 0 });
                if (!r.ok) return;               // 401/5xx — handled elsewhere
                srv = await r.json();
            } catch (e) {                        // offline — keep localStorage
                return;
            } finally {
                clearTimeout(timer);
                if (_stateChannel === mine) _stateChannel = null;
                release();
            }
            if (!_stateBodyOk(srv)) return;         // #247 N1
            // A tab back at its baseline (a failed edit undone without a
            // save) owes nothing, whatever the server's rev says.
            if (_statePushFailed && _stateSerialize() === _stateLastSerialized) {
                _statePushFailed = false;
            }
            const srvRev = srv.rev;
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
            // "unchanged" -- unless this rev was taken without adopting it.
            if (!initial && srvRev === _stateRev && !_stateRevSkipped) {
                _retryOwedPush();
                return;
            }
            const srvSerialized = JSON.stringify({
                settings: srv.settings, layout: srv.layout });
            _stateRev = srvRev;                  // take the new rev either way
            if (!initial && srvSerialized === _stateLastSerialized) {
                _stateRevSkipped = false;        // the server is our baseline
                _retryOwedPush();
                return;
            }
            // The server already holds exactly what this tab has: a PUT whose
            // response was lost DID land, or two writers converged. Nothing
            // is owed and nothing needs adopting -- record it as the baseline.
            if (!initial && srvSerialized === _stateSerialize()) {
                _stateLastSerialized = srvSerialized;
                _statePushFailed = false;
                _stateRevSkipped = false;
                return;
            }
            // A background pull must NOT clobber an in-progress local edit
            // (Codex lost-update case). With unpushed local changes AND a push
            // on its way, keep them: _stateRev now points at the server's rev,
            // so that push rebases onto it and wins (active editor wins;
            // convergence happens once this browser goes idle and a later pull
            // finds it clean). Never skips on the initial boot sync.
            // #246: dirty with NOTHING queued is not an edit in progress -- it
            // is a write nobody called savePrefs() for (a derived mirror the
            // render made). Skipping then is permanent: the rev was just
            // taken, so no later pull retries, and the next unrelated save
            // pushes this stale layout over the newer one. Adopt instead.
            if (!initial && _statePushPending()
                && _stateSerialize() !== _stateLastSerialized) {
                _stateRevSkipped = true;         // reconsidered by later polls
                _retryOwedPush();
                return;
            }
            _applyServerState(srv);
        }
        // A push is on its way: debounced, in flight, queued behind the
        // in-flight one, held until the initial pull, or owed by a failed one
        // (the next save retries it). pullState's dirty skip is honoured only
        // while this is true (#246).
        function _statePushPending() {
            return _statePushQueued() || _statePushFailed;
        }
        // A background pull that KEEPS the local state because of a failed
        // push's debt (rev unchanged, server == our baseline, or the dirty
        // skip): the broker just answered, so retry the debt now rather than
        // at some unrelated later save (owner's call on #246). One debounced
        // retry per such poll, so a PUT that keeps failing costs at most one
        // request per poll tick. Never from a pull that adopts or
        // acknowledges (that settles the debt), and an undone edit has
        // already cleared it at the top of pullState.
        function _retryOwedPush() {
            if (_statePushFailed && !_statePushQueued()) schedulePush();
        }
        // The same minus the failed-push debt: something will send by itself.
        function _statePushQueued() {
            return !!(_statePushTimer || _statePushInFlight
                || _statePushAgain || _statePendingPush);
        }

