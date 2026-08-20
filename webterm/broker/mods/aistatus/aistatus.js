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
        // hostFetch(localHost(), ...) exactly like the loader's /info probe.
        registerMod({
            id: 'aistatus',
            version: '1.0.0',
            ctxVersion: 1,
            defaultEnabled: false,   // #112: ship OFF — no egress until opted in
            tiers: ['taskbar', 'settings', 'window'],
            // #194/#197, and DELIBERATE, on clipboard's precedent: the whole
            // window is built by the core factory now, so on a build without it
            // the (+) menu entry and the taskbar chip would be dead buttons and
            // the registered kind's factory would throw on every launch. A
            // `needs` BLOCKS the mod there and the Mods pane row reads
            // "blocked (needs windows.createAppWindow)", where a typeof bail
            // would leave a mod reading "active" while silently doing nothing.
            needs: ['windows.createAppWindow'],
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
                    green: { border: '#3a6a4a', fg: 'var(--ok)' },
                    amber: { border: '#a8842c', fg: 'var(--warn)' },
                    red:   { border: '#a66',    fg: 'var(--danger)' },
                    grey:  { border: 'var(--bg-3)', fg: 'var(--fg-dim)' },
                };
                // #189: the broker's own /status/fetch gate is closed. This is
                // an ANSWER, not a fetch failure and not a provider outage --
                // mirrors the update mod's "not-opted-in" wording (see
                // mods/update/update.js) so the two gated routes read the same
                // way to an operator. Named after the broker, since the broker
                // is the one that decided.
                const DISABLED_TEXT = 'status checks are switched off on this '
                    + 'broker. That is this broker\'s operator\'s choice, not a '
                    + 'fault here and not a provider problem -- it is switched '
                    + 'on from that broker\'s own desktop, or by an operator '
                    + 'setting "status_fetch_enabled" in its config.';

                // ---- live state (NOT persisted — a live monitor) ----
                let lastData = null;      // [{id,label,indicator,description,incidents,components,error?}]
                let lastCheckedAt = 0;    // epoch ms of the last completed poll
                let lastError = null;     // string when the whole poll failed
                // #189: true when the BROKER answered with its own gate closed
                // (503 status_fetch_disabled), never set for a network/parse
                // failure. Kept apart from lastError on purpose -- the two must
                // render differently, "disabled on this broker" vs "provider
                // down" -- and the poll loop below keeps ticking either way, so
                // an operator grant heals this on the very next tick, no reload.
                let lastDisabled = false;
                let timer = null;
                let inFlight = false;
                // #194/#207: no hand-kept live-window set. ctx.windows.list()
                // IS this mod's bookkeeping -- it prunes closed windows on the
                // way past, so a stale one can never be re-rendered, and the
                // cleanup that had to remove a window from a Set is gone with
                // the Set.

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
                // #119: the app-icon heartbeat leads the chip; the status text
                // rides in its own span so renderChip only rewrites the text (the
                // icon is trusted, hardcoded SVG from the APP_ICON_SVG registry).
                const chipIcon = document.createElement('span');
                chipIcon.className = 'aistatus-chip-ic';
                chipIcon.setAttribute('aria-hidden', 'true');
                chipIcon.innerHTML = appIconSvg('aistatus');
                const chipText = document.createElement('span');
                chipText.textContent = 'AI …';
                chip.appendChild(chipIcon);
                chip.appendChild(chipText);
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
                    if (lastDisabled) return 'AI status — ' + DISABLED_TEXT;
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
                    else if (lastDisabled) txt = 'AI off';
                    else if (!lastData) txt = 'AI …';
                    else {
                        const issues = en.filter(function (p) {
                            const row = providerRow(p.id);
                            return row && row.indicator && row.indicator !== 'none';
                        }).length;
                        txt = issues ? ('AI ⚠ ' + issues) : 'AI ✓';
                    }
                    chipText.textContent = txt;
                    chip.title = chipTitle();
                }

                // ---- polling ----
                function stop() {
                    if (timer) { timer.stop(); timer = null; }
                }
                function start() {
                    stop();
                    const ms = intervalMs();
                    // A tick must never throw out (unhandled rejection).
                    const tick = function () { try { poll(); } catch (_) {} };
                    // #198/#207: ctx.visibility is built into makeCtx's own
                    // object literal and a shipped mod is served in the same
                    // string as its loader, so the setInterval fallback that
                    // used to sit here was unreachable. `timer` holds a
                    // {stop}-shaped handle either way.
                    timer = ctx.visibility.pausableInterval(tick, ms);
                }
                function restart() { if (timer) start(); }

                // #189: the GUI grant the disabled note promises. One
                // named-direction write (#187's rule: the body SAYS true,
                // never implies it) to the serving broker's own consent
                // seam. Success re-polls NOW — no reload, no waiting out
                // the tick — and every refusal is shown as itself: a 409
                // policy_locked names the operator's file as the decider,
                // and nothing here is ever rendered as a provider fault.
                let grantBusy = false;
                async function grantStatusChecks(btn, statusEl) {
                    if (grantBusy) return;
                    grantBusy = true;
                    btn.disabled = true;
                    statusEl.textContent = 'asking this broker…';
                    let refusal = '';
                    try {
                        const opts = {
                            method: 'POST',
                            headers: { 'Content-Type': 'application/json' },
                            body: JSON.stringify(
                                { status_fetch_enabled: true }),
                            timeoutMs: 15000,
                        };
                        // #191: /update/policy is admin-gated on a broker
                        // that configured the class, so this write rides the
                        // page's shared admin flow when it exists (prompt,
                        // held token, one honest retry). Feature-detected:
                        // on a build without the helper — or a broker that
                        // never advertised the class — this is the same
                        // plain write it always was.
                        let r;
                        if (typeof adminGatedFetch === 'function') {
                            const info = (typeof localInfo === 'function')
                                ? await localInfo() : null;
                            const g = await adminGatedFetch(localHost(),
                                '/update/policy', opts,
                                info && info.admin,
                                'switch status checks on');
                            if (g.aborted === 'cancelled') {
                                grantBusy = false;
                                btn.disabled = false;
                                statusEl.textContent = '';
                                return;
                            }
                            r = g.res;
                        } else {
                            r = await hostFetch(localHost(),
                                '/update/policy', opts);
                        }
                        let j = null;
                        try { j = await r.json(); } catch (_) {}
                        if (r.ok && j && j.ok) {
                            refusal = '';
                        } else if (r.status === 409 && j
                                && j.error === 'policy_locked') {
                            refusal = 'this broker’s config names '
                                + '"status_fetch_enabled", so that file '
                                + 'decides — edit it and restart the '
                                + 'broker.';
                        } else {
                            refusal = 'the broker did not accept the write'
                                + (j && j.error
                                    ? ' (' + j.error + ')' : '') + '.';
                        }
                    } catch (_) {
                        refusal = 'could not reach this broker to ask.';
                    }
                    grantBusy = false;
                    if (!refusal) {
                        await poll();
                    } else {
                        btn.disabled = false;
                        statusEl.textContent = refusal;
                    }
                }

                async function poll() {
                    if (inFlight) return;
                    const en = enabledProviders();
                    if (!en.length) {              // nothing selected: clear + grey
                        lastData = [];
                        lastError = null;
                        lastDisabled = false;
                        lastCheckedAt = Date.now();
                        renderAll();
                        return;
                    }
                    inFlight = true;
                    const csv = en.map(function (p) { return p.id; }).join(',');
                    try {
                        // ids are a controlled [a-z] allowlist — query-safe, no encode.
                        // The broker fans out to every selected provider with a
                        // 4s per-provider budget and gathers them, so its own
                        // worst case is ~4s plus overhead — well past the default
                        // deadline. 10s keeps a slow-but-answering tick from
                        // being reported as a failure.
                        const r = await hostFetch(localHost(),
                            '/status/fetch?provider=' + csv,
                            { timeoutMs: 10000 });
                        // #189: the broker's own gate, not a provider or network
                        // failure -- this broker was reached and it answered.
                        // Detected off the response it already made (no extra
                        // request): a 503 carrying this exact error code. An
                        // older broker that predates the gate never sends this
                        // code (it has no 503 branch on this route at all), so
                        // it falls straight through to the ordinary paths below,
                        // byte-identical to today.
                        let gateBody = null;
                        if (r.status === 503) {
                            try { gateBody = await r.json(); } catch (_) {}
                        }
                        if (gateBody && gateBody.error === 'status_fetch_disabled') {
                            lastDisabled = true;
                            lastData = null;
                            lastError = null;
                            lastCheckedAt = Date.now();
                        } else {
                            if (!r.ok) throw new Error('HTTP ' + r.status);
                            const j = await r.json();
                            if (!j || !j.ok || !Array.isArray(j.providers)) {
                                throw new Error('bad_response');
                            }
                            lastDisabled = false;
                            lastData = j.providers;
                            lastError = null;
                            lastCheckedAt = j.fetchedAt
                                ? j.fetchedAt * 1000 : Date.now();
                        }
                    } catch (e) {
                        // Degrade to grey — never block the UI on a failed tick.
                        lastDisabled = false;
                        lastError = String((e && e.message) || e);
                        lastData = null;
                        lastCheckedAt = Date.now();
                    } finally {
                        inFlight = false;
                    }
                    renderAll();
                }

                // ---- app window (ephemeral, like task-manager) ----
                // #194 THE SCAFFOLD IS CORE'S. This used to hand-build the
                // ~30-field app-window record every windowed mod re-typed:
                // buildAppChrome, addResizeHandles, the desktop insertion and
                // its `empty` class, the `win` literal pushed into the core
                // windows Map, the synthetic kind:'app' session, the
                // hand-appended taskbar chip with its cssEscape'd
                // querySelector guard, updateTaskbarColor/Label, the
                // #taskbar-empty removal, wireAppChrome and
                // finishWindowPlacement. All of it is core-owned now and the
                // spec below is only what makes this an AI-STATUS window.
                //
                // `singleton: true` also retires openOrFocusWindow's hand-rolled
                // scan of the core windows Map -- and retires it for a case the
                // scan could not see: core restores an unknown-kind record for
                // a mod-owned kind BEFORE the mod loads (#167), under whatever
                // id the stored record carried, and the factory's dedupe is on
                // KIND over every live window rather than on an id this mod
                // chose.
                //
                // No `geom` is passed: the deleted scaffold asked for
                // appDefaultGeom('text-editor') where the factory's default is
                // appDefaultGeom(kind), and that function only special-cases
                // 'sticky-note' -- the two are the same box.
                // The el arrives classed 'app-toolbar app-ais-toolbar' -- the
                // factory derives the second from appClass, which is exactly
                // what the deleted scaffold wrote by hand.
                function buildAistatusToolbar(el, win) {
                    const refreshBtn = document.createElement('button');
                    refreshBtn.type = 'button';
                    refreshBtn.textContent = 'Refresh';
                    refreshBtn.title = 're-check all enabled providers now';
                    el.appendChild(refreshBtn);
                    const checkedEl = document.createElement('span');
                    checkedEl.className = 'app-ais-checked';
                    el.appendChild(checkedEl);
                    win.checkedEl = checkedEl;

                    // Listener bookkeeping stays the mod's: core owns the
                    // window, not what this mod hangs inside it. win.cleanups
                    // is drained by closeWindow either way.
                    const stopProp = (e) => e.stopPropagation();
                    const onClick = (e) => { e.stopPropagation(); poll(); };
                    refreshBtn.addEventListener('mousedown', stopProp);
                    refreshBtn.addEventListener('click', onClick);
                    win.cleanups.push(function () {
                        refreshBtn.removeEventListener('mousedown', stopProp);
                        refreshBtn.removeEventListener('click', onClick);
                    });
                }
                function openAistatusWindow(appData) {
                    const d = appData || {};
                    const h = ctx.windows.createAppWindow({
                        kind: 'aistatus',
                        // The stored record's own id when core hands one over,
                        // the stable id otherwise; `singleton` dedupes by kind
                        // whichever it is, so a restore adopts (#167).
                        id: (d.id != null && String(d.id)) ? String(d.id) : 'app:ais',
                        singleton: true,        // one window, however it is launched
                        title: d.title || 'AI status',
                        sid: 'ais',             // chip/session short id
                        badge: '#ais',
                        appClass: 'app-ais',
                        // NOT the factory default ('app-aistatus-body'): the
                        // shipped stylesheet matches `.app-ais .app-ais-body`.
                        bodyClass: 'app-ais-body',
                        // Absent fields keep the factory's defaults, which are
                        // the deleted scaffold's own: clampGeom(appDefaultGeom),
                        // normalizeHex(defaultColor(id)) and locked:true.
                        geom: d.geom,
                        color: d.color,
                        locked: d.locked,
                        floatGeom: d.floatGeom,
                        toolbar: buildAistatusToolbar,
                        body: function (el, win) { renderWindow(win); },
                    });
                    // THE TRAP: openAppWindow hands a registered kind's factory
                    // return value straight back to its callers, and they want a
                    // window RECORD. Return h.win, never the handle.
                    return h.win;
                }

                // Idempotent: rebuild the body from lastData every call. ALL
                // third-party text goes through .textContent (innerHTML only '' to
                // clear), so an upstream incident name can never inject markup.
                function renderWindow(win) {
                    if (!win || win.disposed) return;
                    if (win.checkedEl) {
                        if (lastDisabled) {
                            win.checkedEl.textContent = 'disabled here';
                        } else if (lastCheckedAt) {
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
                    if (lastDisabled) {
                        // #189: the broker said no, honestly — never rendered as
                        // four dead providers. providerRow()/lastData stay null
                        // here on purpose so this branch cannot fall through into
                        // the per-provider loop below.
                        const note = document.createElement('div');
                        note.className = 'app-ais-note';
                        note.textContent = DISABLED_TEXT.charAt(0).toUpperCase()
                            + DISABLED_TEXT.slice(1);
                        body.appendChild(note);
                        // The switch that sentence promises (#189's GUI grant
                        // path): one named-direction write to this broker's
                        // own consent seam, right here where the refusal is.
                        const row = document.createElement('div');
                        row.className = 'app-ais-note';
                        const btn = document.createElement('button');
                        btn.type = 'button';
                        btn.textContent =
                            'Switch on status checks on this broker';
                        const st = document.createElement('span');
                        st.className = 'app-ais-grant-note';
                        st.style.marginLeft = '8px';
                        btn.addEventListener('click', function () {
                            grantStatusChecks(btn, st);
                        });
                        row.appendChild(btn);
                        row.appendChild(st);
                        body.appendChild(row);
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
                    for (const h of ctx.windows.list()) renderWindow(h.win);
                }

                function launchAistatus() {
                    openAppWindow({ id: newAppId('ais'), appKind: 'aistatus' });
                }
                // #194: no scan of the core windows Map any more. Either route
                // reaches the factory, whose `singleton` dedupe focuses the
                // live window instead of building a second one -- so the chip
                // and the (+) entry can share one launcher.
                function openOrFocusWindow() { launchAistatus(); }

                // Register the aistatus window kind — EPHEMERAL (no serialize), like
                // task-manager. A duplicate appKind throws -> initMod rolls back.
                ctx.registerWindowKind({
                    appKind: 'aistatus',
                    factory: function (d) { return openAistatusWindow(d); },
                    menu: {
                        label: 'AI status',
                        iconKey: 'aistatus',   // #119: SVG heartbeat pulse in the (+) menu
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
                    // #194: closeAll() replaces the loop that iterated CORE state
                    // to find this mod's own windows -- it walks the owned
                    // registry newest-first and is safe against the live Map
                    // mutating underneath it. Still registered AFTER
                    // registerWindowKind so LIFO closes the windows while the
                    // kind is still registered (ephemeral either way).
                    ctx.windows.closeAll();
                });

                // Go: paint the (grey/checking) chip, start the tick, poll now.
                renderChip();
                start();
                poll();
            },
        });
