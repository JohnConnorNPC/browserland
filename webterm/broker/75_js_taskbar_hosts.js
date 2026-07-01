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

        async function refreshTaskbar() {
            // Coalesce overlapping calls: a slow /sessions response from the
            // 2s tick must not race a fast-poll tick during auto-open.
            if (refreshInFlight) return;
            refreshInFlight = true;
            try {
                await refreshTaskbarInner();
            } finally {
                refreshInFlight = false;
            }
        }

        async function pollHost(host) {
            const st = pollStateFor(host.id);
            const ctrl = new AbortController();
            const timer = setTimeout(() => ctrl.abort(), FETCH_TIMEOUT_MS);
            try {
                const r = await fetch(hostHttpUrl(host, '/sessions'),
                                      { signal: ctrl.signal });
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
            } catch (e) {
                st.ok = false;
                st.consecFailures += 1;
            } finally {
                clearTimeout(timer);
            }
        }

        // ---- host status chips ----------------------------------------------
        // One chip per host, ok/down/auth/lease. renderHostStatus() draws a chip
        // for every broker, always (even a single healthy local), so the
        // per-broker hide toggle is always reachable. The auth chip is the
        // click-to-login fallback once the overlay's one auto-pop was cancelled.
        function hostChipState(st) {
            if (st.authNeeded) return 'auth';
            // Unreachable outranks the lease state (a down host's lease is
            // unknown); a transient blip stays 'ok' (or 'lease') — don't flap.
            if (!st.ok && (!st.everOk
                    || st.consecFailures >= STALE_AFTER_FAILURES)) {
                return 'down';
            }
            if (st.leaseInactive) return 'lease';   // reachable, not our lease
            return 'ok';
        }
        function renderHostStatus() {
            const el = document.getElementById('host-status');
            const hosts = allHosts();
            const states = hosts.map(h => hostChipState(pollStateFor(h.id)));
            // One chip per broker, always (even a single healthy local), so
            // the per-broker hide toggle is always reachable.
            el.textContent = '';
            hosts.forEach((host, i) => {
                const chip = document.createElement('span');
                chip.className = 'host-chip ' + states[i]
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
                let tip = host.id === 'local'
                    ? 'this broker' : host.url;
                if (states[i] === 'auth') {
                    // Auth chips stay login-on-click: a broker you can't reach
                    // has no live windows to hide, and one-click re-login wins.
                    tip += ' — password required (click to log in)';
                    chip.addEventListener('click',
                        () => showAuthOverlay(host, true));
                } else if (states[i] === 'lease') {
                    // Another browser is active on this remote broker; its
                    // windows are masked. Click to take the lease (the broker's
                    // {active:true} push then unmasks them) — mirrors the auth
                    // chip's click-to-login.
                    tip += ' — another browser is active here (click to '
                        + 'take over)';
                    chip.addEventListener('click',
                        () => sendBecomeActive(host));
                } else {
                    if (states[i] === 'down') {
                        tip += ' — unreachable (broker down, or running a '
                            + 'pre-CORS webterm version)';
                    }
                    tip += host.hidden
                        ? ' — hidden (click to show)'
                        : ' — click to hide';
                    chip.addEventListener('click',
                        () => toggleHostHidden(host.id));
                }
                chip.title = tip;
                el.appendChild(chip);
            });
        }

        async function refreshTaskbarInner() {
            // Torn down (HOME lease lost): no session polling, no /state pull,
            // no auto-reattach. The slow interval is also stopped by
            // teardownView, but this guard covers an in-flight tick.
            if (_deactivated) return;
            const hosts = allHosts();
            await Promise.all(hosts.map(pollHost));
            // A teardown can land DURING the poll await; re-check so the rest
            // (which repopulates sessions/taskbar DOM, opens control sockets,
            // runs GC savePrefs, kicks pullState) never runs against a torn-
            // down view.
            if (_deactivated) return;

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
                const ctrl = new AbortController();
                const timer = setTimeout(() => ctrl.abort(), FETCH_TIMEOUT_MS);
                fetch(hostHttpUrl(host, '/session/mcp'), {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ id: sess.sid, mode: want }),
                    signal: ctrl.signal,
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
            // its own while it's unreachable).
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
                    if (hostSt && hostSt.ok) {
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
            // #115: warm each reachable host's /profiles (which now carries the
            // per-profile color map) BEFORE opening any parked/restored window, so
            // openWindow seeds the window FRAME from its launch-profile color on
            // the first paint. Without this, a restored window opened on the first
            // poll (cache cold) would fall through to the host/auto color and the
            // FRAME would never correct (updateTaskbarColor only fixes the chip).
            // fetchProfiles caches, so this is one fetch per host per page load;
            // the `ok` gate keeps us off unreachable/unauth hosts (no auth pop).
            if (pendingOpens.size) {
                await Promise.all(hosts.map(h =>
                    (pollStateFor(h.id).ok && !profilesCache.has(h.id))
                        ? fetchProfiles(h) : null));
                if (_deactivated) return;   // a teardown may land during the await
            }
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
                if (win.ws && win.ws.readyState !== WebSocket.CLOSED) continue;
                if (now < (win.reattachAt || 0)) continue;
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

