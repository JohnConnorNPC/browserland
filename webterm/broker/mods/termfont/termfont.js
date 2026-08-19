        // ---- mod: terminal font (#126) ------------------------------------
        // The selectable terminal-font feature (originally #18), extracted from
        // core as the last core *appearance* setting still hardwired across the
        // fragments — the same extraction theme (#75) and pattern (#76) already
        // did for sibling appearance settings. It owns the EXISTING synced
        // `termFont` key through ctx.settings.select (read-through onto the shared
        // /state blob, NOT a new key / schema field), mounts its own Control Panel
        // <select> into #set-mods (browser-global, so it hides on remote host tabs
        // exactly like the old core "Terminal font" section did), and pushes the
        // chosen CSS font stack onto every xterm terminal.
        //
        // Unlike theme/pattern, the font is applied PER TERMINAL WINDOW, so the mod
        // rides the core per-terminal-window hook ctx.windows.onTerminalCreate
        // (#116, as the git mod does): the callback is REPLAYED over every terminal
        // already open (so enabling the mod mid-session restyles them) and fires for
        // every future one — the chosen font lands on new terminals with NO core
        // involvement. Cross-browser /state sync converges through notifyModSettings()
        // at the end of core applyThemeSettings: a changed `termFont` re-fires
        // onChange -> applyTerminalFont over all live terminals; an unchanged pull is
        // a cheap no-op (applyTerminalFontToWin is change-detected per terminal).
        //
        // Ships DISABLED by default (registerMod defaultEnabled:false), like
        // aistatus/git/clipboard: enable it in Control Panel → Mods. Because core is
        // fully decoupled (it constructs terminals with its OWN baseline font and
        // knows nothing about this feature — the #120 "core keeps zero knowledge"
        // philosophy), a user with a saved `termFont` sees terminals fall back to the
        // baseline until they enable the mod. No data loss: the value persists in
        // /state and the ctx.settings.select read is non-destructive, so enabling the
        // mod restores their font on the next terminal / convergence.
        //
        // TERM_FONTS / terminalFontFamily / applyTerminalFont are (and the retired
        // TERM_FONT_DEFAULT was) moved here from core (was 65_js_display_theming.js),
        // declared at the mod's TOP LEVEL (outside init) — exactly what they were as
        // core symbols — so the "moved" extraction was a relocation.
        // They reach the core globals `windows` / `isResizable` / `refitSoon` /
        // `getSettings` the same way every mod reaches core (one shared page-script
        // scope). Side-effect-free declarations, so they are inert while the mod is
        // disabled (only init() is gated by the loader).

        // THE BASELINE IS READ, NOT COPIED (#201). This used to be
        //     const TERM_FONT_DEFAULT = '<the same literal core has>';
        // duplicating 67_js_window_lifecycle.js's TERM_FONT_BASELINE (the family
        // core constructs terminals with), with a test policing the duplication —
        // because the mod's teardown resets terminals to this string, and a drift
        // would leave a disabled mod's terminals on a different font than a fresh
        // core-only terminal. #201 shipped the baseline as READABLE ctx surface
        // (`ctx.terminals.defaults.fontFamily`, frozen, exposed from 86l BY
        // REFERENCE to that one const), so the copy is retired: there is now
        // exactly one literal and no coupling left to police. Nothing about what
        // the mod applies changed — the value is identical, it is just fetched.
        //
        // ctx is only in scope inside init(), so it is stashed on the first init
        // and read through baselineFontFamily() below. Every caller
        // (terminalFontFamily / the onChange pass / onUnload) runs after init by
        // construction, so the stash is always populated when it is read.
        let _ctx = null;
        // The fallbacks are for a LOADER SKEW, not for normal operation: a build
        // whose ctx predates ctx.terminals still has core's own const in the same
        // shared page-script scope (the route mods/recorder/recorder.js already
        // takes), and only a build with neither reaches the generic stack. None
        // of the three is a second copy of the baseline literal.
        function baselineFontFamily() {
            const t = _ctx && _ctx.terminals;
            const fam = t && t.defaults && t.defaults.fontFamily;
            if (typeof fam === 'string' && fam) return fam;
            try {
                if (typeof TERM_FONT_BASELINE === 'string' && TERM_FONT_BASELINE) {
                    return TERM_FONT_BASELINE;
                }
            } catch (_) { /* not declared on this build */ }
            return 'monospace';
        }
        // Each entry's `value` is the full CSS font-family stack (so an uninstalled
        // choice falls back to Consolas/monospace); '' = the built-in default.
        const TERM_FONTS = [
            { label: 'Default (Consolas)', value: '' },
            { label: 'Cascadia Code', value: '"Cascadia Code", "Cascadia Mono", Consolas, monospace' },
            { label: 'Fira Code', value: '"Fira Code", Consolas, monospace' },
            { label: 'JetBrains Mono', value: '"JetBrains Mono", Consolas, monospace' },
            { label: 'Source Code Pro', value: '"Source Code Pro", Consolas, monospace' },
            { label: 'Courier New', value: '"Courier New", Courier, monospace' },
            { label: 'System monospace', value: 'monospace' },
        ];
        function terminalFontFamily() {
            const f = (getSettings().termFont || '').trim();
            // Whitelist to an OFFERED stack before applying — exactly like theme's
            // applyTheme (THEMES[name] || night) and pattern's applyPattern
            // (unknown -> none). An unknown / hand-edited / version-skewed value
            // falls back to the baseline WITHOUT rewriting the synced blob (non-
            // destructive), preserving the guarantee core's dropped normalizeSettings
            // self-heal used to give. '' (the default option) also yields baseline.
            if (!f) return baselineFontFamily();
            return TERM_FONTS.some(function (o) { return o.value === f; })
                ? f : baselineFontFamily();
        }
        // Push a specific family onto ONE live terminal (no-op for app windows,
        // which have no xterm). Idempotent — a terminal whose family already matches
        // is skipped, so the convergence / replay calls are cheap when nothing
        // changed. This is core's old applyTerminalFont inner-loop body, per window.
        function applyTerminalFontToWin(win, fam) {
            if (!win || win.disposed || !win.term) return;
            if (win.term.options.fontFamily === fam) return;
            try {
                win.term.options.fontFamily = fam;
            } catch (_) { return; }   // assign failed -> don't refit
            // Re-fit only a ready, VISIBLE terminal: fitAddon.fit() on a hidden/zero-
            // size element (minimized, parked, other-workspace) clamps it to 2x1 and
            // corrupts the buffer until it's restored — and restoring already re-fits
            // it, so the new font lands then. A brand-new terminal is not yet
            // termReady, so its font is set on options here and the first ready-fit
            // measures with it (no create-time refit needed).
            if (win.termReady && isResizable(win)) {
                try { if (win.fitAddon) win.fitAddon.fit(); } catch (_) {}
                refitSoon(win);                          // tell the agent
            }
        }
        // Push the configured font onto every live terminal — the onChange target
        // (fires on a local pick AND on a cross-browser /state convergence). Verbatim
        // behavior of core's old applyTerminalFont.
        function applyTerminalFont() {
            const fam = terminalFontFamily();
            for (const [, win] of windows) applyTerminalFontToWin(win, fam);
        }

        registerMod({
            id: 'termfont',
            version: '1.0.0',
            ctxVersion: 1,
            defaultEnabled: false,   // #126: ship OFF — opt in via the Mods pane
            // settings: owns the synced `termFont` key (ctx.settings.select).
            // window: rides the per-terminal-window hook (ctx.windows.onTerminalCreate).
            tiers: ['settings', 'window'],
            init: function (ctx) {
                // Feature-detect the per-terminal-window hook (additive ctx
                // capability, #116). An older loader without ctx.windows -> the mod
                // is inert (no way to apply the font per terminal), matching how the
                // git mod feature-detects ctx.windows before using it.
                if (!ctx.windows) return;
                // Stash ctx for baselineFontFamily(): the top-level helpers are
                // core symbols by relocation and have no ctx of their own, and
                // `ctx.terminals.defaults.fontFamily` is now where the baseline
                // lives. Set BEFORE anything can read it (the select mount, the
                // onTerminalCreate replay and onUnload all follow).
                _ctx = ctx;

                // Mount the Control Panel <select> + own the synced `termFont` key.
                // The options mirror TERM_FONTS so the widget stays in lockstep with
                // applyTerminalFont. isBrowserGlobal => the section hides on remote
                // host tabs (one browser renders its own terminals), and `def: ''`
                // pins the built-in-default fallback for an empty/unknown value. The
                // select's read() is non-destructive (an unknown stored value shows
                // as the default without rewriting the blob), which is why core no
                // longer normalizes `termFont` in 55_js_settings_model.js.
                const options = TERM_FONTS.map(function (f) {
                    return { value: f.value, label: f.label };
                });
                const setting = ctx.settings.select('termFont', options, {
                    title: 'Terminal font',
                    label: 'font',
                    def: '',
                    isBrowserGlobal: true,
                    mount: 'desktop',   // #181: with the rest of the appearance
                });
                // onChange fires on a local pick AND on a cross-browser /state
                // convergence (notifyModSettings, change-detected) — restyle every
                // live terminal to the new font.
                setting.onChange(applyTerminalFont);
                // Ride the per-terminal-window hook: REPLAYED over every open
                // terminal now (so enabling the mod restyles them) and fired for
                // every future one (so a new terminal picks up the chosen font).
                // applyTerminalFontToWin is idempotent, so the replay + the onChange
                // pass never double-apply. No decorate-once guard needed.
                ctx.windows.onTerminalCreate(function (info) {
                    applyTerminalFontToWin(info.win, terminalFontFamily());
                });
                // Clean teardown: a disable() should fully reverse the mod, so reset
                // every live terminal to the core baseline font (which core
                // constructs new terminals with) + refit. The select section + its
                // listener are removed by the ctx primitive that mounted them; the
                // onTerminalCreate subscription is auto-unsubscribed by the loader
                // (rec.unloads), so no new terminals get restyled after this.
                ctx.onUnload(function () {
                    for (const [, win] of windows) {
                        applyTerminalFontToWin(win, baselineFontFamily());
                    }
                });
            },
        });
