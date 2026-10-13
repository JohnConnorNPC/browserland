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
        // #247: a push holds or waits for the /state channel.
        let _statePushInFlight = false;
        // A push was asked for while one was in flight: a WAKE flag only --
        // what is owed is the mutation record below, never this flag.
        let _statePushAgain = false;
        // ---- #247 S8: the mutation record ----------------------------------
        // What this tab owes the broker is ONE thing: the edits up to
        // _stateEditGen that no landed PUT, acknowledgement or adopt has
        // covered. Timers, _statePushAgain and _statePendingPush only WAKE
        // the sender; each settle names the gen it settles.
        //   _stateEditGen   bumps when the synced bytes CHANGE at a save (S4):
        //                   a byte-identical savePrefs() is not new intent;
        //   _stateSentGen   the newest gen a PUT carried, or found nothing to
        //                   send for: _stateEditGen > _stateSentGen = unsent;
        //   _stateFail      the failure of the newest sent gen: {gen, baseRev,
        //                   thrown, fails}. (#246: a failed push of a real edit
        //                   -- 5xx, 401, offline, an unusable 409 -- stays owed
        //                   and a pull must not adopt it away; a 400/413 is a
        //                   verdict on the payload and owes nothing; never set
        //                   by a PUT that outlived its view, _viewEpoch.)
        let _stateEditGen = 0;
        let _stateEditBytes = null;
        let _stateSentGen = 0;
        let _stateFail = null;
        // #247 item 3: a teardown (a lease loss, or resetLocalView) PARKS the
        // owed edit instead of dropping it -- one in its debounce, one queued
        // behind an in-flight PUT, and (R1) the one IN FLIGHT -- with the base
        // it had then. The next view pushes it on that base before its initial
        // pull (_stateFlushOwed), so it lands only if nobody wrote meanwhile
        // and yields otherwise (owner, Q1); until it has gone out, the initial
        // pull never adopts over it (M3).
        let _stateParked = false;
        let _stateParkGen = 0;
        let _stateParkBase = null;
        // ---- #247 owner switches (one line each) ----------------------------
        // Q1: a parked edit is pushed when this page takes the lease back.
        // false = it is dropped at the rebuild instead.
        const STATE_PUSH_PARKED_ON_REGAIN = true;
        // Q2 / item 5: a THROWN failure (the PUT may have landed: offline, a
        // lost reply, the deadline, a 200 without a rev) retries on its
        // ORIGINAL base while the edit is unchanged -- it lands only if nobody
        // wrote meanwhile and yields otherwise. false = rebase.
        const STATE_THROWN_RETRY_YIELDS = true;
        // Item 9 (b): the AUTOMATIC retry of an owed edit rides the poll
        // cadence for the first STATE_RETRY_FAST failures, then backs off
        // 4 s, 8 s, 16 s, 32 s, capped at STATE_RETRY_CAP_MS (performance.now:
        // a wall-clock step can neither stall nor skip it). The ladder belongs
        // to the failed edit: a new edit or any settle starts over. A user's
        // own save is never delayed. (a) = Infinity, (c) = 0.
        const STATE_RETRY_FAST = 3;
        const STATE_RETRY_CAP_MS = 60000;
        // S6: once the ladder has engaged, a retry of a definitive failure
        // (5xx/401/an unusable 409) uses the debt's ORIGINAL base -- it lands
        // only if nobody wrote meanwhile, and yields otherwise. false = rebase.
        const STATE_LATE_RETRY_YIELDS = true;
        // Q4 / item 7: a rejected save (413, or a 400 that is the broker's
        // verdict on the payload) can never land, so the tab goes back to the
        // saved layout and says so. false = keep the rejected edit on screen
        // until a peer rev arrives (pre-#247).
        const STATE_REJECT_CONVERGES = true;
        const STATE_REJECT_NOTICE = "couldn't save this change — back to the saved layout";
        const _STATE_400_VERDICTS = Object.create(null);
        ['bad_json', 'bad_baseRev', 'bad_state'].forEach(function (k) {
            _STATE_400_VERDICTS[k] = true;
        });
        // ---- #247 own-write recognition --------------------------------------
        // The bytes of this tab's last few PUTs. A write whose reply was lost
        // DID land when the server later shows exactly its bytes; an owed or
        // parked edit made on top of it must then move its base up to that
        // rev rather than yield to our own write (found re-probing M3 x Q2).
        // R2: only the DIRECT parent counts -- the newest send older than the
        // edit. A peer that reverts to an OLDER send's bytes is a peer write.
        // One entry per gen (retries of one edit never evict its parent), and
        // an adopt empties it: an edit made after that is built on the adopted
        // state, so no earlier send of ours is its parent.
        const _stateRecentSends = [];
        function _stateNoteSend(bytes, gen) {
            for (let i = _stateRecentSends.length - 1; i >= 0; i--) {
                if (_stateRecentSends[i].gen === gen) _stateRecentSends.splice(i, 1);
            }
            _stateRecentSends.push({ bytes: bytes, gen: gen });
            if (_stateRecentSends.length > 4) _stateRecentSends.shift();
        }
        function _stateDirectParent(gen) {
            let best = null;
            for (const s of _stateRecentSends) {
                if (s.gen < gen && (!best || s.gen > best.gen)) best = s;
            }
            return best;
        }
        // The server shows our direct parent at `rev`: that rev is the base,
        // even a LOWER one (a rollback) -- a base left above it would 409
        // against the same winner on every re-push.
        function _stateSawOwnWrite(bytes, rev) {
            if (_stateFail) {
                const par = _stateDirectParent(_stateFail.gen);
                if (par && par.bytes === bytes) _stateFail.baseRev = rev;
            }
            if (_stateParked && _stateParkBase !== null) {
                const par = _stateDirectParent(_stateParkGen);
                if (par && par.bytes === bytes) _stateParkBase = rev;
            }
        }
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
        function _stateNoteEdit() {
            const b = _stateSerialize();
            if (b !== _stateEditBytes) { _stateEditBytes = b; _stateEditGen++; }
        }
        function _stateUnsent() { return _stateEditGen > _stateSentGen; }
        function _stateOwed() { return _stateUnsent() || !!_stateFail; }
        // Everything up to the current gen is covered (an adopt, an
        // acknowledgement, an undone edit): nothing is owed.
        function _stateSettleAll() {
            _stateSentGen = _stateEditGen;
            _stateEditBytes = _stateSerialize();
            _stateFail = null;
            _stateParked = false;
        }
        // What one sent gen settles: its failure record (an older one too).
        function _stateSettleSent(gen) {
            if (_stateFail && _stateFail.gen <= gen) _stateFail = null;
            if (_stateParked && gen >= _stateParkGen) _stateParked = false;
        }
        // R1: a parked gen whose PUT FAILED in the old view (its 409 not_active
        // at the lease loss, a 5xx, a throw, the deadline) stays owed -- it is
        // unsent again, and the next view's flush carries it.
        function _stateParkedFailure(gen) {
            if (_stateParked && gen <= _stateParkGen && _stateSentGen >= gen) {
                _stateSentGen = gen - 1;
            }
        }
        // The base a PUT of `gen` carries, one rule for a send and for a park
        // (#247 Z1): a parked gen keeps the base it was parked with (Q1); a
        // thrown failure (Q2) or a late ladder retry (S6) keeps the debt's
        // ORIGINAL base; anything else rebases on the last rev seen (#246
        // policy a). A teardown that parked with the rev of the moment instead
        // turned a retry that must yield to a peer write into an overwrite.
        // (An in-flight PUT's base is this same value at the teardown: while
        // it holds the channel nothing moves _stateRev, _stateFail or the park.)
        function _stateSendBase(gen) {
            if (_stateParked && _stateParkGen === gen && _stateParkBase !== null) {
                return _stateParkBase;
            }
            const f = _stateFail;
            if (f && f.gen === gen && ((f.thrown && STATE_THROWN_RETRY_YIELDS)
                    || (STATE_LATE_RETRY_YIELDS && f.fails > STATE_RETRY_FAST))) {
                return f.baseRev;
            }
            return _stateRev;
        }
        // A failure of `gen`; a newer edit starts a fresh record.
        function _stateFailed(gen, baseRev, thrown) {
            const f = (_stateFail && _stateFail.gen === gen) ? _stateFail
                : { gen: gen, baseRev: baseRev, thrown: false, fails: 0, retryAt: 0 };
            f.thrown = f.thrown || !!thrown;
            f.fails += 1;
            const over = f.fails - STATE_RETRY_FAST;
            f.retryAt = over <= 0 ? 0
                : performance.now() + Math.min(STATE_RETRY_CAP_MS, 2000 * Math.pow(2, over));
            _stateFail = f;
        }
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
        // #247 item 2: a pull that outlives its view applies nothing -- except
        // 85's LOAD pull, which may be answered after the first boot has
        // already bumped _viewEpoch. The load pull is always the FIRST initial
        // pull (bootActiveView waits on _stateReadyPromise, which that pull
        // resolves; only a booted page rebuilds), so 52 recognises it without
        // a token from 85. Its exemption ends the moment any later
        // (lifecycle) initial pull is SENT: from then on that pull is newer.
        // If the load pull is dropped that way and the lifecycle pull fails,
        // _stateRev stays unseeded (0) and the first push 409s and adopts
        // the server state: safe.
        let _stateLoadPullSent = false;
        let _stateLoadRevoked = false;
        let _stateChannel = null;
        async function _stateWaitChannel() {
            while (_stateChannel) { try { await _stateChannel; } catch (_) {} }
        }
        // #247 S3: ONE deadline for a whole /state exchange, connect THROUGH
        // body, for GETs and PUTs alike. A broker that answers and then stalls
        // its body would otherwise hang the await forever -- for a pull, with
        // the boot gate behind it, a blank desktop; for a push, every later
        // push queued behind it for the life of the page. hostFetch's own
        // deadline is disarmed at the HEADERS, so these sites pass timeoutMs
        // 0. An expired PUT is an ambiguous failure (it may have landed).
        function _stateDeadline(work, ctrl) {
            let timer = null;
            const dl = new Promise((_, rej) => {
                timer = setTimeout(() => {
                    try { ctrl.abort(); } catch (_) {}
                    rej(new Error('state deadline'));
                }, FETCH_TIMEOUT_MS);
            });
            return Promise.race([work, dl]).finally(() => clearTimeout(timer));
        }

        function schedulePush() {
            if (_deactivated) return;            // torn down — never push state
            if (isDetachedSurface()) return;     // #226: one writer, the desktop
            if (_stateApplying) return;          // don't echo server state back
            _stateNoteEdit();                    // #247 S4: a byte change is intent
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
            let converge = false;
            try {
                // #247 single-flight: wait for the /state channel, then claim
                // it with no await in between (see _stateWaitChannel).
                do { await _stateWaitChannel(); } while (_stateChannel);
                if (_deactivated) return;
                const serialized = _stateSerialize();
                const gen = _stateEditGen;
                if (serialized === _stateLastSerialized) {        // nothing changed
                    _stateSentGen = gen;          // (an undone edit owes nothing)
                    _stateSettleSent(gen);
                    return;
                }
                mine = new Promise(r => { release = r; });
                _stateChannel = mine;
                _stateSentGen = gen;
                _stateNoteSend(serialized, gen);
                const baseRev = _stateSendBase(gen);  // Q1 / Q2 / S6 / policy a
                let out = null;
                const ctrl = new AbortController();
                try {
                    out = await _stateDeadline((async () => {
                        const blob = _stateBlob();
                        const r = await hostFetch(localHost(), '/state', {
                            method: 'PUT',
                            headers: { 'Content-Type': 'application/json' },
                            body: JSON.stringify({
                                baseRev: baseRev,
                                settings: blob.settings,
                                layout: blob.layout,
                                clientId: CLIENT_ID,   // lease: only the active browser
                            }),                        // may write (else 409 not_active)
                            signal: ctrl.signal, timeoutMs: 0,
                        });
                        let body = null;
                        if (r.status === 409 || r.ok) body = await r.json();
                        else if (r.status === 400) { try { body = await r.json(); } catch (_) {} }
                        return { status: r.status, ok: r.ok, body: body };
                    })(), ctrl);
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
                    // Offline / lost response / the deadline — localStorage
                    // already holds it; retry. (The PUT may still have landed:
                    // pullState acknowledges that.)
                    if (epoch === _viewEpoch) _stateFailed(gen, baseRev, true);
                    else _stateParkedFailure(gen);
                    return;
                }
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
                    // #247 item 6: torn down while this PUT was in flight (lost
                    // the lease), or a newer view since: drop it, so a 409 never
                    // re-renders into the overlay the teardown just put up or
                    // into a view that has adopted since. (The probe above runs
                    // first: probing after a teardown is harmless.)
                    if (_deactivated || epoch !== _viewEpoch) {
                        _stateParkedFailure(gen);
                        return;
                    }
                    if (_stateBodyOk(cur)) {
                        const winnerBytes = JSON.stringify({
                            settings: cur.settings, layout: cur.layout });
                        _stateSawOwnWrite(winnerBytes, cur.rev);
                        _stateRev = cur.rev;
                        const par = _stateDirectParent(gen);
                        if (par && par.bytes === winnerBytes) {
                            // The "winner" is OUR previous write (its reply was
                            // lost): this edit was made on top of it. Rebase on
                            // it and re-push; never yield to ourselves.
                            _stateLastSerialized = winnerBytes;
                            _stateRevSkipped = false;
                            _statePushAgain = true;
                        } else if (_stateEditGen === gen) {
                            // #247 S4: no NEW edit (a byte change) since the
                            // send -- yield. (A byte-identical re-save during the
                            // PUT used to set _statePushAgain and re-push over
                            // the winner.)
                            _applyServerState(cur);          // no local edit pending — yield
                        } else {
                            _stateLastSerialized = null;     // keep local; force the re-push
                            _stateRevSkipped = true;         // took cur.rev, did not adopt it
                        }
                    } else if (epoch === _viewEpoch) {
                        _stateFailed(gen, baseRev, false);   // unusable body: still owed
                    }
                } else if (out.ok) {
                    const resp = out.body;
                    if (!resp || !_stateRevOk(resp.rev)) {
                        // A 200 without a usable rev taught us nothing: the PUT
                        // may or may not have landed, so it is still owed (the
                        // next pull acknowledges it if it did).
                        if (epoch === _viewEpoch) _stateFailed(gen, baseRev, true);
                        else _stateParkedFailure(gen);
                        return;
                    }
                    // Our write landed. That fact holds across a teardown (the
                    // next view must not 409 against it), and single-flight
                    // guarantees no newer response has been processed since.
                    _stateRev = resp.rev;
                    _stateLastSerialized = serialized;
                    // A parked edit built on top of this write moves its base
                    // up to it: it must not 409 against our own landed PUT.
                    // (A peer write after it still makes the flush 409.)
                    if (_stateParked && gen < _stateParkGen && _stateParkBase !== null
                            && _stateParkBase < resp.rev) {
                        _stateParkBase = resp.rev;
                    }
                    _stateSettleSent(gen);
                    _stateRevSkipped = false;
                } else if (out.status === 400 || out.status === 413) {
                    // A verdict that outlived its view: a PARKED edit stays
                    // owed, so the next view re-sends it and, refused again,
                    // converges WITH the notice there (one more PUT). Settling
                    // it here would let the rebuild pull drop it silently.
                    if (epoch !== _viewEpoch && _stateParked && gen === _stateParkGen) {
                        _stateParkedFailure(gen);
                        return;
                    }
                    // A verdict on this payload (which carries any older
                    // failed edit too): it can never land, so nothing is owed.
                    _stateSettleSent(gen);
                    // #247 item 7 (Q4): ...and the tab must not keep showing it.
                    // Converge to the shared state -- only for the broker's own
                    // verdict (a proxy's 400 may be transient), only if no NEWER
                    // edit exists (that one re-pushes on its own), only in this
                    // view. The pull runs once the channel is free (finally).
                    const verdict = out.status === 413 || !!(out.body
                        && _STATE_400_VERDICTS[out.body.error] === true);
                    if (STATE_REJECT_CONVERGES && verdict && _stateEditGen === gen
                            && epoch === _viewEpoch && !_deactivated) {
                        _stateRevSkipped = true;      // bypass "unchanged" ...
                        _stateLastSerialized = null;  // ... and "server == baseline"
                        try { showNotice(STATE_REJECT_NOTICE, { type: 'error' }); } catch (_) {}
                        converge = true;
                    }
                } else if (epoch === _viewEpoch) {
                    // 401 (local broker needs auth) / other: keep localStorage;
                    // the existing /sessions auth flow handles login, next
                    // save retries.
                    _stateFailed(gen, baseRev, false);
                } else {
                    _stateParkedFailure(gen);
                }
            } finally {
                _statePushInFlight = false;
                if (release) { if (_stateChannel === mine) _stateChannel = null; release(); }
                if (_statePushAgain) {
                    _statePushAgain = false;
                    if (epoch === _viewEpoch) schedulePush();   // never into a newer view
                }
                if (converge) pullState(false);
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
                _stateSettleAll();              // the adopted state supersedes it
                _stateRecentSends.length = 0;   // ... and no send of ours is a parent
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
            // ONE deadline for the whole exchange, connect THROUGH body
            // (_stateDeadline, #247 S3), which is why this site opts out of
            // hostFetch's built-in one (timeoutMs: 0): that one is disarmed as
            // soon as the response HEADERS arrive, and a broker that answers
            // and then stalls its body would hang this await forever — with
            // the boot gate (_markStateReady) behind it, that is a permanently
            // blank desktop.
            // #247 single-flight: a background poll never queues behind a busy
            // channel (the next tick asks again); an initial pull waits for it.
            if (!initial && (_stateChannel || _statePushInFlight)) return;
            const load = initial && !_stateLoadPullSent;
            if (initial) {
                if (_stateLoadPullSent) _stateLoadRevoked = true;
                _stateLoadPullSent = true;
            }
            const epoch = _viewEpoch;
            if (initial) { do { await _stateWaitChannel(); } while (_stateChannel); }
            // #247 Z4: superseded while it waited -- never claim the channel
            // for a dead view (the next rebuild would wait a deadline behind
            // it). The load pull keeps its exemption (item 2).
            if (initial && epoch !== _viewEpoch && !(load && !_stateLoadRevoked)) return;
            let release;
            const mine = new Promise(r => { release = r; });
            _stateChannel = mine;
            let srv;
            const ctrl = new AbortController();
            try {
                srv = await _stateDeadline((async () => {
                    const r = await hostFetch(localHost(), '/state',
                        { cache: 'no-store', signal: ctrl.signal, timeoutMs: 0 });
                    if (!r.ok) return null;          // 401/5xx — handled elsewhere
                    return await r.json();
                })(), ctrl);
            } catch (e) {                        // offline / deadline — keep local
                return;
            } finally {
                if (_stateChannel === mine) _stateChannel = null;
                release();
            }
            // #247 item 2: superseded by a newer view -- drop it (see above).
            if (epoch !== _viewEpoch && !(load && !_stateLoadRevoked)) return;
            if (!_stateBodyOk(srv)) return;         // #247 N1
            // A tab back at its baseline (a failed edit undone without a
            // save) owes nothing, whatever the server's rev says.
            if (_stateOwed() && _stateSerialize() === _stateLastSerialized) {
                _stateSettleAll();
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
            _stateSawOwnWrite(srvSerialized, srvRev);
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
                _stateSettleAll();
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
            // #247 M3: an INITIAL pull does not adopt over an edit a teardown
            // parked that has still not gone out (the flush failed or timed
            // out): take the rev, keep local, and the retry carries it.
            if (((!initial && _statePushPending())
                    || (initial && _stateParked && _stateOwed()))
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
            return _statePushQueued() || _stateOwed();
        }
        // A background pull that KEEPS the local state because of a failed
        // push's debt (rev unchanged, server == our baseline, or the dirty
        // skip): the broker just answered, so retry the debt now rather than
        // at some unrelated later save (owner's call on #246). One debounced
        // retry per such poll for the first STATE_RETRY_FAST failures, then
        // the item-9 ladder (at most one per 60 s). Never from a pull that adopts or
        // acknowledges (that settles the debt), and an undone edit has
        // already cleared it at the top of pullState.
        function _retryOwedPush() {
            if (!_stateOwed() || _statePushQueued()) return;
            // Item 9 (b): not yet, says the ladder. (Only this automatic
            // retry is gated; schedulePush from a user's save never is.)
            if (_stateFail && _stateFail.gen === _stateEditGen
                    && performance.now() < _stateFail.retryAt) return;
            schedulePush();
        }
        // #247 item 3: called by 84 teardownView in place of clearing the
        // debounce timer. Drops only the WAKE-UPS; the edit itself is the
        // mutation record, which stays owed. R1: an edit whose PUT is in
        // flight right now is parked too (its failure in the old view then
        // keeps it owed, _stateParkedFailure).
        function _stateParkPendingPush() {
            if (_statePushTimer) { clearTimeout(_statePushTimer); _statePushTimer = null; }
            _statePushAgain = false;
            _statePendingPush = false;
            // Z1: the base its next send would carry -- taken BEFORE the park
            // is rewritten, so a gen parked again keeps its original base.
            const base = _stateSendBase(_stateEditGen);
            _stateParked = (_stateOwed() || _statePushInFlight)
                && _stateSerialize() !== _stateLastSerialized;
            if (_stateParked) { _stateParkGen = _stateEditGen; _stateParkBase = base; }
        }
        // #247 item 3: called by 84 rebuildView BEFORE its initial pull. Let
        // the channel drain (an old PUT settles; bounded by the deadline),
        // then push what is parked. Returns whether it is STILL unsent (the
        // initial pull then keeps local, M3). A rebuild therefore waits up to
        // one deadline plus one PUT.
        async function _stateFlushOwed() {
            do { await _stateWaitChannel(); } while (_stateChannel);
            if (!_stateParked) return false;
            if (!STATE_PUSH_PARKED_ON_REGAIN) { _stateSettleAll(); return false; }
            if (!_stateOwed()) { _stateParked = false; return false; }
            try { await pushState(); } catch (_) {}
            return _stateParked && _stateOwed();
        }
        // The same minus the failed-push debt: something will send by itself.
        function _statePushQueued() {
            return !!(_statePushTimer || _statePushInFlight
                || _statePushAgain || _statePendingPush);
        }

