        // ---- mod: pattern (S3 / #76) --------------------------------------
        // The desktop background-pattern feature, extracted from core (#76). It
        // owns the EXISTING synced `pattern` setting through ctx.settings.select
        // (read-through onto the shared /state blob, NOT a new key / schema
        // field), mounts its own Control Panel <select> into #set-mods (browser-
        // global, so it hides on remote host tabs exactly like the old
        // #set-pattern section did), and paints the Win-3.1-style CSS background
        // images on #desktop. Cross-browser /state sync converges through
        // notifyModSettings() at the end of core applyThemeSettings: a changed
        // `pattern` re-fires onChange; an unchanged pull is a cheap no-op.
        //
        // PATTERNS / PATTERN_LABELS / applyPattern are moved here VERBATIM from
        // core (was 65_js_display_theming.js). They USED to be declared at this
        // mod's TOP LEVEL so applyPattern stayed a hoisted GLOBAL, and the theme
        // mod (mods/theme/theme.js) reached it with `typeof applyPattern ===
        // 'function'`. #199 retired that: a top-level `function` declaration
        // outlives the mod that declared it, so the probe read TRUE for a mod
        // switched off ten minutes ago and the call landed in a torn-down
        // closure. The three names now live INSIDE init, where they belong to
        // one activation, and the theme<->pattern coupling runs through the
        // #199 service seam instead:
        //
        //     ctx.provide('pattern', { apply: applyPattern });   // here
        //     const p = ctx.consume('pattern', 'pattern');       // theme
        //
        // The coupling itself is unchanged and still required: theme calls
        // apply() AFTER it writes the six chrome vars, which is the only way a
        // theme-only change repaints the pattern in the NEW colors (the
        // change-detected `pattern` select would not fire when only `theme`
        // changed). What changed is that the call now reaches a LIVE pattern
        // mod or nothing at all — no parse-order dependency, no _MODS ordering
        // requirement, and no call into a disabled mod.
        registerMod({
            id: 'pattern',
            version: '1.0.0',
            ctxVersion: 1,
            tiers: ['settings'],   // #86: owns the synced `pattern` key (ctx.settings.select)
            init: function (ctx) {
                // Theme-var-aware CSS background-images painted on #desktop,
                // BEHIND the strip + floating windows (they are positioned
                // children, so the element background sits under them). `none`
                // clears it. Never throws.
                const PATTERNS = ['none', 'weave', 'dither', 'dots', 'hatch', 'tiles'];
                const PATTERN_LABELS = {
                    none: 'None', weave: 'Weave', dither: 'Dither',
                    dots: 'Dots', hatch: 'Hatch', tiles: 'Tiles',
                };
                function applyPattern(name) {
                    try {
                        const desk = document.getElementById('desktop');
                        if (!desk) return;
                        if (PATTERNS.indexOf(name) < 0) name = 'none';
                        const cs = getComputedStyle(document.documentElement);
                        const bg = (cs.getPropertyValue('--bg') || '#1e1e1e').trim();
                        const bg3 = (cs.getPropertyValue('--bg-3') || '#3a3a3a').trim();
                        let img = '', size = '';
                        if (name === 'weave') {
                            img = 'repeating-linear-gradient(45deg,' + bg3 + ' 0 1px,'
                                + 'transparent 1px 8px),'
                                + 'repeating-linear-gradient(-45deg,' + bg3 + ' 0 1px,'
                                + 'transparent 1px 8px)';
                        } else if (name === 'dither') {
                            img = 'repeating-conic-gradient(' + bg3 + ' 0 25%,'
                                + bg + ' 0 50%)';
                            size = '6px 6px';
                        } else if (name === 'dots') {
                            img = 'radial-gradient(' + bg3 + ' 1.2px, transparent 1.4px)';
                            size = '14px 14px';
                        } else if (name === 'hatch') {
                            img = 'repeating-linear-gradient(45deg,' + bg3 + ' 0 1px,'
                                + 'transparent 1px 10px)';
                        } else if (name === 'tiles') {
                            img = 'linear-gradient(' + bg3 + ' 1px, transparent 1px),'
                                + 'linear-gradient(90deg,' + bg3 + ' 1px, transparent 1px)';
                            size = '24px 24px';
                        }
                        desk.style.backgroundImage = img;
                        desk.style.backgroundSize = size;
                    } catch (_) {}
                }
                // Publish the painter for the theme mod (#199). Auto-revoked at
                // teardown by the seam itself, so a disable() takes the service
                // down with the mod and theme's next consume() reads undefined.
                // ONLY `apply` is published: PATTERNS / PATTERN_LABELS have no
                // cross-mod reader (the Control Panel <select> below is the one
                // consumer and it is in this file), and a service surface that
                // nobody consumes is a contract nobody asked for. Guarded so a
                // build without the seam (no Proxy/Reflect/WeakMap — the CP8
                // no-half-family rule) still gets a working pattern mod.
                if (typeof ctx.provide === 'function') {
                    ctx.provide('pattern', { apply: applyPattern });
                }
                // Mount the Control Panel <select> + own the synced `pattern`
                // key. The options mirror the moved PATTERNS / PATTERN_LABELS so
                // the widget stays in lockstep with applyPattern. isBrowserGlobal
                // => the section hides on remote host tabs (one browser renders
                // one desktop), and `def: 'none'` pins the fallback for an empty/
                // unknown value. The select's read() is non-destructive (an
                // unknown stored value shows as `none` without rewriting the
                // blob), which is why core no longer normalizes `pattern` in
                // 55_js_settings_model.js.
                const options = PATTERNS.map(function (name) {
                    return { value: name, label: PATTERN_LABELS[name] || name };
                });
                const setting = ctx.settings.select('pattern', options, {
                    title: 'Background pattern',
                    label: 'pattern',
                    def: 'none',
                    isBrowserGlobal: true,
                    mount: 'desktop',   // #181: with the rest of the appearance
                });
                // onChange fires on a local pick AND on a cross-browser /state
                // convergence (notifyModSettings, change-detected); apply once
                // now so the saved pattern lands on this mod's boot.
                setting.onChange(applyPattern);
                applyPattern(setting.get());
                // Clean teardown: a disable() should fully reverse the mod, so
                // clear the desktop background the same way applyPattern('none')
                // would (the select section + its listener are removed by the
                // ctx primitive that mounted them).
                ctx.onUnload(function () {
                    try {
                        const desk = document.getElementById('desktop');
                        if (desk) {
                            desk.style.backgroundImage = '';
                            desk.style.backgroundSize = '';
                        }
                    } catch (_) {}
                });
            },
        });
