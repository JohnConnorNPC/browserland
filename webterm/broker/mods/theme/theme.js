        // ---- mod: theme (S2 / #75) ----------------------------------------
        // The color-scheme (theme) feature, extracted from core (#75). It owns
        // the EXISTING synced `theme` setting through ctx.settings.radio (read-
        // through onto the shared /state blob, NOT a new key / schema field),
        // mounts its own Control Panel radio group into #set-mods (browser-
        // global, so it hides on remote host tabs exactly like the old #set-theme
        // section did), and applies the six chrome CSS vars on documentElement.
        // Cross-browser /state sync converges through notifyModSettings() at the
        // end of core applyThemeSettings: a changed `theme` re-fires onChange; an
        // unchanged pull is a cheap no-op (the vars are already set).
        //
        // The palette / labels / apply function are moved here VERBATIM from core
        // (was 65_js_display_theming.js): `night` reproduces the :root defaults
        // EXACTLY, so an empty blob looks identical to today and the visual
        // default survives even before this mod loads (CSS :root is night).
        //
        // PATTERN COUPLING (intentional, do NOT remove): the desktop background
        // pattern is now owned by mods/pattern/pattern.js (S3 / #76) and is theme-
        // var-aware — its applyPattern (a hoisted global declared in that mod)
        // reads the live --bg/--bg-3 this mod sets. So apply() re-runs applyPattern
        // AFTER writing the vars, byte-for-byte what the deleted #set-theme change
        // handler did (applyTheme -> applyPattern). This is the ONLY thing that
        // repaints the pattern on a theme-only change: the pattern mod's select is
        // change-detected, so it does NOT fire when just `theme` changed (local
        // pick or a cross-browser /state pull). Dropping this re-apply would leave
        // a theme change painting the pattern in stale colors. Guarded by typeof
        // so the theme mod still works if the pattern mod is absent/disabled.
        registerMod({
            id: 'theme',
            version: '1.0.0',
            ctxVersion: 1,
            tiers: ['settings'],   // #86: owns the synced `theme` key (ctx.settings.radio)
            init: function (ctx) {
                // A theme is a thin override of the six chrome CSS vars defined in
                // :root. `night` reproduces the current defaults EXACTLY so an
                // empty settings blob looks identical to today. xterm's terminal
                // theme is NOT touched here (terminals stay black).
                const THEMES = {
                    night: {   // current dark default — must match :root exactly
                        '--bg': '#1e1e1e', '--bg-2': '#2a2a2a', '--bg-3': '#3a3a3a',
                        '--fg': '#ddd', '--fg-dim': '#888', '--accent-default': '#4aa3ff',
                    },
                    day: {     // light
                        '--bg': '#e8e8e8', '--bg-2': '#d6d6d6', '--bg-3': '#b8b8b8',
                        '--fg': '#1a1a1a', '--fg-dim': '#5a5a5a', '--accent-default': '#1d6fd0',
                    },
                    redmond: { // Win95 teal/silver
                        '--bg': '#008080', '--bg-2': '#c0c0c0', '--bg-3': '#808080',
                        '--fg': '#000000', '--fg-dim': '#404040', '--accent-default': '#000080',
                    },
                    midnight: {// Midnight Blue
                        '--bg': '#0a1a33', '--bg-2': '#12274d', '--bg-3': '#1e3a6b',
                        '--fg': '#dce8ff', '--fg-dim': '#7d96c4', '--accent-default': '#4aa3ff',
                    },
                    sunday: {  // Sunday Orange
                        '--bg': '#3a1d05', '--bg-2': '#5a2f0a', '--bg-3': '#7d4413',
                        '--fg': '#ffe7cc', '--fg-dim': '#c79873', '--accent-default': '#ff9b3d',
                    },
                };
                const THEME_LABELS = {
                    night: 'Night (dark)', day: 'Day (light)',
                    redmond: 'Redmond (teal)', midnight: 'Midnight Blue',
                    sunday: 'Sunday Orange',
                };
                // Write the six vars inline on documentElement; an unknown name
                // falls back to night. Never throws.
                function applyTheme(name) {
                    try {
                        const t = THEMES[name] || THEMES.night;
                        const root = document.documentElement;
                        for (const k in t) root.style.setProperty(k, t[k]);
                    } catch (_) {}
                }
                // Set the theme vars, then re-paint the (pattern-mod-owned, theme-
                // var-aware) pattern off the fresh vars — see PATTERN COUPLING
                // above. typeof-guarded so an absent/disabled pattern mod is a
                // clean no-op rather than a (caught) ReferenceError.
                function apply(name) {
                    applyTheme(name);
                    try {
                        if (typeof applyPattern === 'function') {
                            applyPattern(getSettings().pattern);
                        }
                    } catch (_) {}
                }

                // Mount the Control Panel radio + own the synced `theme` key. The
                // options mirror the moved THEMES/THEME_LABELS so the widget stays
                // in lockstep with applyTheme. isBrowserGlobal => the section hides
                // on remote host tabs (one browser renders one theme), and `def`
                // pins the night fallback for an empty/unknown value. The radio's
                // read() is non-destructive (an unknown stored value shows as night
                // without rewriting the blob), which is why core no longer
                // normalizes `theme` in 55_js_settings_model.js.
                const options = Object.keys(THEMES).map(function (name) {
                    return { value: name, label: THEME_LABELS[name] || name };
                });
                const setting = ctx.settings.radio('theme', options, {
                    title: 'Color scheme',
                    def: 'night',
                    isBrowserGlobal: true,
                });
                // onChange fires on a local pick AND on a cross-browser /state
                // convergence (notifyModSettings, change-detected); apply once now
                // so the saved theme lands on this mod's boot.
                setting.onChange(apply);
                apply(setting.get());
            },
        });
