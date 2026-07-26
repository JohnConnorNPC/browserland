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
            // ONE deadline covering connect THROUGH body (hence timeoutMs: 0 —
            // hostFetch's built-in deadline stops at the response headers, and a
            // remote broker that answers and then stalls its body would leave
            // this await, and the settings tab waiting on it, hung forever).
            let srv;
            const ctrl = new AbortController();
            const timer = setTimeout(() => ctrl.abort(), FETCH_TIMEOUT_MS);
            try {
                const r = await hostFetch(host, '/state',
                    { cache: 'no-store', signal: ctrl.signal, timeoutMs: 0 });
                if (!r.ok) return null;           // 401/5xx — handled elsewhere
                srv = await r.json();
            } catch (e) {                         // offline / CORS — no cache
                return null;
            } finally {
                clearTimeout(timer);
            }
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
        // #157: resolves with an OUTCOME — 'ok' | 'not_active' | 'error' | 'skipped'
        // — never rejecting, so a caller that needs to know can react while every
        // existing fire-and-forget caller is unaffected. It matters because a
        // remote /state PUT has a PERMANENT refusal mode: 409 not_active (another
        // browser holds that broker's lease) survives the adopt-and-retry below,
        // and a settings control that silently keeps showing the value it failed
        // to write is lying about the state of another machine.
        function putHostState(hostId, entry) {
            // Local writes go through the untouched F0 path.
            if (hostId === 'local') { savePrefs(); return Promise.resolve('ok'); }
            // Use the entry the settings tab CAPTURED (makeRemoteTarget passes it)
            // — never re-read hostStateCache here: an in-flight background prefetch
            // can replace the cached object after the tab fetched it, and PUTting
            // that swapped entry would silently drop the user's edit. Fall back to
            // the cache only for callers that don't hold the entry.
            if (!entry) entry = hostStateCache.get(hostId);
            if (!entry) return Promise.resolve('skipped');
            const prev = hostSaveChains.get(hostId) || Promise.resolve();
            const next = prev.then(() => _putHostStateOnce(hostId, entry))
                             .catch(() => 'error');
            hostSaveChains.set(hostId, next);
            return next;
        }
        async function _putHostStateOnce(hostId, entry) {
            const host = hostById(hostId);
            if (!host || !entry) return 'skipped';
            // PUT requires BOTH settings and layout objects. We only edit
            // settings, so the host's real layout rides along unchanged — but a
            // bare {} would WIPE that layout, so a save before the layout
            // actually loaded is refused rather than clobbering it.
            if (!entry.layoutLoaded || !entry.layout
                || typeof entry.layout !== 'object'
                || Array.isArray(entry.layout)) {
                showNotice('cannot save to ' + (host.label || host.url)
                    + ' — its layout has not loaded yet');
                return 'skipped';
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
            const label = host.label || host.url;
            try {
                let r = await doPut(entry.rev);
                // A 409 is EITHER a rev conflict (retryable) or the lease saying
                // another browser owns this broker (never retryable). Both arrive
                // as 409; `error: 'not_active'` in the inlined body is what tells
                // them apart, and only the first is worth a second PUT.
                let notActive = false;
                if (r.status === 409) {
                    let cur;
                    try { cur = await r.json(); } catch (e) { cur = null; }
                    notActive = !!(cur && cur.error === 'not_active');
                    if (cur && typeof cur === 'object' && !notActive) {
                        // Another viewer of THAT broker won — adopt its rev+layout
                        // baseline and retry ONCE, still carrying our edited
                        // settings (the user's change wins the merge).
                        if (cur.layout && typeof cur.layout === 'object'
                            && !Array.isArray(cur.layout)) {
                            entry.layout = cur.layout;
                            entry.layoutLoaded = true;
                        }
                        entry.rev = (typeof cur.rev === 'number')
                            ? cur.rev : entry.rev;
                        r = await doPut(entry.rev);
                        if (r.status === 409) {
                            let again;
                            try { again = await r.json(); } catch (e) { again = null; }
                            notActive = !!(again && again.error === 'not_active');
                        }
                    }
                }
                if (r.ok) {
                    let resp;
                    try { resp = await r.json(); } catch (e) { resp = null; }
                    if (resp && typeof resp.rev === 'number') entry.rev = resp.rev;
                    return 'ok';
                }
                if (notActive) {
                    showNotice('another browser is the active view on ' + label
                        + ' — it will not accept settings from here');
                    return 'not_active';
                }
                showNotice('could not save settings to ' + label);
                return 'error';
            } catch (e) {
                showNotice('could not reach ' + label + ' to save settings');
                return 'error';
            }
        }

