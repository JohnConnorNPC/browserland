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
        // A session missing from MISSING_POLLS_CLOSE consecutive successful
        // polls closes its open window (~12 s at POLL_MS — above the agent's
        // 10 s reconnect cap, so a broker restart never mass-closes).
        const MISSING_POLLS_CLOSE = 6;
        // A WS that lived this long before dying resets the reattach backoff.
        const REATTACH_STABLE_MS = 5000;
        const REATTACH_BACKOFF_MAX_MS = 30000;

