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
        //
        // #194 REFERENCE MIGRATION. This mod used to hand-build the ~30-field app-
        // window scaffold every windowed mod re-typed: buildAppChrome, the desktop
        // insertion, the `win` literal pushed into the core windows Map, the synthetic
        // kind:'app' session + taskbar chip, addResizeHandles, finishWindowPlacement,
        // and an onUnload that iterated core state to find its own windows. All of it
        // is core-owned now — ctx.windows.createAppWindow builds the window and the
        // loader's staged take-down closes it — so what is left below is only what is
        // specific to a clipboard window: its ring, its toolbar and its rows.
        registerMod({
            id: 'clipboard',
            version: '1.0.0',
            ctxVersion: 1,
            defaultEnabled: false,          // opt-in — clipboards carry secrets
            tiers: ['clipboard', 'window', 'taskbar'], // observes the clipboard (#106) + a window kind + a tray chip (#118)
            // #194/#197, and DELIBERATE: the whole window is built by the core
            // factory now, so on a build without it this mod has no window at
            // all — the (+) menu entry and the tray chip would be dead buttons
            // and the registered kind's factory would throw on every launch.
            // A `needs` BLOCKS the mod there and the Mods pane row reads
            // "blocked (needs windows.createAppWindow)", which is the point:
            // the alternative (a typeof bail) leaves a mod that reads "active"
            // while silently doing nothing. The ctx.clipboard bail below stays
            // as the second line of defence, per #197's own rule that `needs`
            // makes degradation visible rather than replacing feature detection.
            needs: ['windows.createAppWindow'],
            init: function (ctx) {
                // The capture seam is the whole point — without it the window would
                // only ever show an empty ring, so no-op on an older loader lacking it.
                if (!ctx.clipboard) return;

                // The STABLE id of the SINGLE clipboard window (#118). #194 took
                // the open-or-focus HACK off this constant — `singleton: true` on
                // the factory spec owns that now, and dedupes on KIND over every
                // live window rather than on this one id. The literal stays
                // because an id is identity: defaultColor(id) hashes it into the
                // window's accent and the workspace-membership map is keyed by
                // it, so changing it would repaint and re-home the window for
                // nothing.
                const CLIP_WIN_ID = 'app:clip';

                // ---- rolling history state (EPHEMERAL — in-memory only) --------
                // Newest-first ring of the last N entries in both directions. Never
                // serialized (reload clears it). N is fixed at 20 for v1 (zero-config).
                const N = 20;
                const RING = [];             // [{ dir:'in'|'out', text, ts }] newest-first
                // Guard so re-copying an entry (the row click below) doesn't bounce
                // back through the 'out' observer as a fresh top entry.
                let _selfCopy = false;

                // Repaint every live clipboard window from the shared ring.
                // ctx.windows.list() (#194) IS this mod's window bookkeeping — it
                // replaces the hand-kept `liveWins` set and the cleanup that had to
                // drop a window from it, and it prunes closed windows on the way
                // past, so a stale one can never be repainted.
                function renderAll() {
                    for (const h of ctx.windows.list()) {
                        try { if (h.win._clipRender) h.win._clipRender(); } catch (_) {}
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

                // ---- the toolbar: Clear history --------------------------------
                // The .app-toolbar strip above the body. Clearing empties the SHARED
                // ring, so every open clipboard window repaints, not just this one.
                function buildToolbar(el, win, h) {
                    const clearBtn = document.createElement('button');
                    clearBtn.type = 'button';
                    clearBtn.textContent = 'Clear history';
                    clearBtn.title = 'discard all captured clipboard entries';
                    el.appendChild(clearBtn);
                    const onClearDown = (e) => e.stopPropagation();
                    const onClearClick = (e) => {
                        e.stopPropagation();
                        RING.length = 0;
                        renderAll();
                    };
                    clearBtn.addEventListener('mousedown', onClearDown);
                    clearBtn.addEventListener('click', onClearClick);
                    // h.onDispose is win.cleanups spelled the way the handle spells
                    // it — drained by closeWindow and by the lease-loss rebuild.
                    h.onDispose(function () {
                        clearBtn.removeEventListener('mousedown', onClearDown);
                        clearBtn.removeEventListener('click', onClearClick);
                    });
                }

                // ---- the body: the ring, rendered ------------------------------
                // Runs against a window that is already in the document, already in
                // the windows Map and already chipped (the factory's contract), so
                // the render below has a real box to scroll.
                function buildBody(clipBody, win) {
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
                    // clipboard text is arbitrary user input, never rendered as
                    // markup (the auth-overlay rule, 63). Idempotent; a scroll
                    // snapshot keeps a capture-driven repaint from jumping the view.
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
                    // Per-window state on the record, which is where the factory's
                    // body() is meant to hang it. pushEntry / Clear repaint through
                    // this, reached via ctx.windows.list() in renderAll.
                    win._clipRender = render;
                    render();
                }

                // Ephemeral clipboard-history app window, built by the core factory
                // (#194). Everything the deleted scaffold did by hand — chrome, the
                // desktop insertion, the win record, the synthetic kind:'app'
                // session + taskbar chip, the eight resize handles, the placement
                // tail — is core-owned; the spec below is only what makes this
                // window a CLIPBOARD window. NO serialize on the kind, so it is
                // never written to the app store and never restores across reloads.
                function openClipboardWindow(appData) {
                    const d = appData || {};
                    const h = ctx.windows.createAppWindow({
                        kind: 'clipboard',
                        // The stored record's own id when core hands one over,
                        // the stable id otherwise. `singleton` still dedupes by
                        // kind whichever it is, so a restore adopts rather than
                        // building a second window (#167).
                        id: (d.id != null && String(d.id)) ? String(d.id) : CLIP_WIN_ID,
                        singleton: true,        // one shared ring, so one window
                        title: d.title || 'Clipboard',
                        sid: 'clip',            // chip/session short id
                        badge: '#clip',
                        appClass: 'app-clip',
                        // NOT the factory default ('app-clip-body'): the shipped
                        // stylesheet matches `.term-window.app-clip .clip-body`.
                        bodyClass: 'clip-body',
                        // Absent fields keep the factory's defaults, which are the
                        // deleted scaffold's own: clampGeom(appDefaultGeom(kind)),
                        // normalizeHex(defaultColor(id)) and locked:true.
                        geom: d.geom,
                        color: d.color,
                        locked: d.locked,
                        floatGeom: d.floatGeom,
                        toolbar: buildToolbar,
                        body: buildBody,
                    });
                    // THE TRAP: openAppWindow hands a registered kind's factory
                    // return value straight back to its callers, and they want a
                    // window RECORD. Return h.win, never the handle.
                    return h.win;
                }

                // The (+) launcher / taskbar chip: open the clipboard window —
                // singleton (open-or-focus). Routed through openAppWindow, whose
                // id dedupe returns the live window before the kind factory is
                // ever reached; when it is reached, the factory spec's
                // `singleton: true` is the second, wider guarantee (it dedupes on
                // KIND, so even a window restored under some other id is focused
                // rather than duplicated). Both paths reveal-and-focus, and both
                // are idempotent.
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
                        label: 'Clipboard',
                        iconKey: 'clipboard',   // #119: SVG clipboard in the (+) menu
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
                    chip.setAttribute('aria-label', 'Clipboard history');
                    // #119: the app-icon clipboard glyph (trusted, hardcoded SVG
                    // from the APP_ICON_SVG registry) replaces the 📋 emoji.
                    chip.innerHTML = appIconSvg('clipboard');
                    chip.addEventListener('click', function () { launchClipboard(); });
                    ctx.taskbar.addStatusItem(chip);   // before #help-chip; auto-removed
                }

                // NO teardown entry of its own any more (#194). The mod used to
                // register an onUnload AFTER registerWindowKind purely so LIFO would
                // close its windows while the kind was still registered — otherwise
                // saveAppWindow falls back to the shared serializer and persists a
                // junk record for an ephemeral kind. The loader now stages that close
                // pass itself, at the head of _runUnloads and therefore before the
                // first onUnload entry runs, so every factory-owned window is already
                // gone (with every kind still registered) by the time any teardown
                // chain starts. The clipboard observer and the tray chip remove
                // themselves through their own rec.unloads entries, and the ring dies
                // with this closure — so there is nothing left for this mod to undo.
            },
        });
