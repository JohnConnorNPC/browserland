        // ---- display settings ------------------------------------------------
        // ---- theming engine: EXTRACTED to a mod (#75) ----------------------
        // The color scheme moved to mods/theme/theme.js, which owns the palette,
        // its labels, the apply function (the six chrome CSS vars written inline
        // on documentElement) and the #set-mods radio via ctx.settings.radio —
        // byte-identical, incl. cross-browser /state sync. Core no longer reads
        // `theme`; convergence reaches the mod via notifyModSettings() at the end
        // of applyThemeSettings (below). `night` still equals the :root defaults,
        // so the visual default survives even before the mod loads.

        // ---- desktop background patterns (Win 3.1 style) -------------------
        // Theme-var-aware CSS background-images painted on #desktop, BEHIND the
        // strip + floating windows (they are positioned children, so the
        // element background sits under them). `none` clears it. Never throws.
        const PATTERNS = ['none', 'weave', 'dither', 'dots', 'hatch', 'tiles'];
        // Issue #18: selectable terminal font. Each entry's `value` is the full
        // CSS font-family stack (so an uninstalled choice falls back to Consolas/
        // monospace); '' = the built-in default. Browser-global (xterm renders
        // client-side), persisted as getSettings().termFont and synced via /state
        // like theme/pattern. TERM_FONT_DEFAULT mirrors the Terminal() default.
        const TERM_FONT_DEFAULT = 'Consolas, "Liberation Mono", monospace';
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
            return f || TERM_FONT_DEFAULT;
        }
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

        // ---- live clock (bottom-right of the taskbar) ----------------------
        // EXTRACTED to a mod (#71): the clock is now mods/clock/clock.js, which
        // owns the chip, the 1s interval, and the synced `clock` setting through
        // ctx.settings.boolean. Core no longer renders it; convergence reaches
        // the mod via notifyModSettings() at the end of applyThemeSettings.

        // ---- #40 help chip visibility --------------------------------------
        // The taskbar "?" chip; same show/hide-on-an-`.on`-class pattern as the
        // clock, driven by getSettings().showHelpButton. Reads only — converges
        // on boot and on every /state pull via applyThemeSettings.
        function applyHelpButton(on) {
            try {
                const el = document.getElementById('help-chip');
                if (el) el.classList.toggle('on', !!on);
            } catch (_) {}
        }

        // ---- start button label --------------------------------------------
        // The `+` launch button doubles as the Win-style Start button. Only the
        // visible label changes; its click/contextmenu behavior is untouched.
        function applyStartButton() {
            try {
                const btn = document.getElementById('btn-launch');
                if (!btn) return;
                const lbl = (getSettings().startLabel || '+');
                btn.textContent = lbl;
                btn.title = lbl + ' — new terminal (right-click for profiles)';
            } catch (_) {}
        }

        // Apply all theme/Control-Panel settings at once. Called at the end of
        // applyDisplaySettings so it converges on every (re)apply, local or
        // remote (/state pull -> _applyServerState -> applyDisplaySettings).
        // Reads settings only; never calls savePrefs, so it can't echo-loop.
        function applyThemeSettings() {
            try {
                const s = getSettings();
                // #75: `theme` is mod-owned now (mods/theme/theme.js); it writes
                // the chrome vars via notifyModSettings() below. Pattern stays
                // core (S3) and is theme-var-aware, so it still applies here.
                applyPattern(s.pattern);    // reads the live theme vars
                applyHelpButton(s.showHelpButton);   // #40
                applyStartButton();
                applyTerminalFont();        // #18: configurable terminal font
                // #71: fire mod-owned settings (the clock, etc.) LAST so every
                // /state pull converges them too. No-op until loadMods() runs and
                // before any toggle registers (guarded inside).
                notifyModSettings();
            } catch (_) {}
        }

        // Issue #18: push the configured terminal font onto every live terminal
        // (no-op for app windows, which have no xterm). Idempotent — only the
        // terminals whose family actually differs are re-fit (local xterm resize
        // + an agent resize), so the convergence call in applyThemeSettings (run
        // on every settings change / /state pull) is cheap when nothing changed.
        function applyTerminalFont() {
            const fam = terminalFontFamily();
            for (const [, win] of windows) {
                if (!win || win.disposed || !win.term) continue;
                if (win.term.options.fontFamily === fam) continue;
                try {
                    win.term.options.fontFamily = fam;
                } catch (_) { continue; }   // assign failed -> don't refit
                // Re-fit only a ready, VISIBLE terminal: fitAddon.fit() on a
                // hidden/zero-size element (minimized, parked, other-workspace)
                // clamps it to 2x1 and corrupts the buffer until it's restored —
                // and restoring already re-fits it, so the new font lands then.
                if (win.termReady && isResizable(win)) {
                    try { if (win.fitAddon) win.fitAddon.fit(); } catch (_) {}
                    refitSoon(win);                          // tell the agent
                }
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

        // Unified window-color picker (issue 5). Builds the title-bar control
        // shared by terminals AND app windows: a colored button whose dot
        // tracks --accent, opening a dropdown of preset swatches. `swatches` is
        // an array of { color, paper?, fg? }; `applyPick(sw)` does the
        // window-type-specific recolor + persistence (prefs for terminals,
        // appStore via saveAppWindow for app windows). Returns the button so the
        // caller can place it in the title bar; outside-click / Escape dismissal
        // and teardown are registered on win.cleanups (mirrors the git popover).
        function attachColorPicker(win, titleBar, swatches, applyPick) {
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
            // picker, reused across opens and cleaned up with the window, so it
            // never accumulates on document.body. Its change fires after the OS
            // dialog closes — possibly after closePop/window-close, so guard on
            // win.disposed before mutating anything.
            const colorInput = document.createElement('input');
            colorInput.type = 'color';
            colorInput.style.display = 'none';
            document.body.appendChild(colorInput);
            const onColorPick = () => {
                if (win.disposed) return;
                applyPick({ color: colorInput.value });
                closePop();
            };
            colorInput.addEventListener('change', onColorPick);
            const openPop = () => {
                if (pop) { closePop(); return; }   // toggle
                pop = document.createElement('div');
                pop.className = 'swatch-popover';
                const cur = normalizeHex(win.color);
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
                    colorInput.value = normalizeHex(win.color);   // start at current
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
                titleBar.appendChild(pop);
                // Right-align under the button and clamp inside the title bar so
                // a button near the window's right edge never overflows.
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

