        // ---- broker identity (#64) ----------------------------------------
        // The same broker reached via several URLs (127.0.0.1 / localhost /
        // Tailscale 100.x) used to be added as multiple host records, spawning
        // duplicate taskbar chips and making Terminate fail against a stale
        // twin. /info exposes a stable broker_id so we can flag those. The id
        // is stored on the host record under `brokerId` (browser-local — never
        // pushed to /state, see _stateBlob) and is best-effort: an older broker
        // (404) or an auth/transport miss simply leaves it unknown.
        async function probeBrokerId(url, token, out) {
            // Cross-origin /info probe. Returns the broker_id string, or null on
            // any failure (404 older broker, 401 wrong token, network) — caller
            // treats null as "unknown identity, allow with no warning".
            //
            // `out` is an OPTIONAL sink for WHY a null came back, so a caller can
            // word its message honestly: `out.reached` = the broker answered at
            // all, `out.timedOut` = hostFetch's deadline fired. The return value
            // is unchanged, so callers that pass nothing (the background identity
            // refresh) behave exactly as before.
            try {
                const r = await hostFetch({ url, token }, '/info');
                if (out) out.reached = true;
                if (!r.ok) {
                    if (out) out.status = r.status;
                    return null;
                }
                const j = await r.json();
                if (j && j.ok && typeof j.broker_id === 'string'
                        && j.broker_id) {
                    return j.broker_id;
                }
            } catch (err) {
                if (out) out.timedOut = !!(err && err.name === 'TimeoutError');
            }
            return null;
        }
        function findHostByBrokerId(brokerId, hosts) {
            if (!brokerId) return null;
            for (const h of (hosts || getHosts())) {
                if (h.brokerId && h.brokerId === brokerId) return h;
            }
            return null;
        }
        // Learn THIS broker's id same-origin on boot, so it can anchor the
        // duplicate check (a remote record whose URL points back at this same
        // broker is then flagged dup-of-local). Lease-independent, best-effort.
        // Shares the memoized localInfo() (86_js_mod_loader.js) with the mod
        // loader's mods_enabled gate, so same-origin /info is fetched ONCE.
        async function learnLocalBrokerId() {
            let id = null;
            try {
                const info = await localInfo();
                if (info && typeof info.broker_id === 'string') id = info.broker_id;
            } catch (_) {}
            if (id && localHost().brokerId !== id) {
                localHost().brokerId = id;
                savePrefsLocal();        // browser-local only — never PUT /state
            }
        }
        // Background-learn stored remote hosts' ids (remediating the reporter's
        // EXISTING broken state: records added before /info carry no brokerId).
        // Re-renders the Hosts list once if anything changed. Only probes hosts
        // still missing an id, so it converges and never render-loops.
        let _hostIdRefreshing = false;
        async function refreshHostIdentities() {
            if (_hostIdRefreshing) return;
            _hostIdRefreshing = true;
            let changed = false;
            try {
                for (const h of getHosts()) {
                    if (h.id === 'local' || h.brokerId) continue;
                    const id = await probeBrokerId(h.url, h.token);
                    if (id) { h.brokerId = id; changed = true; }
                }
            } finally {
                _hostIdRefreshing = false;
            }
            if (changed) { savePrefsLocal(); renderHostsList(); }
        }
        // Map hostId -> the EARLIER host it duplicates (same broker_id, or same
        // normalized origin when ids are unknown). hosts[0] is always local, so
        // the local record is the primary and is never itself flagged; a remote
        // pointing back at this broker is flagged dup-of-local.
        function computeHostDuplicates(hosts) {
            const byId = new Map();          // brokerId -> first host
            const byOrigin = new Map();      // origin   -> first host
            const dupOf = new Map();
            for (const h of hosts) {
                const origin = h.id === 'local'
                    ? window.location.origin : normalizeHostUrl(h.url);
                if (h.brokerId && byId.has(h.brokerId)) {
                    dupOf.set(h.id, byId.get(h.brokerId));
                    continue;
                }
                if (!h.brokerId && origin && byOrigin.has(origin)) {
                    dupOf.set(h.id, byOrigin.get(origin));
                    continue;
                }
                if (h.brokerId && !byId.has(h.brokerId)) byId.set(h.brokerId, h);
                if (origin && !byOrigin.has(origin)) byOrigin.set(origin, h);
            }
            return dupOf;
        }

        // A double-click on Add used to fire two /info probes and — because both
        // clicks cleared the pre-probe duplicate check before either returned —
        // push TWO records for the same URL. One in-flight guard plus a
        // re-check after the await (below) closes both halves of that window.
        let hostFormBusy = false;
        async function commitHostForm() {
            if (hostFormBusy) return;
            setHostError('');
            const label = hostLabelEl.value.trim();
            const url = normalizeHostUrl(hostUrlEl.value);
            const pass = hostPassEl.value;
            if (!url) {
                setHostError('invalid URL — expected http://hostname:4445');
                return;
            }
            if (url === window.location.origin) {
                setHostError('that is this broker — already listed');
                return;
            }
            const hosts = getHosts();
            const editing = editingHostId ? hostById(editingHostId) : null;
            if (hosts.some(h => h !== editing && h.url === url)) {
                setHostError('host already configured');
                return;
            }
            // Busy from here down: the add path awaits an /info probe (and maybe
            // a confirm dialog), and the button must come back in EVERY case —
            // early return, throw, or success.
            hostFormBusy = true;
            const prevLabel = hostAddBtn.textContent;
            hostAddBtn.disabled = true;
            hostAddBtn.textContent = 'checking…';
            let addWarning = '';
            // #195: the host this commit actually mutated, or '' if none did.
            // Set in BOTH branches and read once after the renders below, so
            // every early return in between (no password, a declined duplicate
            // confirm, the post-await re-check) leaves it empty and emits
            // nothing — a refused commit changed no host.
            let changedHostId = '';
            try {
                if (editing) {
                    editing.label = label || defaultHostLabel(url);
                    editing.url = url;
                    if (pass) editing.token = pass;   // empty = keep existing
                    // The URL may now point somewhere else entirely — restart
                    // its poll/profile state from scratch.
                    hostPolls.delete(editing.id);
                    profilesCache.delete(editing.id);
                    authPrompted.delete(editing.id);
                    changedHostId = editing.id;
                } else {
                    if (!pass) {
                        // A remote /session/kill, /state etc. is token-gated, so a
                        // tokenless remote is undriveable from here even though CORS
                        // is now unconditional — require the token.
                        setHostError('password required — remote brokers must '
                            + 'have an auth token configured');
                        return;
                    }
                    // Probe identity (#64): warn (but allow) if this URL is a broker
                    // we already have under another address. A null id (older broker
                    // / unreachable / wrong token) just skips the warning.
                    let brokerId = null;
                    const probe = {};
                    try {
                        brokerId = await probeBrokerId(url, pass, probe);
                    } catch (_) {}
                    if (brokerId) {
                        const dup = findHostByBrokerId(brokerId, getHosts());
                        if (dup) {
                            const ok = await openConfirmDialog({
                                title: 'Duplicate broker',
                                message: 'This looks like the same broker you '
                                    + 'already have as "' + dup.label
                                    + '". Add anyway?',
                                okLabel: 'Add anyway' });
                            if (!ok) return;
                        }
                    }
                    // A host that is merely offline right now is still worth
                    // storing, so an unreachable probe does NOT block the add —
                    // but it must not be reported as a clean success either, or
                    // the user reads "added" as "reachable" and only finds out
                    // when the chip never goes green.
                    if (!probe.reached) {
                        addWarning = probe.timedOut
                            ? 'added, but this broker did not answer in time — '
                                + 'it may be offline or the URL may be wrong'
                            : 'added, but this broker could not be reached — '
                                + 'it may be offline or the URL may be wrong';
                    }
                    // Re-fetch the live array: getHosts() rebuilds prefs._hosts on
                    // every call (the poll loop runs during the await above), so the
                    // `hosts` captured before the probe may be detached — pushing to
                    // it would be dropped on save.
                    const live = getHosts();
                    // Re-run the duplicate check AFTER the await: the pre-probe one
                    // above answered a question that is now stale (a second Add, or
                    // another browser's synced host list, can have landed the same
                    // URL while we waited). Without this the same broker gets two
                    // records — duplicate chips, and a Terminate that hits a twin.
                    if (live.some(h => h.url === url)) {
                        setHostError('host already configured');
                        return;
                    }
                    const added = { id: mintHostId(),
                                    label: label || defaultHostLabel(url),
                                    url, token: pass, brokerId };
                    live.push(added);
                    changedHostId = added.id;
                }
                savePrefs();
                resetHostForm();          // clears the form AND the error line
                if (addWarning) setHostError(addWarning);
                renderHostsList();
                renderSettingsTabs();   // a new/renamed host changes the tab bar
                refreshTaskbar();
                // #195: ONE edge for both halves of this form. The event table
                // has no `host:added` — an add IS a change to the set of hosts
                // this browser can reach, and a subscriber that wants the new
                // record reads it (the payload carries ids, not snapshots).
                // Emitted AFTER the mutation, the save AND the renders, so a
                // handler that reads a host, a tab or the taskbar sees the
                // committed desktop rather than a half-applied one.
                if (changedHostId && typeof _emitModEvent === 'function') {
                    _emitModEvent('host:changed', { hostId: changedHostId });
                }
            } finally {
                hostFormBusy = false;
                hostAddBtn.disabled = false;
                // resetHostForm() / startEditHost() relabel the button themselves;
                // only put back the label WE replaced.
                if (hostAddBtn.textContent === 'checking…') {
                    hostAddBtn.textContent = prevLabel;
                }
            }
        }
        hostAddBtn.addEventListener('click', commitHostForm);

        function removeHost(id) {
            if (id === 'local') return;
            const hosts = getHosts();
            const idx = hosts.findIndex(h => h.id === id);
            if (idx === -1) return;
            hosts.splice(idx, 1);
            // Close its windows now; drop its sessions, taskbar entries,
            // poll/profile state, and every '<id>:*' pref key. App windows are
            // hostId:'app' (not the removed host), so they're matched by their
            // file host instead:
            //  - a file manager handles removal IN PLACE, per pane (#46) — a
            //    split FM stays open on its surviving pane — and is matched by
            //    appKind, never the legacy win.fileHostId (which it no longer
            //    keeps current);
            //  - an editor / AGENTS-docs window bound to the removed broker via
            //    fileHostId must close — it's fail-closed and can save nowhere.
            for (const [key, win] of Array.from(windows)) {
                if (win.hostId === id) { closeWindow(key); continue; }
                if (win.appKind === 'file-manager') {
                    if (win._hostRemoved) win._hostRemoved(id);
                    continue;
                }
                if (win.fileHostId === id) closeWindow(key);
            }
            const prefix = id + ':';
            for (const key of Array.from(sessions.keys())) {
                if (key.slice(0, prefix.length) !== prefix) continue;
                sessions.delete(key);
                const el = document.querySelector(
                    '.taskbar-item[data-session-id="' + key + '"]');
                if (el) el.remove();
            }
            for (const key of Array.from(pendingOpens.keys())) {
                if (key.slice(0, prefix.length) === prefix) {
                    pendingOpens.delete(key);
                }
            }
            for (const k of Object.keys(prefs)) {
                if (k.charAt(0) === '_') continue;
                if (k.slice(0, prefix.length) === prefix) delete prefs[k];
            }
            closeControlWs(id);          // stop the lease channel for this host
            hostPolls.delete(id);
            profilesCache.delete(id);
            authPrompted.delete(id);
            hostStateCache.delete(id);   // drop the removed host's settings cache
            hostSaveChains.delete(id);
            if (editingHostId === id) resetHostForm();
            // If the removed host's tab was open, fall back to the local tab.
            if (currentSettingsTab === id) {
                currentSettingsTab = 'local';
                settingsTarget = makeLocalTarget();
                settingsOpenHostId = null;
                showSettingsPane('local');
                renderSettings();
            }
            // #107: if the removed host was the START (+) default, fall back to
            // local — mirrors the currentSettingsTab reset above and keeps the
            // stored/synced value tidy so local shows marked after the delete.
            if (getSettings().defaultHost === id) getSettings().defaultHost = '';
            savePrefs();
            renderHostsList();
            renderSettingsTabs();   // the removed host's tab disappears
            renderHostStatus();
            refreshTaskbar();
            // #195: LAST, and deliberately so. This function is the whole
            // removal — the splice, the windows, the sessions, the per-host
            // prefs, the lease socket, five caches and four renders — and the
            // event that replaces `win._hostRemoved` above must mean "that
            // host is gone", not "it is going". The two early returns (the
            // local host, an unknown id) removed nothing and reach nothing
            // here.
            if (typeof _emitModEvent === 'function') {
                _emitModEvent('host:removed', { hostId: id });
            }
        }

        function startEditHost(id) {
            const host = hostById(id);
            if (!host || host.id === 'local') return;
            editingHostId = id;
            hostAddBtn.textContent = 'save';
            hostLabelEl.value = host.label;
            hostUrlEl.value = host.url;
            hostPassEl.value = '';
            setHostError('');
            try { hostLabelEl.focus(); } catch (_) {}
        }

        function hostRowButton(text, title, onClick) {
            const b = document.createElement('button');
            b.type = 'button';
            b.textContent = text;
            if (title) b.title = title;
            b.addEventListener('click', onClick);
            return b;
        }

        // #103: the per-host default-color picker reuses attachColorPicker, which
        // appends a hidden <input type=color> to document.body per picker (cleaned
        // via target.cleanups). renderHostsList wipes + rebuilds its rows on every
        // call (poll-driven identity refresh, add/remove/edit, a color pick), so
        // hold the live pickers' cleanups + their shim targets module-side and
        // flush them at the top of each render: mark the stale targets disposed (a
        // late OS color-dialog resolve then no-ops) and run every cleanup (removes
        // the listeners + the body inputs) BEFORE the fresh pickers register into
        // new arrays. Bounded to one input-per-host on document.body at a time.
        let hostPickerCleanups = [];
        let hostPickerTargets = [];
        function flushHostPickers() {
            for (const t of hostPickerTargets) t.disposed = true;
            for (const fn of hostPickerCleanups) { try { fn(); } catch (_) {} }
            hostPickerCleanups = [];
            hostPickerTargets = [];
        }

        function renderHostsList() {
            hostsListEl.textContent = '';
            flushHostPickers();
            const hosts = getHosts();
            // Flag records that resolve to a broker we already have (#64) so the
            // user can remove the stale/duplicate one — the surgical fix the
            // reporter actually needed (removeHost preserves other tokens).
            const dupOf = computeHostDuplicates(hosts);
            // #107: `curId` = the host START actually launches on (resolves '' /
            // 'local' / a stale-or-foreign id to the local host) — drives the
            // badge. `storedDefault` = the RAW stored value, which drives each
            // row's Default-button disabled state. They diverge when the stored
            // id is unresolvable here (e.g. a non-'local' id synced from another
            // browser, whose ids don't sync): the badge sits on local, but local's
            // button must stay ENABLED so the user can clear the foreign id back
            // to ''. Disabling off `curId` instead would strand it (the only row
            // that could clear it is the one that appears already-selected).
            const curId = defaultLaunchHost().id;
            const storedDefault = getSettings().defaultHost;
            for (const host of hosts) {
                const row = document.createElement('div');
                row.className = 'set-row host-row';
                const name = document.createElement('span');
                name.className = 'host-name';
                // textContent only — labels/URLs are user input.
                name.textContent = host.id === 'local'
                    ? 'this broker (' + window.location.host + ')'
                    : host.label + ' — ' + host.url;
                name.title = name.textContent;
                row.appendChild(name);
                // #190: a host added after page load sits outside the CSP this
                // page was served with — once the policy enforces, connecting
                // needs a reload. Display-only; predicate + wording live in
                // 56_js_hosts.js (the load-time snapshot).
                if (hostAddedAfterLoad(host.id)) {
                    const added = document.createElement('span');
                    added.className = 'host-added-note';
                    added.textContent = HOST_ADDED_NOTE;
                    row.appendChild(added);
                }
                // #107: badge the START (+) default host (mirrors the profiles
                // editor's `set-profile-badge`). Shown for the local row too.
                if (host.id === curId) {
                    const badge = document.createElement('span');
                    badge.className = 'set-profile-badge';
                    badge.textContent = 'default';
                    name.appendChild(badge);
                }
                const dup = dupOf.get(host.id);
                if (dup) {
                    const hint = document.createElement('span');
                    hint.className = 'host-dup';
                    hint.textContent = 'duplicate of "' + dup.label + '"';
                    hint.title = 'Same broker as "' + dup.label
                        + '" reached via another URL — Remove this one to clear '
                        + 'the duplicate taskbar chips (keeps other brokers).';
                    row.appendChild(hint);
                }
                row.appendChild(hostRowButton('password',
                    'enter the password for this host',
                    () => showAuthOverlay(host, true)));
                // #107: mark this host as the START (+) button's default launch
                // target. Selectable for EVERY row (incl. local, stored as '' =
                // canonical unset); disabled on the row already marked default.
                const defBtn = hostRowButton('default',
                    'launch the START (+) button on this host',
                    () => {
                        getSettings().defaultHost =
                            (host.id === 'local' ? '' : host.id);
                        savePrefs();
                        renderHostsList();
                    });
                // Disabled only when the STORED value already selects THIS row
                // (clicking would be a no-op) — for local that's '' or the legacy
                // 'local'. Comparing the raw stored value (not `curId`) keeps
                // local clickable when the stored id is a foreign/stale one that
                // merely resolves to local, so it can be cleared.
                defBtn.disabled = host.id === 'local'
                    ? (storedDefault === '' || storedDefault === 'local')
                    : (storedDefault === host.id);
                row.appendChild(defBtn);
                if (host.id !== 'local') {
                    row.appendChild(hostRowButton('edit', null,
                        () => startEditHost(host.id)));
                    row.appendChild(hostRowButton('remove', null,
                        () => removeHost(host.id)));
                }
                // #149: focus mode's permanent home — hide/show this broker's
                // windows from the Hosts pane too; the single-broker chip and
                // the (+) menu's broker row are shortcuts to the same toggle.
                // toggleHostHidden re-renders this list, so the box tracks a
                // toggle made from any surface (and this row's own change
                // event survives the rebuild it triggers).
                const hideWrap = document.createElement('label');
                hideWrap.className = 'host-hide';
                hideWrap.title = "hide this broker's windows (focus mode) — "
                    + 'they are masked, not closed';
                const hideCb = document.createElement('input');
                hideCb.type = 'checkbox';
                hideCb.checked = !!host.hidden;
                hideCb.addEventListener('change',
                    () => toggleHostHidden(host.id));
                hideWrap.appendChild(hideCb);
                hideWrap.appendChild(document.createTextNode('hidden'));
                row.appendChild(hideWrap);
                // #103: optional per-host DEFAULT accent — the reused swatch
                // picker on a lightweight shim target (a `color` getter reading
                // the live host record, `disposed`, and the shared module cleanups
                // array flushed above). A pick writes host.color (browser-local,
                // like token/hidden — never pushed to /state); the ✕ clears it
                // back to the palette auto-pick. Re-render + refresh so the row
                // dot, new-terminal seed, taskbar chips, and the broker status
                // chip border (renderHostStatus) all reflect it immediately.
                const colorTarget = {
                    get color() { return host.color || ''; },
                    disposed: false,
                    cleanups: hostPickerCleanups,
                };
                hostPickerTargets.push(colorTarget);
                const applyHostColor = (val) => {
                    host.color = val;
                    savePrefs();
                    renderHostsList();
                    refreshTaskbar();
                    renderHostStatus();
                };
                const colorBtn = attachColorPicker(
                    colorTarget, row, PALETTE.map((c) => ({ color: c })),
                    (sw) => applyHostColor(normalizeHex(sw.color)));
                colorBtn.title = 'default color for this host';
                // Dot shows the host color, or the base accent when unset (= auto).
                row.style.setProperty('--accent',
                    host.color || 'var(--accent-default)');
                row.appendChild(colorBtn);
                if (host.color) {
                    row.appendChild(hostRowButton('✕',
                        'clear default color (revert to auto per-window colors)',
                        () => applyHostColor('')));
                }
                hostsListEl.appendChild(row);
            }
            renderTroubleshooting();   // keep the dup recovery list in sync
            refreshHostIdentities();   // background: learns missing ids, re-renders
        }

        // Troubleshooting (#64): echo the detected duplicate brokers with a
        // one-click remove (removeHost keeps every other broker's token), so the
        // user has ONE place to recover from the duplicate-chip / silent-
        // terminate state. Driven by the same computeHostDuplicates as the Hosts
        // list above; re-rendered whenever that list is.
        function renderTroubleshooting() {
            const box = document.getElementById('set-troubleshoot-dups');
            if (!box) return;
            box.textContent = '';
            const hosts = getHosts();
            const dupOf = computeHostDuplicates(hosts);
            const dups = hosts.filter(
                h => h.id !== 'local' && dupOf.has(h.id));
            const label = document.createElement('div');
            label.className = 'set-hint';
            label.textContent = dups.length
                ? 'Duplicate brokers (same backend, different URL):'
                : 'No duplicate brokers detected.';
            box.appendChild(label);
            for (const h of dups) {
                const row = document.createElement('div');
                row.className = 'set-row host-row';
                const name = document.createElement('span');
                name.className = 'host-name';
                name.textContent = h.label + ' — ' + h.url
                    + '  (same as "' + dupOf.get(h.id).label + '")';
                name.title = name.textContent;
                row.appendChild(name);
                row.appendChild(hostRowButton('remove',
                    'remove this duplicate host record (keeps other brokers)',
                    () => removeHost(h.id)));
                box.appendChild(row);
            }
        }

        // "Reset local view": clear ONLY browser-local, non-synced state — the
        // per-session geometry pref keys (prefs['<hostId>:<sid>'], never pushed
        // to /state) and the in-memory view — then rebuild from the authoritative
        // /state via the existing lease teardown->rebuild path. Persisted with
        // savePrefsLocal so it can NEVER PUT /state: _hosts / _settings / _layout
        // are untouched, so another browser's layout and the shared rev are not
        // disturbed. This is the safe "Ctrl+F5 wasn't enough" stale-window
        // recovery; it does NOT by itself fix the duplicate chips (remove the
        // duplicate host above for that).
        function resetLocalView() {
            for (const k of Object.keys(prefs)) {
                if (k.charAt(0) === '_') continue;   // keep _hosts/_settings/_layout
                delete prefs[k];                     // per-session geometry only
            }
            savePrefsLocal();                        // local only — never PUT /state
            if (_booted && !_deactivated) {
                teardownView();                      // dispose windows + in-memory
                rebuildView();                       // re-adopt /state + restore
            }
            showNotice('local view reset');
        }
        const resetViewBtn = document.getElementById('set-reset-view');
        if (resetViewBtn) resetViewBtn.addEventListener('click', resetLocalView);

