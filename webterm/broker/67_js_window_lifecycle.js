        // ---- per-terminal-window lifecycle hook (#116 / S14) ---------------
        // A per-window title-bar control (the git status widget) is not a taskbar
        // chip or an app window, so it needs a hook that fires once per TERMINAL
        // window. registerTerminalCreate(cb) subscribes: cb is REPLAYED over every
        // terminal already open (so enabling the git mod mid-session decorates the
        // existing windows) and fired for every future openWindow. Returns an
        // unsubscribe fn. The loader exposes this as ctx.windows.onTerminalCreate
        // and auto-unsubscribes on the mod's teardown. `windows` is the cross-
        // fragment core Map (64_js_sessions_poll_control.js); these live at the one
        // shared top-level scope, so create-time + replay both reach them.
        //
        // INVARIANT: a callback is a lifetime notification for a CONCRETE `win`.
        // Its per-window teardown rides win.cleanups (via onDispose), drained by
        // closeWindow (73) on close AND by teardownView (84) on a lost HOME lease.
        // teardownView also windows.clear()s, and rebuildView re-opens each
        // terminal through openWindow — so a lease-loss/reactivate DISPOSES the old
        // win and RE-EMITS a fresh create for the rebuilt one; the widget is torn
        // down and re-decorated, never silently lost or double-mounted.
        const termCreateCbs = [];
        // #126: core constructs every terminal with this self-contained baseline
        // monospace stack and knows NOTHING about the (now mod-owned) terminal-font
        // feature. The termfont mod (mods/termfont/) overrides it PER terminal via
        // ctx.windows.onTerminalCreate when enabled, and resets terminals to THIS
        // exact family on disable — so this literal MUST stay equal to the mod's
        // TERM_FONT_DEFAULT (guarded by test_termfont_symbols_removed_from_core_
        // fragments). When the mod is off, terminals use this baseline.
        const TERM_FONT_BASELINE = 'Consolas, "Liberation Mono", monospace';
        // Build the per-window context object and hand it to ONE subscriber. Used
        // by both the create-time emit and the replay, so both see an identical
        // shape. titleBar/minBtn are derived from win.dom so a replayed window
        // (built before the subscriber existed) resolves them the same way the
        // create-time call does. addTitleBarItem inserts BEFORE the min button —
        // the established idiom (col: 221 / MCP: 226) — so a control lands left of
        // min/close. onDispose reuses win.cleanups, which closeWindow (73) and the
        // active-view rebuild (84) already drain on teardown — no new plumbing.
        function _emitTerminalCreate(win, cb) {
            const titleBar = win.dom && win.dom.querySelector('.title-bar');
            if (!titleBar) return;
            const minBtn = titleBar.querySelector('.btn-min');
            try {
                cb({
                    win: win,
                    titleBar: titleBar,
                    host: hostById(win.hostId),
                    wireId: win.sid,
                    addTitleBarItem: function (node) {
                        titleBar.insertBefore(node, minBtn);
                    },
                    onDispose: function (fn) {
                        if (typeof fn === 'function') win.cleanups.push(fn);
                    },
                });
            } catch (e) {
                console.error('[windows] onTerminalCreate callback failed:', e);
            }
        }
        function registerTerminalCreate(cb) {
            if (typeof cb !== 'function') return function () {};
            termCreateCbs.push(cb);
            // Replay over the terminals open right now (app windows are excluded —
            // this is a TERMINAL hook). A disposed-but-not-yet-removed window is
            // skipped so a stale interval isn't started on a dead window.
            for (const win of Array.from(windows.values())) {
                if (win && win.type !== 'app' && !win.disposed) {
                    _emitTerminalCreate(win, cb);
                }
            }
            return function () {
                const i = termCreateCbs.indexOf(cb);
                if (i !== -1) termCreateCbs.splice(i, 1);
            };
        }

        // ---- window create / minimize / restore / close -------------------
        function openWindow(id, sess) {
            id = String(id);
            const existing = windows.get(id);
            if (existing) {
                if (existing.minimized) {
                    existing.minimized = false;
                    existing.dom.classList.remove('minimized');
                    if (existing.tiled) requestRelayout();   // back into its column
                }
                bringToFront(id);
                refitSoon(existing);
                return existing;
            }

            // `id` is the session KEY ('<hostId>:<windowId>'); `sid` is the
            // BARE numeric window id used on the wire (?session= must stay
            // host-unqualified — 48-bit ids are per-broker by protocol) and
            // in visible labels; `hostId` picks which broker to dial.
            const sid = sess && sess.sid != null ? String(sess.sid)
                : (sess && sess.id != null ? String(sess.id)
                    : id.slice(id.indexOf(':') + 1));
            const hostId = (sess && sess.hostId)
                || (id.indexOf(':') !== -1 ? id.slice(0, id.indexOf(':'))
                                           : 'local');
            // Task 15: warm the remote host's settings cache as soon as one of
            // its windows opens, so that broker's per-host settings (default
            // profile, tiling mode) are ready before the slow background
            // prefetch runs. Best-effort, fire-and-forget.
            if (hostId !== 'local' && !hostStateCache.has(hostId)) {
                fetchHostState(hostId);
            }
            const pref = getPref(id);
            const geom = clampGeom(pref.geom || defaultGeom());
            if (isSizeLocked()) {
                const ls = lockedSize();
                geom.width = ls.width;
                geom.height = ls.height;
            }
            // Precedence: a saved per-window color wins; else this window's
            // launch-profile DEFAULT accent (#115); else the host's optional
            // DEFAULT accent (#103); else the palette auto-pick for adjacency.
            // Each helper returns '' when unset, so an absent tier falls through.
            // This seeds the STARTING color only — it is not persisted into
            // pref.color, so an un-recolored window re-seeds from its profile/host
            // on each reopen, and a user recolor permanently wins.
            const color = normalizeHex(
                pref.color || profileDefaultColor(hostId, sess && sess.profile)
                || hostDefaultColor(hostId) || defaultColor(id));
            const name = formatTitle(sess || { id: sid });

            const dom = document.createElement('div');
            dom.className = 'term-window';
            // A deep-link / /launch / pending-open under a hidden broker starts
            // masked; the bringToFront guard already refuses to focus it.
            if (hostHidden(hostId)) dom.classList.add('broker-hidden');
            dom.dataset.sessionId = id;
            dom.style.left = geom.left + 'px';
            dom.style.top = geom.top + 'px';
            // .term-window is content-box with 2px borders all sides, so
            // style.* is content area and offsetWidth/Height = style + 4. Use
            // the same -4 math as applyGeomToWindow so the rendered outer box
            // matches the stored geom — otherwise a freshly-created window's
            // offsetHeight is +4 inflated, which a shift-drag swap then
            // propagates onto the dragged window.
            dom.style.width = (geom.width - 4) + 'px';
            dom.style.height = (geom.height - 4) + 'px';
            dom.style.setProperty('--accent', color);
            dom.classList.toggle('dark-accent', isDarkAccent(color));
            dom.classList.toggle('scroll-locked', !!pref.locked);

            const titleBar = document.createElement('div');
            titleBar.className = 'title-bar';

            const idBadge = document.createElement('span');
            idBadge.className = 'ti-id-badge';
            idBadge.textContent = '#' + sid;

            const titleText = document.createElement('span');
            titleText.className = 'title-text';
            titleText.textContent = name;

            // #120: the per-terminal 📋 "Agent docs" button (opens this folder's
            // AGENTS.md/CLAUDE.md editor) used to be built here; it moved to the
            // default-on agent-docs mod (mods/agent-docs/), which subscribes to
            // ctx.windows.onTerminalCreate and inserts it into this title bar
            // (before the min button, its original slot) — the same seam as the
            // git widget below.

            // #116: the per-terminal git status button + branch label used to be
            // built here; they moved to the default-off git mod (mods/git/), which
            // subscribes to ctx.windows.onTerminalCreate and inserts them into this
            // title bar (before the min button, its original slot) when enabled.

            const minBtn = document.createElement('button');
            minBtn.type = 'button';
            minBtn.className = 'tb-btn btn-min';
            minBtn.textContent = '_';
            minBtn.title = 'minimize';

            const closeBtn = document.createElement('button');
            closeBtn.type = 'button';
            closeBtn.className = 'tb-btn btn-close';
            closeBtn.textContent = '×';
            // #88: reflect the terminal-close setting on the × affordance. Kept in
            // sync afterwards by applyTerminalCloseAffordance (on toggle / /state).
            const _term = !!getSettings().terminalCloseTerminates;
            closeBtn.title = _term ? 'terminate' : 'close';
            closeBtn.classList.toggle('btn-close-terminate', _term);

            titleBar.appendChild(idBadge);
            titleBar.appendChild(titleText);
            titleBar.appendChild(minBtn);
            titleBar.appendChild(closeBtn);

            const body = document.createElement('div');
            body.className = 'term-body';

            dom.appendChild(titleBar);
            dom.appendChild(body);

            for (const dir of ['n','s','e','w','nw','ne','sw','se']) {
                const h = document.createElement('div');
                h.className = 'rh rh-' + dir;
                h.dataset.dir = dir;
                dom.appendChild(h);
            }

            document.getElementById('desktop').appendChild(dom);
            document.getElementById('desktop').classList.remove('empty');

            const term = new Terminal({
                cursorBlink: true,
                cols: 80,
                rows: 24,
                fontFamily: TERM_FONT_BASELINE,   // #126: mod overrides per terminal
                fontSize: 14,
                theme: { background: '#000000' }
            });
            const fitAddon = new FitAddon.FitAddon();
            term.loadAddon(fitAddon);
            term.open(body);

            const win = {
                id, sid, hostId, dom, body, term, fitAddon,
                titleText,
                ws: null,
                minimized: false,
                geom,
                name,
                color,
                wsOpen: false,
                termReady: false,
                resizeTimer: null,
                lastSentDims: null,
                disposed: false,
                staleSession: false,
                authFailed: false,
                reattachAttempts: 0,
                reattachAt: 0,
                lastOpenAt: 0,
                missingPolls: 0,
                cleanups: [],
                // ---- tiling (niri WM) runtime fields; derived, NOT the
                // persisted truth (prefs._layout is). `tiled` is recomputed
                // from _layout membership; `floatGeom` is the last floating
                // box, snapshotted on tile and restored on un-tile.
                tiled: false,
                floatGeom: null,
                // Floating scroll-lock: unlocked (default) windows travel with
                // the strip as it scrolls (geom.left tracks screen position and
                // is shifted by the scroll delta); locked windows stay pinned
                // to the screen like a HUD. Persisted per-window in pref.locked.
                locked: !!getPref(id).locked,
            };
            windows.set(id, win);
            // #116: notify per-terminal-window mods (the git status widget) NOW —
            // right after the map insert and BEFORE the color/MCP buttons are
            // inserted below — so addTitleBarItem lands a control in its original
            // slot (after AGENTS.md, before color/MCP/min), preserving today's
            // title-bar order. Firing at the end of openWindow would place it to
            // the RIGHT of color/MCP. termCreateCbs is snapshotted so a callback
            // that (un)subscribes mid-emit can't skip or revisit a sibling.
            if (win.type !== 'app') {
                for (const cb of termCreateCbs.slice()) _emitTerminalCreate(win, cb);
            }
            // Restore-on-refresh: remember every open TERMINAL so a browser
            // reload can reattach it. openWindow only ever builds terminals,
            // but guard on the app flag for symmetry with closeWindow (app
            // windows restore via appStore, never this set).
            if (win.type !== 'app') addOpenTerm(id);

            // bring to front on any mousedown inside the window dom
            const onMouseDown = () => bringToFront(id);
            dom.addEventListener('mousedown', onMouseDown);
            win.cleanups.push(() => dom.removeEventListener('mousedown', onMouseDown));

            // Window-color control (issue 5): the shared swatch-dropdown,
            // wired after `win` exists (like the git popover). Terminal swatches
            // are the window PALETTE; a pick recolors the title bar and persists
            // to prefs. stopProp is reused by the min/close handlers below.
            const stopProp = (e) => e.stopPropagation();
            const colorBtn = attachColorPicker(
                win, titleBar, PALETTE.map((c) => ({ color: c })),
                (sw) => {
                    if (win.disposed) return;        // dialog finished post-close
                    const c = normalizeHex(sw.color);
                    win.color = c;
                    dom.style.setProperty('--accent', c);
                    dom.classList.toggle('dark-accent', isDarkAccent(c));
                    getPref(id).color = c;
                    saveRecentColor(c);              // global MRU (#29)
                    savePrefs();
                    updateTaskbarColor(id);
                });
            titleBar.insertBefore(colorBtn, minBtn);

            // MCP access control (#20): robot button + dropdown, right next to
            // the color swatch (terminals only — app docs aren't MCP sessions).
            const mcpBtn = attachMcpButton(win, titleBar);
            titleBar.insertBefore(mcpBtn, colorBtn);

            // min / close
            const onMinDown = stopProp;
            const onCloseDown = stopProp;
            const onMinClick = (e) => { e.stopPropagation(); minimizeWindow(id); };
            // #88: the × button soft-closes (detach the view; the shell keeps
            // running) by default. When terminalCloseTerminates is ON it instead
            // hard-kills the session via terminateWindow → POST /session/kill,
            // optionally behind the same styled confirm the right-click Terminate
            // uses. Reads the LOCAL getSettings() (per-host display setting, like
            // stripScrollbar); terminateWindow itself routes the kill to the
            // session's own host. The right-click Close stays the soft-close path.
            const onCloseClick = (e) => {
                e.stopPropagation();
                const st = getSettings();
                if (st.terminalCloseTerminates) {
                    if (st.terminalCloseConfirm) {
                        openConfirmDialog({
                            title: 'Terminate session',
                            message: 'Terminate this session? '
                                + 'The shell process tree will be killed.',
                            okLabel: 'Terminate', danger: true,
                        }).then((ok) => {
                            // The /sessions reaper can tear this window down while
                            // the dialog is open — guard so a stale OK doesn't toast
                            // "session not found".
                            if (ok && windows.has(id)) terminateWindow(id);
                        });
                    } else {
                        terminateWindow(id);
                    }
                    return;
                }
                closeWindow(id);
            };
            minBtn.addEventListener('mousedown', onMinDown);
            minBtn.addEventListener('click', onMinClick);
            closeBtn.addEventListener('mousedown', onCloseDown);
            closeBtn.addEventListener('click', onCloseClick);
            win.cleanups.push(() => {
                minBtn.removeEventListener('mousedown', onMinDown);
                minBtn.removeEventListener('click', onMinClick);
                closeBtn.removeEventListener('mousedown', onCloseDown);
                closeBtn.removeEventListener('click', onCloseClick);
            });

            // drag
            wireDrag(win, titleBar);
            // title-bar right-click: per-window WM menu (float<->tile, column
            // width presets when tiled, minimize/close). stopPropagation keeps
            // the desktop menu from also firing.
            const onTitleCtx = (e) => {
                e.preventDefault();
                e.stopPropagation();
                bringToFront(win.id);
                buildWindowMenu(win, e.clientX, e.clientY);
            };
            titleBar.addEventListener('contextmenu', onTitleCtx);
            win.cleanups.push(() =>
                titleBar.removeEventListener('contextmenu', onTitleCtx));
            // resize
            for (const handle of dom.querySelectorAll('.rh')) {
                wireResize(win, handle, handle.dataset.dir);
            }

            // Send term data in <=256 Ki-char frames: a clipboard paste
            // arrives from the clipboard API / xterm.js as ONE string, and
            // a single oversized ws frame gets the socket killed (1009) by
            // any frame cap between here and the agent. Ordering is
            // preserved — same socket. Never split a surrogate pair.
            const CHUNK_CHARS = 262144;
            const sendChunked = (type, data) => {
                if (!win.ws || win.ws.readyState !== WebSocket.OPEN) return;
                let i = 0;
                while (i < data.length) {
                    let end = Math.min(i + CHUNK_CHARS, data.length);
                    const cc = data.charCodeAt(end - 1);
                    if (end < data.length && cc >= 0xD800 && cc <= 0xDBFF) end -= 1;
                    win.ws.send(JSON.stringify({ type, data: data.slice(i, end) }));
                    i = end;
                }
            };

            // right-click paste — only hijack the native menu when we can
            // actually serve a paste from this context. On http://<LAN-IP>
            // navigator.clipboard.readText() is blocked, so leaving the
            // listener unbound lets the browser's own context menu (with a
            // working Paste entry) appear instead. Loopback / https keep
            // the seamless one-click paste. The text goes through xterm's
            // paste() (#138) — CRLF/LF -> CR, plus ESC[200~ bracketing iff
            // the app enabled DECSET 2004 — and exits via the onData ->
            // sendChunked('input', ...) path below, so a multiline block
            // lands as ONE paste instead of raw newlines that submit at the
            // first line. paste() fires no DOM paste event, so the inline
            // notify here stays this path's only #106 count (no double count
            // with the capture-phase onClipPaste listener).
            if (canReadClipboard()) {
                const onContext = async (e) => {
                    e.preventDefault();
                    if (!win.ws || win.ws.readyState !== WebSocket.OPEN) return;
                    try {
                        const text = await navigator.clipboard.readText();
                        if (text) {
                            term.paste(text);
                            _notifyClipboard('in', text);   // #106 history
                        }
                    } catch (err) {
                        console.error('paste read failed:', err);
                    }
                };
                term.element.addEventListener('contextmenu', onContext);
                win.cleanups.push(() => {
                    try { term.element.removeEventListener('contextmenu', onContext); }
                    catch (_) {}
                });
            }

            // #106: capture-phase paste seam — record text pasted INTO the terminal
            // for the clipboard history mod. Capture phase so it fires before
            // xterm's hidden-textarea paste handler; the event carries the text
            // during the user gesture, so it works even in a non-secure context
            // (where navigator.clipboard.readText is blocked). Ctrl+V and the
            // browser's OWN context-menu Paste both dispatch this DOM event. The
            // right-click onContext path (above) preventDefault()s the native menu
            // and reads the clipboard itself — it does NOT fire a DOM paste, so it
            // notifies inline instead; hence no double count between the two paths.
            const onClipPaste = (e) => {
                try {
                    const t = e.clipboardData && e.clipboardData.getData('text');
                    if (t) _notifyClipboard('in', t);
                } catch (_) {}
            };
            term.element.addEventListener('paste', onClipPaste, true);
            win.cleanups.push(() => {
                try { term.element.removeEventListener('paste', onClipPaste, true); }
                catch (_) {}
            });

            // Shift+wheel scrolls the local xterm.js buffer regardless of
            // whether the running app (claude-code, vim, less, ...) has
            // grabbed mouse events via DECSET 1000/1002/1006. Matches the
            // gnome-terminal/kitty convention so users always
            // have a way to reach scrollback. Capture phase so xterm.js
            // never sees it.
            const onWheel = (e) => {
                if (!e.shiftKey) return;
                e.preventDefault();
                e.stopPropagation();
                const lines = Math.sign(e.deltaY)
                    * Math.max(1, Math.round(Math.abs(e.deltaY) / 40));
                try { term.scrollLines(lines); } catch (_) {}
            };
            term.element.addEventListener('wheel', onWheel,
                { capture: true, passive: false });
            win.cleanups.push(() => {
                try {
                    term.element.removeEventListener('wheel', onWheel,
                        { capture: true });
                } catch (_) {}
            });

            // Auto-copy on selection mouseup. copyTextToClipboard() picks
            // the modern API in secure contexts and falls back to the
            // legacy execCommand path on plain http.
            const onMouseUp = () => {
                if (!term.hasSelection || !term.hasSelection()) return;
                const sel = term.getSelection();
                if (sel) copyTextToClipboard(sel);
            };
            term.element.addEventListener('mouseup', onMouseUp);
            win.cleanups.push(() => {
                try { term.element.removeEventListener('mouseup', onMouseUp); }
                catch (_) {}
            });

            // Ctrl+Shift+C explicit copy. Returning false prevents xterm from
            // forwarding the chord (which would otherwise reach the producer
            // as ^C on most layouts). Plain Ctrl+C falls through unchanged.
            term.attachCustomKeyEventHandler(ev => {
                if (ev.type !== 'keydown') return true;
                if (!(ev.ctrlKey && ev.shiftKey)) return true;
                const key = (ev.key || '').toLowerCase();
                if (key !== 'c') return true;
                const sel = term.getSelection();
                if (sel) copyTextToClipboard(sel);
                ev.preventDefault();
                ev.stopPropagation();
                return false;
            });

            // term -> server (xterm.js delivers a Ctrl+V paste as one
            // onData string, so this path needs the chunking too)
            const onDataDisp = term.onData((data) => sendChunked('input', data));
            win.cleanups.push(() => { try { onDataDisp.dispose(); } catch (_) {} });

            // Track IME composition so relayout never reparents (and aborts a
            // composition) mid-input. compositionstart/end fire on the textarea
            // xterm keeps inside .term-body.
            const onCompStart = () => { _imeComposing = true; };
            const onCompEnd = () => { _imeComposing = false; };
            body.addEventListener('compositionstart', onCompStart, true);
            body.addEventListener('compositionend', onCompEnd, true);
            win.cleanups.push(() => {
                body.removeEventListener('compositionstart', onCompStart, true);
                body.removeEventListener('compositionend', onCompEnd, true);
            });

            // Tiling placement (niri WM): decide this window's role and, if
            // tiled, reparent it into the strip NOW — before the RAF×2
            // measurement below and before attachWebSocket — so the
            // resized-before-snapshot handshake measures the final tiled box
            // (not the floating geometry it was created with).
            if (decideTiled(id)) {
                placeWindowTiled(win);
            }

            // After two RAFs the term has measured itself.
            requestAnimationFrame(() => requestAnimationFrame(() => {
                if (win.disposed) return;
                win.termReady = true;
                maybeSendInitialResize(win);
            }));

            attachWebSocket(win);
            bringToFront(id);
            return win;
        }

