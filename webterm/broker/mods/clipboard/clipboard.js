        // ---- mod: clipboard (#106) ----------------------------------------
        // A rolling history of the last N clipboard entries in BOTH directions —
        // text copied OUT of a terminal (auto-copy on selection / Ctrl+Shift+C) and
        // text pasted IN (Ctrl+V / right-click / context-menu Paste) — surfaced in a
        // small app window where any entry re-copies with one click. Core is write-
        // through and lossy (each copy overwrites the OS clipboard, a prior value is
        // unrecoverable); this keeps a recoverable ring.
        //
        // OPT-IN (defaultEnabled:false): clipboards carry secrets, so this ships OFF
        // and captures NOTHING until the operator enables it in Control Panel → Mods.
        // (Not the first opt-in mod — aistatus #112 and git #116 precede it — but the
        // first whose reason is secret-handling.) Capture rides the core ctx.clipboard
        // observer seam (63/67); when the mod is disabled the observer is removed
        // (rec.unloads) so capturing stops immediately — no background recording.
        //
        // EPHEMERAL — in-memory only, never serialized (like the task-manager). A
        // reload clears the history; a Clear-history button empties it on demand.
        // Safest posture for secrets: nothing durable ever reaches localStorage/state.
        registerMod({
            id: 'clipboard',
            version: '1.0.0',
            ctxVersion: 1,
            defaultEnabled: false,          // opt-in — clipboards carry secrets
            tiers: ['clipboard', 'window', 'taskbar'], // observes the clipboard (#106) + a window kind + a tray chip (#118)
            init: function (ctx) {
                // The capture seam is the whole point — without it the window would
                // only ever show an empty ring, so no-op on an older loader lacking it.
                if (!ctx.clipboard) return;

                // Stable window id for the SINGLE clipboard window (#118). Passing a
                // fixed id through openAppWindow makes it open-or-focus (it dedupes on
                // id and un-minimizes) — one window, reached from both the (+) menu and
                // the taskbar chip below.
                const CLIP_WIN_ID = 'app:clip';

                // ---- rolling history state (EPHEMERAL — in-memory only) --------
                // Newest-first ring of the last N entries in both directions. Never
                // serialized (reload clears it). N is fixed at 20 for v1 (zero-config).
                const N = 20;
                const RING = [];             // [{ dir:'in'|'out', text, ts }] newest-first
                const liveWins = new Set();  // open clipboard windows to repaint on capture
                // Guard so re-copying an entry (the row click below) doesn't bounce
                // back through the 'out' observer as a fresh top entry.
                let _selfCopy = false;

                // Repaint every live clipboard window from the shared ring.
                function renderAll() {
                    for (const w of liveWins) {
                        try { if (w._clipRender) w._clipRender(); } catch (_) {}
                    }
                }

                // Push a captured entry: swallow the self-copy re-emit, drop empties,
                // consecutive-dedupe (a repeat of the current top just refreshes its
                // ts instead of stacking), cap at N, then repaint every live window.
                function pushEntry(dir, text) {
                    if (_selfCopy) return;
                    if (typeof text !== 'string' || !text) return;
                    // Consecutive-dedupe on BOTH text AND direction: a repeated
                    // identical copy/paste just refreshes the top ts instead of
                    // stacking, but copying then pasting the SAME text (the direction
                    // flips) still records the new direction — the mod's whole point
                    // is history in both directions.
                    if (RING.length && RING[0].text === text && RING[0].dir === dir) {
                        RING[0].ts = Date.now();            // identical to top — refresh
                    } else {
                        RING.unshift({ dir: dir, text: text, ts: Date.now() });
                        if (RING.length > N) RING.length = N;
                    }
                    renderAll();
                }

                // Capture every copy-OUT / paste-IN core notifies. Auto-removed on
                // teardown (ctx.clipboard.observe pushes its remover onto rec.unloads),
                // so a disabled mod records nothing.
                ctx.clipboard.observe(function (dir, text) { pushEntry(dir, text); });

                // Ephemeral clipboard-history app window. Mirrors the task-manager
                // scaffold: shared app chrome (title-bar _/×, 8 resize handles), a
                // type:'app' win record, a synthetic kind:'app' session + taskbar
                // chip, and the cleanups teardown contract. NO serialize — never
                // written to the app store, so it never restores across reloads.
                function openClipboardWindow(appData) {
                    const id = String(appData.id);
                    const title = appData.title || 'Clipboard';
                    const geom = clampGeom(appData.geom || appDefaultGeom('text-editor'));
                    const color = normalizeHex(appData.color || defaultColor(id));
                    const locked = appData.locked !== undefined ? !!appData.locked : true;

                    const chrome = buildAppChrome({
                        id, appClass: 'app-clip', badge: '#clip', geom, color, locked, title,
                    });
                    const { dom, titleText } = chrome;

                    const toolbar = document.createElement('div');
                    toolbar.className = 'app-toolbar app-clip-toolbar';
                    const clearBtn = document.createElement('button');
                    clearBtn.type = 'button';
                    clearBtn.textContent = 'Clear history';
                    clearBtn.title = 'discard all captured clipboard entries';
                    toolbar.appendChild(clearBtn);

                    const clipBody = document.createElement('div');
                    clipBody.className = 'clip-body';

                    dom.appendChild(toolbar);
                    dom.appendChild(clipBody);
                    addResizeHandles(dom);   // last children: edge/corner hit zones on top

                    document.getElementById('desktop').appendChild(dom);
                    document.getElementById('desktop').classList.remove('empty');

                    const win = {
                        id, sid: 'clip', hostId: 'app',
                        type: 'app', appKind: 'clipboard',
                        dom, body: clipBody, titleText,
                        term: null, fitAddon: null,
                        ws: null, wsOpen: false, termReady: false,
                        minimized: false, disposed: false,
                        geom, name: title, color,
                        resizeTimer: null, lastSentDims: null,
                        staleSession: false, authFailed: false,
                        reattachAttempts: 0, reattachAt: 0, lastOpenAt: 0, missingPolls: 0,
                        cleanups: [],
                        tiled: false,
                        floatGeom: appData.floatGeom
                            ? Object.assign({}, appData.floatGeom) : null,
                        locked,
                        dirty: false,
                    };
                    windows.set(id, win);

                    const stopProp = (e) => e.stopPropagation();

                    // Short HH:MM:SS stamp + a one-line, whitespace-collapsed preview.
                    const pad2 = (n) => (n < 10 ? '0' + n : '' + n);
                    const fmtTime = (ts) => {
                        const d = new Date(ts);
                        return pad2(d.getHours()) + ':' + pad2(d.getMinutes())
                            + ':' + pad2(d.getSeconds());
                    };
                    const preview = (s) => {
                        s = String(s == null ? '' : s).replace(/\s+/g, ' ').trim();
                        return s.length > 200 ? s.slice(0, 199) + '…' : s;
                    };

                    // Full re-render of the ring into clipBody. textContent ONLY —
                    // clipboard text is arbitrary user input, never innerHTML'd (the
                    // auth-overlay rule, 63). Idempotent; a scroll snapshot keeps a
                    // capture-driven repaint from jumping the view.
                    const render = () => {
                        const scrollTop = clipBody.scrollTop;
                        clipBody.innerHTML = '';
                        if (!RING.length) {
                            const empty = document.createElement('div');
                            empty.className = 'clip-empty';
                            empty.textContent = 'no clipboard history yet';
                            clipBody.appendChild(empty);
                            return;
                        }
                        for (const entry of RING) {
                            const row = document.createElement('div');
                            row.className = 'clip-row';
                            const dirEl = document.createElement('span');
                            dirEl.className = 'clip-dir clip-dir-' + entry.dir;
                            dirEl.textContent = entry.dir === 'in' ? '←' : '→';
                            dirEl.title = entry.dir === 'in' ? 'pasted in' : 'copied out';
                            const timeEl = document.createElement('span');
                            timeEl.className = 'clip-time';
                            timeEl.textContent = fmtTime(entry.ts);
                            const textEl = document.createElement('span');
                            textEl.className = 'clip-text';
                            textEl.textContent = preview(entry.text);
                            textEl.title = 'click to copy';
                            row.appendChild(dirEl);
                            row.appendChild(timeEl);
                            row.appendChild(textEl);
                            // Re-copy this entry to the OS clipboard. The _selfCopy
                            // guard stops the resulting 'out' notify from pushing a
                            // duplicate top entry.
                            row.addEventListener('click', () => {
                                _selfCopy = true;
                                try { copyTextToClipboard(entry.text); }
                                finally { _selfCopy = false; }
                            });
                            clipBody.appendChild(row);
                        }
                        clipBody.scrollTop = scrollTop;
                    };
                    win._clipRender = render;   // pushEntry / clear repaint through this

                    // Toolbar: Clear history empties the SHARED ring, so every open
                    // clipboard window repaints, not just this one.
                    const onClearDown = stopProp;
                    const onClearClick = (e) => {
                        e.stopPropagation();
                        RING.length = 0;
                        renderAll();
                    };
                    clearBtn.addEventListener('mousedown', onClearDown);
                    clearBtn.addEventListener('click', onClearClick);
                    win.cleanups.push(() => {
                        clearBtn.removeEventListener('mousedown', onClearDown);
                        clearBtn.removeEventListener('click', onClearClick);
                    });

                    // Raise / minimize / close / drag / 8-way resize / WM context menu.
                    wireAppChrome(win, chrome);

                    // Manual taskbar item + synthetic kind:'app' session (keeps the
                    // poll reaper off this window; lets formatTitle render it) — same
                    // as the task-manager / file-manager app windows.
                    const appSess = { key: id, sid: 'clip', id, title, stale: false,
                                      kind: 'app', hostId: 'app' };
                    sessions.set(id, appSess);
                    const itemsHost = document.getElementById('taskbar-items');
                    if (!itemsHost.querySelector(
                            '.taskbar-item[data-session-id="' + cssEscape(id) + '"]')) {
                        itemsHost.appendChild(buildTaskbarItem(appSess));
                    }
                    updateTaskbarColor(id);
                    updateTaskbarLabel(id);
                    const emptyMsg = document.getElementById('taskbar-empty');
                    if (emptyMsg) emptyMsg.remove();

                    // Track for live repaint on capture; dropped on close so a stale
                    // window is never repainted.
                    liveWins.add(win);
                    win.cleanups.push(() => liveWins.delete(win));

                    render();
                    if (findKeyInLayout(id)) placeWindowTiled(win);
                    else bringToFront(id);
                    return win;
                }

                // The (+) launcher / taskbar chip: open the clipboard window —
                // singleton (open-or-focus). The stable CLIP_WIN_ID routes through
                // openAppWindow's id dedupe, so a second launch focuses (and un-
                // minimizes) the existing window instead of stacking another. There's
                // one shared ring, so one window is all that's wanted.
                function launchClipboard() {
                    return openAppWindow({ id: CLIP_WIN_ID, appKind: 'clipboard' });
                }

                // Register the clipboard window kind with NO serialize — EPHEMERAL,
                // never written to the app store (like the task-manager). A duplicate
                // appKind throws -> initMod rolls the mod back.
                ctx.registerWindowKind({
                    appKind: 'clipboard',
                    factory: function (d) { return openClipboardWindow(d); },
                    menu: {
                        label: '📋 Clipboard',
                        launch: function () { return launchClipboard(); },
                    },
                });

                // Taskbar tray chip (#118): a small 📋 next to the clock / aistatus /
                // help chips that opens-or-focuses the single clipboard window — the
                // SAME launchClipboard path as the (+) menu. Style lives in the mod
                // CSS (#clipboard-chip), mirroring the help/clock/aistatus chips.
                // addStatusItem auto-removes the chip on teardown (rec.unloads), so no
                // onUnload change is needed. Lightly guarded for an older loader whose
                // taskbar seam predates addStatusItem.
                if (ctx.taskbar && ctx.taskbar.addStatusItem) {
                    const chip = document.createElement('div');
                    chip.id = 'clipboard-chip';
                    chip.title = 'Clipboard history';
                    chip.textContent = '📋';
                    chip.addEventListener('click', function () { launchClipboard(); });
                    ctx.taskbar.addStatusItem(chip);   // before #help-chip; auto-removed
                }

                // Teardown — registered AFTER registerWindowKind so LIFO runs it
                // FIRST, closing live clipboard windows WHILE the kind is still
                // registered (so saveAppWindow sees the no-serialize kind and early-
                // returns — no junk record persists), same reasoning as the task-
                // manager. Then clear liveWins. The ctx.clipboard observer is auto-
                // removed by its own rec.unloads entry, so capturing stops the moment
                // the mod is disabled.
                ctx.onUnload(function () {
                    for (const w of Array.from(windows.values())) {
                        if (w && w.type === 'app' && w.appKind === 'clipboard') {
                            closeWindow(w.id);
                        }
                    }
                    liveWins.clear();
                });
            },
        });
