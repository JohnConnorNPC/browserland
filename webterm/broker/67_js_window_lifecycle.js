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
                // Re-home / navigate first (#152): opening a window that is
                // already live but parked on another workspace must not silently
                // no-op. revealAndFocusWindow un-minimizes via restoreWindow,
                // which is the same three steps this branch used to inline.
                revealAndFocusWindow(id);
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
                // #154: while an app owns the mouse (lazygit/btop/mc), xterm hands
                // drags to the app and drag-select dies. Its escape gesture is
                // Shift-drag everywhere EXCEPT macOS, where shouldForceSelection
                // requires this option — which defaults false, so a Mac user had no
                // gesture at all. Option-drag is the iTerm2 / Terminal.app
                // convention, so this matches what they already expect.
                macOptionClickForcesSelection: true,
                theme: { background: '#000000' }
            });
            // OSC 52 clipboard bridge (#153). Registered HERE — after the
            // Terminal exists, before term.open(body) — because parser
            // registration does not depend on DOM attachment, and the invariant
            // worth having is "the handler exists before this terminal can
            // receive a byte". `term.parser` is stable API in the vendored
            // 5.3.0 build: its getter does NOT call _checkProposedApi() (unlike
            // `get unicode()`), so this is not proposed API behind a flag.
            //
            // Registering inside openWindow is also what keeps recorder
            // PLAYBACK inert: the other two `new Terminal(` sites in the tree
            // are both in mods/recorder/recorder.js, so replaying a recording
            // can never write your clipboard.
            //
            // ALWAYS returns true, so the handler terminates the chain. xterm
            // tries OSC handlers newest-first with fallthrough, and returning
            // false would hand a clipboard request to whatever registered
            // earlier. Disposal below is load-bearing for the same reason: a
            // reopened terminal must not stack duplicate handlers.
            //
            // Cost of registering at all, checked in the vendored bundle rather
            // than assumed: an UNTERMINATED OSC 52 now buffers, where before
            // registration the parser took the no-handler path and accumulated
            // nothing. It is bounded, though — OscHandler.put() zeroes _data and
            // latches _hitLimit once it passes PAYLOAD_LIMIT (1e7), and a
            // limit-hit sequence never reaches this callback at all. So the
            // worst case is a transient buffer, self-clearing, not a leak.
            // A closure variable, not a field on `win` (which is built below):
            // the request identity must not be reachable — or forgeable — from
            // anything the PTY can influence, and this also keeps the handler
            // free of a temporal-dead-zone reference to a `const` declared
            // later in the same scope.
            //
            // Stamped from TRUSTED DOM EVENTS ONLY, and specifically NOT from
            // term.onData. onData looks like the obvious seam and is the wrong
            // one: it carries xterm's OWN automatic replies as well as the
            // user's typing, so a program could emit a DA1 query (`ESC[c`),
            // collect xterm's `ESC[?1;2c` answer through onData, and have
            // stamped the activity gate by itself — then send its clipboard
            // request one byte later. Listening for user input directly closes
            // that. Several event types rather than keydown alone, because a
            // key-only check rejects touch and mouse-driven copies and misfires
            // around IME composition.
            let lastUserInputAt = 0;
            const markUserInput = () => { lastUserInputAt = Date.now(); };
            const onUserInputEv = (e) => { if (e.isTrusted) markUserInput(); };
            const oscDisp = term.parser.registerOscHandler(52, (data) => {
                osc52Request({
                    hostId: hostId, sid: sid, winId: id,
                    lastInputAt: lastUserInputAt,
                }, data);
                return true;
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

            // #153: the OSC 52 registration is disposed with the window. Not
            // hygiene — xterm tries OSC handlers newest-first with fallthrough,
            // so a leaked registration from a reopened terminal would stack.
            win.cleanups.push(() => { try { oscDisp.dispose(); } catch (_) {} });

            // #153: the activity stamp the OSC 52 gate reads. Capture phase, so
            // it lands whether or not the app or xterm consumes the event.
            // passive: it only ever reads a clock, and `wheel`/`touchstart`
            // default to NON-passive on a plain element — declaring it keeps
            // this off the scroll-blocking path next to the existing
            // Shift+wheel handler.
            const USER_INPUT_EVENTS = ['keydown', 'mousedown', 'mouseup',
                'touchstart', 'wheel', 'paste', 'compositionend'];
            const USER_INPUT_OPTS = { capture: true, passive: true };
            for (const t of USER_INPUT_EVENTS) {
                dom.addEventListener(t, onUserInputEv, USER_INPUT_OPTS);
            }
            win.cleanups.push(() => {
                for (const t of USER_INPUT_EVENTS) {
                    try {
                        dom.removeEventListener(t, onUserInputEv, { capture: true });
                    } catch (_) {}
                }
            });

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

            // ---- ConPTY bracketed-paste gap (#138) ----------------------
            // Windows ConPTY does not forward an app's DECSET 2004 request
            // to the terminal-side stream (verified live), so xterm never
            // learns that Claude Code wants bracketed pastes — term.paste()
            // then submits a multiline block at the first CR. Claude Code
            // parses ESC[200~ regardless (it asked for the mode; the request
            // just died inside ConPTY), so when it is the detected foreground
            // agent (/sessions poll) and xterm says the mode is off, wrap the
            // paste by hand. An xterm that DID see ?2004h (POSIX agents, a
            // ConPTY that forwards it) always wins — term.paste() brackets
            // natively and the wrap stays out of the way. Empirical
            // workaround scoped to agents VERIFIED to parse brackets
            // unconditionally — do not add one untested.
            const BRACKET_GAP_AGENTS = { claude: true };
            const needsConptyPasteWrap = () => {
                if (term.modes.bracketedPasteMode) return false;
                const sess = sessions.get(String(win.id));
                // Own-property test, not a bare index: sess.agent now also
                // arrives straight off a JSON `agent` frame (#156), and an
                // inherited key ('constructor', 'toString') would read truthy
                // and wrap a paste. The map's CONTENTS are untouched — #138's
                // "do not extend untested" rule is about which agents are
                // listed, not how the lookup is spelled.
                return !!(sess && Object.prototype.hasOwnProperty.call(
                    BRACKET_GAP_AGENTS, sess.agent));
            };
            const pasteTextToTerm = (text) => {
                if (needsConptyPasteWrap()) {
                    // Strip paste terminators (ESC[201~ and the C1-CSI form)
                    // so pasted content can't break out of the bracket, and
                    // normalize newlines to CR — same prep xterm's paste()
                    // applies natively.
                    const safe = String(text)
                        .replace(/\x1b\[201~|\x9b201~/g, '')
                        .replace(/\r\n|\n/g, '\r');
                    sendChunked('input', '\x1b[200~' + safe + '\x1b[201~');
                    return;
                }
                term.paste(text);
            };

            // ---- clipboard-image paste (#137) ---------------------------
            // An image on the BROWSER's clipboard can't reach the PTY app
            // directly (the agent's S4U window station has its own, empty
            // clipboard): upload the blob to this terminal's HOST broker and
            // paste the returned file path — Claude Code attaches a pasted
            // image path exactly like drag-and-drop. One in-flight upload
            // per window; the busy flag clears in finally and the POST aborts
            // after 30 s, so a hung upload can never wedge the paste paths.
            const pasteImageBlob = async (blob) => {
                if (win._pasteImageBusy) {
                    showNotice('image paste already in progress');
                    return;
                }
                if (blob.size > 5 * 1024 * 1024) {
                    showNotice('image too large to paste (max 5 MiB)',
                        { sticky: true, type: 'error' });
                    return;
                }
                win._pasteImageBusy = true;
                showNotice('Uploading image…', 2000);
                try {
                    const b64 = await blobToBase64(blob);
                    // 30 s, not the 3 s hostFetch default: this POST carries a
                    // whole image (up to 5 MiB of base64) and must survive a
                    // slow link.
                    const resp = await hostFetch(
                        hostById(win.hostId), '/file/paste_image',
                        {
                            method: 'POST',
                            headers: { 'Content-Type': 'application/json' },
                            body: JSON.stringify({ content_b64: b64 }),
                            timeoutMs: 30000,
                        });
                    if (resp.status === 401) {
                        // Same heal path as a window-runtime 4401: flag the
                        // host and pop the login (or its amber chip).
                        showNotice("image paste needs this broker's password",
                            { sticky: true, type: 'error' });
                        try {
                            pollStateFor(win.hostId).authNeeded = true;
                            showAuthOverlay(hostById(win.hostId));
                        } catch (_) {}
                        return;
                    }
                    if (resp.status === 404) {
                        showNotice('this broker predates image paste — '
                            + 'update it', { sticky: true, type: 'error' });
                        return;
                    }
                    let data = null;
                    try { data = await resp.json(); } catch (_) {}
                    if (!resp.ok || !data || !data.ok || !data.path) {
                        showNotice('image paste failed: '
                            + ((data && data.error) || ('HTTP ' + resp.status)),
                            { sticky: true, type: 'error' });
                        return;
                    }
                    // Trailing space separates the path from whatever the
                    // user types next; bracketed paste (when the app asked
                    // for it) keeps the injection from submitting.
                    const injected = quotePathForPrompt(data.path) + ' ';
                    pasteTextToTerm(injected);
                    _notifyClipboard('in', injected);   // #106 history
                } catch (err) {
                    showNotice('image paste failed: '
                        + ((err && (err.name === 'TimeoutError'
                                || err.name === 'AbortError'))
                            ? 'upload timed out' : err),
                        { sticky: true, type: 'error' });
                } finally {
                    win._pasteImageBusy = false;
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
                    // Image-aware read first (#137) where clipboard.read()
                    // exists. TEXT WINS (readClipboardImageBlob returns null
                    // whenever a text/plain item is present), so the text
                    // path below stays the common case; a failed read() falls
                    // back to the plain readText path unchanged.
                    if (canReadClipboardItems()) {
                        let blob = null;
                        try { blob = await readClipboardImageBlob(); }
                        catch (_) { blob = null; }
                        if (blob) { pasteImageBlob(blob); return; }
                    }
                    try {
                        const text = await navigator.clipboard.readText();
                        if (text) {
                            pasteTextToTerm(text);
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
                    if (t) {
                        _notifyClipboard('in', t);
                        // ConPTY 2004 gap (#138): when the wrap applies, take
                        // the paste over so it goes out hand-bracketed —
                        // xterm's own textarea handler would send it raw and
                        // Claude Code would submit at the first CR. Everywhere
                        // else the event falls through to xterm untouched.
                        if (needsConptyPasteWrap()) {
                            e.preventDefault();
                            e.stopPropagation();
                            pasteTextToTerm(t);
                        }
                        return;
                    }
                    // No text on the clipboard: look for an image file (#137).
                    // The gesture-scoped clipboardData carries it even on
                    // plain http — the one image path needing no secure
                    // context. preventDefault so xterm never sees the (empty)
                    // text paste; text-bearing events fall through untouched.
                    const items = (e.clipboardData && e.clipboardData.items)
                        || [];
                    for (let i = 0; i < items.length; i++) {
                        const it = items[i];
                        if (it.kind === 'file'
                                && it.type.indexOf('image/') === 0) {
                            const f = it.getAsFile();
                            if (f) {
                                e.preventDefault();
                                e.stopPropagation();
                                pasteImageBlob(f);
                            }
                            return;
                        }
                    }
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

            // Combined custom key handler — xterm keeps only ONE, so every
            // chord lives here.
            // Ctrl+Shift+C: explicit copy. Returning false prevents xterm
            // from forwarding the chord (which would otherwise reach the
            // producer as ^C on most layouts). Plain Ctrl+C falls through
            // unchanged.
            // Alt+V (#137): probe the clipboard for an image and upload it.
            // clipboard.read() needs a secure context, so elsewhere the chord
            // falls through to xterm untouched (status quo — the app gets
            // ESC v). With no image, or a failed read, the async path sends
            // the byte-identical ESC v itself, so Claude Code's own image
            // hotkey still fires — just a beat later.
            const handleAltVPaste = async () => {
                let blob = null;
                try { blob = await readClipboardImageBlob(); }
                catch (_) { blob = null; }
                if (blob) pasteImageBlob(blob);
                else sendChunked('input', '\x1bv');
            };
            // Ctrl+Shift+V (#153): the symmetry partner of the Ctrl+Shift+C
            // branch below. It already worked BY ACCIDENT in Chrome and Firefox
            // — their native paste-as-plain-text fires a DOM `paste`, which
            // onClipPaste picks up — so this is not a fix for brokenness; it
            // closes the cases that accident misses (macOS/Safari, and a
            // terminal whose xterm textarea does not have focus) and makes the
            // chord layout-independent. Routed through pasteTextToTerm so
            // #138's ConPTY hand-bracketing applies exactly as on every other
            // paste path.
            const handleCtrlShiftVPaste = async () => {
                try {
                    const text = await navigator.clipboard.readText();
                    if (text) {
                        pasteTextToTerm(text);
                        _notifyClipboard('in', text);   // #106 history
                    }
                } catch (err) {
                    console.error('paste read failed:', err);
                }
            };
            term.attachCustomKeyEventHandler(ev => {
                if (ev.type !== 'keydown') return true;
                const key = (ev.key || '').toLowerCase();
                if (ev.ctrlKey && ev.shiftKey && key === 'c') {
                    const sel = term.getSelection();
                    if (sel) copyTextToClipboard(sel);
                    ev.preventDefault();
                    ev.stopPropagation();
                    return false;
                }
                if (ev.ctrlKey && ev.shiftKey && key === 'v') {
                    // On a plain-http LAN origin navigator.clipboard does not
                    // exist. Fall THROUGH rather than swallow the chord, so the
                    // browser's own paste-as-plain-text still lands via
                    // onClipPaste — taking it over there would remove the only
                    // working path on that origin.
                    if (!canReadClipboard()) return true;
                    ev.preventDefault();
                    ev.stopPropagation();
                    handleCtrlShiftVPaste();
                    return false;
                }
                if (ev.altKey && !ev.ctrlKey && !ev.metaKey && !ev.shiftKey
                        && key === 'v' && canReadClipboardItems()) {
                    ev.preventDefault();
                    ev.stopPropagation();
                    handleAltVPaste();
                    return false;
                }
                return true;
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
            } else {
                // Float: stamp workspace membership and mask NOW, in the frame
                // the window is created (#152), so a terminal reattached into a
                // parked workspace never paints before it hides. Masked means
                // display:none, but sendResize bails on a 0x0 box and the
                // hidden->visible transition refits, so nothing is measured
                // wrong — it is measured when the workspace is shown.
                adoptFloatWorkspace(win);
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

