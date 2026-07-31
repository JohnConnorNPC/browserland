        // ---- mod: update (#182) -------------------------------------------
        // Is the build you are looking at current with upstream? Finding that
        // out otherwise means opening a shell, cd-ing to the checkout and
        // running git fetch.
        //
        // Ships DISABLED (defaultEnabled:false) like aistatus, and for the same
        // reason: nothing here runs at top level except registerMod(), because
        // a top-level side effect would defeat the default-off contract. But
        // note the mod's switch is NOT what protects the network — the broker
        // has its own `update_check_enabled` gate, and answers 503 until an
        // operator sets it. Enabling this mod on a broker that never opted in
        // gets you an honest "checking is disabled here", not a silent egress.
        //
        // The single rule this mod exists to keep: NEVER claim "up to date"
        // when the truth is "I could not check". Every failure lands on
        // `unknown` carrying a reason, and the window shows the reason. A
        // version checker that guesses is worse than no version checker,
        // because it is trusted.
        registerMod({
            id: 'update',
            version: '1.0.0',
            ctxVersion: 1,
            defaultEnabled: false,   // #182: ship OFF — no egress until opted in
            tiers: ['taskbar', 'settings', 'window'],
            init: function (ctx) {
                // How often the BROWSER asks. The broker caches for a day, so
                // most of these are cache hits that never leave the machine;
                // this interval only governs how quickly a tab notices that the
                // broker's own cache turned over.
                const POLL_MS = 30 * 60 * 1000;

                // Chip colour bands — border + text only, background stays the
                // theme bg, so it reads on any theme exactly like .host-chip.
                const BANDS = {
                    green: { border: '#3a6a4a', fg: 'var(--ok)' },
                    amber: { border: '#a8842c', fg: 'var(--warn)' },
                    grey:  { border: 'var(--bg-3)', fg: 'var(--fg-dim)' },
                };
                // Why we could not answer, in words. The broker sends a stable
                // machine reason; anything unrecognised falls through to a
                // generic line rather than rendering a raw token at the user.
                const REASONS = {
                    'no-git': 'this install carries no git commit, so there is '
                        + 'nothing to compare — a build id alone cannot tell '
                        + 'two different builds of the same version apart',
                    'compare-unavailable': 'GitHub could not compare these '
                        + 'commits. That happens when the commit was never '
                        + 'pushed, was garbage-collected, or the branch was '
                        + 'force-pushed — it does NOT by itself mean you are '
                        + 'ahead',
                    'rate-limited': 'GitHub’s rate limit is exhausted for '
                        + 'this address. The check will resume by itself',
                    'offline': 'could not reach GitHub',
                    'bad-response': 'GitHub’s answer was not in the '
                        + 'expected form',
                    'too-large': 'the comparison was too large to read',
                    'disabled': 'update checking is switched off on this '
                        + 'broker. An operator enables it with '
                        + '"update_check_enabled" in the broker config',
                };

                // ---- live state (NOT persisted — it is a live check) ----
                let last = null;        // the broker's check payload
                let lastError = null;   // string when the request itself failed
                let checkedAt = 0;      // epoch ms of the last completed poll
                let timer = null;
                let inFlight = false;
                const openWins = new Set();

                // The issue's own objection to a permanent taskbar chip: this
                // is consulted twice a month, not continuously. So by default
                // the chip only appears when there is something to say.
                const quietSetting = ctx.settings.boolean(
                    'update.hideWhenCurrent', true, {
                        title: 'Update check',
                        label: 'hide the chip while the build is current',
                        isBrowserGlobal: true,
                    });
                quietSetting.onChange(function () { renderChip(); });

                // ---- taskbar chip ----
                const chip = document.createElement('div');
                chip.id = 'update-chip';
                chip.title = 'build version check';
                chip.style.cssText = [
                    'flex:0 0 auto', 'display:inline-flex', 'align-items:center',
                    'gap:5px', 'font-family:monospace', 'font-size:11px',
                    'padding:2px 8px', 'border-radius:3px',
                    'border:1px solid var(--bg-3)', 'background:var(--bg)',
                    'color:var(--fg-dim)', 'user-select:none',
                    'white-space:nowrap', 'cursor:pointer', 'margin-left:2px',
                ].join(';');
                const chipIcon = document.createElement('span');
                chipIcon.className = 'update-chip-ic';
                chipIcon.setAttribute('aria-hidden', 'true');
                chipIcon.innerHTML = appIconSvg('update');
                const chipText = document.createElement('span');
                chipText.textContent = '…';
                chip.appendChild(chipIcon);
                chip.appendChild(chipText);
                chip.addEventListener('click', openOrFocusWindow);
                ctx.taskbar.addStatusItem(chip);   // auto-removed on unload

                function state() {
                    if (lastError) return 'unknown';
                    return (last && last.state) || 'unknown';
                }
                function reasonCode() {
                    if (lastError) return lastError;
                    return (last && last.reason) || null;
                }
                function bandFor(s) {
                    if (s === 'current') return 'green';
                    if (s === 'behind') return 'amber';
                    return 'grey';   // ahead-or-diverged and unknown both
                }
                function chipLabel(s) {
                    if (s === 'behind') {
                        const n = last && last.behindBy;
                        return (typeof n === 'number' && n > 0)
                            ? (n + ' behind') : 'update';
                    }
                    if (s === 'current') return 'up to date';
                    if (s === 'ahead-or-diverged') return 'ahead';
                    return 'version ?';
                }

                function renderChip() {
                    const s = state();
                    // "Quiet when current" hides only the CURRENT state, never
                    // unknown: an unknown that hid itself would be
                    // indistinguishable from a green one, which is exactly the
                    // confusion this mod exists to prevent.
                    const quiet = quietSetting.get() && s === 'current';
                    chip.style.display = quiet ? 'none' : 'inline-flex';
                    const band = BANDS[bandFor(s)];
                    chip.style.borderColor = band.border;
                    chip.style.color = band.fg;
                    chipText.textContent = chipLabel(s);
                    chip.title = chipTitle();
                }

                function chipTitle() {
                    const s = state();
                    if (s === 'current') return 'this build is current with upstream';
                    if (s === 'behind') return 'a newer build is available';
                    if (s === 'ahead-or-diverged') {
                        return 'this checkout has commits upstream has not seen';
                    }
                    const r = REASONS[reasonCode()];
                    return 'could not check: ' + (r || 'reason unavailable');
                }

                // ---- the poll ----
                async function poll() {
                    if (inFlight) return;
                    inFlight = true;
                    try {
                        const r = await hostFetch(localHost(), '/update/check',
                                                  { timeoutMs: 20000 });
                        if (r.status === 503) {
                            // The broker's own gate. A capability that is absent
                            // here, NOT an error and NOT "up to date".
                            last = null;
                            lastError = 'disabled';
                        } else if (!r.ok) {
                            throw new Error('HTTP ' + r.status);
                        } else {
                            const j = await r.json();
                            if (!j || !j.ok || !j.check) throw new Error('bad-response');
                            last = j.check;
                            lastError = null;
                        }
                        checkedAt = Date.now();
                    } catch (e) {
                        // Degrade to unknown — never to "current".
                        last = null;
                        lastError = 'offline';
                        checkedAt = Date.now();
                    } finally {
                        inFlight = false;
                    }
                    renderAll();
                }

                function start() {
                    stop();
                    timer = setInterval(poll, POLL_MS);
                }
                function stop() {
                    if (timer) { clearInterval(timer); timer = null; }
                }

                // ---- detail window (ephemeral, like task-manager) ----
                function openUpdateWindow(appData) {
                    const id = String(appData.id);
                    const title = appData.title || 'Update check';
                    const geom = clampGeom(appData.geom
                        || appDefaultGeom('text-editor'));
                    const color = normalizeHex(appData.color || defaultColor(id));
                    const locked = appData.locked !== undefined
                        ? !!appData.locked : true;

                    const chrome = buildAppChrome({
                        id, appClass: 'app-upd', badge: '#upd',
                        geom, color, locked, title,
                    });
                    const { dom, titleText } = chrome;

                    const toolbar = document.createElement('div');
                    toolbar.className = 'app-toolbar app-upd-toolbar';
                    const refreshBtn = document.createElement('button');
                    refreshBtn.type = 'button';
                    refreshBtn.textContent = 'Check now';
                    refreshBtn.title = 're-ask the broker (it caches for a day)';
                    toolbar.appendChild(refreshBtn);
                    const checkedEl = document.createElement('span');
                    checkedEl.className = 'app-upd-checked';
                    toolbar.appendChild(checkedEl);

                    const body = document.createElement('div');
                    body.className = 'app-upd-body';

                    dom.appendChild(toolbar);
                    dom.appendChild(body);
                    addResizeHandles(dom);

                    document.getElementById('desktop').appendChild(dom);
                    document.getElementById('desktop').classList.remove('empty');

                    const win = {
                        id, sid: 'upd', hostId: 'app',
                        type: 'app', appKind: 'update',
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

                    wireAppChrome(win, chrome);

                    const appSess = { key: id, sid: 'upd', id, title,
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
                    finishWindowPlacement(win);
                    return win;
                }

                // One labelled row. Values go through .textContent — everything
                // here except our own literals came off the network.
                function addRow(body, label, value, cls) {
                    const row = document.createElement('div');
                    row.className = 'app-upd-row';
                    const k = document.createElement('span');
                    k.className = 'app-upd-key';
                    k.textContent = label;
                    const v = document.createElement('span');
                    v.className = 'app-upd-val' + (cls ? ' ' + cls : '');
                    v.textContent = value;
                    row.appendChild(k);
                    row.appendChild(v);
                    body.appendChild(row);
                    return v;
                }

                function addNote(body, text, cls) {
                    const el = document.createElement('div');
                    el.className = 'app-upd-note' + (cls ? ' ' + cls : '');
                    el.textContent = text;
                    body.appendChild(el);
                    return el;
                }

                // Idempotent: rebuild from `last` every call.
                function renderWindow(win) {
                    if (!win || win.disposed) return;
                    if (win.checkedEl) {
                        let t = '';
                        if (checkedAt) {
                            try {
                                t = new Date(checkedAt).toLocaleTimeString();
                            } catch (_) {}
                        }
                        win.checkedEl.textContent = checkedAt
                            ? ('checked ' + t) : 'checking…';
                    }
                    const body = win.body;
                    body.innerHTML = '';
                    const s = state();

                    const headline = addRow(body, 'Status', ({
                        'current': 'up to date',
                        'behind': 'a newer build is available',
                        'ahead-or-diverged': 'this checkout is ahead of upstream',
                        'unknown': 'could not be established',
                    })[s] || 'could not be established', 'app-upd-' + bandFor(s));
                    headline.classList.add('app-upd-headline');

                    const local = (last && last.local) || {};
                    addRow(body, 'This build', local.version
                        ? (local.version + (local.sha
                            ? ('  (' + String(local.sha).slice(0, 10) + ')') : ''))
                        : 'unknown');

                    const up = last && last.upstream;
                    if (up) {
                        addRow(body, 'Upstream', up.tag
                            || (up.sha ? String(up.sha).slice(0, 10) : '—')
                            + (up.branch ? ('  on ' + up.branch) : ''));
                    }
                    if (last && last.repo) addRow(body, 'Tracking', last.repo);

                    if (s === 'behind' && typeof last.behindBy === 'number') {
                        addRow(body, 'Behind by', last.behindBy + ' commit'
                            + (last.behindBy === 1 ? '' : 's'));
                    }
                    if (s === 'ahead-or-diverged') {
                        const a = last && last.aheadBy;
                        const b = last && last.behindBy;
                        addRow(body, 'Ahead by', (a || 0) + ' commit'
                            + (a === 1 ? '' : 's')
                            + (b ? (', behind by ' + b) : ''));
                        addNote(body, 'A checkout with commits upstream has '
                            + 'never seen is a development checkout, not a '
                            + 'stale one.');
                    }

                    if (s === 'unknown') {
                        const code = reasonCode();
                        addNote(body, 'Why: ' + (REASONS[code]
                            || 'the reason was not reported') + '.',
                            'app-upd-why');
                    }

                    // The link out. Built from our own constants plus the sha —
                    // href is assigned, never innerHTML'd.
                    if (up && (up.url || up.tag)) {
                        const a = document.createElement('a');
                        a.className = 'app-upd-link';
                        a.target = '_blank';
                        a.rel = 'noopener noreferrer';
                        a.href = up.url || '';
                        a.textContent = up.tag
                            ? 'view this release on GitHub'
                            : 'view the commit range on GitHub';
                        if (a.href) body.appendChild(a);
                    }

                    // Two things this deliberately does NOT claim.
                    addNote(body, 'The working tree is not inspected: build ids '
                        + 'carry no dirty-tree marker, so local uncommitted '
                        + 'changes are invisible here and this reflects your '
                        + 'last commit only.', 'app-upd-caveat');

                    if (s === 'behind') {
                        addNote(body, 'To update: stop the broker, run '
                            + '"git pull --ff-only" in the checkout, reinstall '
                            + 'dependencies if pyproject.toml changed, then '
                            + 'start it again and reload this page.',
                            'app-upd-howto');
                    }
                }

                function renderAll() {
                    renderChip();
                    for (const w of openWins) renderWindow(w);
                }

                function launchUpdate() {
                    openAppWindow({ id: newAppId('upd'), appKind: 'update' });
                }
                function openOrFocusWindow() {
                    for (const w of windows.values()) {
                        if (w && w.appKind === 'update' && !w.disposed) {
                            openAppWindow({ id: w.id, appKind: 'update' });
                            return;
                        }
                    }
                    launchUpdate();
                }

                ctx.registerWindowKind({
                    appKind: 'update',
                    factory: function (d) { return openUpdateWindow(d); },
                    menu: {
                        label: 'Update check',
                        iconKey: 'update',
                        launch: function () { return launchUpdate(); },
                    },
                });
                // Teardown — registered AFTER registerWindowKind so LIFO runs it
                // FIRST: stop the timer and close any live window WHILE the kind
                // is still registered.
                ctx.onUnload(function () {
                    stop();
                    for (const w of Array.from(windows.values())) {
                        if (w && w.type === 'app' && w.appKind === 'update') {
                            closeWindow(w.id);
                        }
                    }
                });

                renderChip();
                start();
                poll();
            },
        });
