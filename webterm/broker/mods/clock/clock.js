        // ---- mod: clock (F057) --------------------------------------------
        // The reference Browserland mod (#71): the taskbar date/time chip,
        // shown whenever the mod is enabled (#102 — no Control Panel toggle).
        // The chip's lifecycle IS the mod's lifecycle: init() builds it and
        // starts the 1s tick, ctx.onUnload tears the interval down, and the ctx
        // primitives that mounted the chip remove it on disable. The per-mod
        // enable switch is the real control, so there is no synced on/off `clock`
        // key / Control Panel checkbox anymore. Keeps the old taskbar slot
        // (before #help-chip).
        //
        // #104 re-adds a `settings` tier — NOT the old on/off toggle, but a
        // searchable time-zone combo owning a distinct synced `clockTz` key. Empty
        // = follow the viewing browser's zone (unchanged default); a chosen IANA
        // zone pins the chip's render to that zone (browser-global, /state-synced,
        // read-through onto the shared blob — no new schema field, like `pattern`).
        //
        // The chip is styled with INLINE styles referencing the live theme vars
        // (the same vars the deleted #clock-chip CSS used), so the mod needs no
        // packaged .css at this phase.
        registerMod({
            id: 'clock',
            version: '1.0.0',
            ctxVersion: 1,
            tiers: ['taskbar', 'settings'],   // #104: chip + synced `clockTz` combo
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
                let tz = '';   // #104: '' = browser-local; else an IANA zone id
                function render() {
                    const d = new Date();
                    // Only thread a { timeZone } when a zone is pinned; otherwise
                    // render in the viewing browser's zone (undefined 2nd arg ==
                    // no options == the old bare toLocale* calls).
                    const opts = tz ? { timeZone: tz } : undefined;
                    try {
                        chip.textContent = d.toLocaleDateString(undefined, opts)
                            + '  ' + d.toLocaleTimeString(undefined, opts);
                    } catch (_) {
                        // An invalid/removed stored zone must not freeze the tick:
                        // fall back to a browser-local render (accept. criterion 4).
                        try {
                            chip.textContent = d.toLocaleDateString() + '  '
                                + d.toLocaleTimeString();
                        } catch (_) {}
                    }
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

                // #104: mount the searchable time-zone combo. Build the zone list
                // dynamically from Intl.supportedValuesOf('timeZone') (~418 IANA
                // zones) when the engine has it, else a small curated fallback so
                // the picker still works. The empty-value option is the default
                // and, as the combo's placeholder, reads as "(browser default)".
                let zones = null;
                try {
                    if (typeof Intl.supportedValuesOf === 'function') {
                        const list = Intl.supportedValuesOf('timeZone');
                        if (Array.isArray(list) && list.length) zones = list;
                    }
                } catch (_) {}
                if (!zones) {
                    zones = ['UTC', 'America/Los_Angeles', 'America/Denver',
                        'America/Chicago', 'America/New_York', 'America/Sao_Paulo',
                        'Europe/London', 'Europe/Paris', 'Europe/Moscow',
                        'Asia/Dubai', 'Asia/Kolkata', 'Asia/Shanghai', 'Asia/Tokyo',
                        'Australia/Sydney', 'Pacific/Auckland'];
                }
                // supportedValuesOf is spec'd unique, but a stray duplicate (a
                // buggy engine, a future fallback typo) would throw in
                // _normChoiceOptions and disable the whole mod (no chip). Dedup so
                // a bad zone list can never nuke the clock.
                zones = Array.from(new Set(zones));
                const tzOptions = [{ value: '', label: '(browser default)' }]
                    .concat(zones.map(function (z) {
                        return { value: z, label: z };   // IANA id as value + label
                    }));
                // Owns the synced `clockTz` key (read-through onto the shared blob,
                // like pattern owns `pattern`). def '' -> browser-local fallback.
                const setting = ctx.settings.combo('clockTz', tzOptions, {
                    title: 'Time zone',
                    label: 'time zone',
                    def: '',
                    isBrowserGlobal: true,
                });
                // Seed tz from the stored value, then repaint on every change —
                // a local pick AND a cross-browser /state convergence both land
                // through onChange (notifyModSettings, change-detected).
                tz = setting.get();
                setting.onChange(function (v) { tz = v; render(); });

                // Mod enabled = time shown. The chip's lifecycle is the mod's
                // lifecycle: start the 1s tick on init, stop it on unload
                // (ctx.onUnload above). No Control Panel toggle (#102). apply(true)
                // is last so its render() reads the seeded tz.
                apply(true);
            },
        });
