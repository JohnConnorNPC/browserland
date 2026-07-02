        // ---- display settings ------------------------------------------------
        // ---- theming engine: EXTRACTED to a mod (#75) ----------------------
        // The color scheme moved to mods/theme/theme.js, which owns the palette,
        // its labels, the apply function (the six chrome CSS vars written inline
        // on documentElement) and the #set-mods radio via ctx.settings.radio —
        // byte-identical, incl. cross-browser /state sync. Core no longer reads
        // `theme`; convergence reaches the mod via notifyModSettings() at the end
        // of applyThemeSettings (below). `night` still equals the :root defaults,
        // so the visual default survives even before the mod loads.

        // ---- desktop background patterns: EXTRACTED to a mod (#76) ----------
        // The background pattern moved to mods/pattern/pattern.js, which owns the
        // PATTERNS list, its labels, the theme-var-aware applyPattern painter and
        // the #set-mods <select> via ctx.settings.select — byte-identical, incl.
        // cross-browser /state sync. Core no longer reads `pattern`; convergence
        // reaches the mod via notifyModSettings() at the end of applyThemeSettings
        // (below). applyPattern stays a hoisted global (now declared in the mod),
        // so the theme mod's theme<->pattern coupling keeps repainting it.

        // ---- terminal font: EXTRACTED to a mod (#126) ----------------------
        // The selectable terminal font (originally #18) moved to mods/termfont/
        // termfont.js, which owns the TERM_FONTS list, terminalFontFamily(), the
        // per-terminal applyTerminalFont painter and the #set-mods <select> via
        // ctx.settings.select — plus the per-terminal apply via ctx.windows.
        // onTerminalCreate (#116). Core no longer reads `termFont`; convergence
        // reaches the mod via notifyModSettings() at the end of applyThemeSettings
        // (below). Core constructs terminals with a self-contained baseline monospace
        // stack (67_js_window_lifecycle.js) and knows nothing about the font feature
        // — when the mod is off, terminals use that baseline.

        // ---- live clock (bottom-right of the taskbar) ----------------------
        // EXTRACTED to a mod (#71): the clock is now mods/clock/clock.js, which
        // owns the chip, the 1s interval, and the synced `clock` setting through
        // ctx.settings.boolean. Core no longer renders it; convergence reaches
        // the mod via notifyModSettings() at the end of applyThemeSettings.

        // ---- #40 help chip visibility: EXTRACTED to a mod (#78) ------------
        // The taskbar "?" Help chip and the whole Help window moved to mods/help/.
        // The mod creates #help-chip and shows it unconditionally while enabled
        // (#101 dropped the redundant per-setting toggle — the mod's own enable/
        // disable is the single control). Core no longer touches the chip here.

        // ---- app-icon system (#119) ---------------------------------------
        // A single source of truth for the SVG "app icons" that replaced the
        // per-app emoji. Two surfaces key off this: the (+) launch menu (each
        // window-kind's menu.iconKey -> appMenuItems -> renderMenu's icon slot)
        // and the Help window's section list (help.js resolves the SVG from the
        // corpus's per-section owner/mod id). The canonical key is the mod id
        // (control-panel is the one core built-in; clock/git/help are help-only
        // — they have no launcher but own a Help section). Icons follow the same
        // house conventions as the eyedropper/robot control glyphs above (24x24
        // viewBox, round caps/joins, aria-hidden) but carry SIGNATURE COLORS
        // (explicit fills) rather than monochrome currentColor — a deliberate
        // #119 departure so the launch menu reads as a set of app icons. Every
        // string here is HARDCODED + trusted, so the render sites may innerHTML
        // it (the labels beside them stay textContent). A key without an entry
        // returns '' so both surfaces degrade to the emoji fallback.
        const APP_ICON_SVG = {
            'editor':
                '<svg viewBox="0 0 24 24" aria-hidden="true">'
                + '<path d="M4 20l1-4L16 5l3 3L8 19l-4 1z" fill="#f7c948" stroke="#7a5c00" stroke-width="1.2" stroke-linejoin="round"/>'
                + '<path d="M14 7l3 3" stroke="#7a5c00" stroke-width="1.2"/>'
                + '<path d="M4 20l1-4 3 3-4 1z" fill="#3a3a3a"/></svg>',
            'sticky':
                '<svg viewBox="0 0 24 24" aria-hidden="true">'
                + '<path d="M4 4h16v11l-5 5H4z" fill="#f5d90a" stroke="#b39a00" stroke-width="1.2" stroke-linejoin="round"/>'
                + '<path d="M15 20v-5h5" fill="#e6c200" stroke="#b39a00" stroke-width="1.2" stroke-linejoin="round"/>'
                + '<path d="M7 9h10M7 12h7" stroke="#8a7500" stroke-width="1.4" stroke-linecap="round"/></svg>',
            'scratchpad':
                '<svg viewBox="0 0 24 24" aria-hidden="true">'
                + '<rect x="5" y="3.5" width="14" height="17" rx="2" fill="#d3f4ee" stroke="#0e6a5e" stroke-width="1.2"/>'
                + '<path d="M5 8h14" stroke="#0e6a5e" stroke-width="1.2"/>'
                + '<path d="M8 3.5v4M12 3.5v4M16 3.5v4" stroke="#0e6a5e" stroke-width="1.4" stroke-linecap="round"/>'
                + '<path d="M8 12h8M8 15h8M8 18h5" stroke="#15a58e" stroke-width="1.4" stroke-linecap="round"/></svg>',
            'file-manager':
                '<svg viewBox="0 0 24 24" aria-hidden="true">'
                + '<path d="M3 6a2 2 0 0 1 2-2h4l2 2h6a2 2 0 0 1 2 2v9a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z" fill="#4c8dff" stroke="#1c4b9c" stroke-width="1.2" stroke-linejoin="round"/>'
                + '<path d="M3 9h18" stroke="#1c4b9c" stroke-width="1.2"/></svg>',
            'task-manager':
                '<svg viewBox="0 0 24 24" aria-hidden="true">'
                + '<rect x="4" y="12" width="3.4" height="8" rx="1" fill="#2f9e5f"/>'
                + '<rect x="10.3" y="7" width="3.4" height="13" rx="1" fill="#37b06c"/>'
                + '<rect x="16.6" y="4" width="3.4" height="16" rx="1" fill="#59c98a"/>'
                + '<path d="M3 20h18" stroke="#2f7d4f" stroke-width="1.4" stroke-linecap="round"/></svg>',
            'clipboard':
                '<svg viewBox="0 0 24 24" aria-hidden="true">'
                + '<rect x="5" y="4" width="14" height="17" rx="2" fill="#eef1f8" stroke="#3a4a7a" stroke-width="1.2"/>'
                + '<rect x="9" y="2.5" width="6" height="3.5" rx="1.2" fill="#5b6bb0" stroke="#3a4a7a" stroke-width="1.2"/>'
                + '<path d="M8 11h8M8 14h8M8 17h5" stroke="#5b6bb0" stroke-width="1.4" stroke-linecap="round"/></svg>',
            'aistatus':
                '<svg viewBox="0 0 24 24" aria-hidden="true">'
                + '<circle cx="12" cy="12" r="9" fill="#e6f7ee" stroke="#1f9d57" stroke-width="1.2"/>'
                + '<path d="M5 12h3l2-4 3 8 2-4h4" fill="none" stroke="#1fb35f" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/></svg>',
            'help':
                '<svg viewBox="0 0 24 24" aria-hidden="true">'
                + '<circle cx="12" cy="12" r="9" fill="#3d7bd6" stroke="#22508f" stroke-width="1.2"/>'
                + '<path d="M9.2 9.4a2.9 2.9 0 0 1 5.6 1c0 2-2.8 2.3-2.8 4" fill="none" stroke="#fff" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/>'
                + '<circle cx="12" cy="17.2" r="1.2" fill="#fff"/></svg>',
            'control-panel':
                '<svg viewBox="0 0 24 24" aria-hidden="true">'
                + '<path d="M5 7h14M5 12h14M5 17h14" stroke="#7a8699" stroke-width="1.6" stroke-linecap="round"/>'
                + '<circle cx="9" cy="7" r="2.4" fill="#eef1f6" stroke="#48566e" stroke-width="1.4"/>'
                + '<circle cx="15" cy="12" r="2.4" fill="#eef1f6" stroke="#48566e" stroke-width="1.4"/>'
                + '<circle cx="10" cy="17" r="2.4" fill="#eef1f6" stroke="#48566e" stroke-width="1.4"/></svg>',
            'clock':
                '<svg viewBox="0 0 24 24" aria-hidden="true">'
                + '<circle cx="12" cy="12" r="9" fill="#eaf0f7" stroke="#3a5a8c" stroke-width="1.4"/>'
                + '<path d="M12 7.5v5l3.2 2" fill="none" stroke="#2f4d78" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/></svg>',
            'git':
                '<svg viewBox="0 0 24 24" aria-hidden="true">'
                + '<path d="M7 6v12M7 10a5 5 0 0 0 5 5h3" fill="none" stroke="#e8622c" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/>'
                + '<circle cx="7" cy="5" r="2.2" fill="#f26b3a" stroke="#b8461c" stroke-width="1.2"/>'
                + '<circle cx="7" cy="19" r="2.2" fill="#f26b3a" stroke="#b8461c" stroke-width="1.2"/>'
                + '<circle cx="17" cy="15" r="2.2" fill="#f26b3a" stroke="#b8461c" stroke-width="1.2"/></svg>',
        };
        // Look up an app icon by canonical key (mod id). OWN-property lookup only
        // (never an inherited member like 'constructor'/'toString'), so the return
        // is ALWAYS either '' or one of our hardcoded SVG strings — the render
        // sites can innerHTML it without a type check. Returns '' for an unknown
        // key so callers fall back to the emoji / '•' they already had.
        function appIconSvg(key) {
            return Object.prototype.hasOwnProperty.call(APP_ICON_SVG, key)
                ? APP_ICON_SVG[key] : '';
        }

        // ---- start button label --------------------------------------------
        // The `+` launch button doubles as the Win-style Start button. The
        // visible label is set here; the tooltip's discoverability hint tracks
        // #114's swapLaunchButtons so it names whichever gesture opens the menu.
        function applyStartButton() {
            try {
                const btn = document.getElementById('btn-launch');
                if (!btn) return;
                const lbl = (getSettings().startLabel || '+');
                btn.textContent = lbl;
                const swapped = !!getSettings().swapLaunchButtons;
                btn.title = lbl + (swapped
                    ? ' — new terminal (left-click for profiles, right-click launches default)'
                    : ' — new terminal (right-click for profiles)');
            } catch (_) {}
        }

        // Apply all theme/Control-Panel settings at once. Called at the end of
        // applyDisplaySettings so it converges on every (re)apply, local or
        // remote (/state pull -> _applyServerState -> applyDisplaySettings).
        // Reads settings only; never calls savePrefs, so it can't echo-loop.
        function applyThemeSettings() {
            try {
                // #75/#76: `theme` and `pattern` are mod-owned now
                // (mods/theme/, mods/pattern/). They converge via
                // notifyModSettings() below — the pattern select repaints on a
                // `pattern` change, and the theme mod repaints the (theme-var-
                // aware) pattern after it writes the chrome vars on a `theme`
                // change. Core no longer paints either directly.
                // #78/#101: the Help chip is mod-owned too (mods/help/); it is shown
                // unconditionally while the mod is enabled (no synced setting to
                // converge — #101 dropped the per-setting toggle).
                applyStartButton();
                // #126: the terminal font is mod-owned now (mods/termfont/). It
                // converges via notifyModSettings() below — a `termFont` change
                // re-fires the mod's onChange over every live terminal. Core no
                // longer applies it directly.
                // #71: fire mod-owned settings (the clock, etc.) LAST so every
                // /state pull converges them too. No-op until loadMods() runs and
                // before any toggle registers (guarded inside).
                notifyModSettings();
            } catch (_) {}
        }

        // #88: keep every open TERMINAL's × affordance (tooltip + destructive
        // hover) in step with the terminalCloseTerminates setting. Walks `windows`
        // like the termfont mod's applyTerminalFont; `!win.term` skips app windows
        // (their × is the unchanged soft-close). Called from applyDisplaySettings so it
        // converges on the local toggle AND every /state pull / boot / view
        // rebuild — matching how onCloseClick reads the same LOCAL getSettings().
        function applyTerminalCloseAffordance() {
            const term = !!getSettings().terminalCloseTerminates;
            for (const [, win] of windows) {
                if (!win || win.disposed || !win.term) continue;   // terminals only
                const b = win.dom && win.dom.querySelector('.btn-close');
                if (!b) continue;
                b.title = term ? 'terminate' : 'close';
                b.classList.toggle('btn-close-terminate', term);
            }
        }

        function applyDisplaySettings() {
            const show = getSettings().show;
            document.body.classList.toggle('hide-ids', !show.id);
            for (const key of sessions.keys()) updateTaskbarLabel(key);
            for (const [key, win] of windows) {
                if (win.disposed) continue;
                const sess = sessions.get(key) || { id: win.sid };
                const display = formatTitle(sess);
                if (display !== win.name) {
                    win.name = display;
                    try { win.titleText.textContent = display; } catch (_) {}
                }
            }
            // Theme / pattern / clock / start-label converge here too, so a
            // remote /state pull (which calls applyDisplaySettings) re-themes.
            applyThemeSettings();
            // Workspace-scrollbar option may have toggled (local change or a
            // /state adopt, which routes through here) → re-evaluate visibility.
            updateStripScrollbar();
            // #88: terminal × terminate affordance may have toggled the same way.
            applyTerminalCloseAffordance();
        }

        function hexToRgb(c) {
            const s = c.replace('#', '');
            return [
                parseInt(s.slice(0, 2), 16),
                parseInt(s.slice(2, 4), 16),
                parseInt(s.slice(4, 6), 16),
            ];
        }
        function isDarkAccent(hex) {
            // YIQ luminance; < 128 = dark background -> white foreground.
            const rgb = hexToRgb(normalizeHex(hex));
            return (rgb[0] * 299 + rgb[1] * 587 + rgb[2] * 114) / 1000 < 128;
        }
        function colorDist(a, b) {
            const ar = hexToRgb(a), br = hexToRgb(b);
            const dr = ar[0] - br[0], dg = ar[1] - br[1], db = ar[2] - br[2];
            return dr * dr + dg * dg + db * db;
        }
        function pickFarthestColor(usedColors) {
            // Greedy maximin: pick the palette entry whose nearest in-use
            // color is the farthest. Unused colors automatically win because
            // their min-distance is +Infinity. Ties resolved by palette order
            // for stability.
            if (!usedColors.size) return PALETTE[0];
            let best = PALETTE[0];
            let bestScore = -1;
            for (const c of PALETTE) {
                let nearest = Infinity;
                for (const u of usedColors) {
                    const d = colorDist(c, u);
                    if (d < nearest) nearest = d;
                }
                if (nearest > bestScore) {
                    bestScore = nearest;
                    best = c;
                }
            }
            return best;
        }
        function inUseColors(excludeId) {
            const used = new Set();
            for (const [id, win] of windows) {
                if (win.disposed) continue;
                if (excludeId !== undefined && id === excludeId) continue;
                if (win.color) used.add(normalizeHex(win.color));
            }
            // Also consider stored prefs for sessions not currently open, so
            // a closed-then-reopened pair still gets distinct colors.
            try {
                for (const id in (prefs || {})) {
                    if (id.charAt(0) === '_') continue;  // reserved keys
                    if (excludeId !== undefined && id === excludeId) continue;
                    if (windows.has(id)) continue;
                    const c = prefs[id] && prefs[id].color;
                    if (c) used.add(normalizeHex(c));
                }
            } catch (_) {}
            return used;
        }
        function defaultColor(id) {
            // Pick the palette entry farthest from every in-use color so
            // adjacent windows are visually distinguishable. Falls back to
            // the id-modulo scheme when nothing is open yet (trailing digit
            // run = the window id).
            const used = inUseColors(id);
            if (!used.size) {
                const m = String(id).match(/(\d+)$/);
                const n = m ? (Number(m[1]) | 0) : 0;
                const i = ((n % PALETTE.length) + PALETTE.length) % PALETTE.length;
                return PALETTE[i];
            }
            return pickFarthestColor(used);
        }
        function defaultGeom() {
            const desktop = document.getElementById('desktop');
            const dw = desktop.clientWidth || 1024;
            const dh = desktop.clientHeight || 700;
            const ds = defaultPixelSize();
            const w = Math.min(ds.width, Math.max(MIN_W, dw - 40));
            const h = Math.min(ds.height, Math.max(MIN_H, dh - 40));
            const slackX = Math.max(1, dw - w - 60);
            const slackY = Math.max(1, dh - h - 60);
            const left = 30 + (cascadeIndex * CASCADE_DX) % slackX;
            const top = 20 + (cascadeIndex * CASCADE_DY) % slackY;
            cascadeIndex++;
            return { left, top, width: w, height: h };
        }
        function clampGeom(g) {
            const desktop = document.getElementById('desktop');
            const dw = desktop.clientWidth || 1024;
            const dh = desktop.clientHeight || 700;
            const width = Math.max(MIN_W, Math.min(g.width | 0, dw));
            const height = Math.max(MIN_H, Math.min(g.height | 0, dh));
            const left = Math.max(0, Math.min(g.left | 0, Math.max(0, dw - 80)));
            const top = Math.max(0, Math.min(g.top | 0, Math.max(0, dh - 30)));
            return { left, top, width, height };
        }
        function normalizeHex(c) {
            if (typeof c !== 'string') return PALETTE[0];
            if (/^#[0-9a-fA-F]{6}$/.test(c)) return c.toLowerCase();
            if (/^#[0-9a-fA-F]{3}$/.test(c)) {
                const s = c.slice(1);
                return ('#' + s[0]+s[0]+s[1]+s[1]+s[2]+s[2]).toLowerCase();
            }
            return PALETTE[0];
        }
        // Sticky-note paper/fg derived from ANY accent (#29): paper is the accent
        // mixed 80% toward white, fg the accent mixed 72% toward black. By
        // construction paper is always light and fg always dark, so body text is
        // high-contrast for any color — preset, custom, or recent. (Legacy preset
        // accents keep their hand-tuned triples via noteSwatchFor.)
        function deriveNoteColors(accent) {
            const rgb = hexToRgb(normalizeHex(accent));
            const mix = (c, t, amt) => Math.round(c + (t - c) * amt);
            const toHex = (r, g, b) => '#' + [r, g, b].map(
                (v) => Math.max(0, Math.min(255, v)).toString(16)
                    .padStart(2, '0')).join('');
            const paper = toHex(mix(rgb[0], 255, 0.80), mix(rgb[1], 255, 0.80),
                                mix(rgb[2], 255, 0.80));
            const fg = toHex(mix(rgb[0], 0, 0.72), mix(rgb[1], 0, 0.72),
                             mix(rgb[2], 0, 0.72));
            return { accent: normalizeHex(accent), paper, fg };
        }

        // Unified color picker (issue 5; generalized #103). Builds a colored
        // button whose dot tracks the target's current color, opening a dropdown
        // of preset swatches. `target` is any DUCK-TYPED object exposing
        // { color (getter), disposed, cleanups } — a real window (terminals, app
        // windows) OR a lightweight host-row shim (#103, the Hosts settings pane);
        // `container` is the element the popover mounts into (a title bar, or a
        // host row). `swatches` is an array of { color, paper?, fg? };
        // `applyPick(sw)` does the target-specific recolor + persistence (prefs
        // for terminals, appStore via saveAppWindow for app windows, prefs._hosts
        // for the per-host default). Returns the button so the caller can place
        // it; outside-click / Escape dismissal and teardown are registered on
        // target.cleanups (mirrors the git popover).
        function attachColorPicker(target, container, swatches, applyPick) {
            const stopProp = (e) => e.stopPropagation();
            const btn = document.createElement('button');
            btn.type = 'button';
            btn.className = 'tb-btn color-btn';
            btn.title = 'window color';
            const dot = document.createElement('span');
            dot.className = 'color-dot';   // background:var(--accent) via CSS
            btn.appendChild(dot);

            let pop = null;
            const closePop = () => {
                if (!pop) return;
                document.removeEventListener('mousedown', onOutside, true);
                document.removeEventListener('keydown', onKey, true);
                try { pop.remove(); } catch (_) {}
                pop = null;
            };
            const onOutside = (e) => {
                if (pop && !pop.contains(e.target)
                    && e.target !== btn && e.target !== dot) closePop();
            };
            const onKey = (e) => {
                if (e.key === 'Escape') {
                    e.preventDefault(); e.stopPropagation(); closePop();
                }
            };
            // Hidden native color input for the custom-color cell (#29). One per
            // picker, reused across opens and cleaned up with the target, so it
            // never accumulates on document.body. Its change fires after the OS
            // dialog closes — possibly after closePop/target-dispose, so guard on
            // target.disposed before mutating anything.
            const colorInput = document.createElement('input');
            colorInput.type = 'color';
            colorInput.style.display = 'none';
            document.body.appendChild(colorInput);
            const onColorPick = () => {
                if (target.disposed) return;
                applyPick({ color: colorInput.value });
                closePop();
            };
            colorInput.addEventListener('change', onColorPick);
            const openPop = () => {
                if (pop) { closePop(); return; }   // toggle
                pop = document.createElement('div');
                pop.className = 'swatch-popover';
                const cur = normalizeHex(target.color);
                swatches.forEach((sw) => {
                    const cell = document.createElement('button');
                    cell.type = 'button';
                    cell.className = 'swatch-cell';
                    cell.style.background = sw.color;
                    cell.title = sw.color;
                    if (normalizeHex(sw.color) === cur) cell.classList.add('sel');
                    cell.addEventListener('mousedown', stopProp);
                    cell.addEventListener('click', (e) => {
                        e.stopPropagation();
                        applyPick(sw);     // recolor + persist (dot follows --accent)
                        closePop();
                    });
                    pop.appendChild(cell);
                });
                // Custom-color cell (#29): the 12th slot opens the native input.
                const customCell = document.createElement('button');
                customCell.type = 'button';
                customCell.className = 'swatch-cell custom';
                customCell.title = 'custom color…';
                // Eyedropper (color-picker) icon marks the custom-color slot.
                customCell.innerHTML = '<svg viewBox="0 0 24 24" fill="none" '
                    + 'stroke="currentColor" stroke-width="2.2" '
                    + 'stroke-linecap="round" stroke-linejoin="round" '
                    + 'aria-hidden="true"><path d="m2 22 1-1h3l9-9"/>'
                    + '<path d="M3 21v-3l9-9"/><path d="m15 6 3.4-3.4a2.1 2.1 0 '
                    + '1 1 3 3L18 9l.4.4a2.1 2.1 0 1 1-3 3l-3.8-3.8a2.1 2.1 0 1 '
                    + '1 3-3l.4.4Z"/></svg>';
                customCell.addEventListener('mousedown', stopProp);
                customCell.addEventListener('click', (e) => {
                    e.stopPropagation();
                    colorInput.value = normalizeHex(target.color);   // start at current
                    colorInput.click();    // OS picker; change -> applyPick (above)
                });
                pop.appendChild(customCell);
                // Recents row (#29): a full-width divider then the last-used
                // colors (global MRU). Omitted entirely when there are none yet.
                const recents = loadRecentColors();
                if (recents.length) {
                    const divider = document.createElement('div');
                    divider.className = 'swatch-divider';
                    pop.appendChild(divider);
                    recents.forEach((rc) => {
                        const cell = document.createElement('button');
                        cell.type = 'button';
                        cell.className = 'swatch-cell';
                        cell.style.background = rc;
                        cell.title = rc + ' (recent)';
                        if (normalizeHex(rc) === cur) cell.classList.add('sel');
                        cell.addEventListener('mousedown', stopProp);
                        cell.addEventListener('click', (e) => {
                            e.stopPropagation();
                            applyPick({ color: rc });
                            closePop();
                        });
                        pop.appendChild(cell);
                    });
                }
                container.appendChild(pop);
                // Right-align under the button and clamp inside the container so
                // a button near its right edge never overflows.
                const popW = pop.offsetWidth;
                const tbW = container.clientWidth;
                let left = btn.offsetLeft + btn.offsetWidth - popW;
                left = Math.max(2, Math.min(left, Math.max(2, tbW - popW - 2)));
                pop.style.top = (btn.offsetTop + btn.offsetHeight + 2) + 'px';
                pop.style.left = left + 'px';
                document.addEventListener('mousedown', onOutside, true);
                document.addEventListener('keydown', onKey, true);
            };
            const onClick = (e) => { e.stopPropagation(); openPop(); };
            btn.addEventListener('mousedown', stopProp);
            btn.addEventListener('click', onClick);
            target.cleanups.push(() => {
                btn.removeEventListener('mousedown', stopProp);
                btn.removeEventListener('click', onClick);
                colorInput.removeEventListener('change', onColorPick);
                try { colorInput.remove(); } catch (_) {}
                closePop();
            });
            return btn;
        }

        // MCP access button (#20): a robot icon next to the color swatch whose
        // dropdown sets THIS terminal's per-window MCP access mode (off / read /
        // read-write) — the same per-window pin as the title-bar context menu,
        // surfaced as a visible, at-a-glance control. The robot lights up (.on)
        // when access is read/readwrite and dims when off. Terminals only (app
        // docs aren't server sessions); mirrors attachColorPicker's popover.
        const MCP_MODES = [['off', 'Off'], ['read', 'Read'],
                           ['readwrite', 'Read-write']];
        const MCP_ROBOT_SVG =
            '<svg viewBox="0 0 24 24" aria-hidden="true">'
            + '<line x1="12" y1="2" x2="12" y2="5" stroke="currentColor" stroke-width="2" stroke-linecap="round"/>'
            + '<circle cx="12" cy="2" r="1.5" fill="currentColor"/>'
            + '<rect x="4" y="6" width="16" height="13" rx="3" fill="none" stroke="currentColor" stroke-width="2"/>'
            + '<circle cx="9" cy="12" r="1.6" fill="currentColor"/>'
            + '<circle cx="15" cy="12" r="1.6" fill="currentColor"/>'
            + '<line x1="9.5" y1="16" x2="14.5" y2="16" stroke="currentColor" stroke-width="2" stroke-linecap="round"/>'
            + '</svg>';

        function attachMcpButton(win, titleBar) {
            const stopProp = (e) => e.stopPropagation();
            const btn = document.createElement('button');
            btn.type = 'button';
            btn.className = 'tb-btn mcp-btn';
            btn.innerHTML = MCP_ROBOT_SVG;

            // This is a SECURITY indicator, so the highlight must reflect the
            // broker's ACTUAL effective mode (sess.mcp), never the desired pin —
            // a pin that the broker rejected/hasn't yet applied must not show a
            // safer state than reality. Fall back to the pin only before the
            // first poll (sess absent), and to 'off' otherwise.
            const curMode = () => {
                const s = sessions.get(win.id);
                if (s) return (s.mcp || 'off');
                return getMcpMode(win.id) || 'off';
            };
            const refresh = () => {
                const s = sessions.get(win.id);
                // A pre-MCP broker can't honour MCP at all (the re-assert pass
                // skips it too) — hide the control rather than offer a no-op.
                if (s && s.mcpKnown === false) { btn.style.display = 'none'; return; }
                btn.style.display = '';
                const m = curMode();
                btn.classList.toggle('on', m !== 'off');
                btn.classList.toggle('mcp-off', m === 'off');
                const lab = (MCP_MODES.find(([v]) => v === m) || [, 'Off'])[1];
                btn.title = 'MCP access: ' + lab;
            };
            win.refreshMcpBtn = refresh;   // kept live by refreshTaskbarInner
            win.mcpBtn = btn;              // #33: target for the activity flash

            let pop = null;
            const closePop = () => {
                if (!pop) return;
                document.removeEventListener('mousedown', onOutside, true);
                document.removeEventListener('keydown', onKey, true);
                try { pop.remove(); } catch (_) {}
                pop = null;
            };
            const onOutside = (e) => {
                if (pop && !pop.contains(e.target) && !btn.contains(e.target))
                    closePop();
            };
            const onKey = (e) => {
                if (e.key === 'Escape') {
                    e.preventDefault(); e.stopPropagation(); closePop();
                }
            };
            const openPop = () => {
                if (pop) { closePop(); return; }   // toggle
                pop = document.createElement('div');
                pop.className = 'mcp-popover';
                const head = document.createElement('div');
                head.className = 'mcp-head';
                head.textContent = 'MCP access';
                pop.appendChild(head);
                const cur = curMode();
                MCP_MODES.forEach(([val, lab]) => {
                    const opt = document.createElement('button');
                    opt.type = 'button';
                    opt.className = 'mcp-opt' + (val === cur ? ' sel' : '');
                    opt.textContent = (val === cur ? '✓ ' : ' ') + lab;
                    opt.addEventListener('mousedown', stopProp);
                    opt.addEventListener('click', (e) => {
                        e.stopPropagation();
                        setWindowMcpMode(win, val);   // persist + POST + re-assert
                        refresh();
                        closePop();
                    });
                    pop.appendChild(opt);
                });
                titleBar.appendChild(pop);
                // Right-align under the button, clamped inside the title bar.
                const popW = pop.offsetWidth;
                const tbW = titleBar.clientWidth;
                let left = btn.offsetLeft + btn.offsetWidth - popW;
                left = Math.max(2, Math.min(left, Math.max(2, tbW - popW - 2)));
                pop.style.top = (btn.offsetTop + btn.offsetHeight + 2) + 'px';
                pop.style.left = left + 'px';
                document.addEventListener('mousedown', onOutside, true);
                document.addEventListener('keydown', onKey, true);
            };
            const onClick = (e) => { e.stopPropagation(); openPop(); };
            btn.addEventListener('mousedown', stopProp);
            btn.addEventListener('click', onClick);
            win.cleanups.push(() => {
                btn.removeEventListener('mousedown', stopProp);
                btn.removeEventListener('click', onClick);
                closePop();
                win.refreshMcpBtn = null;
                win.mcpBtn = null;
                if (win._mcpFlashTimer) { clearTimeout(win._mcpFlashTimer);
                    win._mcpFlashTimer = null; }
            });
            refresh();
            return btn;
        }
        // #33: briefly flash a window's robot icon on an MCP touch — cool/soft
        // for a read, warm/sharp for a write, so observation vs mutation is
        // tellable at a glance. No-op for windows without a robot button
        // (app/non-terminal, or a pre-MCP broker where it's hidden). Re-triggers
        // cleanly on a rapid burst by dropping the class + forcing a reflow.
        function flashMcpBtn(win, kind) {
            const btn = win && win.mcpBtn;
            if (!btn) return;
            const cls = kind === 'write' ? 'mcp-flash-write' : 'mcp-flash-read';
            btn.classList.remove('mcp-flash-read', 'mcp-flash-write');
            void btn.offsetWidth;                 // reflow -> restart keyframes
            btn.classList.add(cls);
            if (win._mcpFlashTimer) clearTimeout(win._mcpFlashTimer);
            win._mcpFlashTimer = setTimeout(() => {
                win._mcpFlashTimer = null;
                if (win.mcpBtn) win.mcpBtn.classList.remove(
                    'mcp-flash-read', 'mcp-flash-write');
            }, 320);                              // > the longest keyframe (~300ms)
        }

