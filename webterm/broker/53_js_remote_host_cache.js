        // ---- remote host settings cache (tasks 13/14/15) ------------------
        // Per-host settings+layout snapshots for OTHER brokers, keyed by
        // hostId. The LOCAL host is deliberately NEVER cached here — its
        // settings are the live getSettings()/getLayout() (the F0 path owns
        // them). A remote host's blob is fetched from that broker's /state,
        // run through normalizeSettings, and used for (a) reading its per-host
        // settings (default profile, tiling mode) and (b) editing its settings
        // in the per-host settings tab (task 14). Optimistic concurrency on the
        // remote's own integer rev, mirroring pushState's 409 adopt-and-retry.
        const hostStateCache = new Map();   // hostId -> {rev, settings, layout}
        const hostSaveChains = new Map();   // hostId -> tail Promise (serialize PUTs)
        // While a REMOTE host's settings tab is open its cached blob is the
        // settingsTarget the change handlers mutate — the background prefetch
        // must not swap that object out from under an in-flight edit. Set by
        // the settings modal's tab switch; null = no remote tab open.
        let settingsOpenHostId = null;

        async function fetchHostState(hostId) {
            // Local is live — never cached. Hand back the authoritative objects
            // so a settings tab over 'local' edits getSettings() directly.
            if (hostId === 'local') {
                return { rev: _stateRev, settings: getSettings(),
                         layout: getLayout() };
            }
            const host = hostById(hostId);
            if (!host) return null;
            let r;
            try {
                const ctrl = new AbortController();
                const timer = setTimeout(() => ctrl.abort(), FETCH_TIMEOUT_MS);
                try {
                    r = await hostFetch(host, '/state',
                        { cache: 'no-store', signal: ctrl.signal });
                } finally { clearTimeout(timer); }
            } catch (e) { return null; }          // offline / CORS — no cache
            if (!r.ok) return null;               // 401/5xx — handled elsewhere
            let srv;
            try { srv = await r.json(); } catch (e) { return null; }
            if (!srv || typeof srv !== 'object') return null;
            const realLayout = (srv.layout && typeof srv.layout === 'object'
                && !Array.isArray(srv.layout));
            const entry = {
                rev: (typeof srv.rev === 'number') ? srv.rev : 0,
                settings: normalizeSettings((srv.settings
                    && typeof srv.settings === 'object'
                    && !Array.isArray(srv.settings)) ? srv.settings : {}),
                layout: realLayout ? srv.layout : {},
                // True only when the server returned a real layout object: a PUT
                // must carry the host's real layout unchanged, never a bare {}
                // that would wipe it. A fresh broker (rev 0, no layout yet) is
                // safe to seed with {} on the first save.
                layoutLoaded: realLayout || (typeof srv.rev !== 'number')
                    || srv.rev === 0,
            };
            hostStateCache.set(hostId, entry);
            return entry;
        }

        // Persist a remote host's edited settings back to its /state. Saves
        // are SERIALIZED per host (a tail promise chain): independent change
        // handlers firing in quick succession would otherwise PUT with the
        // same baseRev and race their 409 retries out of order. The `entry`
        // object the settings tab edited is captured up front and PUT directly
        // (NOT re-read from hostStateCache, which a background prefetch may have
        // swapped) so a queued save never drops the user's edit.
        function putHostState(hostId, entry) {
            // Local writes go through the untouched F0 path.
            if (hostId === 'local') { savePrefs(); return Promise.resolve(); }
            // Use the entry the settings tab CAPTURED (makeRemoteTarget passes it)
            // — never re-read hostStateCache here: an in-flight background prefetch
            // can replace the cached object after the tab fetched it, and PUTting
            // that swapped entry would silently drop the user's edit. Fall back to
            // the cache only for callers that don't hold the entry.
            if (!entry) entry = hostStateCache.get(hostId);
            if (!entry) return Promise.resolve();
            const prev = hostSaveChains.get(hostId) || Promise.resolve();
            const next = prev.then(() => _putHostStateOnce(hostId, entry))
                             .catch(() => {});
            hostSaveChains.set(hostId, next);
            return next;
        }
        async function _putHostStateOnce(hostId, entry) {
            const host = hostById(hostId);
            if (!host || !entry) return;
            // PUT requires BOTH settings and layout objects. We only edit
            // settings, so the host's real layout rides along unchanged — but a
            // bare {} would WIPE that layout, so a save before the layout
            // actually loaded is refused rather than clobbering it.
            if (!entry.layoutLoaded || !entry.layout
                || typeof entry.layout !== 'object'
                || Array.isArray(entry.layout)) {
                showNotice('cannot save to ' + (host.label || host.url)
                    + ' — its layout has not loaded yet');
                return;
            }
            // Always re-PUT the USER's edited settings — on a 409 only the rev
            // (and the layout baseline) adopts the winner; entry.settings is the
            // pending edit and must survive the retry.
            const doPut = (baseRev) => hostFetch(host, '/state', {
                method: 'PUT',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    baseRev: baseRev,
                    settings: entry.settings,
                    layout: entry.layout,
                    clientId: CLIENT_ID,   // lease: only the active browser on
                }),                        // this remote may write its /state
            });
            try {
                let r = await doPut(entry.rev);
                if (r.status === 409) {
                    // Another viewer of THAT broker won — adopt its rev+layout
                    // baseline and retry ONCE, still carrying our edited
                    // settings (the user's change wins the merge).
                    let cur;
                    try { cur = await r.json(); } catch (e) { cur = null; }
                    if (cur && typeof cur === 'object') {
                        if (cur.layout && typeof cur.layout === 'object'
                            && !Array.isArray(cur.layout)) {
                            entry.layout = cur.layout;
                            entry.layoutLoaded = true;
                        }
                        entry.rev = (typeof cur.rev === 'number')
                            ? cur.rev : entry.rev;
                        r = await doPut(entry.rev);
                    }
                }
                if (r.ok) {
                    let resp;
                    try { resp = await r.json(); } catch (e) { resp = null; }
                    if (resp && typeof resp.rev === 'number') entry.rev = resp.rev;
                } else {
                    showNotice('could not save settings to '
                        + (host.label || host.url));
                }
            } catch (e) {
                showNotice('could not reach ' + (host.label || host.url)
                    + ' to save settings');
            }
        }

