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
        // a cheap no-op (info.setFont is change-detected per terminal, in core).
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
        // ---- #222: CORE OWNS THE APPLY, AND THE REVERT ----------------------
        // This mod used to write `win.term.options.fontFamily` itself, then
        // `win.fitAddon.fit()` + `refitSoon(win)`, and reset every live terminal
        // to the baseline from its own ctx.onUnload. #201 shipped
        // `info.setFont(family)` with a per-terminal OWNER RECORD, and named the
        // gap this closes: the owner record's guarantee held among setFont USERS
        // only, so a direct writer was invisible to it. Concretely — termfont
        // applies Fira, some other setFont user is disabled, its entry leaves the
        // stack, and the revert writes the BASELINE over termfont's live font.
        // Latent while nothing else called setFont; real the moment something
        // does. Adopting setFont is what makes the record total.
        //
        // Three consequences, all deliberate:
        //
        //  1. THE EMPTY VALUE IS A RELEASE, NOT A BASELINE REQUEST. 'Default'
        //     ('' — also what an unknown / hand-edited / version-skewed value
        //     reads back as, see below) hands setFont an empty family, which
        //     drops THIS mod's entry rather than claiming the baseline as an
        //     override. Claiming it would stomp a surviving writer, which is the
        //     very bug being fixed, one layer out.
        //  2. NO ctx.onUnload RESET LOOP. Teardown is core's: setFont arms a
        //     release on info.onModTeardown (mod disabled, terminal still open)
        //     and info.onDispose (window closed), and that release re-applies the
        //     SURVIVING WRITER if one remains, else the baseline. A mod-side loop
        //     writing the baseline would be exactly the stomp again. The unload
        //     below only drops this activation's references.
        //  3. THE REFLOW DEFERS. Core's apply is `term.options.fontFamily` then
        //     refitSoon(win) — deliberately NOT fitAddon.fit(), because core
        //     loads exactly one addon and never drives it (a checked-in test pins
        //     that). So the new grid lands through the existing resize funnel and
        //     a round trip, not in this frame. That is slower on screen and it is
        //     the correct trade: the old fit() moved the browser's grid ahead of
        //     the PTY's, and it was recorded in docs-terminal-funnels.md as a
        //     named LIMIT precisely because it resized INSIDE xterm where no core
        //     call site could see it.
        //
        // With the apply gone, so are this mod's reads of the core globals
        // `windows` / `isResizable` / `refitSoon` / `getSettings`, and its read of
        // `ctx.terminals.defaults.fontFamily` (#201's baseline surface): a mod
        // that never applies the baseline has no reason to know it. The stored
        // value now arrives through the accessor's own non-destructive read
        // (`setting.get()`), which already returns the select's `def` — '' — for
        // anything not in the offered list, so the mod does not re-implement that
        // whitelist either.

        // Each entry's `value` is the full CSS font-family stack (so an uninstalled
        // choice falls back to Consolas/monospace); '' = the built-in default,
        // which for setFont means "no override from termfont".
        const TERM_FONTS = [
            { label: 'Default (Consolas)', value: '' },
            { label: 'Cascadia Code', value: '"Cascadia Code", "Cascadia Mono", Consolas, monospace' },
            { label: 'Fira Code', value: '"Fira Code", Consolas, monospace' },
            { label: 'JetBrains Mono', value: '"JetBrains Mono", Consolas, monospace' },
            { label: 'Source Code Pro', value: '"Source Code Pro", Consolas, monospace' },
            { label: 'Courier New', value: '"Courier New", Courier, monospace' },
            { label: 'System monospace', value: 'monospace' },
        ];

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

                // Mount the Control Panel <select> + own the synced `termFont` key.
                // The options mirror TERM_FONTS so the widget stays in lockstep with
                // what applyTerminalFont pushes. isBrowserGlobal => the section hides
                // on remote host tabs (one browser renders its own terminals), and
                // `def: ''` pins the built-in-default fallback for an empty/unknown
                // value. The select's read() is non-destructive (an unknown stored
                // value shows as the default without rewriting the blob), which is
                // why core no longer normalizes `termFont` in 55_js_settings_model.js.
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

                // THE LIVE POPULATION, ACTIVATION-SCOPED. setFont lives on the
                // onTerminalCreate bag, but the onChange pass has to reach every
                // terminal at once, so the bags are kept. A Set declared HERE (not
                // at the mod's top level) is the shape that matters: a bag retains
                // the closure that retains this activation's record, so a top-level
                // map would keep a DISABLED activation's setFont alive for as long
                // as its terminal stayed open — defeating the arming that core does
                // one-shot. The replay makes the Set complete on init (every
                // already-open terminal), and onDispose prunes a closed window.
                const live = new Set();

                // What THIS mod wants on screen: the stored stack, or '' meaning
                // "no override of mine". Non-destructive — the accessor never
                // writes on a read, so an unknown value from a newer peer survives
                // in the blob while rendering as the default here.
                function terminalFontFamily() { return setting.get(); }

                // Push the configured font onto ONE terminal. Core does the write,
                // the change-detection and the refit, and records the ownership, so
                // this is a hand-off and not an apply. A throw from a stale bag is
                // swallowed: one dead terminal must not abort the pass over the
                // others.
                function applyTerminalFontTo(info) {
                    try { info.setFont(terminalFontFamily()); } catch (_) {}
                }
                // Push it onto every live terminal — the onChange target (fires on
                // a local pick AND on a cross-browser /state convergence).
                function applyTerminalFont() {
                    live.forEach(applyTerminalFontTo);
                }
                setting.onChange(applyTerminalFont);

                // Ride the per-terminal-window hook: REPLAYED over every open
                // terminal now (so enabling the mod restyles them) and fired for
                // every future one (so a new terminal picks up the chosen font).
                // setFont is change-detected in core, so the replay + the onChange
                // pass never double-apply. No decorate-once guard needed.
                ctx.windows.onTerminalCreate(function (info) {
                    // #201's members are additive on ctxVersion 1. Without them
                    // there is no supported way to style a terminal — reaching
                    // into win.term is what this migration removed — so the mod
                    // stays inert for that terminal rather than half-adopting.
                    if (!info || typeof info.setFont !== 'function') return;
                    live.add(info);
                    if (typeof info.onDispose === 'function') {
                        try {
                            info.onDispose(function () { live.delete(info); });
                        } catch (_) { /* a bag without cleanups: the Set dies with the activation */ }
                    }
                    applyTerminalFontTo(info);
                });

                // Teardown is CORE'S (see #222 note above): the release armed by
                // setFont reverts each terminal to the surviving writer, else the
                // baseline. Nothing to undo here — only references to drop, so a
                // disabled activation stops holding the bags of terminals that are
                // still open. The select section and the onTerminalCreate
                // subscription are removed by the primitives that mounted them.
                ctx.onUnload(function () { live.clear(); });
            },
        });
