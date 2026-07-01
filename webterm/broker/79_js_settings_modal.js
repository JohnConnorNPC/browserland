        // ---- settings modal -------------------------------------------------
        // Instant-persist on change, matching the rest of the UI — there is
        // no OK button. Tabbed (tasks 13/14): the same fixed-ID host form is
        // re-used for every host tab via the settingsTarget indirection.
        const settingsOverlay = document.getElementById('settings-overlay');
        const settingsTabsEl = document.getElementById('set-tabs');
        const setPaneHost = document.getElementById('set-pane-host');
        const setPaneBrowser = document.getElementById('set-pane-browser');
        const setHostLoadingEl = document.getElementById('set-host-loading');
        const setColsEl = document.getElementById('set-cols');
        const setRowsEl = document.getElementById('set-rows');
        // #123: the id/pid/host/title toggles + order are rendered by
        // renderLabelOrder() into #set-label-order (resolved by id), so the old
        // fixed setShowId/Pid/Host checkbox refs are gone.
        const setTiling = document.getElementById('set-tiling');
        const setStripScrollbar = document.getElementById('set-strip-scrollbar');
        const setCloseTerminates = document.getElementById('set-close-terminates');   // #88
        const setConfirmTerminate = document.getElementById('set-confirm-terminate'); // #88
        const setSnapHold = document.getElementById('set-snap-hold');   // #38 dwell delay (ms)
        const setSlideMs = document.getElementById('set-slide-ms');     // #125 slide duration (ms)
        const setRestore = document.getElementById('set-restore-refresh');
        const setHideOtherWs = document.getElementById('set-taskbar-hide-other-ws');
        const setDefaultProfile = document.getElementById('set-default-profile');
        const setKeybindingsEl = document.getElementById('set-keybindings');
        // Appearance controls (browser-local; bound to the LIVE local
        // getSettings(), like restore-on-refresh — NOT settingsTarget). The font
        // <option>s are injected from the shared TERM_FONTS constant so the modal
        // stays in sync with applyThemeSettings() at boot / on /state pull.
        // #75/#76: the color-scheme radio and the background-pattern select are no
        // longer fixed core controls — mods/theme/theme.js and mods/pattern/
        // pattern.js mount them into #set-mods via ctx.settings.radio /
        // ctx.settings.select (like the clock checkbox below).
        // #71: the clock's "Show date & time" checkbox is no longer a fixed core
        // control — the clock mod mounts it into #set-mods via ctx.settings.
        // #78: the Help button "show ? chip" checkbox moved the same way (the help
        // mod mounts it into #set-mods via ctx.settings.boolean).
        const setStartLabelEl = document.getElementById('set-start-label');
        const setSwapLaunchEl = document.getElementById('set-swap-launch');   // #114
        const setStartPathEl = document.getElementById('set-start-path');
        const setTermFontEl = document.getElementById('set-term-font');   // #18
        for (const f of TERM_FONTS) {           // #18: terminal font choices
            const opt = document.createElement('option');
            opt.value = f.value;
            opt.textContent = f.label;
            setTermFontEl.appendChild(opt);
        }
        // MCP access section (per-broker; NOT part of the synced /state blob —
        // fetched/saved via /mcp/config on the settings-target host, mirroring
        // renderDefaultProfile's per-host pattern). The token is a secret, so it
        // only ever travels to an already-authenticated browser.
        const setMcpEnabled = document.getElementById('set-mcp-enabled');
        const setMcpToken = document.getElementById('set-mcp-token');
        const setMcpGenerate = document.getElementById('set-mcp-generate');
        const setMcpDefaultMode = document.getElementById('set-mcp-default-mode');
        const setMcpAllowLaunch = document.getElementById('set-mcp-allow-launch');
        const setMcpUrlEl = document.getElementById('set-mcp-url');
        const mcpConfigCache = new Map();     // hostId -> {enabled,token,default_mode,allow_launch}
        const mcpConfigFetching = new Set();  // hostIds with an in-flight GET
        // Launch-profile editor (#70; per-host, same posture as MCP). The cache
        // holds the FULL /profiles/config objects (command/title/cwd + exists),
        // browser-realm-only — /profiles stays names-only.
        const setProfilesListEl = document.getElementById('set-profiles-list');
        const setProfileAddBtn = document.getElementById('set-profile-add');
        const setProfileDetectBtn = document.getElementById('set-profile-detect');
        const profilesConfigCache = new Map();     // hostId -> /profiles/config
        const profilesConfigFetching = new Set();  // hostIds with an in-flight GET

        // settingsTarget = the host whose settings the host-tab form edits.
        //   {hostId, isLocal, s (the settings object), save()}.
        // LOCAL: s is the live getSettings() and save() runs savePrefs() — the
        // change handlers ALSO apply live local effects (display/size/tiling).
        // REMOTE: s is hostStateCache.get(hostId).settings and
        // save() PUTs that broker's /state — NO local effects (those settings
        // only govern that broker's own viewers).
        let settingsTarget = null;
        let currentSettingsTab = 'local';   // hostId or 'browser'

        function makeLocalTarget() {
            // `s` is a getter so it always resolves the LIVE getSettings()
            // object — if _applyServerState replaces prefs._settings while the
            // modal is open, local edits still land on (and push) the current
            // object, matching the pre-tabs getSettings()-per-handler behavior.
            return { hostId: 'local', isLocal: true, save: savePrefs,
                     get s() { return getSettings(); } };
        }
        function makeRemoteTarget(hostId, entry) {
            // Capture `entry` in the save closure so the PUT carries the exact
            // object the form edited, even if a prefetch swaps hostStateCache.
            return { hostId: hostId, isLocal: false, s: entry.settings,
                     save: () => putHostState(hostId, entry) };
        }

        function openSettings() {
            // The Control Panel is a moveable floating window now (#59); open or
            // focus it. Kept as the shared "open settings" entry point.
            return openControlPanelWindow({});
        }
        // Flush in-progress settings edits: a debounced start-path edit (#17), the
        // remote-prefetch pause, and any keybinding capture. Shared by the global
        // Escape handler and the Control Panel window's teardown.
        function flushSettingsEdits() {
            if (_startPathTimer) commitStartPath();
            settingsOpenHostId = null;   // resume background prefetch for remotes
            _kbRecording = null;         // abandon any in-progress capture
        }
        function closeSettings() {
            // The modal presentation was replaced by a floating window (#59). This
            // is now just the shared edit-flush invoked by the global Escape key
            // and any legacy caller — it does NOT close the window itself (that is
            // closeControlPanelWindow). Removing a (never-set) .open class is a
            // harmless no-op kept so the overlay can't get stuck visible.
            flushSettingsEdits();
            settingsOverlay.classList.remove('open');
        }
        settingsOverlay.addEventListener('mousedown', (e) => {
            // The overlay is no longer shown as a modal; guard the legacy
            // click-outside-to-close so a stray event can't act while it's hidden.
            if (settingsOverlay.classList.contains('open')
                && e.target === settingsOverlay) closeSettings();
        });
        document.getElementById('settings-close')
            .addEventListener('click', () => closeControlPanelWindow());

