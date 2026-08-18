        // ---- visibility-aware timers ----------------------------------------
        // One shared listener pair feeds every subscriber; callbacks fire only
        // on the transition TO visible. 'pageshow' rides the same dispatch
        // because a bfcache restore skips 'visibilitychange'.
        const HIDDEN_MULT = 10;
        const _visibilityCbs = new Set();
        // _wasHidden gates the dispatch to real hidden->visible transitions:
        // without it the load-time 'pageshow' fires subscribers on a page that
        // was never hidden, and a bfcache restore (pagehide + pageshow +
        // visibilitychange, order browser-dependent) fires them twice.
        let _wasHidden = document.visibilityState === 'hidden';
        function _dispatchVisible() {
            if (document.visibilityState !== 'visible') return;
            if (!_wasHidden) return;
            _wasHidden = false;
            for (const fn of _visibilityCbs) {
                // Isolated: one throwing callback must not starve the rest.
                try { fn(); } catch (_) {}
            }
        }
        document.addEventListener('visibilitychange', function () {
            if (document.visibilityState === 'hidden') { _wasHidden = true; return; }
            _dispatchVisible();
        });
        window.addEventListener('pagehide', function () { _wasHidden = true; });
        window.addEventListener('pageshow', _dispatchVisible);
        function onVisibility(fn) {
            _visibilityCbs.add(fn);
            return function () { _visibilityCbs.delete(fn); };
        }

        // setInterval that slows to ms*HIDDEN_MULT while the page is hidden.
        // The interval itself keeps ticking at the base ms; a hidden tick only
        // runs fn once the slowed cadence has elapsed since the last actual
        // run, otherwise it sets a missed flag. On becoming visible a missed
        // run fires exactly once (N missed ticks coalesce — never queued).
        function visibilityInterval(fn, ms) {
            // performance.now(): a wall-clock step (NTP, DST, manual set) must
            // not stretch or collapse the hidden cadence.
            let lastRun = performance.now();
            let missed = false;
            function runNow() {
                lastRun = performance.now();
                missed = false;
                fn();
            }
            const timer = setInterval(function () {
                const elapsed = performance.now() - lastRun;
                if (document.visibilityState === 'hidden') {
                    if (elapsed >= ms * HIDDEN_MULT) runNow();
                    else missed = true;
                } else if (elapsed >= ms / 2) {
                    // The half-interval floor swallows the tick already queued
                    // behind a resume catch-up, which would otherwise run fn
                    // twice back-to-back.
                    runNow();
                }
            }, ms);
            const offVisible = onVisibility(function () {
                // Catch up on a run skipped while hidden — including when the
                // browser froze the timer outright (bfcache, throttled tab):
                // no tick fired there, so `missed` alone cannot be trusted.
                if (missed || performance.now() - lastRun >= ms) runNow();
            });
            return {
                stop: function () {
                    clearInterval(timer);
                    offVisible();
                },
            };
        }

        // Mod-facing wrapper: every teardown rides rec.unloads (drained LIFO by
        // _runUnloads in 86_js_mod_loader.js), so a disabled mod's timers and
        // subscriptions die with it; the !rec.unloading guard keeps a callback
        // quiet while its mod is mid-teardown, and a create AFTER teardown
        // began fails closed (a surviving async continuation would otherwise
        // leave a permanent timer whose cleanup can never drain).
        function makeModVisibilityApi(rec) {
            // A manual stop removes its own rec.unloads record, or a churny
            // mod (one timer per window) grows the list for its whole life.
            // Safe against the drain itself: _runUnloads splices the array
            // empty before running anything, so indexOf misses there.
            function unloadable(teardown) {
                function stop() {
                    const i = rec.unloads.indexOf(stop);
                    if (i !== -1) rec.unloads.splice(i, 1);
                    teardown();
                }
                rec.unloads.push(stop);
                return stop;
            }
            return {
                pausableInterval: function (fn, ms) {
                    if (rec.unloading) return { stop: function () {} };
                    const handle = visibilityInterval(function () {
                        if (!rec.unloading) fn();
                    }, ms);
                    return { stop: unloadable(handle.stop) };
                },
                onVisibility: function (fn) {
                    if (rec.unloading) return function () {};
                    const off = onVisibility(function () {
                        if (!rec.unloading) fn();
                    });
                    return unloadable(off);
                },
            };
        }

        // ---- shared state -------------------------------------------------
        // key -> merged session object (built in refreshTaskbarInner):
        //   {key, id (real numeric), sid (String(id)), title, cols, rows,
        //    host, pid, kind, stale, hostId, hostLabel}
        // Keys are host-qualified '<hostId>:<windowId>' (window ids are
        // only unique per broker) and opaque everywhere downstream; `sid`
        // stays the bare wire id.
        const sessions = new Map();

        // #123: single source of truth for the taskbar/title label — order AND
        // per-component visibility. Returns the VISIBLE parts in `show.order`,
        // each {key, text, cls} so the taskbar can render one span per part (id
        // can interleave) and formatTitle() can join a string. The datum-guard
        // expressions are byte-identical to the old formatTitle (same id/title/
        // host/pid truthiness + host trim), so the default order+toggles (host
        // on, pid off, title on) reproduce "#id host: title [pid]" exactly.
        // Always use textContent at call sites — never inject as HTML.
        function composeLabelParts(sess) {
            const show = getSettings().show, order = show.order;
            const id    = sess && sess.id != null ? String(sess.id) : '';
            const title = (sess && sess.title) || ('session ' + id);
            const host  = sess && sess.host ? String(sess.host).trim() : '';
            // pid is absent from old brokers' /sessions — guarded like the old code.
            const pid   = (sess && sess.pid) ? String(sess.pid) : '';
            const want = { id: !!(show.id && id), host: !!(show.host && host),
                           title: !!show.title, pid: !!(show.pid && pid) };
            let visible = order.filter(k => want[k]);
            // Never-empty guarantee: a session whose toggled-on components have no
            // datum (e.g. host-only toggle on a hostless app session) still gets a
            // label — the title text — instead of a blank chip/title.
            if (!visible.length) visible = ['title'];
            return visible.map((k, i) => {
                const next = visible[i + 1];
                let text;
                if (k === 'id')        text = '#' + id;
                else if (k === 'pid')  text = '[' + pid + ']';
                // colon only immediately before the title, so the default reads
                // "host: title" but a reordered host (e.g. host last) reads bare.
                else if (k === 'host') text = (next === 'title') ? host + ':' : host;
                else                   text = title;
                return { key: k, text: text, cls: 'ti-' + k };
            });
        }
        // String label for the window TITLE BAR: the same composer, minus the id
        // (the title bar shows #id separately as a leading .ti-id-badge). Falls
        // back to the bare title if id was the ONLY visible part, so the
        // title-text is never blank (id-only setting still leaves the badge).
        function formatTitle(sess) {
            const parts = composeLabelParts(sess).filter(p => p.key !== 'id');
            if (parts.length) return parts.map(p => p.text).join(' ');
            const id = sess && sess.id != null ? String(sess.id) : '';
            return (sess && sess.title) || ('session ' + id);
        }
        // Hover tooltip: always shows everything, independent of toggles.
        // `agent` rides here only — most webterm sessions are agents, so a
        // taskbar chip would be noise; the outlier terminal shows on hover.
        function formatTooltip(sess) {
            const id = sess && sess.id != null ? String(sess.id) : '';
            const title = (sess && sess.title) || ('session ' + id);
            const parts = ['#' + id];
            if (sess && sess.pid) parts.push('pid ' + sess.pid);
            if (sess && sess.host) parts.push('host ' + String(sess.host).trim());
            if (sess && sess.kind === 'agent') parts.push('agent');
            // Which broker it lives on — only interesting with >1 of them.
            if (sess && sess.hostLabel && getHosts().length > 1) {
                parts.push('on ' + sess.hostLabel);
            }
            return parts.join(' · ') + ' — ' + title;
        }
        const windows = new Map();    // key -> win record (see openWindow)
        let nextZ = 100;
        // Floating windows stack in "always-on-top" tiers above the plain nextZ
        // band, so focus-raising a normal window never covers them; modals
        // (>=100000) and drop overlays still sit above every tier:
        //   plain windows   nextZ                          (~100+)
        //   sticky notes     NOTE_Z_BASE + nextZ           (90000+)  above windows
        //   control panel    CONTROL_PANEL_Z_BASE + nextZ  (95000+)  above notes (#98)
        // Sticky notes are always-on-top (todo2 task 9) WHEN PINNED (#95): the
        // note branch stays SCOPED to sticky notes — `pinned` is a per-note
        // toggle, not a global z-capability — and `!== false` keeps a note with
        // no/true `pinned` always-on-top (backward-compatible for existing
        // persisted notes); only an explicitly unpinned note (pinned === false)
        // drops to the normal tier.
        // The floating Control Panel (#98) rides a tier ABOVE notes so notes
        // never obscure it. All tiers share nextZ, so fronting the Control Panel
        // captures the current high-water mark + the tier gap and thus lands
        // above every note alive at that moment; a note only re-covers it after
        // ~5000 further focuses with no panel interaction, and clicking the panel
        // restores it on top (self-healing). Distinct from scroll-lock, which
        // only pins notes against strip scroll.
        const NOTE_Z_BASE = 90000;
        const CONTROL_PANEL_Z_BASE = NOTE_Z_BASE + 5000;   // 95000 (#98)
        function floatZIndex(win) {
            nextZ += 1;
            if (win && win.appKind === 'control-panel') return CONTROL_PANEL_Z_BASE + nextZ;
            if (win && win.appKind === 'sticky-note' && win.pinned !== false) return NOTE_Z_BASE + nextZ;
            return nextZ;
        }
        let cascadeIndex = 0;
        let frontId = null;
        // The most-recently-fronted TERMINAL window id (NOT an app window), so
        // the file tools can open "where I am" — at the active terminal's cwd
        // and on its host (#35). bringToFront keeps it current.
        let lastTermId = null;
        let refreshInFlight = false;
        // Monotonic generation for refreshTaskbar (same shape as the mod's
        // gitSeq). refreshInFlight is the mutex; a WATCHDOG can force-release it
        // if a run ever wedges (see refreshTaskbar), and a force-release means
        // two runs can be alive at once. So every await boundary in the run
        // re-checks its seq against this counter and a superseded run bails
        // instead of mutating sessions/windows underneath the newer one — and
        // its `finally` only clears the mutex when it still owns it.
        let refreshSeq = 0;
        // Per-window MCP-pin re-assertions in flight (by window key). The 2s
        // re-assert pass never fires a duplicate POST for a key it is already
        // driving, and a user-initiated mode change serialises against it via
        // the same set. A hung POST is bounded by FETCH_TIMEOUT_MS, which frees
        // the key so a still-mismatched pin retries on the next tick.
        const _mcpAsserting = new Set();
        // compound key -> deadline(ms). /launch and ?session= deep links
        // park keys here; refreshTaskbarInner opens each one as it appears
        // in its host's /sessions (id-directed — no snapshot diffing).
        const pendingOpens = new Map();
        // Subset of pendingOpens keys seeded by restore-on-refresh (vs. /launch
        // or a ?session= deep link). Restore is best-effort: a restored key
        // that never shows up expires SILENTLY (no "not found" notice), and is
        // pruned from openTerms only once its host has actually answered.
        const restoreKeys = new Set();

        // ---- per-host poll state ----------------------------------------------
        // One state record per configured host. `sessions` holds that
        // host's last-GOOD list and is retained across failures so a
        // transiently down broker never mass-closes its windows — and a
        // down remote never touches another host's. gcDone arms the
        // per-host one-time prefs GC; authNeeded drives the amber chip.
        //
        // The poll is no longer a per-tick barrier (a black-holed host used to
        // cost every other host the full deadline before ANYTHING rendered), so
        // three more fields track the poll itself rather than its result:
        //   pollPromise  the in-flight poll, so a tick never stacks a second
        //                request on a host that is still hanging on the first
        //   polling      that poll is outstanding — with everOk false it means
        //                "no answer YET", which the chip shows as neutral
        //                instead of flashing red at a host that is merely slow
        //   fresh        this host answered since the last tick consumed it, so
        //                a tick that reuses last-good data can't double-count a
        //                window's missingPolls and close it early
        const hostPolls = new Map();   // hostId -> state record
        function pollStateFor(hostId) {
            let st = hostPolls.get(hostId);
            if (!st) {
                st = { ok: false, everOk: false, consecFailures: 0,
                       sessions: [], authNeeded: false, gcDone: false,
                       pollPromise: null, polling: false, fresh: false };
                hostPolls.set(hostId, st);
            }
            return st;
        }

        // ---- single-active-browser control channel (F-ACTIVECLIENT) -------
        // One /control WS per host carries this browser's lease status. The
        // HOME broker's lease drives the WHOLE page (boot / overlay / teardown
        // / rebuild); a REMOTE broker's lease only masks that host's windows +
        // flips its taskbar chip (see the multi-host handler). Reconnect uses
        // the scheduleReattach backoff shape; a light app-level ping keeps the
        // socket warm through idle proxies.
        const controlSockets = new Map();   // hostId -> { ws, ... }
        const CONTROL_PING_MS = 25000;
        function homeHostId() { return (localHost() || {}).id || 'local'; }

        function openControlWs(host) {
            if (!host) return;
            const hid = host.id;
            const existing = controlSockets.get(hid);
            if (existing && existing.ws && (
                    existing.ws.readyState === WebSocket.OPEN
                    || existing.ws.readyState === WebSocket.CONNECTING)) {
                return;                          // already dialing/connected
            }
            const rec = existing || {
                hostId: hid, ws: null, attempts: 0, lastOpenAt: 0,
                reattachTimer: null, pingTimer: null, closedByUs: false,
            };
            rec.closedByUs = false;
            controlSockets.set(hid, rec);
            let url;
            try {
                url = hostWsUrl(host, '/control?clientId='
                    + encodeURIComponent(CLIENT_ID));
            } catch (e) { return; }
            let ws;
            try { ws = new WebSocket(url); }
            catch (e) { scheduleControlReattach(hid); return; }
            rec.ws = ws;
            // Every handler guards on socket identity: openControlWs reuses the
            // rec across reconnects, so a stale socket's late callback must not
            // clear the replacement's ws/pingTimer or deliver a stale status.
            ws.onopen = () => {
                if (rec.ws !== ws) { try { ws.close(); } catch (_) {} return; }
                rec.attempts = 0;
                rec.lastOpenAt = Date.now();
                if (rec.pingTimer) clearInterval(rec.pingTimer);
                rec.pingTimer = setInterval(() => {
                    // App-level keepalive: an unknown-type JSON frame the broker
                    // ignores (protocol.parse -> not become_active) but that
                    // keeps the pipe warm through idle proxies.
                    try { ws.send(JSON.stringify({ type: 'ping' })); }
                    catch (_) {}
                }, CONTROL_PING_MS);
            };
            ws.onmessage = (ev) => {
                if (rec.ws !== ws) return;
                let msg;
                try { msg = JSON.parse(ev.data); } catch (_) { return; }
                if (!msg || msg.type !== 'status') return;
                onControlStatus(hid, msg);
            };
            ws.onclose = (ev) => {
                if (rec.ws !== ws) return;       // superseded socket — ignore
                if (rec.pingTimer) { clearInterval(rec.pingTimer); rec.pingTimer = null; }
                rec.ws = null;
                if (ev && ev.code === 4401) {
                    // Token rejected — defer to the existing auth flow (the
                    // auth-form success path re-dials this control WS). Do NOT
                    // hammer-reconnect a broker that needs a password.
                    pollStateFor(hid).authNeeded = true;
                    try { showAuthOverlay(hostById(hid)); } catch (_) {}
                    try { renderHostStatus(); } catch (_) {}
                    return;
                }
                if (rec.closedByUs) return;      // closeControlWs / host removed
                scheduleControlReattach(hid);
            };
            ws.onerror = () => { /* onclose schedules the reconnect */ };
        }

        function scheduleControlReattach(hostId) {
            const rec = controlSockets.get(hostId);
            if (!rec || rec.closedByUs || rec.reattachTimer) return;
            if (rec.lastOpenAt
                && (Date.now() - rec.lastOpenAt) >= REATTACH_STABLE_MS) {
                rec.attempts = 0;            // stable connection — reset backoff
            }
            const delay = Math.min(REATTACH_BACKOFF_MAX_MS,
                2000 * Math.pow(2, rec.attempts));
            rec.attempts += 1;
            rec.reattachTimer = setTimeout(() => {
                rec.reattachTimer = null;
                const h = hostById(hostId);  // re-resolve: settings may change
                if (h) openControlWs(h);
            }, delay);
        }

        function closeControlWs(hostId) {
            const rec = controlSockets.get(hostId);
            if (!rec) return;
            rec.closedByUs = true;
            if (rec.reattachTimer) { clearTimeout(rec.reattachTimer); rec.reattachTimer = null; }
            if (rec.pingTimer) { clearInterval(rec.pingTimer); rec.pingTimer = null; }
            if (rec.ws) {
                try {
                    rec.ws.onopen = rec.ws.onmessage = null;
                    rec.ws.onclose = rec.ws.onerror = null;
                } catch (_) {}
                try { rec.ws.close(); } catch (_) {}
                rec.ws = null;
            }
            controlSockets.delete(hostId);
        }

        function sendBecomeActive(host) {
            host = host || localHost();
            const rec = host && controlSockets.get(host.id);
            if (!rec || !rec.ws || rec.ws.readyState !== WebSocket.OPEN) return;
            try { rec.ws.send(JSON.stringify({ type: 'become_active' })); }
            catch (_) {}
        }

        function onControlStatus(hostId, msg) {
            const active = !!msg.active;
            if (hostId === homeHostId()) {
                _homeActive = active;
                if (active) becomeActiveTransition();
                else deactivateTransition();
                // #195: the single-active lease, as a LEVEL — the event that
                // deletes workspaces' read of the core-private _deactivated.
                //
                // THIS is the site and not the four `_deactivated = …` writes
                // inside bootActiveView / teardownView / rebuildView: this is
                // the only place core LEARNS its HOME lease level (the broker
                // is the source of truth; _homeActive is the mirror, assigned
                // three lines up), and resetLocalView (83) drives
                // teardownView+rebuildView back to back with no lease change at
                // all — emitting off those writes would announce a loss and a
                // regain that never happened.
                //
                // HOME only. The payload has no hostId, so one retained level
                // cannot speak for N brokers; a remote broker's lease is a
                // per-host mask (applyRemoteLease, 84) and would need its own
                // event, which this build's frozen table does not have.
                //
                // AFTER the transition: a loss has fully torn the view down
                // before any handler runs, and a gain has already set
                // _deactivated=false (both bootActiveView and rebuildView do it
                // synchronously, before their first await), so a handler that
                // reacts by writing shared state is no longer gated out. A
                // repeated {active:X} is a level no-op, which is the same
                // idempotence the transition wrappers below already enforce.
                if (typeof _emitModEvent === 'function') {
                    _emitModEvent('lease:changed', { active: _homeActive });
                }
            } else {
                onRemoteControlStatus(hostId, active, msg);
            }
        }

        // Idempotent transition wrappers around the heavy boot / rebuild /
        // teardown. The broker is the source of truth, so a repeated
        // {active:true}/{active:false} after the first must be a no-op.
        function becomeActiveTransition() {
            if (!_booted) { bootActiveView(); return; }  // first activation
            if (!_deactivated) return;                   // already active
            rebuildView();
        }
        function deactivateTransition() {
            if (!_booted) {
                // Never booted (first status was inactive): just show the
                // button — no windows were ever built, so near-zero flash.
                _deactivated = true;
                showBecomeActiveOverlay();
                return;
            }
            if (_deactivated) return;                    // already torn down
            teardownView();
        }

