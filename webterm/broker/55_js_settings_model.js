        // ---- global settings ------------------------------------------------
        // Single reserved `_settings` key inside the same prefs object —
        // numeric session keys never collide with the leading underscore,
        // so per-session entries are untouched. Every field self-heals so a
        // hand-edited or stale localStorage blob can never break startup.
        // Repair/fill EVERY field a settings blob must guarantee, in place,
        // and return it. Factored out of getSettings() so the SAME self-heal
        // runs on a REMOTE host's settings too (task 13/14): the local blob is
        // getSettings()'s live prefs._settings; a remote blob comes from that
        // broker's /state and is normalized before it lands in hostStateCache.
        function normalizeSettings(s) {
            if (!s || typeof s !== 'object' || Array.isArray(s)) s = {};
            const sz = s.size;
            if (!sz || typeof sz !== 'object'
                || !Number.isFinite(sz.cols) || !Number.isFinite(sz.rows)
                || sz.cols < 1 || sz.rows < 1) {
                s.size = null;                    // null = legacy 720x480 px
            }
            if (!s.show || typeof s.show !== 'object') s.show = {};
            // Defaults reproduce the bridge's labels exactly: id chip on,
            // host prefix on, pid off.
            if (typeof s.show.id !== 'boolean') s.show.id = true;
            if (typeof s.show.pid !== 'boolean') s.show.pid = false;
            if (typeof s.show.host !== 'boolean') s.show.host = true;
            const cd = s.cellDims;
            if (!cd || typeof cd !== 'object' || !(cd.w > 0) || !(cd.h > 0)) {
                s.cellDims = null;                // {w,h} last measured cell
            }
            // Workspace label mode (task 19): show the number or the name.
            if (s.wsLabelMode !== 'name' && s.wsLabelMode !== 'number') {
                s.wsLabelMode = 'number';
            }
            // Restore-on-refresh (default on): on a browser reload, re-open the
            // terminals that were open and reattach to their live agents.
            if (typeof s.restoreOnRefresh !== 'boolean') s.restoreOnRefresh = true;
            // Custom workspace scrollbar (default on): a slim themed overlay bar
            // at the bottom of the tiling strip, shown whenever it can scroll
            // right. Shared setting (syncs via /state like theme/show.*).
            if (typeof s.stripScrollbar !== 'boolean') s.stripScrollbar = true;
            // #38: dwell delay (ms) before a still drag offers snap (float->tile)
            // or pop-out (tile->float). Per-host, syncs via /state like the
            // toggles above. 0 disables BOTH dwell gestures; otherwise clamp to a
            // sane [250, 20000] so a hand-edited blob can't make the gesture
            // impossible to fire or never time out. Missing/invalid -> the
            // 3000ms default (= HOLD_MS).
            if (s.snapHoldMs !== 0) {
                if (typeof s.snapHoldMs !== 'number' || !isFinite(s.snapHoldMs)) {
                    s.snapHoldMs = 3000;
                } else {
                    s.snapHoldMs = Math.max(250, Math.min(20000, Math.round(s.snapHoldMs)));
                }
            }
            // Hide (vs. dim) taskbar items for windows on other workspaces.
            // Browser-global UI-chrome preference; default off (today's behavior).
            if (typeof s.hideTaskbarOtherWs !== 'boolean') s.hideTaskbarOtherWs = false;
            // Task 4: customizable keybindings (actionId -> combo string).
            // Any missing/non-string action falls back to its default so a
            // partially hand-edited blob still has a working set.
            if (!s.keybindings || typeof s.keybindings !== 'object'
                || Array.isArray(s.keybindings)) s.keybindings = {};
            for (const id of Object.keys(DEFAULT_KEYBINDINGS)) {
                if (typeof s.keybindings[id] !== 'string') {
                    s.keybindings[id] = DEFAULT_KEYBINDINGS[id];
                }
            }
            // Task 12: Control Panel theming. Each field self-heals so a hand-
            // edited blob AND a remote host's settings converge to valid values;
            // the defaults reproduce the current look exactly (none/off/+). #75:
            // `theme` is owned by mods/theme/theme.js now — its radio does the
            // read-through validation (unknown -> night) WITHOUT rewriting the
            // synced blob, so core no longer normalizes it here or references the
            // palette. Default stays night via the mod's fallback + the :root CSS.
            if (typeof s.pattern !== 'string' || PATTERNS.indexOf(s.pattern) < 0) {
                s.pattern = 'none';
            }
            // Issue #18: terminal font. Whitelist to the offered TERM_FONTS
            // values ('' = built-in default) so an unknown/hand-edited value
            // can't leave the picker blank while a stray font silently applies.
            if (typeof s.termFont !== 'string'
                || !TERM_FONTS.some(f => f.value === s.termFont)) {
                s.termFont = '';
            }
            if (typeof s.clock !== 'boolean') s.clock = false;
            // #40: the taskbar "?" help chip (browser-global, default ON) plus a
            // one-time first-run nudge flag (flipped true once the hint shows or
            // Help is first opened, so it never repeats).
            if (typeof s.showHelpButton !== 'boolean') s.showHelpButton = true;
            if (typeof s.helpHintSeen !== 'boolean') s.helpHintSeen = false;
            // Task 8: per-host default terminal profile. '' = use the broker's
            // own server default (the prior behavior).
            if (typeof s.defaultProfile !== 'string') s.defaultProfile = '';
            if (typeof s.startLabel !== 'string') {
                s.startLabel = '+';
            } else {
                s.startLabel = s.startLabel.trim().slice(0, 24) || '+';
            }
            // Issue #10: single default start path for new terminals (collapses
            // the per-OS pair from #2). Browser-local (like startLabel) and
            // belongs to THIS broker's host; resolveStartPath only sends it to a
            // host whose OS matches this broker's, so a wrong-OS cwd is never
            // sent. '' = the agent's own default cwd. The legacy
            // startPaths{windows,posix} map is intentionally left untouched here
            // and honored read-only as a fallback (resolveStartPath) so an
            // upgrade never loses a saved path; it is retired only when the box
            // is next saved (commitStartPath).
            s.startPath = (typeof s.startPath === 'string') ? s.startPath.trim() : '';
            // User-editable AGENTS.md section library (synced via /state like the
            // rest of settings). Seed from DEFAULT_SECTIONS the first time a blob
            // lacks one; otherwise validate in place (drop malformed entries,
            // unique delimiter-safe ids). An empty array is respected (the user
            // may have removed every section) — only a missing/non-array seeds.
            if (!Array.isArray(s.sections)) {
                s.sections = DEFAULT_SECTIONS.map(
                    t => ({ id: t.id, label: t.label, body: t.body }));
            } else {
                s.sections = normalizeSectionsArray(s.sections);
            }
            // Per-window MCP access pins (off/read/readwrite), keyed by the
            // durable window key `<hostId>:<sid>`. Synced via /state so a
            // window's chosen mode follows it across refresh, browsers, and
            // same-id broker reconnects. Self-heal: a non-object becomes {};
            // any entry whose value is not a valid mode is dropped, so a hand-
            // edited or stale blob can never inject a bogus mode.
            if (!s.mcpModes || typeof s.mcpModes !== 'object'
                || Array.isArray(s.mcpModes)) {
                s.mcpModes = {};
            } else {
                for (const k of Object.keys(s.mcpModes)) {
                    const v = s.mcpModes[k];
                    if (v !== 'off' && v !== 'read' && v !== 'readwrite') {
                        delete s.mcpModes[k];
                    }
                }
            }
            return s;
        }
        function getSettings() {
            let s = prefs._settings;
            if (!s || typeof s !== 'object' || Array.isArray(s)) {
                s = prefs._settings = {};
            }
            return normalizeSettings(prefs._settings);
        }
        // ---- AGENTS.md section library (synced) -----------------------------
        // The library of insertable sections, kept inside the settings blob so
        // it rides the existing /state sync (Control Panel path). getSections
        // returns the live, self-healed array; setSections replaces it, persists
        // through the SAME savePrefs() the Control Panel uses (local + debounced
        // push), then refreshes any open Sections panels + AGENTS.md checklists.
        function getSections() { return getSettings().sections; }
        function setSections(arr, opts) {
            const s = getSettings();
            s.sections = normalizeSectionsArray(arr);
            savePrefs();                          // localStorage + schedulePush
            refreshOpenSectionUIs(opts && opts.skipPanelFor);
        }
        // ---- per-window MCP access pins (synced) ----------------------------
        // getMcpMode returns the saved DESIRED mode for a window key, or null =
        // "no pin, inherit this broker's live default". setMcpMode records the
        // pin and persists it through the SAME savePrefs() the Control Panel
        // uses (localStorage + debounced /state push). The re-assert pass in
        // refreshTaskbarInner enforces a pin against the window's OWN broker, so
        // the choice survives refresh / another browser / a same-id reconnect.
        function getMcpMode(key) {
            const v = getSettings().mcpModes[key];
            return (v === 'off' || v === 'read' || v === 'readwrite') ? v : null;
        }
        function setMcpMode(key, mode) {
            getSettings().mcpModes[key] = mode;
            savePrefs();                          // localStorage + schedulePush
        }
        // Re-render every open agent-docs window's Sections panel + AGENTS.md
        // checklist from the current library. skipPanelWin is excluded from the
        // PANEL rebuild (the window whose textarea the user is editing right
        // now), so committing a label/body edit never clobbers their caret; its
        // checklist is still refreshed. Called after a local edit AND after a
        // remote /state adoption (_applyServerState).
        function refreshOpenSectionUIs(skipPanelWin) {
            for (const w of windows.values()) {
                if (!w || !w.tabs) continue;
                if (w._renderSectionsPanel && w !== skipPanelWin) {
                    try { w._renderSectionsPanel(); } catch (_) {}
                }
                if (w._renderTplPanel) { try { w._renderTplPanel(); } catch (_) {} }
            }
        }

