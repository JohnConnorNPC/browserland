        const STORAGE_KEY = 'webterm:prefs:v1';
        // Stable per-browser id for the single-active-browser lease
        // (F-ACTIVECLIENT). Minted once into localStorage and sent to every
        // broker (terminal /ws, /control, /state PUT) so each broker can
        // enforce "one active browser at a time". crypto.randomUUID needs a
        // secure context (https / localhost); fall back for a plain-http
        // tailnet load. Private-mode storage failure yields an ephemeral
        // per-load id — the lease still works, it just won't persist.
        const CLIENT_ID_KEY = 'webterm:clientId';
        function getClientId() {
            try {
                let id = localStorage.getItem(CLIENT_ID_KEY);
                if (!id) {
                    id = (window.crypto && crypto.randomUUID)
                        ? crypto.randomUUID()
                        : 'c-' + Date.now().toString(36) + '-'
                            + Math.random().toString(16).slice(2);
                    localStorage.setItem(CLIENT_ID_KEY, id);
                }
                return id;
            } catch (e) {
                return 'c-' + Date.now().toString(36) + '-'
                    + Math.random().toString(16).slice(2);
            }
        }
        const CLIENT_ID = getClientId();
        // 11-color preset palette (#29): one shared set for EVERY window type
        // (terminals, notes, editors). 8 originals + cyan/lime/coral, laid out
        // as the first three rows of the 4x4 picker (the 12th slot is custom).
        const PALETTE = [
            '#4aa3ff', '#5fbf7f', '#e09b3a', '#b78bf0',
            '#e96d6d', '#3fb6b6', '#d2c54a', '#d96fb1',
            '#5cc8e6', '#9bd14f', '#ff8c5a'
        ];
        const DEFAULT_W = 720;
        const DEFAULT_H = 480;
        const CASCADE_DX = 28;
        const CASCADE_DY = 28;
        const POLL_MS = 2000;
        const FAST_POLL_MS = 250;
        // Covers the /launch 202 path: the broker answers after 10 s even
        // when the agent hasn't registered yet, so give the id time to land.
        const AUTO_OPEN_TIMEOUT_MS = 15000;
        const RESIZE_DEBOUNCE_MS = 150;
        const MIN_W = 280;
        const MIN_H = 160;
        const FETCH_TIMEOUT_MS = 3000;
        const STALE_AFTER_FAILURES = 2;
        // #157: a mod-policy key is a mod id — the SAME shape the broker enforces
        // (_MODSTORE_ID_RE in app.py) and the shape every mod dir uses. Anything
        // else in a /state blob is junk the policy editor must not render, and the
        // cap bounds the row count (and the blob) no matter what a peer sends.
        const MOD_ID_RE = /^[a-z0-9][a-z0-9-]{0,63}$/;
        const MAX_MOD_POLICY_KEYS = 256;
        // #168: the hard ceiling on a ctx.settings.text value — a mod may ask for
        // LESS (opts.maxLength), never more. Counted in UTF-16 code units
        // (String.length), which is the SAME unit mod-sync's STR_MAX (4096)
        // counts, and well under it: a value this cap admits therefore always
        // survives a cross-broker carry, whatever it is made of. (Code POINTS
        // would be a different, smaller number for astral text and would let a
        // 4096-code-unit value through; bytes would be a third. One unit, shared.)
        const MAX_MOD_TEXT_LEN = 1024;
        // A session missing from MISSING_POLLS_CLOSE consecutive successful
        // polls closes its open window (~12 s at POLL_MS — above the agent's
        // 10 s reconnect cap, so a broker restart never mass-closes).
        const MISSING_POLLS_CLOSE = 6;
        // A WS that lived this long before dying resets the reattach backoff.
        const REATTACH_STABLE_MS = 5000;
        const REATTACH_BACKOFF_MAX_MS = 30000;

        // ---- per-host cache invalidation (#195) ---------------------------
        // ONE core-owned answer to "forget everything this page knows about
        // host <id>".
        //
        // Every per-host cache is declared in the fragment that USES it —
        // hostStateCache/hostSaveChains in 53, authPrompted in 63, hostPolls
        // and the /control socket in 64, profilesCache in 76, the MCP and
        // launch-profile config caches in 79 — and until #195 the only
        // COMPLETE list of them lived in a mod (host-registry's own
        // invalidateHost), while core's host-edit path cleared a strict subset
        // of it. A list kept by a stranger goes stale, which that mod's comment
        // said out loud ("a known latent gap"). So the direction is inverted
        // here: a cache REGISTERS ITSELF beside its own declaration, and the
        // next per-host cache is covered by the fragment that adds it instead
        // of being remembered somewhere else.
        //
        // WHY THIS FRAGMENT: it is the FIRST JS fragment in ui.py's _ORDERED,
        // and every registration happens at fragment top level, so this is the
        // only place the whole page — starting with 53 — can register into.
        //
        // A clearer is (id) -> void and must be IDEMPOTENT: clearing an id that
        // has no entry is a no-op, and invalidateHost runs on paths that may
        // have run already. Clearing a Map does not cancel an in-flight request
        // — it drops the stale value so the NEXT request uses the new
        // url/token, and the poll loop re-primes on its next tick. That is the
        // model core's host edit has always used.
        const _hostCacheClearers = [];
        function registerHostCache(name, clear) {
            if (typeof name !== 'string' || !name) return false;
            if (typeof clear !== 'function') return false;
            for (let i = 0; i < _hostCacheClearers.length; i++) {
                if (_hostCacheClearers[i].name !== name) continue;
                // A duplicate NAME is a programming error, and never a second
                // clearer quietly shadowing the first: refused loudly, at load,
                // where it is still readable as what it is.
                try {
                    console.warn('[hosts] a per-host cache named "' + name
                        + '" is already registered; ignoring the second');
                } catch (_) { /* console is gone */ }
                return false;
            }
            _hostCacheClearers.push({ name: name, clear: clear });
            return true;
        }

        // The repaint one host change costs, in the order removeHost has always
        // run it — and it is INSIDE the invalidation (below) rather than beside
        // it, because a cleared cache that nothing repaints leaves stale pixels,
        // which is the half of "invalidate" a caller most easily forgets.
        // renderHostStatus is called EXPLICITLY, and that is the subtlety no
        // caller should have to re-derive: refreshTaskbar coalesces onto an
        // in-flight run and can no-op, so the status chips would keep the old
        // host's colour. Each call is isolated for the reason the clearers are:
        // a broken render must not cost the other three. (A rejected promise
        // from the async refreshTaskbar is left alone — exactly as every
        // existing caller leaves it.)
        function _repaintHostSurfaces() {
            const renders = [
                ['renderHostsList', renderHostsList],
                ['renderSettingsTabs', renderSettingsTabs],
                ['renderHostStatus', renderHostStatus],
                ['refreshTaskbar', refreshTaskbar],
            ];
            for (let i = 0; i < renders.length; i++) {
                try { renders[i][1](); }
                catch (e) { _logHostInvalidate(renders[i][0], '', e); }
            }
        }

        // One place this reports a failure, and it must not become the second
        // one: a console that is gone (or replaced) must not turn a swallowed
        // cache error into a throw escaping into removeHost.
        function _logHostInvalidate(what, id, e) {
            try {
                console.error('[hosts] invalidate: ' + what
                    + (id ? ' for host "' + id + '"' : '') + ' failed:', e);
            } catch (_) { /* console is gone; keep going */ }
        }

        // THE invalidation path (#195). Core's own host edit and host removal
        // (83) call it, and so does ctx.hosts.invalidate (86c) — one function,
        // so a mod and core cannot disagree about what "forget this host" means.
        //
        // ISOLATED PER CLEARER: one throwing cache must not cost the other
        // seven. Half-cleared is the worst of the three possible outcomes.
        //
        // TWO THINGS IT DELIBERATELY DOES NOT DO:
        //   savePrefs() — that is a MUTATION (localStorage + a /state push).
        //     Invalidation has to stay safe to call from anywhere, including a
        //     `host:changed` subscriber; a caller that CHANGED something saves
        //     it itself.
        //   _emitModEvent('host:changed', …) — invalidation and notification
        //     are separate acts. Only core mutation sites emit (83), so a mod
        //     that reacts to `host:changed` by invalidating cannot recurse.
        //
        // Returns true when it ran, false for a missing/blank id — nothing to
        // forget, and no repaint owed.
        function invalidateHost(id) {
            if (typeof id !== 'string' || !id) return false;
            for (let i = 0; i < _hostCacheClearers.length; i++) {
                const entry = _hostCacheClearers[i];
                try { entry.clear(id); }
                catch (e) { _logHostInvalidate(entry.name, id, e); }
            }
            _repaintHostSurfaces();
            return true;
        }
        // ---- end per-host cache invalidation -------------------------------

