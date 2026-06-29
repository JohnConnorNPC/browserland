        // ---- prefs (localStorage) -----------------------------------------
        function loadPrefs() {
            try {
                const raw = localStorage.getItem(STORAGE_KEY);
                if (!raw) return {};
                const obj = JSON.parse(raw);
                return (obj && typeof obj === 'object' && !Array.isArray(obj)) ? obj : {};
            } catch (e) {
                console.warn('prefs load failed:', e);
                return {};
            }
        }
        // localStorage write only — the instant offline cache.
        function savePrefsLocal() {
            try { localStorage.setItem(STORAGE_KEY, JSON.stringify(prefs)); }
            catch (e) { console.warn('prefs save failed:', e); }
        }
        // savePrefs() keeps its signature (every existing caller is untouched)
        // but now ALSO mirrors _settings + _layout to the same-origin broker's
        // /state (F0), so a user's other browsers / another viewer of this
        // broker converge. _hosts and per-session pixel geometry stay
        // browser-local and are NEVER pushed (see _stateBlob).
        function savePrefs() {
            savePrefsLocal();
            schedulePush();
        }
        function getPref(id) {
            const k = String(id);
            if (!prefs[k]) prefs[k] = {};
            return prefs[k];
        }
        const prefs = loadPrefs();

        // ---- recent colors (#29) ------------------------------------------
        // The last 4 colors picked from ANY window's color dropdown, global and
        // most-recent-first. Stored under a DEDICATED local key — never prefs or
        // appStore, both of which sync to the broker /state (a per-browser MRU
        // shouldn't ride the shared blob). Every read/write is best-effort.
        const RECENT_COLORS_KEY = 'webterm:recentColors:v1';
        const MAX_RECENT_COLORS = 4;
        // STRICT hex validator: a normalized #rrggbb, or null for anything else.
        // (normalizeHex coerces junk to PALETTE[0]; recents must DROP junk, not
        // silently turn corrupted storage into blue.)
        function strictHex(c) {
            if (typeof c !== 'string') return null;
            if (/^#[0-9a-fA-F]{6}$/.test(c)) return c.toLowerCase();
            if (/^#[0-9a-fA-F]{3}$/.test(c)) {
                const s = c.slice(1);
                return ('#' + s[0]+s[0]+s[1]+s[1]+s[2]+s[2]).toLowerCase();
            }
            return null;
        }
        function loadRecentColors() {
            try {
                const raw = localStorage.getItem(RECENT_COLORS_KEY);
                if (!raw) return [];
                const arr = JSON.parse(raw);
                if (!Array.isArray(arr)) return [];
                const out = [];
                for (const c of arr) {
                    const h = strictHex(c);
                    if (h && !out.includes(h)) out.push(h);   // validate + dedupe
                    if (out.length >= MAX_RECENT_COLORS) break;
                }
                return out;
            } catch (e) { return []; }
        }
        function saveRecentColor(color) {
            const h = strictHex(color);
            if (!h) return;
            try {
                const list = loadRecentColors().filter((c) => c !== h);
                list.unshift(h);                              // most-recent first
                localStorage.setItem(RECENT_COLORS_KEY,
                    JSON.stringify(list.slice(0, MAX_RECENT_COLORS)));
            } catch (e) { /* quota/security: a missing MRU is harmless */ }
        }

