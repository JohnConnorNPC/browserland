        // Tiling-membership GC bookkeeping (#147 follow-up; used by the poll
        // sweep below). key -> ms timestamp of the FIRST successful poll that
        // failed to mention it. Any reappearance deletes the entry, so only a
        // CONTINUOUS absence ages toward the grace cutoff. Deliberately not
        // persisted: a fresh page starts every key's clock at zero, which fails
        // safe (nothing is dropped for the first grace window after a load).
        const layoutGcMiss = new Map();
        const LAYOUT_GC_GRACE_MS = 120000;   // 2 min of continuous absence

        // ---- taskbar ------------------------------------------------------
        // #123: render one ordered span per visible label part (id can now
        // interleave), replacing the old fixed idSpan+titleSpan pair. Rebuild
        // from composeLabelParts each call so a toggle/order change converges on
        // the next tick. Each span keeps its per-part class (.ti-id dim color +
        // <110px container auto-hide, .ti-title ellipsis anchor). Re-insert
        // BEFORE the trailing .ti-ws workspace badge (added by 62_js_workspaces)
        // so the badge + off-workspace dimming survive per-tick relabels. The
        // .ti-ws badge carries no .ti-part class, so the clear pass never removes
        // it and it never duplicates.
        function renderChipLabel(el, sess) {
            el.querySelectorAll('.ti-part').forEach(n => n.remove());
            const frag = document.createDocumentFragment();
            for (const p of composeLabelParts(sess)) {
                const span = document.createElement('span');
                span.className = 'ti-part ' + p.cls;      // e.g. "ti-part ti-host"
                span.textContent = p.text;
                frag.appendChild(span);
            }
            const ws = el.querySelector('.ti-ws');
            if (ws) el.insertBefore(frag, ws); else el.appendChild(frag);
        }
        // Single label/tooltip composer: only this function writes el.title,
        // so the stale suffix and the always-everything tooltip stay
        // consistent no matter who triggers the update.
        function updateTaskbarLabel(key) {
            const el = document.querySelector(
                '.taskbar-item[data-session-id="' + cssEscape(key) + '"]');
            if (!el) return;
            const sess = sessions.get(key);
            if (!sess) return;
            renderChipLabel(el, sess);
            el.title = formatTooltip(sess)
                + (sess.stale ? ' — not responding' : '');
        }

        function updateTaskbarColor(id) {
            const el = document.querySelector(
                '.taskbar-item[data-session-id="' + cssEscape(id) + '"]');
            if (!el) return;
            const win = windows.get(id);
            const pref = prefs[String(id)] || {};
            // #103/#115: mirror openWindow's seed so a chip without an open window
            // still reflects the same color — live win.color, saved pref.color,
            // the launch-profile default (#115), the host default (#103), then the
            // palette auto-pick.
            const ci = String(id).indexOf(':');
            const hid = ci !== -1 ? String(id).slice(0, ci) : 'local';
            const sess = sessions.get(id);
            const color = (win && win.color) || pref.color
                || profileDefaultColor(hid, sess && sess.profile)
                || hostDefaultColor(hid) || defaultColor(id);
            el.style.setProperty('--accent', color);
        }

        function buildTaskbarItem(s) {
            // dataset.sessionId carries the session KEY; the visible chip
            // shows the real window id. Label + tooltip are refreshed by
            // updateTaskbarLabel right after insertion (renderChipLabel here
            // paints the initial label so a new chip is never momentarily blank).
            const key = s.key != null ? String(s.key) : String(s.id);
            const el = document.createElement('div');
            el.className = 'taskbar-item';
            el.dataset.sessionId = key;
            const pref = prefs[key] || {};
            // #103/#115: seed from the launch-profile default (#115), else the
            // host default (#103), when there is no saved per-window color — so
            // the chip matches a would-be terminal on that host/profile.
            const ci = key.indexOf(':');
            const hid = ci !== -1 ? key.slice(0, ci) : 'local';
            el.style.setProperty('--accent',
                pref.color || profileDefaultColor(hid, s.profile)
                || hostDefaultColor(hid) || defaultColor(key));
            renderChipLabel(el, s);   // #123: ordered .ti-part spans
            el.addEventListener('click', () => onTaskbarClick(key));
            return el;
        }

        function onTaskbarClick(id) {
            // If the window lives on another (parked) workspace — tiled OR a
            // workspace-locked floating window (task 8/16) — switch there first
            // so it mounts visibly, instead of toggling minimize on a window
            // that's hidden off the active workspace. Membership holds whether
            // or not the window is currently open.
            const targetWs = workspaceIndexForKey(id);
            const switched = (targetWs !== null
                && targetWs !== getLayout().activeWs);
            if (switched) switchWorkspace(targetWs);
            const win = windows.get(id);
            if (!win) {
                // App docs (sticky note / editor) reopen via their own factory
                // from the persisted store; terminals re-open from /sessions.
                if (appStore[id]) openAppWindow(appStore[id]);
                else openWindow(id, sessions.get(id));
                return;
            }
            // A chip click must never silently no-op: if an existing float is
            // still masked off-workspace after the switch attempt (e.g. a
            // dangling membership the switch couldn't resolve), re-home it to
            // the active ws and reveal it before we focus it. Never touch an
            // all-workspaces float (windowWsId === null) — it is never hidden by
            // design — and record the reveal so the minimize-toggle below does
            // not immediately round-trip it back to hidden.
            let revealed = false;
            if (!win.tiled && windowWsId(win) !== null
                && win.dom.classList.contains('ws-hidden')) {
                setWindowWs(win, activeWorkspace().id, true);  // reveal on active ws
                revealed = true;
            }
            if (win.minimized) { restoreWindow(id); return; }
            // Never minimize a window we just revealed (here or from another ws).
            if (!switched && !revealed && frontId === id) { minimizeWindow(id); return; }
            bringToFront(id);
        }

        // Hard ceiling on how long ONE refresh run may hold refreshInFlight.
        // Both timers (the 2 s slow poll and the 250 ms auto-open fast poll) and
        // every manual gesture that refreshes early-return while it is held, so
        // a run that wedges freezes the desktop's whole heartbeat until F5.
        const REFRESH_WATCHDOG_MS = POLL_MS * 5;
        async function refreshTaskbar() {
            // Coalesce overlapping calls: a slow /sessions response from the
            // 2s tick must not race a fast-poll tick during auto-open.
            if (refreshInFlight) return;
            refreshInFlight = true;
            const seq = ++refreshSeq;
            // Watchdog: no awaited sub-call may hold the GLOBAL mutex forever.
            // Every fetch below is deadlined today, but this is the backstop for
            // the next await someone adds — a wedged tick is indistinguishable
            // from a dead UI. A force-release alone would be a BUG (two runs
            // mutating sessions/windows at once), so releasing the mutex ALSO
            // retires the generation: taking a run's mutex away is exactly the
            // moment it stops being the current run, so it must bail at its next
            // await whether or not a newer run has started yet — and its
            // `finally` must no longer clear a mutex someone else may hold.
            const watchdog = setTimeout(() => {
                if (refreshSeq === seq && refreshInFlight) {
                    refreshSeq += 1;
                    refreshInFlight = false;
                }
            }, REFRESH_WATCHDOG_MS);
            try {
                await refreshTaskbarInner(seq);
            } finally {
                clearTimeout(watchdog);
                // Only the run that still OWNS the mutex may release it: after a
                // watchdog force-release a newer run holds it, and clearing it
                // here would let a third run in alongside that one.
                if (refreshSeq === seq) refreshInFlight = false;
            }
        }

        async function pollHost(host) {
            const st = pollStateFor(host.id);
            // This site owns its deadline (timeoutMs: 0 opts out of hostFetch's,
            // which is disarmed once the response HEADERS land): the one below
            // spans the json() read as well, so a broker that answers and then
            // stalls its body can never wedge refreshTaskbar's in-flight guard.
            const ctrl = new AbortController();
            const timer = setTimeout(() => ctrl.abort(), FETCH_TIMEOUT_MS);
            try {
                const r = await hostFetch(host, '/sessions',
                                      { signal: ctrl.signal, timeoutMs: 0 });
                if (r.status === 401) {
                    // A READABLE 401 = host up, wrong/missing password (the
                    // broker sends CORS headers on 401s precisely so this
                    // is distinguishable). A fetch TypeError lands in the
                    // catch instead: host down — or a pre-CORS broker.
                    st.ok = false;
                    st.consecFailures += 1;
                    st.authNeeded = true;
                    showAuthOverlay(host);
                    return;
                }
                const list = await r.json();
                if (!Array.isArray(list)) throw new Error('bad /sessions payload');
                st.ok = true;
                st.everOk = true;
                st.consecFailures = 0;
                st.authNeeded = false;
                authPrompted.delete(host.id);
                st.sessions = list;
                // This answer has not been consumed by a tick yet. Ticks no
                // longer wait for the poll, so without this a tick that reused
                // last-good data would count a window's absence a SECOND time
                // and close it well inside its intended grace window.
                st.fresh = true;
            } catch (e) {
                st.ok = false;
                st.consecFailures += 1;
            } finally {
                clearTimeout(timer);
            }
        }

        // One outstanding poll per host, ever. The tick fires this and does NOT
        // block on it: a host that is black-holed (asleep, off the tailnet,
        // silently firewalled) holds its connection open for the full
        // FETCH_TIMEOUT_MS, which spans several ticks, and stacking a fresh
        // doomed request on it every tick would be both pointless and a slow
        // socket leak. Never rejects — pollHost swallows its own errors, and the
        // catch here keeps a future refactor from producing an unhandled
        // rejection at a fire-and-forget call site.
        // The slot is only safe to hold because pollHost's own abort covers the
        // headers AND the body read, so the promise ALWAYS settles within
        // FETCH_TIMEOUT_MS — that deadline, not this map, is what guarantees a
        // host gets re-polled. (Consequence: editing a host's url/token while its
        // poll hangs costs at most one poll cycle before the new config is used.)
        function pollHostOnce(host) {
            const st = pollStateFor(host.id);
            if (st.pollPromise) return null;   // already hanging on this host
            st.polling = true;
            const p = pollHost(host).catch(() => {}).then(() => {
                st.polling = false;
                st.pollPromise = null;
            });
            st.pollPromise = p;
            return p;
        }

        // ---- host status chips ----------------------------------------------
        // ONE broker: a chip per host, exactly as before #149 — ok/down/auth/
        // lease, the per-broker hide toggle always reachable, the auth chip
        // the click-to-login fallback once the overlay's one auto-pop was
        // cancelled. MORE than one broker: the chips collapse into a single
        // aggregate badge (worst state colors it, every host gets a line in
        // its tooltip) and the per-host detail lives on the live broker rows
        // of the start (+) menu, which the badge opens on click.
        function hostChipState(st) {
            if (st.authNeeded) return 'auth';
            // Never answered, and its FIRST poll is still out: "not known yet",
            // not "down". The tick no longer waits for the poll, so without this
            // every host would paint red on the first tick and a merely slow
            // (WAN, waking) broker would flash red before its first answer —
            // exactly the flap the barrier used to hide by rendering nothing at
            // all. 'pending' has no .host-chip rule on purpose: it falls back to
            // the neutral base chip.
            if (!st.everOk && st.polling) return 'pending';
            // Unreachable outranks the lease state (a down host's lease is
            // unknown); a transient blip stays 'ok' (or 'lease') — don't flap.
            if (!st.ok && (!st.everOk
                    || st.consecFailures >= STALE_AFTER_FAILURES)) {
                return 'down';
            }
            if (st.leaseInactive) return 'lease';   // reachable, not our lease
            return 'ok';
        }
        // The (+) menu must not paint a merely never-polled host as dead:
        // pollStateFor() records start ok=false/everOk=false, which
        // hostChipState reads as 'down' before a poll has even been TRIED.
        // consecFailures > 0 is the "actually failed" proof (the rule the
        // menu's old launchHostDownSuffix carried); until then, 'pending'.
        function hostMenuState(st) {
            const s = hostChipState(st);
            if (s === 'down' && !st.consecFailures) return 'pending';
            return s;
        }
        // Short state phrase, shared by the aggregate badge's tooltip and the
        // (+) menu's broker rows — one string source, so the two surfaces can
        // never describe the same state differently. '' for ok; callers
        // append it after a dash only when non-empty (the badge tooltip
        // spells 'ok' explicitly instead).
        function hostStateSuffix(state) {
            if (state === 'auth') return 'password required';
            if (state === 'lease') return 'another browser is active';
            if (state === 'down') return 'down';
            if (state === 'pending') return 'checking…';
            return '';
        }
        // The single-broker chip's tooltip, verbatim as it has always read.
        // The (+) menu's broker rows reuse it, so both surfaces describe the
        // same click the same way.
        function hostChipTip(host, state) {
            let tip = host.id === 'local'
                ? 'this broker' : host.url;
            if (state === 'auth') {
                tip += ' — password required (click to log in)';
            } else if (state === 'lease') {
                tip += ' — another browser is active here (click to '
                    + 'take over)';
            } else {
                if (state === 'down') {
                    tip += ' — unreachable (broker down, or running a '
                        + 'pre-CORS webterm version)';
                } else if (state === 'pending') {
                    tip += ' — checking…';
                }
                tip += host.hidden
                    ? ' — hidden (click to show)'
                    : ' — click to hide';
            }
            return tip;
        }
        // The (+) menu's live broker row (#149): the same three actions the
        // taskbar chip has always had, but decided at CLICK time, not at
        // build time. The row's LABEL can lag one poll (~2 s) behind reality
        // (renderHostStatus repaints the open menu on the next status
        // change); the ACTION re-reads the state, so it never acts on a
        // stale snapshot — an ok row that just lost its lease requests the
        // lease back instead of hiding the windows (codex review). keepOpen:
        // the hide toggle re-renders the still-open menu (via the repaint
        // renderHostStatus triggers); auth/lease close it because their
        // overlay / notice takes over from there.
        function hostMenuItems(host) {
            const state = hostMenuState(pollStateFor(host.id));
            const suffix = hostStateSuffix(state);
            const label = host.label
                + (suffix ? ' — ' + suffix : '')
                + (host.hidden ? ' — hidden' : '');
            const action = () => {
                const h = hostById(host.id) || host;
                const now = hostMenuState(pollStateFor(h.id));
                if (now === 'auth') {
                    hideCtxMenu();
                    showAuthOverlay(h, true);
                    return;
                }
                if (now === 'lease') {
                    hideCtxMenu();
                    sendBecomeActive(h);
                    // Honest wording: the broker's {active:true} push is
                    // what confirms the takeover, not this click.
                    showNotice('takeover requested — ' + h.label);
                    return;
                }
                toggleHostHidden(h.id);
                const fresh = hostById(h.id);
                if (fresh && fresh.hidden) {
                    showNotice(h.label
                        + ' hidden — its windows are masked, not closed');
                }
            };
            return [{
                label,
                enabled: true,
                action,
                keepOpen: true,
                // 'broker-row', not 'host-row': the settings pane already
                // owns an UNSCOPED .host-row rule (15_css_dialogs) that
                // would silently restyle a menu row wearing that name.
                cls: 'broker-row' + (host.hidden ? ' off' : ''),
                title: hostChipTip(host, state),
                swatch: { state, color: strictHex(host.color) },
            }];
        }
        // A single broker keeps exactly the pre-#149 chip: label, state
        // class, identity border, and the state's own click action.
        function renderSingleHostChip(el, host, state) {
            const chip = document.createElement('span');
            chip.className = 'host-chip ' + state
                + (host.hidden ? ' off' : '');
            // #103: the per-host identity color paints this broker chip's
            // BORDER, thicker. Inline style beats the .host-chip.<state>
            // class's border-color (inline > class), so the identity color
            // wins the border — while the state classes' TEXT color (down=red,
            // auth=amber, lease=blue, ok=green) stays untouched and legible.
            // strictHex rejects a corrupted value → the status border as today.
            const chipColor = strictHex(host.color);
            if (chipColor) {
                chip.style.borderColor = chipColor;
                chip.style.borderWidth = '2px';
            }
            chip.textContent = host.label;
            if (state === 'auth') {
                // Auth chips stay login-on-click: a broker you can't reach
                // has no live windows to hide, and one-click re-login wins.
                chip.addEventListener('click',
                    () => showAuthOverlay(host, true));
            } else if (state === 'lease') {
                // Another browser is active on this remote broker; its
                // windows are masked. Click to take the lease (the broker's
                // {active:true} push then unmasks them) — mirrors the auth
                // chip's click-to-login.
                chip.addEventListener('click',
                    () => sendBecomeActive(host));
            } else {
                chip.addEventListener('click',
                    () => toggleHostHidden(host.id));
            }
            chip.title = hostChipTip(host, state);
            el.appendChild(chip);
        }
        // #149: two or more brokers collapse into one badge. The WORST state
        // colors it (auth > down > lease — a fault is never hidden behind a
        // healthy majority), but the COUNT covers every abnormal host, so one
        // auth host cannot mask three down ones ('4 brokers · 4 need
        // attention'). Per-host detail rides the tooltip/aria-label; the
        // click opens the start (+) menu, where each broker has a live row
        // carrying the chip's old per-host actions.
        function renderAggregateChip(el, hosts, states) {
            const chip = document.createElement('span');
            const bad = states.filter(s =>
                s === 'auth' || s === 'down' || s === 'lease').length;
            const worst = ['auth', 'down', 'lease'].find(
                s => states.indexOf(s) !== -1)
                || (states.indexOf('pending') !== -1 ? 'pending' : 'ok');
            let text = hosts.length + ' brokers';
            if (bad) {
                text += ' · ' + bad
                    + (bad === 1 ? ' needs attention' : ' need attention');
            } else if (worst === 'pending') {
                text += ' · checking…';
            }
            chip.className = 'host-chip agg ' + worst
                + (hosts.every(h => h.hidden) ? ' off' : '');
            chip.textContent = text;
            const lines = hosts.map((h, i) =>
                h.label + ' — ' + (hostStateSuffix(states[i]) || 'ok')
                + (h.hidden ? ' — hidden' : ''));
            chip.title = lines.join('\n') + '\n— click for the broker menu';
            // Keyboard/AT parity is cheap here, and multi-line titles don't
            // exist on touch — the per-host breakdown rides aria-label too.
            chip.setAttribute('role', 'button');
            chip.tabIndex = 0;
            chip.setAttribute('aria-label',
                text + ': ' + lines.join('; ') + ' — opens the broker menu');
            const open = () => {
                const r = chip.getBoundingClientRect();
                openLaunchMenu(r.left, r.bottom);
            };
            chip.addEventListener('click', open);
            chip.addEventListener('keydown', (e) => {
                if (e.key === 'Enter' || e.key === ' ') {
                    e.preventDefault();   // Space must not scroll the page
                    open();
                }
            });
            el.appendChild(chip);
        }
        function renderHostStatus() {
            const el = document.getElementById('host-status');
            if (!el) return;   // 9 of its 10 call sites are unguarded (#149)
            const hosts = allHosts();
            const states = hosts.map(h => hostChipState(pollStateFor(h.id)));
            el.textContent = '';
            if (hosts.length === 1) {
                renderSingleHostChip(el, hosts[0], states[0]);
            } else {
                renderAggregateChip(el, hosts, states);
            }
            // The (+) menu shows these same states on its broker rows; every
            // status repaint refreshes it through the owner + signature gate
            // in 76, so an OPEN menu tracks auth/lease/down transitions
            // instead of going behaviorally stale (codex review). A
            // no-change tick is a no-op there, so this never eats a click or
            // a hover.
            repaintLaunchMenu();
        }

        // ---- auto-reattach eligibility ------------------------------------
        // The healing pass used to re-dial only a CLOSED socket. A socket stuck
        // in CONNECTING is the case that actually hurts: a black-holed host
        // (asleep, off the tailnet, silently dropped) never refuses the upgrade,
        // so Chromium sits in CONNECTING for ~240 s with no onclose at all — no
        // onclose means no scheduleReattach, so the window was never healed and
        // rendered as a silent black rectangle until F5.
        //
        // The danger in relaxing the gate is re-dialling a HEALTHY socket that
        // was dialled milliseconds ago, which would make a slow-but-fine connect
        // unable to ever finish. There is no dial timestamp reachable from here
        // (win.lastOpenAt is 0 until the socket OPENS, and reattachAt is only
        // written by scheduleReattach, i.e. by an onclose that never came), so
        // age is measured from the first TICK that saw this exact socket in
        // CONNECTING — always an UNDER-estimate of its real age, so the gate errs
        // toward waiting. Keyed on socket identity so each re-dial restarts the
        // clock. 73's connect watchdog closes such sockets sooner; this is the
        // belt to that pair of braces, so it waits longer on purpose.
        const WS_CONNECTING_STUCK_MS = 8000;
        function reattachable(win, now) {
            const ws = win.ws;
            if (!ws || ws.readyState === WebSocket.CLOSED) {
                win._connectingWs = null;
                return true;                 // the original condition
            }
            if (ws.readyState !== WebSocket.CONNECTING) {
                win._connectingWs = null;    // OPEN / CLOSING — not ours to heal
                return false;
            }
            if (win._connectingWs !== ws) {  // first sighting of THIS socket
                win._connectingWs = ws;
                win._connectingSince = now;
                return false;
            }
            return (now - win._connectingSince) >= WS_CONNECTING_STUCK_MS;
        }

        // hostId -> ms of the last /profiles warm-up fired at it. The warm-up is
        // no longer awaited, and fetchProfiles' cache only populates when the
        // answer LANDS, so without this the 250 ms fast poll would fire a fresh
        // /profiles at the same host every tick until the first came back.
        const profilesWarmAt = new Map();
        const PROFILES_WARM_RETRY_MS = 5000;

        // ---- late profile-color correction (#115) -------------------------
        // The /profiles warm-up is fired, not awaited (see refreshTaskbarInner),
        // so a window can open against a COLD cache and paint its frame from the
        // host/auto color. openWindow paints the frame exactly ONCE and
        // updateTaskbarColor only ever fixes the chip, so without this pass that
        // window keeps the wrong frame color for its whole life. This is what
        // lets the correction land on a later tick instead of never: once a
        // host's /profiles is cached, its windows re-seed from their launch-
        // profile color.
        //
        // Runs every tick rather than once-per-host-per-warm on purpose: it is a
        // map lookup per open window, and "already reconciled" state is exactly
        // the kind of sticky flag that strands the one window that happened to
        // be mid-restore when it was set. Idempotent by construction — a window
        // the user has recolored (a saved pref.color, the top of openWindow's
        // precedence chain) is never touched, a host with no profile color for
        // that window is skipped, and a window already wearing the right color
        // short-circuits — so the steady state is a no-op.
        function applyWarmProfileColors() {
            for (const [key, win] of windows) {
                if (win.disposed || !win.dom || !win.hostId) continue;
                if ((prefs[key] || {}).color) continue;   // user's pick wins
                const sess = sessions.get(key);
                const want = profileDefaultColor(win.hostId,
                    sess && sess.profile);
                if (!want) continue;      // cold cache / no profile color: later
                const hex = normalizeHex(want);
                if (hex === win.color) continue;
                win.color = hex;
                win.dom.style.setProperty('--accent', hex);
                win.dom.classList.toggle('dark-accent', isDarkAccent(hex));
                updateTaskbarColor(key);
            }
        }

        // How long a tick will wait for the polls it just started before it
        // renders with what it has. Deliberately far below both POLL_MS (2000)
        // and FAST_POLL_MS (250): a healthy broker answers in single-digit ms,
        // so the common case is unchanged, while a dead one costs the tick this
        // and no more. Its answer, if it ever comes, lands in hostPolls and is
        // picked up by a later tick.
        const HOST_POLL_SETTLE_MS = 150;

        async function refreshTaskbarInner(seq) {
            // Torn down (HOME lease lost): no session polling, no /state pull,
            // no auto-reattach. The slow interval is also stopped by
            // teardownView, but this guard covers an in-flight tick.
            if (_deactivated) return;
            const hosts = allHosts();
            // NOT a barrier. `await Promise.all(hosts.map(pollHost))` made every
            // tick cost the deadline of the SLOWEST host before a single chip
            // rendered — one black-holed broker degraded the 2 s heartbeat to
            // 3 s+ and collapsed the 250 ms restore poll entirely, which is the
            // "unavailable server locks the interface up" report. Each poll now
            // settles on its own into its host's hostPolls record, and the tick
            // waits only a short budget for the ones it just started. A host
            // whose answer misses that budget keeps its LAST-GOOD record (the
            // retention this file already relies on) and lands on a later tick;
            // a host that has never answered reads as 'pending', not 'down'.
            const started = [];
            for (const host of hosts) {
                const p = pollHostOnce(host);
                if (p) started.push(p);
            }
            if (started.length) {
                let budget;
                const capped = new Promise(res => {
                    budget = setTimeout(res, HOST_POLL_SETTLE_MS);
                });
                await Promise.race([Promise.all(started), capped]);
                clearTimeout(budget);
            }
            // A teardown can land DURING the poll await; re-check so the rest
            // (which repopulates sessions/taskbar DOM, opens control sockets,
            // runs GC savePrefs, kicks pullState) never runs against a torn-
            // down view. The seq check is the same guard against a run the
            // watchdog force-released: superseded, so it must not touch shared
            // state again (see refreshTaskbar).
            if (_deactivated || seq !== refreshSeq) return;

            // Poll state for hosts no longer configured is dropped, never
            // merged — their windows were closed by removeHost().
            const hostIds = new Set(hosts.map(h => h.id));
            for (const id of Array.from(hostPolls.keys())) {
                if (!hostIds.has(id)) hostPolls.delete(id);
            }

            // Lazily open a control WS per REACHABLE remote host so its single-
            // active lease can mask our view of it (the HOME control WS is
            // opened at boot). Once opened the rec persists with its own
            // reconnect logic, so this fires at most once per host.
            for (const host of hosts) {
                if (host.id === 'local') continue;
                if (pollStateFor(host.id).ok && !controlSockets.has(host.id)) {
                    openControlWs(host);
                }
            }

            // ---- merge (all hosts) ----
            // Per-host last-good retention: a host's `sessions` survives
            // its failures, so a transiently unreachable broker keeps its
            // windows (dashed after STALE_AFTER_FAILURES misses) instead
            // of mass-closing — and never disturbs another host's.
            const merged = new Map();
            for (const host of hosts) {
                const st = pollStateFor(host.id);
                if (!st.everOk) continue;
                const stale = !st.ok
                    && st.consecFailures >= STALE_AFTER_FAILURES;
                for (const s of st.sessions) {
                    const key = host.id + ':' + String(s.id);
                    merged.set(key, {
                        key,
                        id: s.id,
                        sid: String(s.id),
                        title: s.title,
                        cols: s.cols,
                        rows: s.rows,
                        host: s.host,
                        pid: s.pid,
                        kind: s.kind,
                        agent: s.agent || '',   // absent on old brokers
                        cwd: s.cwd || '',       // absent on old brokers
                        profile: s.profile || '', // #115: launch profile (old brokers: '')
                        mcp: s.mcp || 'off',    // effective MCP mode (old brokers: off)
                        mcpKnown: ('mcp' in s), // broker reports MCP at all?
                        stale,
                        hostId: host.id,
                        hostLabel: host.label,
                    });
                }
            }

            // One-time-per-host prefs GC: webterm ids are random
            // (2^52 | rand32), so every launch mints a new localStorage
            // key — drop entries for ids that host no longer knows, after
            // its first successful NON-empty poll (an empty answer is
            // likely a broker that just restarted, before its agents
            // re-register). Open/pending keys survive. Keys belonging to a
            // host that is no longer configured go unconditionally.
            let dropped = 0;
            for (const host of hosts) {
                const st = pollStateFor(host.id);
                if (!st.ok || st.gcDone || !st.sessions.length) continue;
                st.gcDone = true;
                const prefix = host.id + ':';
                for (const k of Object.keys(prefs)) {
                    if (k.charAt(0) === '_') continue;
                    if (k.slice(0, prefix.length) !== prefix) continue;
                    if (merged.has(k) || windows.has(k)
                        || pendingOpens.has(k)) continue;
                    delete prefs[k];
                    dropped += 1;
                }
                // Prune synced per-window MCP pins for THIS host's now-dead
                // sessions, on the SAME "first OK non-empty poll" safety (a
                // just-restarted broker's transient [] never reaches here, so a
                // live pin is never nuked before its agent re-registers). We
                // prune ONLY here — for a CONFIGURED host that answered — and
                // NEVER by "prefix not in hostIds": mcpModes is shared via
                // /state, but a remote host's id is browser-local random, so
                // deleting an unknown-prefix key would wipe ANOTHER browser's
                // pin. (Trade: a removed remote host's pins leak as a few small
                // strings until that browser reloads — harmless.)
                const mm = getSettings().mcpModes;
                for (const k of Object.keys(mm)) {
                    if (k.slice(0, prefix.length) !== prefix) continue;
                    if (merged.has(k) || windows.has(k)
                        || pendingOpens.has(k)) continue;
                    delete mm[k];
                    dropped += 1;
                }
            }
            // ---- tiling-membership GC (#147 follow-up) --------------------
            // The prefs sweep above skips reserved ('_'-prefixed) keys, so
            // `_layout` outlives a dead session's prefs and a workspace hoards
            // its column forever — one real deploy reached 47 dead keys on a
            // single workspace, which is what made the switcher preview look
            // wrong whichever way it rendered.
            //
            // Deliberately NOT reusing the one-shot `gcDone` gate above. That
            // gate ("first OK non-empty poll") only proves ONE agent has
            // re-registered: right after a broker restart a fast agent can
            // satisfy it while slower SURVIVING agents are still absent, and
            // dropping their membership would lose real placement — tolerable
            // for a browser-local pref, not for `_layout`, which is synced and
            // shared. So a key must be continuously missing from this host's
            // successful polls for LAYOUT_GC_GRACE_MS before it is dropped; any
            // reappearance resets it. Runs every poll (not once per load), so a
            // long-lived page keeps converging instead of hoarding.
            let layoutDropped = 0;
            for (const host of hosts) {
                const st = pollStateFor(host.id);
                if (!st.ok || !st.sessions.length) continue;
                const prefix = host.id + ':';
                // Collect BEFORE mutating: removeKeyFromLayout rewrites the very
                // rows/cells we would otherwise still be walking.
                const seen = [];
                for (const lws of getLayout().workspaces) {
                    for (const lcol of (lws.columns || [])) {
                        for (const lrow of (lcol.rows || [])) {
                            for (const k of rowKeys(lrow)) {
                                // `app:` is a reserved namespace, not a host — an
                                // app window's content lives in the app store and
                                // no session poll can attest to it. Skip it even if
                                // some persisted host is literally called 'app'.
                                if (k.slice(0, 4) === 'app:') continue;
                                if (k.slice(0, prefix.length) !== prefix) continue;
                                seen.push(k);
                            }
                        }
                    }
                }
                const now = Date.now();
                for (const k of seen) {
                    if (merged.has(k) || windows.has(k) || pendingOpens.has(k)) {
                        layoutGcMiss.delete(k);          // alive -> reset the clock
                        continue;
                    }
                    const since = layoutGcMiss.get(k);
                    if (since === undefined) { layoutGcMiss.set(k, now); continue; }
                    if (now - since < LAYOUT_GC_GRACE_MS) continue;
                    if (removeKeyFromLayout(k)) layoutDropped += 1;
                    layoutGcMiss.delete(k);
                }
            }
            if (layoutDropped) {
                savePrefs();
                // Layout mutations must be paired with a re-render; the dropped
                // keys were dormant so the strip is unchanged, but the workspace
                // dots/preview read `_layout` live.
                renderWorkspaces();
            }
            for (const k of Object.keys(prefs)) {
                if (k.charAt(0) === '_') continue;
                const cut = k.indexOf(':');
                if (cut === -1) continue;
                if (!hostIds.has(k.slice(0, cut))) {
                    delete prefs[k];
                    dropped += 1;
                }
            }
            if (dropped) savePrefs();

            const itemsHost = document.getElementById('taskbar-items');
            for (const [key, sess] of merged) {
                sessions.set(key, sess);
                // Passive title sync — reflect the producer's title and the
                // broker-stamped host together.
                const win = windows.get(key);
                if (win) {
                    win.missingPolls = 0;
                    const display = formatTitle(sess);
                    if (display !== win.name) {
                        win.name = display;
                        win.titleText.textContent = display;
                    }
                    // Keep the MCP robot's highlight in sync with the effective
                    // mode (broker-default change, re-assert, etc. — #20).
                    if (win.refreshMcpBtn) win.refreshMcpBtn();
                }
                let el = itemsHost.querySelector(
                    '.taskbar-item[data-session-id="' + key + '"]');
                if (!el) {
                    el = buildTaskbarItem(sess);
                    itemsHost.appendChild(el);
                }
                el.classList.toggle('stale', !!sess.stale);
                updateTaskbarLabel(key);
            }

            // ---- re-assert per-window MCP pins ----
            // The saved mcpModes[key] is the DESIRED policy; each tick, every
            // OPEN window whose broker-reported mode differs gets ONE idempotent
            // POST /session/mcp to its OWN broker (mirrors setWindowMcpMode's
            // host resolution). Mismatch-gated so it clears the moment the
            // broker reflects the pin, and so an UNTOUCHED window (no pin) keeps
            // inheriting the live broker default. This restores the mode after a
            // refresh, a cross-browser move, or a same-id broker reconnect.
            // Guards: skip pre-MCP brokers (mcpKnown false) so we never hammer
            // an old broker that can't honour it; lease-gate (a remote broker
            // another browser owns is leaseInactive — only its active browser
            // re-asserts, so two views never fight over it; the HOME host is
            // never leaseInactive); de-dup in-flight POSTs via _mcpAsserting,
            // each bounded by FETCH_TIMEOUT_MS so a hung fetch frees its key.
            // This pass NEVER calls savePrefs(), so it can't feed the /state
            // push loop.
            for (const [key, sess] of merged) {
                if (!windows.has(key)) continue;       // only attached windows
                if (!sess.mcpKnown) continue;          // pre-MCP broker
                if (_mcpAsserting.has(key)) continue;  // already driving this key
                const want = getMcpMode(key);
                if (!want || want === sess.mcp) continue;
                if (pollStateFor(sess.hostId).leaseInactive) continue;
                const host = hostById(sess.hostId);
                if (!host) continue;
                _mcpAsserting.add(key);
                // Site-owned deadline (timeoutMs: 0 opts out of hostFetch's,
                // which ends at the response headers): this one is only cleared
                // once the json() read has settled too, which is what actually
                // guarantees the key is freed.
                const ctrl = new AbortController();
                const timer = setTimeout(() => ctrl.abort(), FETCH_TIMEOUT_MS);
                hostFetch(host, '/session/mcp', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ id: sess.sid, mode: want }),
                    signal: ctrl.signal,
                    timeoutMs: 0,
                }).then(r => r.json().catch(() => null)).then(j => {
                    // Adopt optimistically ONLY if the user hasn't re-pinned
                    // this key meanwhile (a stale assert must never clobber a
                    // newer choice; the next poll reconciles the broker).
                    if (j && j.ok && getMcpMode(key) === want) sess.mcp = want;
                }).catch(() => {}).finally(() => {
                    clearTimeout(timer);
                    _mcpAsserting.delete(key);
                });
            }

            // Removal: a session drops out of `merged` only when ITS host
            // answered successfully and omitted it (transient failures
            // never get here — per-host last-good retention above).
            // Taskbar-only entries go immediately; OPEN windows get a
            // grace period (missingPolls + stale style) because right
            // after a broker restart the answer is honestly [] until
            // agents re-register (≤10 s) — only MISSING_POLLS_CLOSE
            // consecutive misses close the window, and the counter only
            // advances on ticks where that window's OWN host answered OK
            // (a down remote must never close another host's windows, nor
            // its own while it's unreachable) — and only on ticks that saw a
            // NEW answer from it (`fresh`). The poll is no longer awaited, so a
            // tick can re-render the previous answer; counting that as another
            // miss would shrink the grace window (12 s) toward nothing on a host
            // whose replies straddle a tick.
            for (const el of Array.from(itemsHost.querySelectorAll('.taskbar-item'))) {
                const key = el.dataset.sessionId;
                // App docs are not server sessions — live app docs own their
                // taskbar chip manually and must never be reaped by the poll
                // (which would accrue missingPolls and closeWindow them). Key
                // off the SESSION kind: an open app doc keeps a kind:'app'
                // session entry. Closed docs no longer have a session/chip at
                // all (closeWindow drops both), so they never reach this loop.
                const appSess = sessions.get(key);
                if (appSess && appSess.kind === 'app') continue;
                if (merged.has(key)) continue;
                const win = windows.get(key);
                if (win && !win.disposed) {
                    const hostSt = hostPolls.get(win.hostId);
                    if (hostSt && hostSt.ok && hostSt.fresh) {
                        win.missingPolls = (win.missingPolls || 0) + 1;
                    }
                    el.classList.add('stale');
                    const sess = sessions.get(key);
                    if (sess) { sess.stale = true; updateTaskbarLabel(key); }
                    if (win.missingPolls >= MISSING_POLLS_CLOSE) {
                        el.remove();
                        sessions.delete(key);
                        showNotice('session ' + win.sid + ' disappeared');
                        closeWindow(key);
                    }
                } else {
                    el.remove();
                    sessions.delete(key);
                }
            }
            // Every host's latest answer has now been accounted for. Cleared
            // here, at the one point after the last reader and before the tick's
            // remaining (await-free) work, so the next tick can tell a NEW answer
            // from a redisplay of this one.
            for (const st of hostPolls.values()) st.fresh = false;

            // Empty message only after some broker has answered at least
            // once — a fresh page with every broker down shows nothing.
            // Lives inside #taskbar-items: appended to #taskbar it would
            // sit right of the flex-1 strip, hugging the far edge.
            const anyEverOk = hosts.some(h => pollStateFor(h.id).everOk);
            let emptyMsg = document.getElementById('taskbar-empty');
            if (merged.size === 0 && anyEverOk) {
                if (!emptyMsg) {
                    emptyMsg = document.createElement('div');
                    emptyMsg.id = 'taskbar-empty';
                    emptyMsg.textContent = 'no sessions registered';
                    itemsHost.appendChild(emptyMsg);
                }
            } else if (emptyMsg) {
                emptyMsg.remove();
            }

            applyHostVisibilityAll();
            applyWorkspaceVisibility();   // assign/mask floating wins per ws (task 8)
            updateTaskbarActive();
            applyTaskbarWorkspace();      // ws badges + off-ws dimming (task 16)
            renderHostStatus();

            // ---- id-directed auto-open ----
            // /launch returns the new id and ?session= deep links seed it;
            // open each parked key the moment it shows up in its host's
            // /sessions.
            // #115: warm each reachable host's /profiles (which carries the
            // per-profile color map) so openWindow can seed a parked/restored
            // window's FRAME from its launch-profile color. Fired, NEVER awaited:
            // this is an optimisation, not a precondition, and awaiting it here
            // put an unbounded-by-nature request inside the global heartbeat
            // mutex — one stalled host meant no /sessions poll, no host-status
            // repaint, no auto-open, no dead-window reaping, no auto-reattach and
            // no /state pull, for as long as it stalled. `pendingOpens` is
            // non-empty on every page load with restored windows, so that was the
            // routine path, and only F5 got out of it.
            // fetchProfiles caches (and never throws), so this is one fetch per
            // host per page load; the `ok` gate keeps us off unreachable/unauth
            // hosts (no auth pop), and profilesWarmAt keeps an un-landed request
            // from being re-fired every 250 ms tick (a failed attempt leaves no
            // cache entry, so it retries after PROFILES_WARM_RETRY_MS).
            if (pendingOpens.size) {
                for (const h of hosts) {
                    if (!pollStateFor(h.id).ok) continue;
                    if (profilesCache.has(h.id)) continue;
                    const last = profilesWarmAt.get(h.id) || 0;
                    if ((Date.now() - last) < PROFILES_WARM_RETRY_MS) continue;
                    profilesWarmAt.set(h.id, Date.now());
                    fetchProfiles(h);
                }
            }
            // The cost of not waiting: a window opened against a COLD cache falls
            // through to the host/auto color. openWindow paints the frame once,
            // so the correction has to be re-applied here once the cache lands.
            applyWarmProfileColors();
            if (pendingOpens.size) {
                const now = Date.now();
                for (const [key, deadline] of Array.from(pendingOpens)) {
                    if (merged.has(key)) {
                        pendingOpens.delete(key);
                        restoreKeys.delete(key);
                        openWindow(key, merged.get(key));
                    } else if (now >= deadline) {
                        pendingOpens.delete(key);
                        if (restoreKeys.has(key)) {
                            // Restore-on-refresh is best-effort: no "not found"
                            // notice (the user didn't ask for THIS open). Prune
                            // the dead key from openTerms ONLY once its host has
                            // actually answered — a transient unreachable / auth-
                            // pending host at refresh time must not permanently
                            // drop a still-valid session from the restore set.
                            restoreKeys.delete(key);
                            const ci = key.indexOf(':');
                            const hid = ci !== -1 ? key.slice(0, ci) : 'local';
                            if (pollStateFor(hid).everOk) removeOpenTerm(key);
                        } else {
                            showNotice('session '
                                + key.slice(key.indexOf(':') + 1)
                                + ' not found');
                        }
                    }
                }
            }
            if (!pendingOpens.size) stopFastPoll();

            // ---- auto-reattach ----
            // Re-dial windows whose WS died while their session is still
            // alive (agent reconnect bounce, broker restart). Gated on a
            // SUCCESSFUL poll of THAT window's host this tick: last-good
            // data must never trigger dials at a dead broker — a down WSL
            // box must never re-dial (or starve) the other host's windows
            // — and 4401 (authFailed) never retries.
            const now = Date.now();
            for (const [key, win] of windows) {
                if (win.disposed || win.wsOpen) continue;
                if (win.authFailed || win.staleSession) continue;
                if (!merged.has(key)) continue;
                const hostSt = hostPolls.get(win.hostId);
                if (!hostSt || !hostSt.ok) continue;
                if (!reattachable(win, now)) continue;
                if (now < (win.reattachAt || 0)) continue;
                // A socket stuck in CONNECTING never produced an onclose, so it
                // never scheduled a reattach and its backoff never advanced.
                // Advance it HERE, before re-dialling, so a host that keeps
                // black-holing the upgrade backs off 2s/4s/8s… like any other
                // flapping window instead of being re-dialled every tick.
                if (win.ws && win.ws.readyState === WebSocket.CONNECTING) {
                    scheduleReattach(win);
                }
                reattachWindow(win);
            }

            // F0: pull the shared /state on the slow cadence (throttled so the
            // 250 ms auto-open fast-poll doesn't hammer it). Re-applies
            // settings+layout when another browser changed them (task 2).
            if (_stateReady && (now - _lastStatePoll) >= 1500) {
                _lastStatePoll = now;
                pullState(false);
            }

            // Tasks 13/15: keep each reachable REMOTE host's settings warm in
            // hostStateCache so its per-host settings apply and the per-host
            // settings tab opens instantly. Same slow cadence as
            // the /state pull above (a clone of _lastStatePoll) so the 250 ms
            // fast-poll never hammers remote brokers.
            if ((now - _lastHostStatePoll) >= 1500) {
                _lastHostStatePoll = now;
                for (const host of hosts) {
                    if (host.id === 'local') continue;
                    // Don't swap the cache object out from under an open remote
                    // settings tab mid-edit (the change handlers mutate it).
                    if (host.id === settingsOpenHostId) continue;
                    const hostSt = hostPolls.get(host.id);
                    if (hostSt && hostSt.ok) fetchHostState(host.id);
                }
            }
        }

