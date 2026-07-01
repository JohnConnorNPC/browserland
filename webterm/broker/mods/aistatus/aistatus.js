        // ---- mod: aistatus (#112) -----------------------------------------
        // A taskbar chip + app window that monitor the major AI providers'
        // health. When a /loop or MCP call starts failing the first question is
        // "is it me or is the provider down?" — this answers it at a glance.
        //
        // Ships DISABLED (defaultEnabled:false, the #112 loader capability): the
        // mod makes NO outbound request until the operator opts in via the Mods
        // pane. That opt-in gate is why NOTHING here runs at top level except the
        // registerMod() call — every fetch/timer lives inside init(), which the
        // loader only calls once the mod is enabled (a top-level side effect would
        // defeat the default-off privacy contract; see 86_js_mod_loader.js).
        //
        // Provider data comes from the broker's GET /status/fetch proxy (the
        // broker's only outbound HTTP), allowlisted + cached server-side. The
        // client passes only enabled provider IDs; the token rides via
        // hostHttpUrl(localHost(), ...) exactly like the loader's /info probe.
        registerMod({
            id: 'aistatus',
            version: '1.0.0',
            ctxVersion: 1,
            defaultEnabled: false,   // #112: ship OFF — no egress until opted in
            tiers: ['taskbar', 'settings', 'window'],
            init: function (ctx) {
                // Mirrors the server allowlist (app.py STATUS_ALLOWLIST): id +
                // display label. Keep in sync — an id the server drops as unknown
                // simply never comes back in the /status/fetch response.
                const PROVIDERS = [
                    { id: 'anthropic', label: 'Anthropic' },
                    { id: 'openai',    label: 'OpenAI' },
                    { id: 'cohere',    label: 'Cohere' },
                    { id: 'copilot',   label: 'GitHub Copilot' },
                ];
                const INTERVALS = [
                    { value: '30',  label: '30 seconds' },
                    { value: '60',  label: '1 minute' },
                    { value: '120', label: '2 minutes' },
                    { value: '300', label: '5 minutes' },
                ];
                // Statuspage indicator -> severity rank (worst wins) + a friendly
                // label. Anything unrecognized normalizes to 'unknown' server-side.
                const RANK = { critical: 4, major: 3, minor: 2, unknown: 1, none: 0 };
                const IND_LABEL = {
                    none: 'Operational', minor: 'Minor issues',
                    major: 'Major outage', critical: 'Critical outage',
                    unknown: 'Unknown',
                };
                // Chip color bands — border + text only, background stays the theme
                // bg, so it reads on any theme exactly like the .host-chip states.
                const BANDS = {
                    green: { border: '#3a6a4a', fg: '#5fbf7f' },
                    amber: { border: '#a8842c', fg: '#e0a93a' },
                    red:   { border: '#a66',    fg: '#e96d6d' },
                    grey:  { border: 'var(--bg-3)', fg: 'var(--fg-dim)' },
                };

                // ---- live state (NOT persisted — a live monitor) ----
                let lastData = null;      // [{id,label,indicator,description,incidents,components,error?}]
                let lastCheckedAt = 0;    // epoch ms of the last completed poll
                let lastError = null;     // string when the whole poll failed
                let timer = null;
                let inFlight = false;
                const openWins = new Set();   // live aistatus windows to re-render/close

                // ---- settings (reuse the proven synced primitives, #104 clock) ----
                // One browser-global boolean per provider (default ON) + a poll
                // interval combo. Each returns {get,set,onChange}; a local toggle OR
                // a cross-browser /state convergence both land through onChange.
                const providerSetting = {};
                PROVIDERS.forEach(function (p, i) {
                    const s = ctx.settings.boolean('aistatus.' + p.id, true, {
                        // Only the first control carries the group title, so the
                        // four checkboxes read as one labeled list (not four boxes).
                        title: i === 0 ? 'AI status — monitored providers' : undefined,
                        label: p.label,
                        isBrowserGlobal: true,
                    });
                    // A provider toggle: repaint at once (drop/add is instant from
                    // the enabled set) AND re-poll so a newly-enabled provider fills.
                    s.onChange(function () { renderAll(); poll(); });
                    providerSetting[p.id] = s;
                });
                const intervalSetting = ctx.settings.combo(
                    'aistatus.interval', INTERVALS, {
                        title: 'AI status — poll interval',
                        label: 'poll interval', def: '60', isBrowserGlobal: true,
                    });
                intervalSetting.onChange(function () { restart(); });

                function enabledProviders() {
                    return PROVIDERS.filter(function (p) {
                        return providerSetting[p.id].get();
                    });
                }
                function intervalMs() {
                    const v = parseInt(intervalSetting.get(), 10);
                    return (v >= 30 ? v : 60) * 1000;
                }
                function providerRow(id) {
                    if (!lastData) return null;
                    for (const r of lastData) if (r && r.id === id) return r;
                    return null;
                }
                function indLabel(ind) {
                    return IND_LABEL[ind] || IND_LABEL.unknown;
                }

                // ---- taskbar chip ----
                const chip = document.createElement('div');
                chip.id = 'aistatus-chip';
                chip.title = 'AI provider status';
                chip.style.cssText = [
                    'flex:0 0 auto',
                    'display:inline-flex',
                    'align-items:center',
                    'gap:5px',
                    'font-family:monospace',
                    'font-size:11px',
                    'padding:2px 8px',
                    'border-radius:3px',
                    'border:1px solid var(--bg-3)',
                    'background:var(--bg)',
                    'color:var(--fg-dim)',
                    'user-select:none',
                    'white-space:nowrap',
                    'cursor:pointer',
                    'margin-left:2px',
                ].join(';');
                chip.textContent = 'AI …';
                chip.addEventListener('click', openOrFocusWindow);
                ctx.taskbar.addStatusItem(chip);   // before #help-chip; auto-removed

                // Worst indicator across ENABLED providers -> a color band.
                //   any critical            -> red
                //   any major / minor       -> amber
                //   any unknown / error     -> grey
                //   all none (or no data)   -> green / grey
                function aggregateBand() {
                    const en = enabledProviders();
                    if (!en.length || !lastData) return 'grey';
                    let worst = -1;
                    for (const p of en) {
                        const row = providerRow(p.id);
                        const ind = row ? row.indicator : 'unknown';
                        const r = (RANK[ind] != null) ? RANK[ind] : RANK.unknown;
                        if (r > worst) worst = r;
                    }
                    if (worst >= RANK.critical) return 'red';
                    if (worst >= RANK.minor) return 'amber';   // minor or major
                    if (worst >= RANK.unknown) return 'grey';  // any unknown
                    return 'green';                            // all operational
                }
                function chipTitle() {
                    const en = enabledProviders();
                    if (!en.length) return 'AI status — no providers selected';
                    const lines = ['AI provider status:'];
                    for (const p of en) {
                        const row = providerRow(p.id);
                        const ind = row ? row.indicator : 'unknown';
                        lines.push('  ' + p.label + ': ' + indLabel(ind)
                            + (row && row.description ? ' — ' + row.description : ''));
                    }
                    if (lastCheckedAt) {
                        try {
                            lines.push('checked '
                                + new Date(lastCheckedAt).toLocaleTimeString());
                        } catch (_) {}
                    }
                    if (lastError) lines.push('(last fetch failed)');
                    return lines.join('\n');
                }
                function renderChip() {
                    const band = aggregateBand();
                    const c = BANDS[band] || BANDS.grey;
                    chip.style.borderColor = c.border;
                    chip.style.color = c.fg;
                    const en = enabledProviders();
                    let txt;
                    if (!en.length) txt = 'AI —';
                    else if (!lastData) txt = 'AI …';
                    else {
                        const issues = en.filter(function (p) {
                            const row = providerRow(p.id);
                            return row && row.indicator && row.indicator !== 'none';
                        }).length;
                        txt = issues ? ('AI ⚠ ' + issues) : 'AI ✓';
                    }
                    chip.textContent = txt;
                    chip.title = chipTitle();
                }

                // ---- polling ----
                function stop() {
                    if (timer) { clearInterval(timer); timer = null; }
                }
                function start() {
                    stop();
                    timer = setInterval(function () {
                        // A tick must never throw out (unhandled rejection).
                        try { poll(); } catch (_) {}
                    }, intervalMs());
                }
                function restart() { if (timer) start(); }

                async function poll() {
                    if (inFlight) return;
                    const en = enabledProviders();
                    if (!en.length) {              // nothing selected: clear + grey
                        lastData = [];
                        lastError = null;
                        lastCheckedAt = Date.now();
                        renderAll();
                        return;
                    }
                    inFlight = true;
                    const csv = en.map(function (p) { return p.id; }).join(',');
                    try {
                        // ids are a controlled [a-z] allowlist — query-safe, no encode.
                        const url = hostHttpUrl(localHost(),
                            '/status/fetch?provider=' + csv);
                        const r = await fetch(url);
                        if (!r.ok) throw new Error('HTTP ' + r.status);
                        const j = await r.json();
                        if (!j || !j.ok || !Array.isArray(j.providers)) {
                            throw new Error('bad_response');
                        }
                        lastData = j.providers;
                        lastError = null;
                        lastCheckedAt = j.fetchedAt
                            ? j.fetchedAt * 1000 : Date.now();
                    } catch (e) {
                        // Degrade to grey — never block the UI on a failed tick.
                        lastError = String((e && e.message) || e);
                        lastData = null;
                        lastCheckedAt = Date.now();
                    } finally {
                        inFlight = false;
                    }
                    renderAll();
                }

                // ---- app window (ephemeral, like task-manager) ----
                function openAistatusWindow(appData) {
                    const id = String(appData.id);
                    const title = appData.title || 'AI status';
                    const geom = clampGeom(appData.geom
                        || appDefaultGeom('text-editor'));
                    const color = normalizeHex(appData.color || defaultColor(id));
                    const locked = appData.locked !== undefined
                        ? !!appData.locked : true;

                    const chrome = buildAppChrome({
                        id, appClass: 'app-ais', badge: '#ais',
                        geom, color, locked, title,
                    });
                    const { dom, titleText } = chrome;

                    const toolbar = document.createElement('div');
                    toolbar.className = 'app-toolbar app-ais-toolbar';
                    const refreshBtn = document.createElement('button');
                    refreshBtn.type = 'button';
                    refreshBtn.textContent = 'Refresh';
                    refreshBtn.title = 're-check all enabled providers now';
                    toolbar.appendChild(refreshBtn);
                    const checkedEl = document.createElement('span');
                    checkedEl.className = 'app-ais-checked';
                    toolbar.appendChild(checkedEl);

                    const body = document.createElement('div');
                    body.className = 'app-ais-body';

                    dom.appendChild(toolbar);
                    dom.appendChild(body);
                    addResizeHandles(dom);   // last children: hit zones on top

                    document.getElementById('desktop').appendChild(dom);
                    document.getElementById('desktop').classList.remove('empty');

                    const win = {
                        id, sid: 'ais', hostId: 'app',
                        type: 'app', appKind: 'aistatus',
                        dom, body, titleText, checkedEl,
                        term: null, fitAddon: null,
                        ws: null, wsOpen: false, termReady: false,
                        minimized: false, disposed: false,
                        geom, name: title, color,
                        resizeTimer: null, lastSentDims: null,
                        cleanups: [],
                        tiled: false,
                        floatGeom: appData.floatGeom
                            ? Object.assign({}, appData.floatGeom) : null,
                        locked, dirty: false,
                    };
                    windows.set(id, win);
                    openWins.add(win);
                    win.cleanups.push(function () { openWins.delete(win); });

                    const stopProp = (e) => e.stopPropagation();
                    const wireBtn = (btn, fn) => {
                        const onClick = (e) => { e.stopPropagation(); fn(); };
                        btn.addEventListener('mousedown', stopProp);
                        btn.addEventListener('click', onClick);
                        win.cleanups.push(function () {
                            btn.removeEventListener('mousedown', stopProp);
                            btn.removeEventListener('click', onClick);
                        });
                    };
                    wireBtn(refreshBtn, function () { poll(); });

                    // Raise / minimize / close / drag / 8-way resize / WM menu.
                    wireAppChrome(win, chrome);

                    // Manual taskbar item — app windows are never poll-managed. The
                    // synthetic kind:'app' session keeps the poll reaper off it and
                    // lets formatTitle render it (mirrors openTaskManagerWindow).
                    const appSess = { key: id, sid: 'ais', id, title,
                                      stale: false, kind: 'app', hostId: 'app' };
                    sessions.set(id, appSess);
                    const itemsHost = document.getElementById('taskbar-items');
                    if (!itemsHost.querySelector(
                            '.taskbar-item[data-session-id="'
                            + cssEscape(id) + '"]')) {
                        itemsHost.appendChild(buildTaskbarItem(appSess));
                    }
                    updateTaskbarColor(id);
                    updateTaskbarLabel(id);
                    const emptyMsg = document.getElementById('taskbar-empty');
                    if (emptyMsg) emptyMsg.remove();

                    renderWindow(win);
                    if (findKeyInLayout(id)) placeWindowTiled(win);
                    else bringToFront(id);
                    return win;
                }

                // Idempotent: rebuild the body from lastData every call. ALL
                // third-party text goes through .textContent (innerHTML only '' to
                // clear), so an upstream incident name can never inject markup.
                function renderWindow(win) {
                    if (!win || win.disposed) return;
                    if (win.checkedEl) {
                        if (lastCheckedAt) {
                            let t = '';
                            try {
                                t = new Date(lastCheckedAt).toLocaleTimeString();
                            } catch (_) {}
                            win.checkedEl.textContent = (lastError ? '⚠ ' : '')
                                + 'checked ' + t
                                + (lastError ? ' (fetch failed)' : '');
                        } else {
                            win.checkedEl.textContent = 'checking…';
                        }
                    }
                    const body = win.body;
                    body.innerHTML = '';
                    const en = enabledProviders();
                    if (!en.length) {
                        const note = document.createElement('div');
                        note.className = 'app-ais-note';
                        note.textContent = 'No providers selected — enable some in '
                            + 'the Control Panel (AI status settings).';
                        body.appendChild(note);
                        return;
                    }
                    for (const p of en) {
                        const row = providerRow(p.id);
                        const ind = row ? row.indicator : 'unknown';
                        const rowEl = document.createElement('div');
                        rowEl.className = 'app-ais-row';
                        const dot = document.createElement('span');
                        dot.className = 'ais-dot ais-'
                            + (RANK[ind] != null ? ind : 'unknown');
                        rowEl.appendChild(dot);
                        const nameEl = document.createElement('span');
                        nameEl.className = 'app-ais-name';
                        nameEl.textContent = p.label;
                        rowEl.appendChild(nameEl);
                        const descEl = document.createElement('span');
                        descEl.className = 'app-ais-desc';
                        descEl.textContent = (row && row.description)
                            ? row.description : indLabel(ind);
                        rowEl.appendChild(descEl);
                        body.appendChild(rowEl);
                        if (row && Array.isArray(row.incidents)) {
                            for (const inc of row.incidents) {
                                const incEl = document.createElement('div');
                                incEl.className = 'app-ais-incident';
                                incEl.textContent = '• ' + (inc.name || '')
                                    + (inc.impact ? ' [' + inc.impact + ']' : '');
                                body.appendChild(incEl);
                            }
                        }
                        if (row && row.error) {
                            const errEl = document.createElement('div');
                            errEl.className = 'app-ais-incident app-ais-err';
                            errEl.textContent = '• unreachable (' + row.error + ')';
                            body.appendChild(errEl);
                        }
                    }
                }

                function renderAll() {
                    renderChip();
                    for (const w of openWins) renderWindow(w);
                }

                function launchAistatus() {
                    openAppWindow({ id: newAppId('ais'), appKind: 'aistatus' });
                }
                function openOrFocusWindow() {
                    // Focus an existing window (openAppWindow dedups by id) rather
                    // than stacking a new one on every chip click.
                    for (const w of windows.values()) {
                        if (w && w.appKind === 'aistatus' && !w.disposed) {
                            openAppWindow({ id: w.id, appKind: 'aistatus' });
                            return;
                        }
                    }
                    launchAistatus();
                }

                // Register the aistatus window kind — EPHEMERAL (no serialize), like
                // task-manager. A duplicate appKind throws -> initMod rolls back.
                ctx.registerWindowKind({
                    appKind: 'aistatus',
                    factory: function (d) { return openAistatusWindow(d); },
                    menu: {
                        label: '🩺 AI status',
                        launch: function () { return launchAistatus(); },
                    },
                });
                // Teardown — registered AFTER registerWindowKind so LIFO runs it
                // FIRST: stop the timer and close any live aistatus window WHILE the
                // kind is still registered (ephemeral, so no record persists either
                // way). The chip + settings sections auto-remove via their ctx
                // primitives.
                ctx.onUnload(function () {
                    stop();
                    for (const w of Array.from(windows.values())) {
                        if (w && w.type === 'app' && w.appKind === 'aistatus') {
                            closeWindow(w.id);
                        }
                    }
                });

                // Go: paint the (grey/checking) chip, start the tick, poll now.
                renderChip();
                start();
                poll();
            },
        });
