        // ---- hosts ----------------------------------------------------------
        // prefs._hosts = [{id, label, url, token, hidden, color}], reserved key
        // _settings. Entry 0 is always the implicit local host (id 'local',
        // url '' = same-origin) — non-removable, its token slot replaces the
        // old ?token= URL persistence. Remote ids are random strings minted
        // at add time and stable across URL edits, so host-qualified
        // per-session prefs survive. Self-heals like getSettings(): a
        // hand-edited blob can never break startup.
        function getHosts() {
            const raw = Array.isArray(prefs._hosts) ? prefs._hosts : [];
            const clean = [];
            let local = null;
            for (const h of raw) {
                if (!h || typeof h !== 'object' || Array.isArray(h)) continue;
                if (h.id === 'local') { if (!local) local = h; continue; }
                if (typeof h.id !== 'string' || !h.id) continue;
                if (typeof h.url !== 'string' || !h.url) continue;
                if (typeof h.label !== 'string' || !h.label) h.label = h.url;
                if (typeof h.token !== 'string') h.token = '';
                if (typeof h.hidden !== 'boolean') h.hidden = false;
                if (typeof h.color !== 'string') h.color = '';
                clean.push(h);
            }
            if (!local) local = { id: 'local' };
            local.label = 'this broker';
            local.url = '';
            if (typeof local.token !== 'string') local.token = '';
            if (typeof local.hidden !== 'boolean') local.hidden = false;
            if (typeof local.color !== 'string') local.color = '';
            clean.unshift(local);
            prefs._hosts = clean;
            return clean;
        }
        function allHosts() { return getHosts(); }
        function localHost() { return getHosts()[0]; }
        function hostById(id) {
            for (const h of getHosts()) { if (h.id === id) return h; }
            return null;
        }
        // #190: a host added after this page's boot can't be reached until
        // reload. The CSP is per-document — a loaded page keeps the
        // connect-src it was served with, and adding a host mid-session only
        // lands it in the broker's NEXT response — so the raw symptom is a
        // fetch() TypeError that reads exactly like the host being down.
        // prefs._hosts never rides /state (52_js_state_sync.js's _stateBlob
        // deliberately excludes it — hosts are per-browser), so the add flow
        // in this fragment's orbit is the only way a host record can arrive;
        // a one-time snapshot of every id already present when this fragment
        // first ran (i.e. already in localStorage at load) covers it.
        // Display-only: hostAddedAfterLoad() is a predicate for whichever
        // layer renders a host row to show HOST_ADDED_NOTE next to a
        // matching one — it never gates a connection attempt, and the
        // connection may in fact still work until the CSP starts enforcing.
        const _hostIdsAtLoad = new Set(getHosts().map(h => h.id));
        const HOST_ADDED_NOTE = 'added — reload to connect';
        function hostAddedAfterLoad(id) {
            return typeof id === 'string' && id !== '' && !_hostIdsAtLoad.has(id);
        }

        // #190 / OD8: the broker cannot see this registry — prefs._hosts is
        // deliberately per-browser and never rides /state (52_js_state_sync.js
        // excludes it from _stateBlob; 83_js_broker_identity.js persists it
        // with savePrefsLocal so it can NEVER PUT /state), which left the
        // server's connect-src fragment EMPTY in production. OD8 chose option
        // (a): the PAGE reports the origins it intends to dial, in a cookie
        // read on the document request. It grants nothing a token-holder could
        // not already get by writing _hosts (#190's own Problem section: "the
        // allowlist is self-referential"), so it costs no property the CSP was
        // buying, and it keeps hosts per-browser.
        //
        // ORIGINS ONLY, never the entry: a host record carries a `token`, and a
        // stored URL can embed userinfo credentials — so what goes out is
        // scheme+host+port rebuilt from a parse, and nothing else. Capped in
        // count and length here as well as on the server: a cookie over ~4KB is
        // silently dropped by browsers and would push other cookies out.
        // Values are '|'-joined: '|' cannot occur in an origin and needs no
        // encoding in a cookie value, so no decoder sits in front of the
        // broker's hardened parse.
        const CSP_HOSTS_COOKIE = 'bl_csp_hosts';
        const CSP_COOKIE_MAX_ORIGINS = 32;
        const CSP_COOKIE_MAX_CHARS = 1024;
        //: shape a rebuilt http(s) origin may have (host chars, IPv6 brackets,
        //: an optional port) — a last belt over the URL parser's output.
        const CSP_ORIGIN_RE = /^https?:\/\/(?:[a-z0-9.\-]+|\[[0-9a-f:.]+\])(?::[0-9]{1,5})?$/;
        function hostOriginsForCsp() {
            const out = [];
            const seen = new Set();
            for (const h of getHosts()) {
                if (out.length >= CSP_COOKIE_MAX_ORIGINS) break;
                const raw = (h && typeof h.url === 'string') ? h.url.trim() : '';
                if (!raw) continue;          // the local host is '' = same-origin
                let u;
                try { u = new URL(raw); } catch (e) { continue; }
                if (u.protocol !== 'http:' && u.protocol !== 'https:') continue;
                if (u.username || u.password) continue;   // never credentials
                // u.host is host[:port] with a default port already dropped.
                const origin = u.protocol + '//' + u.host;
                if (!CSP_ORIGIN_RE.test(origin)) continue;
                if (seen.has(origin)) continue;
                seen.add(origin);
                out.push(origin);
            }
            return out;
        }
        let _cspCookieLast = null;
        // Idempotent and cheap when nothing moved (a string compare), so it is
        // safe to call from any repaint the registry triggers.
        function syncHostOriginsCookie() {
            let value = hostOriginsForCsp().join('|');
            while (value.length > CSP_COOKIE_MAX_CHARS) {
                const cut = value.lastIndexOf('|');
                if (cut === -1) { value = ''; break; }
                value = value.slice(0, cut);   // drop whole origins, never split one
            }
            if (value === _cspCookieLast) return;
            _cspCookieLast = value;
            try {
                // Path=/ (every route the page dials) + SameSite=Strict (this
                // cookie has no business riding a cross-site request). Secure
                // ONLY over https: the dev broker is plain http, where a Secure
                // cookie is silently never sent — which reads exactly like
                // the feature not working.
                let cookie = CSP_HOSTS_COOKIE + '=' + value
                    + '; Path=/; SameSite=Strict; Max-Age=31536000';
                if (window.location.protocol === 'https:') cookie += '; Secure';
                document.cookie = cookie;
            } catch (e) { console.warn('csp hosts cookie write failed:', e); }
        }
        syncHostOriginsCookie();            // once at load, before any edit
        // #107: the host the START (+) button targets. Empty / 'local' / a removed
        // id all fall back to the local broker, so a deleted host never bricks
        // START. Resolution is deferred here (not in normalizeSettings) — same
        // precedent as hostDefaultProfile / hostDefaultColor.
        function defaultLaunchHost() {
            return hostById(getSettings().defaultHost) || localHost();
        }
        // Optional per-host DEFAULT accent (#103): the color a new terminal on
        // this host seeds from when it has no saved per-window color, instead of
        // the palette auto-pick. '' (or junk) = no default. strictHex rejects a
        // hand-edited/corrupted blob (never coerces to blue like normalizeHex),
        // so an invalid stored value cleanly falls through to the auto-pick.
        function hostDefaultColor(id) {
            const h = hostById(id);
            const c = h && h.color;
            return (typeof c === 'string' && strictHex(c)) ? c : '';
        }
        // Optional per-launch-profile DEFAULT accent (#115): the color a new
        // terminal seeds from when its launch profile carries one — it sits
        // ABOVE the per-host default (#103) but BELOW a saved per-window color
        // in the precedence chain. Unlike the host color (browser-local), the
        // profile color is broker-owned: it rides the names-only /profiles map
        // as a name->hex side-map, cached per host in profilesCache (76). Same
        // contract as hostDefaultColor: strictHex rejects a corrupt/absent value
        // so it cleanly falls through, and an unknown/renamed/deleted profile
        // name (or a cold cache) just returns '' -> host default / auto-pick.
        function profileDefaultColor(hostId, name) {
            if (!name) return '';
            const cached = profilesCache.get(hostId);
            const colors = cached && cached.colors;
            if (!colors || typeof colors !== 'object') return '';
            const c = colors[name];
            return (typeof c === 'string' && strictHex(c)) ? c : '';
        }

        // ---- per-broker hide toggle ("focus mode") ------------------------
        // Each host carries a persisted `hidden` flag (see getHosts). Hiding
        // is a pure display mask: the .broker-hidden class (display:none) is
        // applied to that broker's .term-window and .taskbar-item nodes; the
        // xterm + WebSocket stay alive so toggling back is instant. The
        // bringToFront() guard keeps focus off hidden windows everywhere.
        function hostHidden(id) {
            const h = hostById(id);
            // Display-masked when the user hid this broker (focus mode) OR when
            // another browser holds its single-active lease (leaseInactive) —
            // either way its windows + chips go display:none. The HOME host is
            // never leaseInactive (its lease drives the whole page, not a mask).
            return !!(h && h.hidden) || !!pollStateFor(id).leaseInactive;
        }
        // Idempotent reconciler — called on toggle AND every poll tick (so a
        // window/taskbar item that appears later under a hidden broker gets
        // masked). The per-node early-out (class already matches) is what
        // makes the per-tick call cheap and avoids focus/relayout churn.
        function applyHostVisibilityAll() {
            let changedTiled = false;
            for (const win of windows.values()) {
                if (win.disposed) continue;
                const hide = hostHidden(win.hostId);
                if (win.dom.classList.contains('broker-hidden') === hide) continue;
                win.dom.classList.toggle('broker-hidden', hide);
                if (hide && frontId === win.id) frontId = null;
                if (win.tiled) changedTiled = true;
            }
            document.querySelectorAll('.taskbar-item').forEach(el => {
                const key = el.dataset.sessionId || '';
                const ci = key.indexOf(':');
                const hid = ci !== -1 ? key.slice(0, ci) : 'local';
                el.classList.toggle('broker-hidden', hostHidden(hid));
            });
            if (changedTiled) requestRelayout();
        }
        function toggleHostHidden(id) {
            const h = hostById(id);
            if (!h) return;
            h.hidden = !h.hidden;
            savePrefs();
            applyHostVisibilityAll();
            updateTaskbarActive();
            renderHostStatus();
            // #149: the toggle now has three surfaces — single-broker chip,
            // (+) menu broker row, and the Hosts pane's hidden checkbox — so
            // a toggle from any of them must repaint the pane's checkbox.
            // renderHostsList is safe with the Control Panel closed (its
            // #set-hosts-list mount is static markup in 40_body.html).
            renderHostsList();
            // #195: `hidden` is a persisted field of the host record, written
            // from three surfaces (this pane's checkbox, the single-broker chip,
            // the (+) menu row) and from none of them reachable by a mod. The
            // bus is a mod's ONLY notification that a host moved, so a mod
            // rendering its own host list would otherwise show a broker the
            // user hid, forever, with nothing to poll. Under-emitting is the
            // unfixable direction; an extra edge costs a re-read.
            //
            // NOT because the Hosts form emits for this field — it does not.
            // That form is label/url/password only; `hidden` and `color` never
            // go through it. This site and the colour swatch are the only
            // writers of their fields, which is precisely why they must speak.
            // The edge carries an id, not a snapshot, so a subscriber that only
            // cares about connection identity re-reads and ignores it. No
            // invalidate: no per-host cache is keyed on `hidden`.
            if (typeof _emitModEvent === 'function') {
                _emitModEvent('host:changed', { hostId: id });
            }
        }

