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
        //    host, pid, kind, agent, cwd, profile, mcp, mcpKnown, mcp_scope,
        //    stale, hostId, hostLabel, controllerKnown, controller}
        // The merge copies those fields BY NAME, so a new /sessions field
        // reaches the page only once it is added there too.
        // Keys are host-qualified '<hostId>:<windowId>' (window ids are
        // only unique per broker) and opaque everywhere downstream; `sid`
        // stays the bare wire id.
        const sessions = new Map();
        // #239: who controls each terminal, by the same '<hostId>:<sid>' key
        // -> {state, label, since, gen, at}. Runtime only, never /state or
        // prefs: the broker is the truth and re-tells a reloaded page. Written
        // only by applyControlView (84b); it survives teardownView, so a
        // lease rebuild re-enters the state, and is pruned where the poll
        // drops a session (75). Prototype-free: keys are wire data.
        const terminalControl = Object.create(null);
        // #239: Take back control was refused because another browser holds
        // the HOME lease, so the Become active overlay was put up over a view
        // that is still built. `armed` = it was put up that way; `asked` = its
        // button was pressed since. Only an {active:true} after both may take
        // it down again (becomeActiveTransition): a still-active page gets no
        // rebuild that would do it.
        const _leaseAffordance = { armed: false, asked: false };

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
            // #234: the window's MCP scope tag, when it has one.
            if (sess && sess.mcp_scope) parts.push('scope ' + sess.mcp_scope);
            // #245: provenance, not live control -- an admin credential
            // launched this window or last set its policy.
            if (sess && sess.mcp_admin === true) {
                parts.push('Set by an MCP admin agent');
            }
            // #239: the device driving it (the broker-validated label).
            const ctl = sess && sess.key ? controlledViewFor(sess.key) : null;
            // FSI..PDI isolate the label so its direction cannot reorder the
            // rest of the tooltip.
            if (ctl) parts.push('controlled from \u2068' + ctl.label + '\u2069');
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
        // re-assert pass (which runs only for a broker without /info
        // `mcp_scopes`, #233) never fires a duplicate POST for a key it is already
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
        // #195: the poll record is per-host state about an ADDRESS, so a host
        // whose url/token changed must start from scratch — everOk, the failure
        // count and the session list all described the old broker. The next
        // tick re-primes it through pollStateFor.
        registerHostCache('hostPolls', id => hostPolls.delete(id));
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
        // the scheduleReattach backoff shape.
        //
        // #248: the channel's LIVENESS is checked, not assumed. A socket the
        // browser still believes is OPEN can be dead (sleep, a network change,
        // a black-holed path): it never fires onclose, so it never re-dialed,
        // and the page stayed live and "active" while the broker had long
        // released the lease and handed it to another device. Now:
        //   - every socket owns its state (timers, outstanding ping, role):
        //     the rec's `cur` is the socket in charge, `cand` a replacement
        //     being dialed. Nothing about one socket is kept on the rec, so a
        //     late event on one can never clear the other's timers;
        //   - a ping carries a nonce `n` and only the pong echoing it counts;
        //     a pong also carries the live lease, which goes through
        //     onControlStatus like a status frame (level-idempotent);
        //   - a socket whose FIRST status carried `gen` + `bootId` is from a
        //     broker that pongs, so only such a socket gets a pong deadline
        //     (an older broker never pongs and must not be flapped);
        //   - a missed deadline dials a replacement BEFORE dropping the old
        //     socket (make-before-break). The old socket keeps working until
        //     the replacement's first status, or until it misses a second
        //     deadline; a failed replacement never takes the old one down. A
        //     false positive (a stalled main thread) therefore costs one extra
        //     socket, never the lease;
        //   - a dial still CONNECTING after 5 s is abandoned (Chromium leaves a
        //     black-holed upgrade CONNECTING for ~4 min, which would park the
        //     re-dial past any recovery);
        //   - a page that becomes visible re-pings at once (the wake probe),
        //     and a backed-off re-dial is brought forward.
        const controlSockets = new Map();   // hostId -> { cur, cand, ws, ... }
        const CONTROL_PING_MS = 25000;
        const CONTROL_PONG_TIMEOUT_MS = 10000;
        const CONTROL_CONNECT_TIMEOUT_MS = 5000;
        let _controlPingSeq = 0;
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
            // A replacement already in flight becomes the socket in charge on
            // its first status; dialing a third would only race it.
            if (existing && existing.cand) return;
            const rec = existing || {
                hostId: hid, ws: null, cur: null, cand: null, attempts: 0,
                lastOpenAt: 0, reattachTimer: null, closedByUs: false,
                wantActive: 0,
            };
            rec.closedByUs = false;
            controlSockets.set(hid, rec);
            _dialControl(rec, host, 'cur');
        }

        // Dial one /control socket for `rec` in `role` ('cur' or 'cand') and
        // wire its handlers to ITS OWN state `st`. rec.ws always mirrors
        // rec.cur's socket (sendBecomeActive and the startup guard read it).
        function _dialControl(rec, host, role) {
            const hid = rec.hostId;
            let url;
            try {
                url = hostWsUrl(host, '/control?clientId='
                    + encodeURIComponent(CLIENT_ID));
            } catch (e) { return null; }
            let ws;
            try { ws = new WebSocket(url); }
            catch (e) {
                if (role === 'cur') scheduleControlReattach(hid);
                return null;
            }
            const st = {
                rec: rec, ws: ws, role: role, done: false,
                pingTimer: null, deadlineTimer: null, connectTimer: null,
                pendN: null, lastN: 0, missed: 0, capable: false, statusSeen: false,
            };
            if (role === 'cur') { rec.cur = st; rec.ws = ws; }
            else rec.cand = st;
            st.connectTimer = setTimeout(() => {
                st.connectTimer = null;
                if (!st.done && ws.readyState === WebSocket.CONNECTING) {
                    _controlSocketGone(st, null);
                }
            }, CONTROL_CONNECT_TIMEOUT_MS);
            ws.onopen = () => {
                if (st.done) { try { ws.close(); } catch (_) {} return; }
                if (st.connectTimer) { clearTimeout(st.connectTimer); st.connectTimer = null; }
                if (st.role === 'cur') rec.attempts = 0;
                rec.lastOpenAt = Date.now();
                // #239: a new /control socket may be a new broker run, whose
                // control gens are re-seeded from its clock: forget this
                // host's, so its next view per terminal is taken whatever its
                // gen. (A 1012 close for a stalled send lands here too, via
                // scheduleControlReattach.)
                resetControlBaselines(hid);
                // The first ping goes out now, so a broker that pongs is
                // checked from the start; then one per CONTROL_PING_MS, which
                // also keeps the pipe warm through idle proxies.
                st.pingTimer = setInterval(() => _controlPing(st), CONTROL_PING_MS);
                _controlPing(st);
            };
            ws.onmessage = (ev) => {
                if (st.done) return;
                let msg;
                try { msg = JSON.parse(ev.data); } catch (_) { return; }
                if (!msg) return;
                if (msg.type === 'pong') { _controlPong(st, msg); return; }
                // #239 (#238): a terminal on this broker changed controller.
                // The one way a controlled window, which holds no /ws, hears
                // of a release it did not cause.
                if (msg.type === 'terminal_control') {
                    applyControlView(controlKey(hid, msg.id), msg, 'push');
                    return;
                }
                // #244: an MCP desktop op relayed by this broker to us, the
                // lease holder. Fire-and-forget: handleDesktopRpc owns its
                // own errors and always answers (or errors into) exactly one
                // desktop_result frame per rid. desktopReply answers only
                // while this socket is rec.ws: a replacement's first frame is
                // its status (promoting it) and a retired socket handles
                // nothing, so every socket that can deliver an rpc is rec.ws.
                if (msg.type === 'desktop_rpc') {
                    Promise.resolve(handleDesktopRpc(hid, rec, ws, msg))
                        .catch(function () {});
                    return;
                }
                if (!msg || msg.type !== 'status') return;
                _controlStatus(st, msg);
            };
            ws.onclose = (ev) => {
                if (st.done) return;             // retired by us — handled
                _controlSocketGone(st, ev);
            };
            ws.onerror = () => { /* onclose schedules the reconnect */ };
            return st;
        }

        // Send one nonce'd ping on `st`. The deadline protects the OLDEST ping
        // still unanswered (st.pendN): a periodic ping never pushes a running
        // deadline out. `restart` (the wake probe) makes this ping the one
        // protected and counts the deadline from now, so a pong to any ping
        // sent BEFORE it no longer counts.
        function _controlPing(st, restart) {
            if (st.done || !st.ws || st.ws.readyState !== WebSocket.OPEN) return;
            const n = ++_controlPingSeq;
            st.lastN = n;
            desktopWsSend(st.ws, { type: 'ping', n: n });
            if (st.pendN === null || restart) {
                st.pendN = n;
                _armPongDeadline(st);
            }
        }
        // Only a socket whose first status proved a ponging broker, and only
        // while a ping is outstanding.
        function _armPongDeadline(st) {
            if (st.deadlineTimer) { clearTimeout(st.deadlineTimer); st.deadlineTimer = null; }
            if (st.done || !st.capable || st.pendN === null) return;
            st.deadlineTimer = setTimeout(() => {
                st.deadlineTimer = null;
                _controlPongMissed(st);
            }, CONTROL_PONG_TIMEOUT_MS);
        }
        function _controlPong(st, msg) {
            // Any pong to the protected ping or a later one on THIS socket
            // proves it alive since that ping; an older or foreign nonce does
            // not.
            if (st.pendN !== null && typeof msg.n === 'number'
                    && msg.n >= st.pendN && msg.n <= st.lastN) {
                st.pendN = null;
                st.missed = 0;
                if (st.deadlineTimer) { clearTimeout(st.deadlineTimer); st.deadlineTimer = null; }
            }
            // The pong carries the live lease (built like a status frame), so
            // a socket that is alive but MISSED a status -- the broker logs a
            // failed status send and moves on -- is corrected by its next
            // pong. onControlStatus is level-idempotent: a repeat of the
            // current level runs no transition and emits nothing new.
            if (typeof msg.active === 'boolean') {
                _controlStatus(st, { type: 'status', active: msg.active,
                    activeClientId: msg.activeClientId,
                    gen: msg.gen, bootId: msg.bootId });
            }
        }
        function _controlPongMissed(st) {
            if (st.done) return;
            st.missed += 1;
            st.pendN = null;
            const rec = st.rec;
            // A replacement that stops answering is just a failed replacement.
            if (st.role !== 'cur' || st.missed >= 2) {
                _controlSocketGone(st, null);
                return;
            }
            // First miss on the socket in charge: dial the replacement now and
            // give the old socket a second chance with a fresh ping.
            if (!rec.cand && !rec.closedByUs) {
                const h = hostById(rec.hostId);
                if (h) _dialControl(rec, h, 'cand');
            }
            _controlPing(st);
        }

        // A status frame (or a pong's lease) from socket `st`.
        function _controlStatus(st, msg) {
            const rec = st.rec;
            if (!st.statusSeen) {
                st.statusSeen = true;
                // Capability from the FIRST status, per socket: a broker that
                // stamps gen + bootId answers pings.
                st.capable = typeof msg.gen === 'number'
                    && typeof msg.bootId === 'string';
                _armPongDeadline(st);
                if (st.role === 'cand') _promoteControl(st);
            }
            if (rec.hostId === homeHostId()) _clearLostContact();
            onControlStatus(rec.hostId, msg);
            _settleWantActive(rec, msg);
            _noteLeaseHolding(rec.hostId, msg);
        }

        // #248 C1: the holder owns the PTY size (owner O1), so a NEW holding
        // re-sends this page's size for that host's windows -- once per
        // (host, bootId, gen) held. Keyed on the broker's generation, not on
        // "a status arrived": a silent re-acquire (this page never torn down,
        // the auto-claim bumped gen) reclaims, a remote host whose lease came
        // back without ever being masked reclaims, a broker restart (new
        // bootId) reclaims; an A2 replacement reporting the SAME holding, or a
        // repeated status/pong, does not (each reclaim is a forced resize the
        // agent answers by dropping its #130 keyframe). An older broker sends
        // no gen/bootId and never reclaims (its behaviour is unchanged).
        const _leaseHeld = new Map();       // hostId -> 'bootId#gen' reclaimed
        function _noteLeaseHolding(hostId, msg) {
            if (!msg.active || typeof msg.gen !== 'number'
                    || typeof msg.bootId !== 'string') return;
            const key = msg.bootId + '#' + msg.gen;
            if (_leaseHeld.get(hostId) === key) return;
            _leaseHeld.set(hostId, key);
            if (typeof reclaimHostSize === 'function') reclaimHostSize(hostId);
        }

        // The replacement reported: it is now the socket in charge, and the
        // old one is let go.
        function _promoteControl(st) {
            const rec = st.rec;
            const old = rec.cur;
            st.role = 'cur';
            rec.cand = null;
            rec.cur = st;
            rec.ws = st.ws;
            rec.attempts = 0;
            if (rec.reattachTimer) { clearTimeout(rec.reattachTimer); rec.reattachTimer = null; }
            if (old && old !== st) _retireControlSocket(old);
        }

        // Stop everything `st` owns and close its socket. Idempotent.
        function _retireControlSocket(st) {
            if (st.done) return;
            st.done = true;
            if (st.pingTimer) { clearInterval(st.pingTimer); st.pingTimer = null; }
            if (st.deadlineTimer) { clearTimeout(st.deadlineTimer); st.deadlineTimer = null; }
            if (st.connectTimer) { clearTimeout(st.connectTimer); st.connectTimer = null; }
            const ws = st.ws;
            st.ws = null;
            if (ws) {
                try {
                    ws.onopen = ws.onmessage = null;
                    ws.onclose = ws.onerror = null;
                } catch (_) {}
                try { ws.close(); } catch (_) {}
            }
        }

        // Socket `st` is gone: closed by the broker or the network, abandoned
        // by the connect watchdog, or out of pong deadlines (ev null).
        function _controlSocketGone(st, ev) {
            if (st.done) return;
            const rec = st.rec;
            const wasCur = rec.cur === st;
            _retireControlSocket(st);
            if (!wasCur) {
                if (rec.cand === st) rec.cand = null;
                // Nothing left in charge either: back to the normal re-dial.
                if (!rec.cur && !rec.closedByUs) scheduleControlReattach(rec.hostId);
                return;
            }
            rec.cur = null;
            rec.ws = null;
            if (ev && ev.code === 4401) {
                if (rec.cand) { _retireControlSocket(rec.cand); rec.cand = null; }
                // Token rejected — defer to the existing auth flow (the
                // auth-form success path re-dials this control WS). Do NOT
                // hammer-reconnect a broker that needs a password.
                pollStateFor(rec.hostId).authNeeded = true;
                try { showAuthOverlay(hostById(rec.hostId)); } catch (_) {}
                try { renderHostStatus(); } catch (_) {}
                return;
            }
            if (rec.closedByUs) return;          // closeControlWs / host removed
            if (rec.hostId === homeHostId() && rec.lastOpenAt) _armLostContact();
            // A replacement in flight takes charge when it reports; its own
            // failure re-dials (above).
            if (rec.cand) return;
            scheduleControlReattach(rec.hostId);
        }

        // The wake probe: a page coming back to the foreground re-checks
        // every channel NOW instead of at the next 25 s tick — a fresh nonce,
        // so a late pong to an older ping cannot satisfy it, and a deadline
        // counted from now, so timers that fire late on thaw cannot trip it.
        // A channel waiting out its backoff is re-dialed at once.
        onVisibility(function () {
            for (const rec of controlSockets.values()) {
                if (rec.closedByUs) continue;
                const st = rec.cur;
                if (st && !st.done && st.ws
                        && st.ws.readyState === WebSocket.OPEN) {
                    if (st.capable) _controlPing(st, true);
                } else {
                    _redialNow(rec);
                }
            }
        });
        // #248 B2 (owner O2): the page stays usable while its HOME lease
        // channel is down -- the overlay comes only from a real {active:
        // false} -- but after 10 s without one it says so. Only for a channel
        // that was up once (the startup guard in 85 owns "never reached"),
        // and not for a 4401 (the auth overlay owns that). Cleared by the
        // next home status.
        const LOST_CONTACT_NOTICE = 'lost contact with this broker — retrying…';
        const CONTROL_LOST_NOTICE_MS = 10000;
        let _lostContactTimer = null;
        function _armLostContact() {
            if (_lostContactTimer) return;
            _lostContactTimer = setTimeout(() => {
                _lostContactTimer = null;
                // (A home status in between cleared this timer.)
                const rec = controlSockets.get(homeHostId());
                if (!rec || rec.closedByUs) return;
                showNotice(LOST_CONTACT_NOTICE, { sticky: true, type: 'error' });
            }, CONTROL_LOST_NOTICE_MS);
        }
        function _clearLostContact() {
            if (_lostContactTimer) { clearTimeout(_lostContactTimer); _lostContactTimer = null; }
            try {
                const host = document.getElementById('notice-host');
                if (!host) return;
                for (const n of Array.from(host.querySelectorAll('.notice-sticky'))) {
                    if (n._noticeText === LOST_CONTACT_NOTICE) n.remove();
                }
            } catch (_) {}
        }

        // Bring a backed-off re-dial forward. Only a channel that is WAITING
        // to re-dial: a 4401'd one has no timer and waits for the auth form.
        function _redialNow(rec) {
            if (!rec || rec.closedByUs || rec.cur || rec.cand
                    || !rec.reattachTimer) return;
            clearTimeout(rec.reattachTimer);
            rec.reattachTimer = null;
            const h = hostById(rec.hostId);
            if (h) openControlWs(h);
        }

        // #248 A4: a HINT that this page may no longer hold `hostId`'s lease
        // -- a terminal closed 4409, a terminal `error` not_active (its input
        // or resize was dropped), a home PUT /state 409 not_active. A hint
        // never tears anything down itself: it asks the broker NOW, by a
        // ping whose pong carries the live lease (routed to onControlStatus),
        // so the broker stays the one authority. In-flight guard (S3): a
        // stale page with an owed push re-PUTs on every poll, so a probe is a
        // no-op while a deadline is already running or a replacement is
        // already dialing. No channel at all: the waiting re-dial is brought
        // forward, and its first status answers instead.
        function probeControl(hostId) {
            const rec = controlSockets.get(hostId);
            if (!rec || rec.closedByUs || rec.cand) return;
            const st = rec.cur;
            if (!st || st.done || !st.ws) { _redialNow(rec); return; }
            if (st.ws.readyState !== WebSocket.OPEN) return;  // its status answers
            // A deadline is already running -- or the broker is an older one
            // that never pongs, whose first ping therefore stays outstanding.
            if (st.pendN !== null) return;
            _controlPing(st);
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
            // #248: both sockets — the one in charge and any replacement.
            if (rec.cur) _retireControlSocket(rec.cur);
            if (rec.cand) _retireControlSocket(rec.cand);
            rec.cur = rec.cand = null;
            rec.ws = null;
            controlSockets.delete(hostId);
        }
        // #195: the one member of the invalidation set that is not a cache but
        // a live SOCKET — a lease channel still dialing the old address has to
        // go, or the host keeps a connection to a broker it no longer is. It is
        // idempotent (a no-op when nothing is open), which is what the registry
        // asks of a clearer.
        //
        // A REMOTE host's channel comes back on the poll loop's next tick. The
        // HOME broker's does NOT: the loop skips it outright (75, "the HOME
        // control WS is opened at boot"), boot has long since run, and the only
        // other opener is the auth overlay's submit — which needs a 401 first.
        // So closing home's here and walking away would retire the
        // single-active lease for the life of the page: no further
        // lease:changed, a takeover by another browser never learned (two live
        // writers, which is the exact thing the lease exists to prevent), and a
        // "Become active" button that silently does nothing because
        // sendBecomeActive finds no socket. Nothing in core invalidates the
        // home host today, but #195 documents `host:changed` -> invalidate as
        // THE pattern, and both new edge sites (the hidden checkbox, the colour
        // swatch) render for the local row — so a mod following the manual
        // could reach it. Re-dial immediately instead; openControlWs is
        // idempotent against a live socket.
        registerHostCache('controlWs', id => {
            closeControlWs(id);
            if (id === homeHostId()) {
                try { openControlWs(localHost()); } catch (_) {}
            }
        });

        // #248 B1: a take-back click is an INTENT, kept for 15 s. With the
        // channel down (reconnecting, or a stale socket the click's probe
        // then replaces) it used to vanish silently -- the overlay button
        // and the lease chip did nothing. Now the click says so, brings the
        // re-dial forward, and the next status that still says "not you"
        // sends become_active once. Any {active:true} clears the intent (S9),
        // so an auto-claim on reconnect can never leave it armed for another
        // device's later takeover to trip; the deferred send never re-arms
        // it, so two pages cannot ping-pong the lease without a new click.
        const CONTROL_WANT_ACTIVE_MS = 15000;
        // `deferred`: the send _settleWantActive makes for an earlier click --
        // it never records a new intent and never dials.
        function sendBecomeActive(host, deferred) {
            host = host || localHost();
            if (!host) return;
            let rec = controlSockets.get(host.id);
            if (!rec || !rec.ws || rec.ws.readyState !== WebSocket.OPEN) {
                if (deferred) return;
                if (!rec) {
                    openControlWs(host);
                    rec = controlSockets.get(host.id);
                } else {
                    _redialNow(rec);
                }
                if (rec) rec.wantActive = Date.now();
                showNotice('reconnecting to ' + (host.label || host.id)
                    + ' — will take over when connected');
                return;
            }
            if (!deferred) rec.wantActive = Date.now();
            if (_leaseAffordance.armed && host.id === homeHostId()) {
                _leaseAffordance.asked = true;
            }
            try { rec.ws.send(JSON.stringify({ type: 'become_active' })); }
            catch (_) {}
            // The socket may be the stale one: check it, so a dead channel is
            // replaced and the intent above lands on the replacement.
            if (!deferred && typeof probeControl === 'function') probeControl(host.id);
        }
        // Called after every status the channel delivers (64 _controlStatus).
        function _settleWantActive(rec, msg) {
            if (!rec.wantActive) return;
            if (msg.active) { rec.wantActive = 0; return; }
            const fresh = (Date.now() - rec.wantActive) < CONTROL_WANT_ACTIVE_MS;
            rec.wantActive = 0;
            const host = hostById(rec.hostId);
            if (fresh && host) sendBecomeActive(host, true);
        }

        function onControlStatus(hostId, msg) {
            const active = !!msg.active;
            if (hostId === homeHostId()) {
                _homeActive = active;
                // try/finally, because the emit must survive a transition that
                // throws. teardownView is synchronous, so a throw inside it
                // propagates out of here and would skip the emit — and the
                // level is only retained by an emit that actually happens, so
                // the loss would be permanent: a repeat frame carrying the same
                // {active:false} hits `if (_deactivated) return` and tears down
                // nothing, and a late subscriber would replay a stale level.
                // The finally does not paper over the throw (it still
                // propagates to the frame handler); it only stops one broken
                // transition from muting the lease for the rest of the page.
                try {
                    if (active) becomeActiveTransition();
                    else deactivateTransition();
                } finally {
                // #195: the single-active lease, as a LEVEL — the event that
                // workspaces' read of the core-private _deactivated becomes
                // once #204 migrates it. That read is still shipped; this is
                // the channel that makes deleting it possible, not the
                // deletion itself.
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
                // before any handler runs (teardownView is synchronous), and a
                // gain has already set _deactivated=false (both bootActiveView
                // and rebuildView do it synchronously, before their first
                // await), so a handler that reacts by writing shared state is
                // no longer gated out. Note the asymmetry is deliberate and the
                // claim is only what it says: on a GAIN the rebuild is still in
                // flight when handlers run. It has to be — onControlStatus is
                // the frame handler and cannot await — and the event names the
                // LEASE, which core has genuinely learned, not the view. A
                // repeated {active:X} is a level no-op, which is the same
                // idempotence the transition wrappers below already enforce.
                    if (typeof _emitModEvent === 'function') {
                        _emitModEvent('lease:changed', { active: _homeActive });
                    }
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
            if (!_deactivated) {                         // already active
                // #239: the overlay a refused Take back put up over a live
                // view comes down on the answer to its own button.
                if (_leaseAffordance.armed && _leaseAffordance.asked) {
                    _leaseAffordance.armed = false;
                    _leaseAffordance.asked = false;
                    hideBecomeActiveOverlay();
                }
                return;
            }
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

        // ---- desktop relay (#244) ------------------------------------------
        // The browser half of the MCP desktop relay (design/broker.md ##
        // Wire contract). The broker sends {type:'desktop_rpc', rid:int, op,
        // args} to ONE designated /control socket of the lease holder; this
        // handler answers exactly one {type:'desktop_result', rid, ...} per
        // rid. args carry BROKER-LOCAL integer window ids, resolved against
        // the host id of the connection that DELIVERED the frame -- never
        // the home broker's namespace for a frame that arrived on a remote
        // connection (the gh#161 trap: a null host silently hits the
        // serving origin; this handler never fetches anything, it reads the
        // merged sessions/windows maps).
        //
        // Every mutation rides the normal mutators (savePrefs + relayout, so
        // peer browsers converge through the debounced /state push) plus one
        // explicit pushState() flush BEFORE the reply, so an acknowledged op
        // has already been offered to /state under our lease. It runs only
        // while this browser holds its lease -- the _deactivated re-check
        // after each await answers lost_lease rather than landing a
        // half-applied op -- never waits on animation, and applies each rid
        // exactly once via a small answered-rid cache.
        //
        // Scope rule (the owner's #244 words, enforced where the topology is
        // known): a NON-admin caller's frame carries args.visible -- the int
        // ids it can see on this broker. An op may apply only when every
        // window it AFFECTS is in that set: the named window, plus the whole
        // column for move_column/set_width, plus both windows' columns for
        // tab_into/split. Keys outside this broker's namespace: a terminal
        // of ANOTHER broker has no id in visible and is invisible to this
        // caller by construction -- it gates (review round 2, R2-1) -- while
        // app windows carry no MCP identity on any broker and never gate.
        // The refusal is the same unknown_window as any unknown id, so the
        // gate leaks nothing. Workspace ops are admin-only at the broker and
        // mutate the HOME broker's /state blob, so they answer not_home on
        // any other connection.
        const _DESKTOP_OPS = Object.create(null);
        ['get', 'focus', 'move_column', 'set_width', 'tile', 'float',
         'set_geometry', 'tab_into', 'split'].forEach(function (k) {
            _DESKTOP_OPS[k] = true;
        });
        const _DESKTOP_WS_OPS = Object.create(null);
        ['workspaces:list', 'workspaces:add', 'workspaces:rename',
         'workspaces:remove', 'workspaces:switch',
         'workspaces:send-window'].forEach(function (k) {
            _DESKTOP_WS_OPS[k] = true;
        });
        // rid -> the desktop_result payload already sent. The broker sends
        // each rpc to ONE designated socket, so a duplicate frame should be
        // rare; this cache is the belt -- a replayed rid re-serves its answer
        // without re-applying. Bounded; oldest evicted.
        const _desktopAppliedRids = new Map();
        const _DESKTOP_RID_CACHE_MAX = 128;
        function _desktopRememberRid(rid, payload) {
            _desktopAppliedRids.delete(rid);
            _desktopAppliedRids.set(rid, payload);
            while (_desktopAppliedRids.size > _DESKTOP_RID_CACHE_MAX) {
                _desktopAppliedRids.delete(_desktopAppliedRids.keys().next().value);
            }
        }
        // The ONE ws.send call site in this fragment (the terminal-funnel
        // enumeration pins the count): both the control-ping keepalive and
        // every desktop_result reply ride it.
        function desktopWsSend(ws, payload) {
            try { ws.send(JSON.stringify(payload)); } catch (_) {}
        }
        function desktopLeaseLost() {
            try { return _deactivated === true; } catch (_) { return false; }
        }
        function desktopReply(rec, ws, rid, payload) {
            // Socket identity: a reply must never ride a socket the rec has
            // already replaced (openControlWs reuses recs across reconnects).
            if (!rec || rec.ws !== ws) return;
            desktopWsSend(ws, Object.assign(
                { type: 'desktop_result', rid: rid }, payload));
        }
        // Broker-local int id (the DELIVERING connection's namespace) ->
        // winKey, or null. The ONLY place desktop ids are resolved.
        function resolveDesktopWinKey(hid, id) {
            if (!Number.isInteger(id)) return null;
            const key = hid + ':' + id;
            if (sessions.has(key) || windows.has(key)
                    || findKeyInLayout(key)) return key;
            return null;
        }
        // A winKey's int id on THIS connection's broker, or null when the
        // key belongs to another namespace (app windows, other brokers).
        function _desktopIntId(hid, key) {
            const ci = key.indexOf(':');
            if (ci <= 0) return null;
            if (key.slice(0, ci) !== hid) return null;
            const tail = key.slice(ci + 1);
            if (!/^\d+$/.test(tail)) return null;
            return Number(tail);
        }
        // The winKeys of every window the op would reshape through `key`'s
        // membership: its whole column when tiled, just itself otherwise.
        // PER-OP RULE (review round 1, N4): focus arranges nothing, so ONLY
        // the named window gates for it (singleOnly in desktopVisibleGate);
        // move_column/set_width reshape the whole column, tab_into/split
        // both windows' columns, and tile/float/set_geometry reshape a tiled
        // window's column too (a floating one is alone in its blast radius).
        function _desktopAffectedKeys(hid, key) {
            const loc = findKeyInLayout(key);
            if (!loc) return [key];
            return columnKeys(loc.col);
        }
        // A key belonging to a TERMINAL of another broker (a host prefix
        // that is not this connection's). Its id can never appear in this
        // caller's visible set -- the broker lists only its own windows --
        // so for a non-admin it is invisible BY CONSTRUCTION and gates the
        // same way an invisible window of this broker does (review round 2,
        // R2-1: "a non-admin agent may arrange only the windows it can
        // see"). APP windows are the exception: they have no MCP identity
        // on any broker, so they never gate. Admin (visible === null) never
        // reaches this at all.
        function _desktopForeignTerminal(hid, key) {
            const ci = key.indexOf(':');
            if (ci <= 0) return false;
            const host = key.slice(0, ci);
            if (host === hid) return false;          // this broker's namespace
            if (host === 'app') return false;        // app: keys carry no identity
            const win = windows.get(key);
            if (win && win.type === 'app') return false;
            return true;
        }
        function desktopVisibleGate(hid, visible, keys, singleOnly) {
            if (visible === null) return true;          // admin, unrestricted
            const seen = Object.create(null);
            for (let i = 0; i < visible.length; i++) seen[visible[i]] = true;
            for (const key of keys) {
                const affected = singleOnly ? [key]
                    : _desktopAffectedKeys(hid, key);
                for (const k of affected) {
                    if (_desktopForeignTerminal(hid, k)) return false;
                    const id = _desktopIntId(hid, k);
                    if (id !== null && seen[id] !== true) return false;
                }
            }
            return true;
        }
        // ctx.commands result -> desktop_result payload. execute() resolves a
        // closed vocabulary that never rejects; command VALUES carry data or
        // a {refusal} this maps onto the wire's error codes, so nothing but
        // genuine success is ok:true on the wire.
        function desktopCommandResult(r) {
            if (r && r.ok === true) {
                const v = r.value;
                if (v && typeof v === 'object'
                        && typeof v.refusal === 'string') {
                    if (v.refusal === 'needs_confirm') {
                        return { ok: false, error: 'needs_confirm',
                                 detail: { count: v.windows || 0 } };
                    }
                    if (v.refusal === 'bad_args') {
                        return { ok: false, error: 'bad_args', detail: 'args' };
                    }
                    if (v.refusal === 'last_workspace'
                            || v.refusal === 'unknown_window') {
                        return { ok: false, error: v.refusal };
                    }
                }
                return { ok: true, value: v === undefined ? null : v };
            }
            return { ok: false,
                     error: (r && typeof r.reason === 'string')
                         ? r.reason : 'error' };
        }
        // `get`'s snapshot, as THIS browser sees the desktop. Rows: every
        // live window (minimized and host-hidden included; app windows with
        // id:null; dormant layout keys excluded -- no live window, nothing
        // to arrange). A non-admin caller keeps only rows whose id is in its
        // visible set (the broker re-filters as a belt). Geometry is the
        // live floating box; the broker relabels identity from its registry.
        function desktopSnapshot(hid, visible, wsResult) {
            const L = getLayout();
            let items = null, active = null, columnWs = null, floatWs = null;
            const wsv = (wsResult && wsResult.ok
                && wsResult.value && typeof wsResult.value === 'object')
                ? wsResult.value : null;
            if (wsv) {
                items = Array.isArray(wsv.items) ? wsv.items : null;
                active = (typeof wsv.active === 'number') ? wsv.active : null;
                columnWs = (wsv.columnWs && typeof wsv.columnWs === 'object'
                    && !Array.isArray(wsv.columnWs)) ? wsv.columnWs : null;
                floatWs = (wsv.floatWs && typeof wsv.floatWs === 'object'
                    && !Array.isArray(wsv.floatWs)) ? wsv.floatWs : null;
            }
            const wsIdx = Object.create(null);
            let activeWsId = null;
            if (items) for (const it of items) {
                if (it && typeof it.id === 'string') wsIdx[it.id] = it.index;
                if (it && active !== null && it.index === active
                        && typeof it.id === 'string') activeWsId = it.id;
            }
            const wsField = function (wsId) {
                if (typeof wsId !== 'string') return null;  // null = all-ws
                const i = wsIdx[wsId];
                return (typeof i === 'number') ? i : null;
            };
            const rows = [];
            for (const ent of windows) {
                const key = ent[0], win = ent[1];
                if (!win || win.disposed) continue;
                const id = _desktopIntId(hid, key);
                if (visible !== null
                        && (id === null || visible.indexOf(id) === -1)) continue;
                const sess = sessions.get(key);
                const loc = findKeyInLayout(key);
                const vci = loc ? visibleColIndex(loc.col) : -1;
                const row = {
                    key: key,
                    id: id,
                    title: (sess && sess.title) || win.name || key,
                    kind: (win.type === 'app') ? 'app' : 'terminal',
                    tiled: !!win.tiled,
                    minimized: !!win.minimized,
                    column: (loc && vci !== -1) ? vci : null,
                    row: loc ? loc.rowIndex : null,
                    tabIndex: loc ? loc.tabIndex : null,
                    width_preset: loc ? (loc.col.widthPreset || null) : null,
                };
                if (loc && loc.row) {
                    row.tabbed = loc.row.mode === 'tabbed';
                    row.split = loc.row.mode === 'split';
                    if (typeof loc.col.widthFrac === 'number') {
                        row.widthFrac = loc.col.widthFrac;
                    }
                }
                if (win.tiled) {
                    row.geometry = null;
                    const cid = loc && loc.col ? loc.col.id : null;
                    const wsId = (cid !== null && columnWs)
                        ? (columnWs[cid] !== undefined ? columnWs[cid]
                                                      : activeWsId)
                        : activeWsId;
                    row.workspace = wsField(wsId);
                } else {
                    const g = currentFloatGeom(win);
                    row.geometry = { x: g.left, y: g.top,
                                     w: g.width, h: g.height };
                    let wsId;
                    if (!floatWs || floatWs[key] === undefined) wsId = activeWsId;
                    else wsId = floatWs[key];
                    row.workspace = wsField(wsId);
                }
                rows.push(row);
            }
            return {
                viewport: { w: window.innerWidth, h: window.innerHeight },
                mode: L.mode,
                focused_column: L.focusedCol,
                workspaces: items,
                active_workspace: active,
                home: hid === homeHostId(),
                windows: rows,
            };
        }
        // One core (non-workspaces) op. Pure dispatcher over the EXISTING
        // mutators: validates and gates BEFORE the first mutator call, and
        // the whole op is synchronous, so no /state adopt can interleave
        // between validation and application.
        function desktopCoreOp(hid, op, args, visible) {
            const badArgs = function (detail) {
                return { ok: false, error: 'bad_args', detail: detail };
            };
            const refuseGate = function (keys, singleOnly) {
                if (!desktopVisibleGate(hid, visible, keys, singleOnly)) {
                    return { ok: false, error: 'unknown_window' };
                }
                return null;
            };
            const key = resolveDesktopWinKey(hid, args.id);
            if (key === null) return { ok: false, error: 'unknown_window' };
            const win = windows.get(key);
            const live = (win && !win.disposed) ? win : null;
            if (op === 'focus') {
                if (!live) return { ok: false, error: 'unknown_window' };
                // Focus arranges nothing: only the named window gates (N4) --
                // refusing because an invisible sibling shares the column
                // would be over-strict.
                const g = refuseGate([key], true);
                if (g) return g;
                revealAndFocusWindow(key);
                return { ok: true, value: {
                    focused_column: getLayout().focusedCol,
                    controlled: !!live.controlled,
                } };
            }
            if (op === 'move_column') {
                const loc = findKeyInLayout(key);
                if (!loc) return badArgs('not_tiled');
                const g = refuseGate([key]);
                if (g) return g;
                if (args.to === 'left' || args.to === 'right') {
                    moveColumn(visibleColIndex(loc.col),
                               args.to === 'left' ? -1 : 1);
                } else if (Number.isInteger(args.to)) {
                    dragDropNewColumn(key, args.to);
                } else return badArgs('to');
                const loc2 = findKeyInLayout(key);
                return { ok: true, value: {
                    column: loc2 ? visibleColIndex(loc2.col) : null,
                } };
            }
            if (op === 'set_width') {
                if (WIDTH_PRESETS.indexOf(args.preset) === -1) {
                    return badArgs('preset');
                }
                if (!live || !findKeyInLayout(key)) return badArgs('not_tiled');
                const g = refuseGate([key]);
                if (g) return g;
                setColumnPreset(live, args.preset);
                return { ok: true, value: { preset: args.preset } };
            }
            if (op === 'tile') {
                if (!live) return { ok: false, error: 'unknown_window' };
                const g = refuseGate([key]);
                if (g) return g;
                attachToStrip(live);
                return { ok: true, value: { tiled: true } };
            }
            if (op === 'float') {
                if (!live) return { ok: false, error: 'unknown_window' };
                const g = refuseGate([key]);
                if (g) return g;
                detachToFloat(live);
                return { ok: true, value: { tiled: false } };
            }
            if (op === 'set_geometry') {
                if (!live) return { ok: false, error: 'unknown_window' };
                if (live.tiled) return badArgs('not_floating');
                const num = function (v) {
                    return typeof v === 'number' && isFinite(v);
                };
                if (!num(args.x) || !num(args.y) || !num(args.w) || !num(args.h)) {
                    return badArgs('geometry');
                }
                const g = refuseGate([key]);
                if (g) return g;
                applyGeomToWindow(live, clampGeom({
                    left: args.x, top: args.y, width: args.w, height: args.h,
                }));
                // Persist the box AS APPLIED: a size-locked window keeps its
                // locked w/h, and prefs/value must not claim otherwise.
                const gm = currentFloatGeom(live);
                live.geom = { left: gm.left, top: gm.top,
                              width: gm.width, height: gm.height };
                getPref(key).geom = Object.assign({}, live.geom);
                savePrefs();
                refitSoon(live);
                return { ok: true, value: { geometry: {
                    x: gm.left, y: gm.top, w: gm.width, h: gm.height,
                } } };
            }
            if (op === 'tab_into' || op === 'split') {
                if (args.id === args.target) return badArgs('target');
                const tkey = resolveDesktopWinKey(hid, args.target);
                if (tkey === null) return { ok: false, error: 'unknown_window' };
                const twin = windows.get(tkey);
                if (!live || !twin || twin.disposed) {
                    return { ok: false, error: 'unknown_window' };
                }
                if (op === 'split' && args.axis !== 'row'
                        && args.axis !== 'column') return badArgs('axis');
                // Gate on the PRE-op topology: both windows plus every
                // window of the columns this op leaves or reshapes.
                const g = refuseGate([key, tkey]);
                if (g) return g;
                if (op === 'tab_into') {
                    tabWindowInto(live, twin);
                } else {
                    // The split mutators need the drag key IN the layout;
                    // floats attach first (the same handoff the UI drag
                    // makes). Validation above already ran, and attach +
                    // split are one sync block -- nothing can interleave.
                    if (!findKeyInLayout(key)) attachToStrip(live);
                    if (!findKeyInLayout(tkey)) attachToStrip(twin);
                    splitAtWindow(key, tkey,
                                  args.axis === 'row' ? 'bottom' : 'right');
                }
                const loc2 = findKeyInLayout(key);
                return { ok: true, value: {
                    column: loc2 ? visibleColIndex(loc2.col) : null,
                    row: loc2 ? loc2.rowIndex : null,
                } };
            }
            return { ok: false, error: 'bad_op' };
        }
        async function handleDesktopRpc(hid, rec, ws, msg) {
            let out = null;
            let rid = msg ? msg.rid : undefined;
            try {
                if (!Number.isInteger(rid)
                        || !msg || typeof msg.op !== 'string') {
                    desktopReply(rec, ws,
                        Number.isInteger(rid) ? rid : null,
                        { ok: false, error: 'bad_args', detail: 'rid' });
                    return;
                }
                const cached = _desktopAppliedRids.get(rid);
                if (cached !== undefined) {
                    desktopReply(rec, ws, rid, cached);
                    return;
                }
                const argsOf = function () {
                    return (msg.args && typeof msg.args === 'object'
                        && !Array.isArray(msg.args)) ? msg.args : {};
                };
                const visibleOf = function (args) {
                    // args.visible: int[] for a non-admin caller, null for an
                    // admin. Absent or malformed fails CLOSED (empty set).
                    if (args.visible === null) return null;
                    if (Array.isArray(args.visible)) {
                        return args.visible.filter(function (v) {
                            return Number.isInteger(v);
                        });
                    }
                    return [];
                };
                if (desktopLeaseLost()) {
                    out = { ok: false, error: 'lost_lease' };
                } else if (_DESKTOP_OPS[msg.op] === true) {
                    const args = argsOf();
                    const visible = visibleOf(args);
                    if (msg.op === 'get') {
                        // The one await: the workspaces store is read BEFORE
                        // the snapshot so decoration and layout agree on the
                        // post-await world (a read command; it writes
                        // nothing, so not_home does not apply).
                        let wsr = null;
                        if (typeof _execModCommand === 'function') {
                            wsr = await _execModCommand('workspaces:list');
                        }
                        if (desktopLeaseLost()) {
                            out = { ok: false, error: 'lost_lease' };
                        } else {
                            out = { ok: true,
                                    value: desktopSnapshot(hid, visible, wsr) };
                        }
                    } else {
                        out = desktopCoreOp(hid, msg.op, args, visible);
                        if (out.ok) {
                            // Flush before ack: success means the /state
                            // write was attempted under our lease.
                            if (!desktopLeaseLost()) {
                                try { await pushState(); } catch (_) {}
                            }
                            if (desktopLeaseLost()) {
                                out = { ok: false, error: 'lost_lease' };
                            }
                        }
                    }
                } else if (_DESKTOP_WS_OPS[msg.op] === true) {
                    // Workspace data lives in the HOME broker's /state blob;
                    // mutating it from any other broker's relay would edit
                    // the wrong desktop.
                    if (hid !== homeHostId()) {
                        out = { ok: false, error: 'not_home' };
                    } else {
                        const args = argsOf();
                        let r = null;
                        if (msg.op === 'workspaces:send-window') {
                            // The wire's `win` int resolves in THIS
                            // connection's namespace before dispatch.
                            const k = resolveDesktopWinKey(hid, args.win);
                            if (k === null) {
                                out = { ok: false, error: 'unknown_window' };
                            } else {
                                args.key = k;
                                r = await _execModCommand(msg.op, args);
                            }
                        } else {
                            r = await _execModCommand(msg.op, args);
                        }
                        if (r !== null) {
                            out = desktopLeaseLost()
                                ? { ok: false, error: 'lost_lease' }
                                : desktopCommandResult(r);
                            // Flush before ack, exactly as the core branch
                            // (S1): every MUTATING workspace op awaits
                            // pushState() first -- 'list' is a read -- and
                            // the lease is re-checked after the flush.
                            if (out.ok === true
                                    && msg.op !== 'workspaces:list') {
                                if (!desktopLeaseLost()) {
                                    try { await pushState(); } catch (_) {}
                                }
                                if (desktopLeaseLost()) {
                                    out = { ok: false, error: 'lost_lease' };
                                }
                            }
                        }
                    }
                } else {
                    out = { ok: false, error: 'bad_op' };
                }
            } catch (e) {
                out = { ok: false, error: 'error',
                        detail: String((e && e.message) || e) };
            }
            if (out !== null) {
                _desktopRememberRid(rid, out);
                desktopReply(rec, ws, rid, out);
            }
        }
