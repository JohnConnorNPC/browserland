        // ---- launch ---------------------------------------------------------
        let fastPollTimer = null;
        function startFastPoll() {
            if (fastPollTimer) return;
            fastPollTimer = setInterval(refreshTaskbar, FAST_POLL_MS);
        }
        function stopFastPoll() {
            if (!fastPollTimer) return;
            clearInterval(fastPollTimer);
            fastPollTimer = null;
        }

        const launchBtn = document.getElementById('btn-launch');
        // Resolve a host's per-host default profile (task 8): the LOCAL host
        // reads the live getSettings(); a remote host reads its cached settings
        // blob. '' / missing -> null = broker default.
        function hostDefaultProfile(host) {
            host = host || localHost();
            let s;
            if (host.id === 'local') {
                s = getSettings();
            } else {
                const cached = hostStateCache.get(host.id);
                s = (cached && cached.settings) || {};
            }
            return (s && s.defaultProfile) ? s.defaultProfile : null;
        }
        // Task 7: `cwd` (optional absolute dir) sets the spawned shell's working
        // directory; older brokers ignore it. Default undefined keeps every
        // existing call site at the broker's own default cwd.
        async function launchProfile(host, name, cwd) {
            host = host || localHost();
            // Issue #10: when no explicit cwd was chosen (e.g. via "Open in
            // folder…"), fall back to the configured default start path for this
            // host. resolveStartPath returns '' (and skips the /profiles
            // round-trip) when nothing is configured, so unconfigured users keep
            // the broker's default cwd and are otherwise unaffected.
            if (!cwd) cwd = await resolveStartPath(host);
            // The (+) button is the ONLY launch control, and a disabled button
            // dispatches NEITHER click nor contextmenu — so holding it across the
            // POST killed both gestures for however long the network took, while
            // still looking alive (it isn't visibly greyed either). It is held
            // only to swallow a double-launch now: the 500 ms debounce runs from
            // the moment the request is DISPATCHED, not from when it answers.
            let btnReleased = false;
            const releaseLaunchBtn = () => {
                if (btnReleased) return;
                btnReleased = true;
                if (launchBtn) {
                    setTimeout(() => { launchBtn.disabled = false; }, 500);
                }
            };
            if (launchBtn) launchBtn.disabled = true;
            try {
                const payload = {};
                if (name) payload.profile = name;
                if (cwd) payload.cwd = cwd;
                // Per-call deadline: the broker answers /launch with 202 only
                // after REGISTER_TIMEOUT (10 s, launcher.py) when the agent
                // hasn't registered yet, so the default 3 s client deadline
                // would routinely abort a launch that is actually succeeding.
                const pending = hostFetch(host, '/launch', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(payload),
                    timeoutMs: 15000,
                });
                releaseLaunchBtn();     // dispatched — stop holding the button
                const r = await pending;
                if (r.status === 401) {
                    // Direct user gesture — always worth the modal.
                    pollStateFor(host.id).authNeeded = true;
                    showAuthOverlay(host, true);
                    return;
                }
                const d = await r.json().catch(() => ({}));
                if (!d || !d.ok || d.id == null) {
                    showNotice('launch failed: '
                        + ((d && d.error) || ('HTTP ' + r.status)));
                    return;
                }
                // 200 = agent registered, 202 = still starting. Either way
                // park the key and fast-poll until it lands in /sessions.
                pendingOpens.set(host.id + ':' + String(d.id),
                    Date.now() + AUTO_OPEN_TIMEOUT_MS);
                startFastPoll();
                refreshTaskbar();
            } catch (e) {
                // A client-side deadline is NOT proof the launch did not happen:
                // the request reached the broker and the shell may well be
                // starting. Don't claim a failure — the session lands in the
                // taskbar on its own once the agent registers.
                if (e && e.name === 'TimeoutError') {
                    showNotice('launch is taking a while — if it started it '
                        + 'will appear in the taskbar');
                    refreshTaskbar();
                } else {
                    showNotice('launch failed: ' + e);
                }
            } finally {
                releaseLaunchBtn();     // no-op unless we never got to dispatch
            }
        }

        // /profiles is static per broker run — cache each host's first 200.
        const profilesCache = new Map();   // hostId -> {default, profiles}
        // Negative cache: a host whose /profiles just FAILED is not re-dialed for
        // PROFILES_FAIL_TTL_MS, so repeated right-clicks on (+) don't queue up a
        // fresh doomed request per gesture against a dead broker.
        //
        // It hangs off profilesCache's own delete/clear rather than living beside
        // it, so every existing invalidation site (81's profile editor, 83's host
        // edit / host removal) drops the negative entry too — there is exactly one
        // "forget what we know about this host" call and it keeps working without
        // any of those fragments knowing this cache exists.
        const PROFILES_FAIL_TTL_MS = 10000;
        const profilesFailAt = new Map();  // hostId -> Date.now() of the failure
        // One dial per host at a time: without this, N right-clicks inside the
        // 3 s deadline queue N doomed requests before the first one can write a
        // negative entry. Entries drop on settle, so nothing needs to invalidate
        // this one. It also de-dupes against 75's warm-up prefetch.
        const profilesInflight = new Map();   // hostId -> Promise
        {
            const _cacheDelete = profilesCache.delete.bind(profilesCache);
            const _cacheClear = profilesCache.clear.bind(profilesCache);
            profilesCache.delete = (id) => {
                profilesFailAt.delete(id);
                return _cacheDelete(id);
            };
            profilesCache.clear = () => {
                profilesFailAt.clear();
                return _cacheClear();
            };
        }
        function profilesFailedRecently(hostId) {
            const at = profilesFailAt.get(hostId);
            if (at == null) return false;
            if (Date.now() - at < PROFILES_FAIL_TTL_MS) return true;
            profilesFailAt.delete(hostId);      // expired — allow a retry
            return false;
        }
        function fetchProfiles(host) {
            host = host || localHost();
            if (profilesCache.has(host.id)) {
                return Promise.resolve(profilesCache.get(host.id));
            }
            if (profilesFailedRecently(host.id)) return Promise.resolve(null);
            const live = profilesInflight.get(host.id);
            if (live) return live;
            const p = fetchProfilesOnce(host);
            profilesInflight.set(host.id, p);
            const done = () => {
                if (profilesInflight.get(host.id) === p) {
                    profilesInflight.delete(host.id);
                }
            };
            p.then(done, done);
            return p;
        }
        async function fetchProfilesOnce(host) {
            try {
                const r = await hostFetch(host, '/profiles');
                if (r.status === 401) {
                    pollStateFor(host.id).authNeeded = true;
                    showAuthOverlay(host);
                    profilesFailAt.set(host.id, Date.now());
                    return null;
                }
                if (r.ok) {
                    const d = await r.json();
                    if (d && Array.isArray(d.profiles)) {
                        profilesCache.set(host.id, d);
                        return d;
                    }
                }
            } catch (_) {}
            profilesFailAt.set(host.id, Date.now());
            return null;
        }

        // Issue #10/#17: resolve the default start path to use as the launch cwd
        // on `host`. The path is PER-HOST: the LOCAL host reads the live
        // getSettings(); a remote host reads its OWN cached settings blob (kept
        // warm by the background prefetch) — same pattern as hostDefaultProfile.
        // A host's path is for its own OS, so no cross-host OS matching is needed.
        // Returns '' (=> the agent's own default cwd) when nothing is configured
        // for that host. The legacy per-OS startPaths map (#2) is a LOCAL-only
        // migration artifact, honored read-only keyed by this host's OS, so an
        // upgrade keeps working until the path is re-saved.
        async function resolveStartPath(host) {
            try {
                host = host || localHost();
                let s;
                if (host.id === localHost().id) {
                    s = getSettings();
                } else {
                    // Use the warm prefetch cache; on a cold cache (e.g. a launch
                    // right after refresh) fetch once so the host's configured
                    // path is honored deterministically rather than nondeterministically
                    // falling back to the broker default.
                    let cached = hostStateCache.get(host.id);
                    if (!cached) cached = await fetchHostState(host.id);
                    s = (cached && cached.settings) || null;
                }
                if (!s) return '';
                const p = (typeof s.startPath === 'string') ? s.startPath.trim() : '';
                if (p) return p;
                const legacy = (s.startPaths && typeof s.startPaths === 'object'
                    && !Array.isArray(s.startPaths)) ? s.startPaths : null;
                if (!legacy) return '';
                const prof = await fetchProfiles(host);
                const osName = prof && prof.os;
                if (osName === 'windows') {
                    return (typeof legacy.windows === 'string')
                        ? legacy.windows.trim() : '';
                }
                if (osName === 'posix') {
                    return (typeof legacy.posix === 'string')
                        ? legacy.posix.trim() : '';
                }
                return '';
            } catch (_) { return ''; }
        }

        // Issue #10/#17: the value to show in the single start-path box — the
        // saved startPath, else the legacy per-OS value for ``host``'s OS (a host
        // is one OS, so typically only one legacy field is set). Host-aware: an
        // upgraded REMOTE broker can carry its own legacy startPaths, so the OS
        // must come from THAT host's /profiles, not the local broker's. Display
        // only; resolveStartPath owns what actually gets sent.
        function startPathForDisplay(s, host) {
            const p = (typeof s.startPath === 'string') ? s.startPath.trim() : '';
            if (p) return p;
            const legacy = s.startPaths;
            if (!legacy || typeof legacy !== 'object' || Array.isArray(legacy)) {
                return '';
            }
            const w = (typeof legacy.windows === 'string') ? legacy.windows.trim() : '';
            const px = (typeof legacy.posix === 'string') ? legacy.posix.trim() : '';
            const prof = profilesCache.get((host || localHost()).id);
            const os = prof && prof.os;
            if (os === 'windows') return w;
            if (os === 'posix') return px;
            // Local OS not known yet: show an UNAMBIGUOUS legacy value (exactly
            // one OS set), but never guess between two — picking the wrong OS
            // would persist a wrong-OS path on the first edit (and drop the
            // right one). renderSettings refreshes the box once /profiles lands.
            if (w && !px) return w;
            if (px && !w) return px;
            return '';
        }

        function profileMenuItems(host, d) {
            const items = d.profiles.map(name => ({
                label: name === d.default ? name + ' (default)' : name,
                enabled: true,
                action: () => launchProfile(host, name),
            }));
            // Task 7: pick a starting folder, then launch this host's default
            // profile (per-host defaultProfile, else broker default) there.
            // Folder picker browses the whole host now (#35); cancel = no-op.
            // Browse on the SAME (possibly remote) host we'll launch on, so the
            // chosen absolute path exists there — not the local FS (#35 review).
            items.push({
                label: 'Open in folder…',
                enabled: true,
                action: async () => {
                    try {
                        const dir = await openFileDialog({ mode: 'dir',
                                                           startDir: '',
                                                           host: host });
                        if (dir) launchProfile(host, hostDefaultProfile(host),
                                               dir);
                    } catch (_) {}
                },
            });
            return items;
        }

        // Client-only apps offered below the terminal profiles in the launch
        // menu. The sticky-note (#81/S8), text-editor (#83/S10), file-manager
        // (#84/S11) and task-manager (#85/S12) launchers moved to mods/sticky/,
        // mods/editor/, mods/file-manager/ and mods/task-manager/; control-panel
        // (below) is the remaining core kind (registered as a built-in,
        // 54_js_app_windows_store.js's registerBuiltinWindowKinds).
        function launchControlPanel() {
            // The Control Panel is a moveable floating window (#59); open or focus
            // it. Kept as the named entry point for the launch-menu item.
            return openControlPanelWindow({});
        }
        // The "client apps" block of the (+) launch menu, now driven by the window-
        // kind registry (#80/S7): a leading separator, one launcher per registered
        // kind that carries a menu (registration order — so the built-ins reproduce
        // the old Sticky / Editor / File-mgr / Task-mgr / Control-panel / Help
        // order, and a mod's kind appends after Help), then each kind's closed-doc
        // items (only the sticky note contributes any — its "Closed notes" list).
        function appMenuItems() {
            const kinds = windowKindMenuList();
            const items = [{ sep: true }];
            for (const k of kinds) {
                const m = k.menu;
                if (m && m.label && typeof m.launch === 'function') {
                    // #119: pass the kind's iconKey (a mod id) through untouched;
                    // renderMenu resolves it to the trusted APP_ICON_SVG glyph, so
                    // the raw SVG never travels on the item object.
                    items.push({ label: m.label, enabled: true, action: m.launch,
                                 iconKey: m.iconKey || '' });
                }
            }
            for (const k of kinds) {
                const m = k.menu;
                if (m && typeof m.closedItems === 'function') {
                    try {
                        for (const it of m.closedItems()) items.push(it);
                    } catch (_) {}
                }
            }
            return items;
        }

        // '' when this host is worth dialing, else the suffix its single
        // disabled row carries. Deliberately NOT a bare `!ok`: a
        // freshly-created poll record starts ok=false, so before the first
        // /sessions answer every host would look dead and never be dialed at
        // all. The reachability rule is the one the taskbar chip already uses
        // (hostChipState in 75) so the menu and the chip can never disagree —
        // plus consecFailures > 0, i.e. a poll must actually have been tried
        // and failed. authNeeded is folded in here so a mere right-click can
        // never pop the login overlay (same reasoning as 75's prefetch gate).
        function launchHostDownSuffix(host) {
            const st = pollStateFor(host.id);
            if (st.authNeeded) return ' — sign in required';
            if (!st.ok && st.consecFailures > 0
                    && (!st.everOk
                        || st.consecFailures >= STALE_AFTER_FAILURES)) {
                return ' — unreachable';
            }
            return '';
        }
        // One host's rows, built from what is known RIGHT NOW — never awaits,
        // never dials.
        function launchHostItems(host, showHeader) {
            const items = [];
            // Down is checked BEFORE the cache: a host that has since gone
            // away must not keep offering enabled launch rows out of a stale
            // profile list (codex review). Zero network traffic either way,
            // and the menu still opens instantly.
            const downSuffix = launchHostDownSuffix(host);
            if (downSuffix) {
                items.push({ label: host.label + downSuffix,
                             enabled: false });
                return items;
            }
            const d = profilesCache.get(host.id);
            if (d && d.profiles.length) {
                if (showHeader) {
                    items.push({ label: host.label, enabled: false });
                }
                items.push(...profileMenuItems(host, d));
                return items;
            }
            if (showHeader) items.push({ label: host.label, enabled: false });
            items.push({
                label: (d || profilesFailedRecently(host.id))
                    ? 'profiles unavailable' : 'loading profiles…',
                enabled: false,
            });
            return items;
        }
        function buildLaunchMenuItems(hosts, showHeader) {
            const items = [];
            hosts.forEach((host, i) => {
                if (i) items.push({ sep: true });
                items.push(...launchHostItems(host, showHeader));
            });
            // The client apps need no broker at all, so they are ALWAYS in
            // the very first paint — a dead host must never suppress them.
            // Their leading separator is dropped when nothing precedes it.
            const apps = appMenuItems();
            items.push(...(items.length ? apps : apps.slice(1)));
            return items;
        }
        // Cheap content fingerprint, so a host resolving to what is already
        // on screen doesn't re-render the menu out from under the cursor.
        function launchMenuSig(items) {
            return items.map(it => it.sep ? '|'
                : ((it.enabled ? '+' : '-') + it.label)).join('\n');
        }
        // Late-resolve guard. renderMenu (77) rebuilds #ctx-menu's children
        // from scratch and every menu in the app shares that one element, so
        // "is the menu we painted still the one on screen?" is exactly "is
        // our first child still there, and is the menu still open?".
        // hideCtxMenu (78) only drops the .open class, hence both checks. The
        // generation counter additionally retires the callbacks of a previous
        // open of THIS menu.
        let launchMenuGen = 0;
        let launchMenuNode = null;
        function launchMenuStillOurs(gen) {
            return gen === launchMenuGen
                && !!launchMenuNode
                && ctxMenu.classList.contains('open')
                && ctxMenu.firstChild === launchMenuNode;
        }
        // Paints SYNCHRONOUSLY from whatever is cached, then re-paints as
        // each host's /profiles lands. One dead host can no longer keep the
        // whole menu — apps included — off the screen.
        // Hoisted out of the if(launchBtn) block (#149) so the taskbar's
        // aggregate broker badge (75) can open the menu too — the badge must
        // work even on a page whose launch button is missing.
        function openLaunchMenu(x, y) {
            const hosts = allHosts();
            const showHeader = hosts.length > 1;
            const gen = ++launchMenuGen;
            let sig = null;
            const paint = (first) => {
                const items = buildLaunchMenuItems(hosts, showHeader);
                const next = launchMenuSig(items);
                if (!first) {
                    if (!launchMenuStillOurs(gen)) return;
                    if (next === sig) return;
                }
                sig = next;
                renderMenu(items, x, y);
                launchMenuNode = ctxMenu.firstChild;
            };
            paint(true);
            for (const host of hosts) {
                // Skip anything already answered, known down, or recently
                // failed — those rows are final until something changes.
                // (fetchProfiles re-checks the cache/negative cache anyway;
                // this keeps the dead-host path free of even a promise.)
                if (profilesCache.has(host.id)
                        || launchHostDownSuffix(host)
                        || profilesFailedRecently(host.id)) {
                    continue;
                }
                fetchProfiles(host).then(() => paint(false), () => {});
            }
        }
        if (launchBtn) {
            // Two gestures on the START (+) button: quick-launch a terminal, and
            // a picker menu — profiles grouped under disabled host-header rows
            // when >1 host. Which gesture is which is decided at click time by
            // #114's swapLaunchButtons: default OFF maps left = quick-launch,
            // right = menu; ON swaps them. The listeners stay bound once and the
            // contextmenu one always suppresses the native menu (see below).
            // #107: quick-launch targets the DEFAULT host (s.defaultHost, via
            // defaultLaunchHost() — unset/removed → local, prior behavior) using
            // THAT host's per-host defaultProfile (else the broker default).
            const quickLaunch = () => {
                const h = defaultLaunchHost();
                launchProfile(h, hostDefaultProfile(h));
            };
            launchBtn.addEventListener('click', (e) => {
                // e.clientX/e.clientY are valid on a plain click too, so the menu
                // opens at the cursor when swapped.
                if (getSettings().swapLaunchButtons) openLaunchMenu(e.clientX, e.clientY);
                else quickLaunch();
            });
            launchBtn.addEventListener('contextmenu', (e) => {
                // Suppress the native menu unconditionally (before branching), so
                // it never shows regardless of which action right-click runs.
                e.preventDefault();
                e.stopPropagation();
                if (getSettings().swapLaunchButtons) quickLaunch();
                else openLaunchMenu(e.clientX, e.clientY);
            });
        }

        // ---- in-app fullscreen (Fullscreen API) --------------------------
        // App-initiated element-fullscreen (vs. the browser's F11) so Chrome/
        // Firefox don't flick the tab bar down on mouse-to-top — which would
        // collide with the tile tab strips at the very top of the UI. Targets
        // the whole page so all of Browserland goes fullscreen. webkit fallbacks
        // for older Safari; everything is no-op when unsupported.
        function fsElement() {
            return document.fullscreenElement
                || document.webkitFullscreenElement || null;
        }
        function toggleFullscreen() {
            // Called synchronously from a click / trusted keydown so the
            // Fullscreen API's user-gesture requirement is satisfied. Both
            // request AND exit return promises that can reject — swallow them
            // so a denied transition never surfaces an unhandled rejection.
            try {
                if (fsElement()) {
                    const p = document.exitFullscreen ? document.exitFullscreen()
                        : (document.webkitExitFullscreen && document.webkitExitFullscreen());
                    if (p && p.catch) p.catch(() => {});
                } else {
                    const el = document.documentElement;
                    const req = el.requestFullscreen || el.webkitRequestFullscreen;
                    if (req) { const p = req.call(el); if (p && p.catch) p.catch(() => {}); }
                }
            } catch (_) { /* unsupported — no-op */ }
        }
        function updateFullscreenBtn() {
            const btn = document.getElementById('btn-fullscreen');
            if (!btn) return;
            const on = !!fsElement();
            btn.classList.toggle('active', on);
            const label = on ? 'Exit fullscreen' : 'Fullscreen';
            btn.title = label;
            btn.setAttribute('aria-label', label);
        }
        {
            const fsBtn = document.getElementById('btn-fullscreen');
            if (fsBtn) {
                // Method-based detection: hide only when the page genuinely
                // can't request fullscreen — never hide a button that would
                // work (the prefixed `*Enabled` flag is unreliable).
                const docEl = document.documentElement;
                const canFs = !!(docEl.requestFullscreen || docEl.webkitRequestFullscreen);
                if (!canFs) {
                    fsBtn.style.display = 'none';
                } else {
                    fsBtn.addEventListener('click', toggleFullscreen);
                    document.addEventListener('fullscreenchange', updateFullscreenBtn);
                    document.addEventListener('webkitfullscreenchange', updateFullscreenBtn);
                    updateFullscreenBtn();
                }
            }
        }

