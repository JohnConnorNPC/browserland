        // ---- mod: clock (F057) --------------------------------------------
        // The reference Browserland mod (#71): the taskbar date/time chip,
        // shown whenever the mod is enabled (#102 — no Control Panel toggle).
        // The chip's lifecycle IS the mod's lifecycle: init() builds it and
        // starts the 1s tick, ctx.onUnload tears the interval down, and the ctx
        // primitives that mounted the chip remove it on disable. The per-mod
        // enable switch is the real control, so there is no synced `clock`
        // setting / Control Panel checkbox anymore. Keeps the old taskbar slot
        // (before #help-chip).
        //
        // The chip is styled with INLINE styles referencing the live theme vars
        // (the same vars the deleted #clock-chip CSS used), so the mod needs no
        // packaged .css at this phase.
        registerMod({
            id: 'clock',
            version: '1.0.0',
            ctxVersion: 1,
            tiers: ['taskbar'],   // #102: taskbar chip only (no synced setting)
            init: function (ctx) {
                // Build the chip once. align-items is harmless while hidden; the
                // display toggle (none <-> inline-flex) is what shows/hides it,
                // exactly like the old `#clock-chip` / `#clock-chip.on` rules.
                const chip = document.createElement('div');
                chip.id = 'clock-chip';
                chip.title = 'date & time';
                chip.style.cssText = [
                    'display:none',
                    'flex:0 0 auto',
                    'align-items:center',
                    'font-family:monospace',
                    'font-size:11px',
                    'padding:2px 8px',
                    'border-radius:3px',
                    'border:1px solid var(--bg-3)',
                    'background:var(--bg)',
                    'color:var(--fg-dim)',
                    'user-select:none',
                    'white-space:nowrap',
                    'margin-left:2px',
                ].join(';');
                ctx.taskbar.addStatusItem(chip);   // before #help-chip; auto-removed

                let timer = null;
                function render() {
                    try {
                        const d = new Date();
                        chip.textContent = d.toLocaleDateString() + '  '
                            + d.toLocaleTimeString();
                    } catch (_) {}
                }
                // Idempotent: exactly one chip, exactly one interval. apply(true)
                // is safe to call repeatedly (the timer guard prevents stacking).
                function apply(on) {
                    if (on) {
                        chip.style.display = 'inline-flex';
                        render();
                        if (!timer) timer = setInterval(render, 1000);
                    } else {
                        chip.style.display = 'none';
                        if (timer) { clearInterval(timer); timer = null; }
                        chip.textContent = '';
                    }
                }
                // Teardown stops the tick (the chip + checkbox are removed by the
                // ctx primitives that mounted them).
                ctx.onUnload(function () {
                    if (timer) { clearInterval(timer); timer = null; }
                });

                // Mod enabled = time shown. The chip's lifecycle is the mod's
                // lifecycle: start the 1s tick on init, stop it on unload
                // (ctx.onUnload above). No Control Panel toggle (#102).
                apply(true);
            },
        });
