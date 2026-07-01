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
        }

