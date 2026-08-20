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
        // (was 65_js_display_theming.js), with one exception that #223 fixed.
        //
        // `night` USED TO BE A SIXTH COPY of the :root defaults -- the same six
        // hexes as 10_css_root.css, kept equal by nothing but care, so an edit
        // to either one silently made "the default theme" and "what the page
        // looks like before this mod loads" two different things. The audit
        // asked for a readable core default to fold into the theme API; the
        // cheaper and stronger answer is that CSS ALREADY HAS ONE. `night` is
        // now the ABSENCE of an override: applying it REMOVES the six inline
        // properties and lets :root show through, so ":root is night" stops
        // being a byte-match between two files and becomes the cascade doing
        // what it is for. There is exactly one copy of those hexes in the tree
        // and it is the stylesheet.
        //
        // Two things fall out of that, both wanted:
        //   - the mod's boot on a night/empty blob is now a no-op rather than a
        //     write of the values that were already computing;
        //   - TEARDOWN IS THE SAME CODE PATH. #223's other half: nothing
        //     reverted the vars, so disabling the mod left its palette on the
        //     page for the rest of the session (a Control Panel toggle that
        //     visibly does nothing). Unloading now removes the six properties,
        //     which IS "apply night", which IS what the page renders with no
        //     theme mod at all.
        //
        // PATTERN COUPLING (intentional, do NOT remove): the desktop background
        // pattern is owned by mods/pattern/pattern.js (S3 / #76) and is theme-
        // var-aware — its painter reads the live --bg/--bg-3 this mod sets. So
        // apply() re-paints the pattern AFTER writing the vars, byte-for-byte what
        // the deleted #set-theme change handler did (applyTheme -> applyPattern).
        // This is the ONLY thing that repaints the pattern on a theme-only change:
        // the pattern mod's select is change-detected, so it does NOT fire when
        // just `theme` changed (local pick or a cross-browser /state pull).
        // Dropping this re-apply would leave a theme change painting the pattern
        // in stale colors.
        //
        // #199 MIGRATED HOW IT IS REACHED. It used to be
        // `typeof applyPattern === 'function'` against a hoisted global. That
        // probe answered one question — "did some fragment declaring that name
        // evaluate?" — and a top-level `function` declaration outlives the mod
        // that declared it, so it read TRUE for a pattern mod that had been
        // switched off, and the call landed in a torn-down closure. It is now
        // ctx.consume('pattern', 'pattern'), which is undefined unless the
        // pattern mod is ACTIVE right now. USER-VISIBLE CONSEQUENCE, stated
        // rather than slipped in: with the pattern mod DISABLED and a pattern
        // still selected in the settings blob, a theme change no longer repaints
        // the desktop pattern. That is correct — a disabled mod's teardown has
        // already cleared the desktop background — where the old probe would
        // have re-painted it from a dead mod's closure.
        //
        // NO `needs: ['consume']`: `needs` BLOCKS a mod on a build that lacks the
        // capability, and a theme mod that sets six CSS vars but cannot repaint
        // somebody else's pattern is still a working theme mod. The soft
        // feature-detect below is the honest shape for an optional coupling; a
        // hard declaration would trade a missing repaint for no theme at all.
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
                //
                // `night` is null, not an object: it is the DEFAULT, and the
                // default is what :root already paints (see the header). Every
                // other entry is a real override of the same six names, so the
                // set of names lives in ONE place -- VARS below -- rather than
                // being re-listed per palette for a removal to walk.
                const VARS = ['--bg', '--bg-2', '--bg-3', '--fg', '--fg-dim',
                    '--accent-default'];
                const THEMES = {
                    night: null,   // the :root default, applied by REMOVING the overrides
                    day: {     // light
                        '--bg': '#e8e8e8', '--bg-2': '#d6d6d6', '--bg-3': '#b8b8b8',
                        '--fg': '#1a1a1a', '--fg-dim': '#5a5a5a', '--accent-default': '#1d6fd0',
                    },
                    redmond: { // early-90s desktop: teal ground, silver chrome
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
                // Write the six vars inline on documentElement, or REMOVE them
                // for night / an unknown name so :root's own defaults show
                // through. Never throws.
                //
                // `Object.prototype.hasOwnProperty.call` and not `THEMES[name]`:
                // a stored theme literally named 'constructor' or 'toString'
                // reads a FUNCTION off the prototype chain, which is truthy, and
                // the loop below would then walk a function's enumerable
                // properties instead of falling back to the default. The same
                // prototype-bearing-table trap #203 hit in _normChoiceOptions.
                function applyTheme(name) {
                    try {
                        const known = typeof name === 'string'
                            && Object.prototype.hasOwnProperty.call(THEMES, name);
                        const t = known ? THEMES[name] : null;
                        const root = document.documentElement;
                        if (!t) {
                            // night, unknown, or teardown: back to :root.
                            for (const k of VARS) root.style.removeProperty(k);
                            return;
                        }
                        for (const k in t) root.style.setProperty(k, t[k]);
                    } catch (_) {}
                }
                // Set the theme vars, then re-paint the (pattern-mod-owned, theme-
                // var-aware) pattern off the fresh vars — see PATTERN COUPLING
                // above. Consumed PER USE, which is #199's recommended pattern
                // and the reason nothing here has to be revalidated: every call
                // asks the loader's active-mod map afresh, so an absent, not-yet-
                // loaded, disabled or mid-teardown pattern mod is undefined and
                // this is a clean no-op.
                //
                // THE try/catch STAYS, and it is not the old ReferenceError
                // guard. It covers what the seam does NOT: `ctx.consume` is
                // absent entirely on a build without Proxy/Reflect/WeakMap (the
                // no-half-family rule), where calling it would be a TypeError,
                // and `getSettings()` is core's, evaluated in this frame before
                // the proxy is ever touched. The proxy isolates only throws from
                // INSIDE the provider's member.
                function apply(name) {
                    applyTheme(name);
                    try {
                        const pattern = (typeof ctx.consume === 'function')
                            ? ctx.consume('pattern', 'pattern') : null;
                        if (pattern) pattern.apply(getSettings().pattern);
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
                    // #181: sits with the rest of the desktop's appearance in the
                    // Control Panel's Desktop applet rather than in the shared
                    // Mods bucket. A HINT against a core-owned closed set -- an
                    // unknown id degrades to Mods, it never mints an applet.
                    mount: 'desktop',
                });
                // onChange fires on a local pick AND on a cross-browser /state
                // convergence (notifyModSettings, change-detected); apply once now
                // so the saved theme lands on this mod's boot.
                setting.onChange(apply);
                apply(setting.get());

                // #223: A DISABLE MUST REVERSE THE MOD. Until now nothing
                // reverted the vars, so switching the mod off in Control Panel
                // left its palette on the page until the next reload -- and on
                // a non-night theme that is the whole visible effect of the mod
                // persisting after the mod is gone.
                //
                // apply(null) rather than a bespoke removal loop: it takes the
                // same not-a-known-name branch that night takes, so the revert
                // path IS the set path and cannot drift from it. It also
                // repaints the pattern off the restored vars through the same
                // #199 seam -- without that, disabling theme would leave the
                // desktop pattern drawn in the palette that just went away.
                // (The pattern mod is a DIFFERENT activation, so the loader's
                // `unloading` flag on this record does not block consuming it.)
                ctx.onUnload(function () { apply(null); });
            },
        });
