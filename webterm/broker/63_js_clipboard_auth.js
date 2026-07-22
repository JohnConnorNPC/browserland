        // ---- clipboard ----------------------------------------------------
        // The broker is commonly served on http://<LAN-IP>:4445, which
        // browsers treat as a non-secure context. navigator.clipboard.*
        // is gated on isSecureContext, so on that origin both copy and
        // paste fall through to the legacy paths below.
        function canReadClipboard() {
            return !!(window.isSecureContext
                && navigator.clipboard
                && navigator.clipboard.readText);
        }
        function canWriteClipboardModern() {
            return !!(window.isSecureContext
                && navigator.clipboard
                && navigator.clipboard.writeText);
        }
        // ---- clipboard images (#137) ----------------------------------------
        // clipboard.read() (unlike readText) can return image/* items — the
        // capture side of image paste. Secure contexts only; on plain http the
        // Ctrl+V DOM-paste path still carries images via the gesture-scoped
        // clipboardData, which needs no API permission.
        function canReadClipboardItems() {
            return !!(window.isSecureContext
                && navigator.clipboard
                && navigator.clipboard.read);
        }
        async function readClipboardImageBlob() {
            // First image/* blob on the clipboard, or null. TEXT WINS: any
            // text/plain item -> null, so copying from apps that put both text
            // and a rendered bitmap on the clipboard (Word, browsers) pastes
            // the text, as a terminal user expects. Throws propagate to the
            // caller (permission denied / unsupported browser).
            const items = await navigator.clipboard.read();
            for (const item of items) {
                if (item.types.indexOf('text/plain') !== -1) return null;
            }
            for (const item of items) {
                for (const type of item.types) {
                    if (type.indexOf('image/') === 0) {
                        return await item.getType(type);
                    }
                }
            }
            return null;
        }
        function blobToBase64(blob) {
            // FileReader dataURL with the data:...;base64, prefix stripped —
            // same idiom as the file manager's readB64.
            return new Promise((resolve, reject) => {
                const fr = new FileReader();
                fr.onload = () => {
                    const s = String(fr.result || '');
                    const i = s.indexOf(',');
                    resolve(i === -1 ? '' : s.slice(i + 1));
                };
                fr.onerror = () => reject(fr.error || new Error('read failed'));
                fr.readAsDataURL(blob);
            });
        }
        function quotePathForPrompt(p) {
            // Double-quote only when whitespace forces it: Claude Code (and
            // shells) take a bare path fine, and quoting matches what its own
            // drag-and-drop inserts on Windows.
            return /\s/.test(p) ? '"' + p + '"' : p;
        }
        // ---- clipboard observers (#106) -----------------------------------
        // An explicit, reviewable notify registry so a mod (the #106 clipboard
        // history mod) can watch every copy-OUT / paste-IN WITHOUT monkey-patching
        // core. Additive: core calls _notifyClipboard at its copy/paste seams
        // (copyTextToClipboard below + the terminal paste listeners in
        // 67_js_window_lifecycle.js); the Set is empty — a clean no-op — until a
        // mod registers via addClipboardObserver (exposed as ctx.clipboard.observe).
        // Each observer is isolated so one bad handler can't break a copy/paste or a
        // sibling. Nothing is captured while no mod is enabled, so the default
        // posture records nothing (the history mod ships default-off — clipboards
        // carry secrets).
        const _clipboardObservers = new Set();
        function _notifyClipboard(dir, text) {
            if (!text) return;
            for (const fn of Array.from(_clipboardObservers)) {
                try { fn(dir, text); } catch (_) {}
            }
        }
        function addClipboardObserver(fn) {
            _clipboardObservers.add(fn);
            return function () { _clipboardObservers.delete(fn); };
        }
        function copyTextToClipboard(text) {
            if (!text) return;
            _notifyClipboard('out', text);   // #106: one call covers both write paths
            if (canWriteClipboardModern()) {
                navigator.clipboard.writeText(text).catch(() => {
                    copyTextLegacy(text);
                });
                return;
            }
            copyTextLegacy(text);
        }
        function copyTextLegacy(text) {
            // execCommand('copy') still works in non-secure contexts as
            // long as it runs inside a user gesture (mouseup, keydown).
            // Stash a hidden textarea, select it, copy, restore focus to
            // whatever xterm.js had so further keystrokes still hit the
            // terminal.
            const prev = document.activeElement;
            const ta = document.createElement('textarea');
            ta.value = text;
            ta.setAttribute('readonly', '');
            ta.style.position = 'fixed';
            ta.style.left = '-9999px';
            ta.style.top = '0';
            ta.style.opacity = '0';
            document.body.appendChild(ta);
            try {
                ta.select();
                ta.setSelectionRange(0, text.length);
                document.execCommand('copy');
            } catch (e) {
                console.debug('legacy copy failed:', e);
            } finally {
                document.body.removeChild(ta);
                try { prev && prev.focus && prev.focus(); } catch (_) {}
            }
        }

        // ---- auth -----------------------------------------------------------
        // The password IS the broker's auth_token — one secret per broker,
        // kept per-browser in localStorage (each host record's token slot).
        // ?token=/?auth= in the URL is still accepted for deep links:
        // adopted into the local host's slot, then scrubbed via
        // history.replaceState (?session= survives). No cookies, no /login.
        const params = new URLSearchParams(window.location.search);
        (function adoptUrlToken() {
            const t = params.get('token') || params.get('auth');
            if (!t) return;
            localHost().token = t;
            savePrefs();
            try {
                const u = new URL(window.location.href);
                u.searchParams.delete('token');
                u.searchParams.delete('auth');
                history.replaceState(null, '', u);
            } catch (_) {}
        })();

        // Every fetch / WS dial goes through these: same-origin for the local
        // host, the stored origin for remotes, each host's own token attached.
        //
        // HTTP sends the token as `Authorization: Bearer`, never in the query
        // string (#144). A URL credential leaks everywhere a header does not:
        // performance.getEntriesByType('resource') hands the full URL to ANY
        // script on the page, DevTools HAR exports carry it into bug reports,
        // and a reverse proxy (`tailscale serve`, the recommended topology)
        // logs it. There is deliberately NO function here that builds a
        // token-bearing HTTP URL, so one cannot be reintroduced by accident.
        //
        // Cost: an Authorization header makes a cross-origin request
        // non-simple, so remote hosts now pay an OPTIONS preflight. The broker
        // answers every one and sets Access-Control-Max-Age: 86400, so it is
        // one round trip per host per day, not per request. ACAO stays `*`:
        // a header set explicitly by fetch() is NOT a "credentialed" request in
        // the CORS sense (that needs credentials:'include'), so `*` remains
        // legal and a wrong token still returns a READABLE 401 rather than an
        // opaque failure — which is what keeps the login overlay working.
        function hostUrl(host, path) {
            return ((host && host.url) ? host.url : '') + path;
        }
        function hostFetch(host, path, opts) {
            const o = Object.assign({}, opts || {});
            // Headers(), NOT Object.assign: a caller passing a real Headers
            // instance (or an array of pairs) would spread to {} and SILENTLY
            // drop its Content-Type, turning a JSON POST into a bodyless one
            // the broker then rejects as bad_json. No caller does that today;
            // this makes it impossible for one to start. Headers() also
            // replaces any existing authorization case-insensitively.
            const headers = new Headers(o.headers || undefined);
            if (host && host.token) {
                headers.set('Authorization', 'Bearer ' + host.token);
            }
            o.headers = headers;
            return fetch(hostUrl(host, path), o);
        }
        function hostWsUrl(host, path) {
            let base;
            if (host && host.url) {
                base = host.url.replace(/^http/, 'ws');
            } else {
                const proto = window.location.protocol === 'https:'
                    ? 'wss:' : 'ws:';
                base = proto + '//' + window.location.host;
            }
            // The ONE place a token still rides a URL, and it is unavoidable:
            // the browser WebSocket API cannot set request headers on the
            // handshake. /ws, /control and /browserland therefore keep
            // ?token=. Documented in docs/TECHNICAL.md rather than papered
            // over — closing it needs a connect-ticket scheme, not a refactor.
            if (!host || !host.token) return base + path;
            return base + path + (path.indexOf('?') === -1 ? '?' : '&')
                + 'token=' + encodeURIComponent(host.token);
        }

        // Login overlay, per host: shown on any 401/4401 — first visit
        // without a password, or a stale one after that broker restarted
        // with a new token. Auto-pops at most ONCE per failing host
        // (authPrompted); after a cancel the amber "auth" taskbar chip is
        // the way back in — never a re-popping modal loop.
        const authOverlay = document.getElementById('auth-overlay');
        const authErrorEl = document.getElementById('auth-error');
        const authTokenEl = document.getElementById('auth-token');
        const authHostLine = document.getElementById('auth-host-line');
        let authOverlayHostId = null;   // host the open overlay belongs to
        const authPrompted = new Set(); // host ids already auto-popped
        function showAuthOverlay(host, force) {
            if (!host) return;
            if (authOverlay.classList.contains('open')) {
                // Never clobber a form the user is typing into.
                if (authOverlayHostId === host.id || !force) return;
            } else if (!force && authPrompted.has(host.id)) {
                return;                  // degraded to the taskbar chip
            }
            authPrompted.add(host.id);
            authOverlayHostId = host.id;
            // textContent only — labels are user input and tokens live in
            // localStorage; never innerHTML a user-controlled string.
            authHostLine.textContent = host.id === 'local'
                ? 'this broker (' + window.location.host + ')'
                : host.label + ' (' + host.url + ')';
            authErrorEl.classList.remove('show');
            authTokenEl.value = '';
            authOverlay.classList.add('open');
            try { authTokenEl.focus(); } catch (_) {}
        }
        function hideAuthOverlay() {
            authOverlay.classList.remove('open');
            authOverlayHostId = null;
        }
        document.getElementById('auth-cancel').addEventListener('click', () => {
            hideAuthOverlay();
            // The amber chip must render even for a cancelled single-host
            // local login, or the UI is bricked with no way back in.
            renderHostStatus();
        });
        document.getElementById('auth-form').addEventListener('submit', async (e) => {
            e.preventDefault();
            const host = hostById(authOverlayHostId);
            if (!host) { hideAuthOverlay(); return; }
            const candidate = authTokenEl.value;
            if (!candidate) return;
            let resp;
            try {
                // Probe with the CANDIDATE token, not the stored one, via the
                // same Bearer path every other request uses (#144). It must
                // not be a ?token= URL: the broker reads the query BEFORE the
                // header, so a leftover query token would silently win over
                // the header and make a correct password look wrong -- and the
                // probe URL would land in Resource Timing like any other.
                resp = await hostFetch({ url: host.url, token: candidate },
                                       '/sessions');
            } catch (err) {
                // With CORS pinned onto 401s too, a TypeError here really
                // is "host down", not "wrong password".
                authErrorEl.textContent = 'broker unreachable: ' + err;
                authErrorEl.classList.add('show');
                return;
            }
            if (resp.status === 401) {
                authErrorEl.textContent = 'invalid password';
                authErrorEl.classList.add('show');
                return;
            }
            if (!resp.ok) {
                authErrorEl.textContent = 'probe failed: HTTP ' + resp.status;
                authErrorEl.classList.add('show');
                return;
            }
            host.token = candidate;
            savePrefs();
            authPrompted.delete(host.id);   // future failures may pop again
            const st = hostPolls.get(host.id);
            if (st) st.authNeeded = false;
            authTokenEl.value = '';
            hideAuthOverlay();
            renderHostStatus();
            refreshTaskbar();
            // Re-dial this host's control WS (the single-active lease channel):
            // it may have died on a 4401 before the token was entered. For the
            // HOME broker this is what un-blocks the deferred boot / overlay.
            try { openControlWs(host); } catch (_) {}
            // App windows (text editor / file manager) carry hostId 'app', so
            // the terminal-healing loop below never reaches them. Notify any
            // file tool bound to the host that just authenticated so it can
            // refresh its view in place rather than wait for a manual retry
            // (#46). Wrapped per-window so one bad hook can't break the rest.
            for (const win of windows.values()) {
                if (win.disposed || !win._onHostAuth) continue;
                try { win._onHostAuth(host.id); } catch (_) {}
            }
            // Heal in place: re-dial THIS host's windows that died on auth
            // (or are just dead) — the relay re-snapshots on attach.
            for (const win of windows.values()) {
                if (win.disposed || win.hostId !== host.id) continue;
                if (!win.wsOpen || win.authFailed) {
                    win.authFailed = false;
                    win.reattachAttempts = 0;
                    win.reattachAt = 0;
                    reattachWindow(win);
                }
            }
        });

        // ---- single-active-browser overlay (F-ACTIVECLIENT) ---------------
        // Full-page takeover prompt shown when the HOME broker reports another
        // browser holds the lease. The only path back in is the button, which
        // sends become_active; the view rebuilds ONLY on the broker's
        // {active:true} push (no optimistic UI).
        const becomeActiveOverlay =
            document.getElementById('become-active-overlay');
        function showBecomeActiveOverlay() {
            if (becomeActiveOverlay) becomeActiveOverlay.classList.add('open');
        }
        function hideBecomeActiveOverlay() {
            if (becomeActiveOverlay) {
                becomeActiveOverlay.classList.remove('open');
            }
        }
        const becomeActiveBtn = document.getElementById('become-active-btn');
        if (becomeActiveBtn) {
            becomeActiveBtn.addEventListener('click', () => {
                // Request only — wait for the broker's {active:true} to rebuild.
                sendBecomeActive(localHost());
            });
        }

        // ---- default / locked size ------------------------------------------
        // One size drives both: the Control Panel cols×rows value (when set) is
        // converted to pixels through the measured cell box plus the
        // .term-window chrome constants (+12 horiz, +38 vert — see the
        // box-model comment in the `resized` handler). With no setting the
        // legacy 720×480 px default applies unchanged.
        function liveCellDims() {
            for (const win of windows.values()) {
                if (win.disposed) continue;
                const dims = readCellDims(win);
                if (dims) return dims;
            }
            return getSettings().cellDims;   // last measured, persisted
        }
        function defaultPixelSize() {
            const size = getSettings().size;
            if (size) {
                const dims = liveCellDims();
                if (dims) {
                    return {
                        width: Math.max(MIN_W, Math.ceil(size.cols * dims.w) + 12),
                        height: Math.max(MIN_H, Math.ceil(size.rows * dims.h) + 38),
                    };
                }
            }
            return { width: DEFAULT_W, height: DEFAULT_H };
        }

        // ---- size lock ----------------------------------------------------
        // Reserved key inside the same prefs object — numeric session ids
        // never collide with the leading underscore.
        function isSizeLocked() { return !!prefs._lockSize; }
        function lockedSize() { return defaultPixelSize(); }
        function applyLockedSizeToAll() {
            // Snap every live window to the locked size at its current
            // position. applyGeomToWindow's internal lock guard makes
            // this idempotent.
            const ls = lockedSize();
            for (const win of windows.values()) {
                if (win.disposed || win.tiled) continue;   // tiled: flex-sized
                const g = win.geom || {};
                applyGeomToWindow(win, {
                    left: g.left | 0,
                    top: g.top | 0,
                    width: ls.width,
                    height: ls.height,
                });
                refitSoon(win);
            }
            savePrefs();
        }
        function setSizeLocked(on) {
            prefs._lockSize = !!on;
            savePrefs();
            document.body.classList.toggle('size-locked', !!on);
            if (on) applyLockedSizeToAll();
            // Unlock leaves windows at their current (locked) size — user
            // can drag-resize or run a tile action to redistribute.
        }

