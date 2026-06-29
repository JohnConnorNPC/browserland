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
            const color = normalizeHex(pref.color || defaultColor(id));
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

            // Open this folder's AGENTS.md in a dedicated editor (Task 12).
            const agentsMdBtn = document.createElement('button');
            agentsMdBtn.type = 'button';
            agentsMdBtn.className = 'tb-btn btn-agentsmd';
            agentsMdBtn.textContent = '📋';
            agentsMdBtn.title = 'Edit AGENTS.md for this folder';

            // Git status button (Task 6): shows this terminal's working-dir git
            // branch + a dirty badge; click opens a status popover. Starts muted
            // (no status known yet); hidden when the cwd is not a repo or the
            // broker is too old to have /session/git. Branch label sits to its
            // right; both are managed by refreshGit/renderGit below.
            const gitBtn = document.createElement('button');
            gitBtn.type = 'button';
            gitBtn.className = 'tb-btn btn-git muted';
            gitBtn.textContent = '⎇';
            gitBtn.title = 'Git status';
            const gitLabel = document.createElement('span');
            gitLabel.className = 'git-label';

            const minBtn = document.createElement('button');
            minBtn.type = 'button';
            minBtn.className = 'tb-btn btn-min';
            minBtn.textContent = '_';
            minBtn.title = 'minimize';

            const closeBtn = document.createElement('button');
            closeBtn.type = 'button';
            closeBtn.className = 'tb-btn btn-close';
            closeBtn.textContent = '×';
            closeBtn.title = 'close';

            titleBar.appendChild(idBadge);
            titleBar.appendChild(titleText);
            titleBar.appendChild(agentsMdBtn);
            titleBar.appendChild(gitBtn);
            titleBar.appendChild(gitLabel);
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
                fontFamily: terminalFontFamily(),   // #18: configurable
                fontSize: 14,
                theme: { background: '#000000' }
            });
            const fitAddon = new FitAddon.FitAddon();
            term.loadAddon(fitAddon);
            term.open(body);

            const win = {
                id, sid, hostId, dom, body, term, fitAddon,
                titleText,
                // Git status (task 6): gitBtn/gitLabel are the title-bar UI;
                // gitStatus the last successful {ok:true,...} payload (or null);
                // gitState one of 'unknown'|'repo'|'norepo'|'unavailable' so the
                // button can render muted/hidden without a status toast.
                gitBtn, gitLabel, gitStatus: null, gitState: 'unknown',
                gitPopover: null, gitFetching: false, gitTimer: null, gitSeq: 0,
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
            const onCloseClick = (e) => { e.stopPropagation(); closeWindow(id); };
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

            // AGENTS.md: open (or focus) a dedicated editor for THIS terminal's
            // working dir, keyed by cwd so re-clicking reuses the same window.
            // openAgentsMdEditor handles the sandbox/new-file cases + the
            // CLAUDE.md + template wiring.
            const onAgentsMdClick = (e) => {
                e.stopPropagation();
                openAgentsMdEditor(id);
            };
            agentsMdBtn.addEventListener('mousedown', stopProp);
            agentsMdBtn.addEventListener('click', onAgentsMdClick);
            win.cleanups.push(() => {
                agentsMdBtn.removeEventListener('mousedown', stopProp);
                agentsMdBtn.removeEventListener('click', onAgentsMdClick);
            });

            // ---- Git status (task 6) --------------------------------------
            // Host-aware POST /session/git {id:<wireId>}: the AGENT runs git in
            // its own live cwd, so we send only the bare wire id. Wrapped to a
            // {status, json} result so a non-repo / 404 / network error never
            // throws or toasts on a routine terminal.
            const gitPost = () => {
                const host = hostById(win.hostId);
                if (!host) {
                    return Promise.resolve(
                        { status: 0, json: { ok: false, error: 'no_host' } });
                }
                return fetch(hostHttpUrl(host, '/session/git'), {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ id: win.sid }),
                }).then(r => r.json()
                    .then(j => ({ status: r.status, json: j }))
                    .catch(() => ({ status: r.status,
                                    json: { ok: false, error: 'HTTP ' + r.status } })))
                 .catch(e => ({ status: 0, json: { ok: false, error: String(e) } }));
            };
            // Paint the button + label from win.gitState/gitStatus. Muted when
            // not-a-repo / unknown; HIDDEN when the route is unavailable (404 on
            // an old broker). Branch name + a dirty badge ride alongside.
            const renderGit = () => {
                if (win.disposed) return;
                if (win.gitState === 'unavailable') {
                    gitBtn.style.display = 'none';
                    gitLabel.style.display = 'none';
                    return;
                }
                gitBtn.style.display = '';
                const st = win.gitStatus;
                const isRepo = win.gitState === 'repo' && st && st.ok;
                gitBtn.classList.toggle('muted', !isRepo);
                if (!isRepo) {
                    gitLabel.style.display = 'none';
                    gitLabel.textContent = '';
                    gitBtn.title = (win.gitState === 'norepo')
                        ? 'Git: not a repository' : 'Git status';
                    return;
                }
                const branch = st.detached ? 'detached'
                    : (st.branch || '(no branch)');
                gitLabel.style.display = '';
                gitLabel.textContent = branch;
                gitLabel.classList.toggle('git-dirty', !!st.dirty);
                // A small dirty badge: the change count when known, else a dot.
                let badge = '';
                if (st.dirty) {
                    badge = (typeof st.dirty_count === 'number'
                             && st.dirty_count > 0)
                        ? (' ●' + st.dirty_count) : ' ●';
                }
                gitLabel.textContent = branch + badge;
                const ab = [];
                if (st.ahead) ab.push('↑' + st.ahead);
                if (st.behind) ab.push('↓' + st.behind);
                gitBtn.title = 'Git: ' + branch
                    + (ab.length ? (' ' + ab.join(' ')) : '')
                    + (st.dirty ? ' (dirty)' : ' (clean)');
                // If the popover is open, keep it in sync with the new status.
                if (win.gitPopover) fillGitPopover();
            };
            // Fetch + classify. Never throws. 404/no route -> 'unavailable'
            // (hide forever this session); not_a_repo/no_cwd -> 'norepo'
            // (muted); ok -> 'repo'.
            const refreshGit = async () => {
                if (win.disposed || win.gitFetching) return;
                win.gitFetching = true;
                // Monotonic token: a slow earlier reply must not paint over a
                // newer one (gitFetching blocks overlap from THIS caller, but
                // the token is the durable guard).
                const seq = ++win.gitSeq;
                let res;
                try { res = await gitPost(); }
                catch (_) { res = { status: 0, json: { ok: false } }; }
                finally { win.gitFetching = false; }
                if (win.disposed || seq !== win.gitSeq) return;
                const j = res.json || {};
                if (res.status === 404) {
                    win.gitState = 'unavailable';
                    // Old broker without the route: stop the keep-alive poll —
                    // it will only keep 404ing. The button stays hidden.
                    if (win.gitTimer) {
                        clearInterval(win.gitTimer); win.gitTimer = null;
                    }
                } else if (j.ok) {
                    win.gitState = 'repo';
                    win.gitStatus = j;
                } else if (j.error === 'not_a_repo' || j.error === 'no_cwd') {
                    win.gitState = 'norepo';
                    win.gitStatus = null;
                } else {
                    // Transient/unknown error: stay muted, don't toast, don't
                    // hide (a later refresh may succeed).
                    if (win.gitState === 'unknown') win.gitState = 'norepo';
                }
                renderGit();
            };
            // Status popover anchored under the button: branch/detached, ahead/
            // behind, the four index counts, + a Refresh button. Closes on
            // outside-click / Escape; registered for teardown in win.cleanups.
            const fillGitPopover = () => {
                const pop = win.gitPopover;
                if (!pop) return;
                const st = win.gitStatus;
                pop.innerHTML = '';
                const head = document.createElement('div');
                head.className = 'git-pop-head';
                if (win.gitState === 'repo' && st && st.ok) {
                    head.textContent = st.detached
                        ? 'detached HEAD' : (st.branch || '(no branch)');
                } else if (win.gitState === 'norepo') {
                    head.textContent = 'not a git repository';
                } else {
                    head.textContent = 'git status unavailable';
                }
                pop.appendChild(head);
                if (win.gitState === 'repo' && st && st.ok) {
                    const ab = document.createElement('div');
                    ab.className = 'git-pop-row';
                    ab.textContent = 'ahead ↑' + (st.ahead || 0)
                        + '   behind ↓' + (st.behind || 0);
                    pop.appendChild(ab);
                    const counts = [
                        ['staged', st.staged], ['unstaged', st.unstaged],
                        ['untracked', st.untracked], ['conflicts', st.conflicts],
                    ];
                    for (const [k, v] of counts) {
                        const r = document.createElement('div');
                        r.className = 'git-pop-row';
                        r.textContent = k + ': ' + (v || 0);
                        if (k === 'conflicts' && v) r.classList.add('git-bad');
                        pop.appendChild(r);
                    }
                    const dirty = document.createElement('div');
                    dirty.className = 'git-pop-row '
                        + (st.dirty ? 'git-dirty' : '');
                    dirty.textContent = st.dirty
                        ? ('dirty (' + (st.dirty_count || 0) + ')') : 'clean';
                    pop.appendChild(dirty);
                }
                const foot = document.createElement('div');
                foot.className = 'git-pop-foot';
                const refreshBtn = document.createElement('button');
                refreshBtn.type = 'button';
                refreshBtn.className = 'tb-btn';
                refreshBtn.style.width = 'auto';
                refreshBtn.style.padding = '0 8px';
                refreshBtn.textContent = 'Refresh';
                refreshBtn.addEventListener('mousedown', stopProp);
                refreshBtn.addEventListener('click', (e) => {
                    e.stopPropagation();
                    refreshGit();
                });
                foot.appendChild(refreshBtn);
                pop.appendChild(foot);
            };
            const closeGitPopover = () => {
                if (!win.gitPopover) return;
                document.removeEventListener('mousedown', onGitOutside, true);
                document.removeEventListener('keydown', onGitKey, true);
                try { win.gitPopover.remove(); } catch (_) {}
                win.gitPopover = null;
            };
            const onGitOutside = (e) => {
                // The button + its branch label form one affordance: a click on
                // either is "inside" (the button toggles, the popover stays).
                if (win.gitPopover && !win.gitPopover.contains(e.target)
                    && e.target !== gitBtn && e.target !== gitLabel) {
                    closeGitPopover();
                }
            };
            const onGitKey = (e) => {
                if (e.key === 'Escape') {
                    e.preventDefault(); e.stopPropagation(); closeGitPopover();
                }
            };
            const openGitPopover = () => {
                if (win.gitPopover) { closeGitPopover(); return; }
                const pop = document.createElement('div');
                pop.className = 'git-popover';
                titleBar.appendChild(pop);
                win.gitPopover = pop;
                fillGitPopover();
                // Anchor under the button within the title bar (relative parent).
                pop.style.left = Math.max(0, gitBtn.offsetLeft) + 'px';
                pop.style.top = (gitBtn.offsetTop + gitBtn.offsetHeight + 2) + 'px';
                document.addEventListener('mousedown', onGitOutside, true);
                document.addEventListener('keydown', onGitKey, true);
                // Always refresh on open (cheap, keeps the popover live).
                refreshGit();
            };
            const onGitClick = (e) => {
                e.stopPropagation();
                openGitPopover();
            };
            gitBtn.addEventListener('mousedown', stopProp);
            gitBtn.addEventListener('click', onGitClick);
            win.cleanups.push(() => {
                gitBtn.removeEventListener('mousedown', stopProp);
                gitBtn.removeEventListener('click', onGitClick);
                closeGitPopover();
                if (win.gitTimer) { clearInterval(win.gitTimer); win.gitTimer = null; }
            });
            // Initial fetch shortly after open, plus a slow keep-alive poll.
            // Both best-effort; refreshGit never throws and self-guards on
            // disposed. Each call runs a git subprocess on the agent, so keep
            // the interval slow.
            setTimeout(() => { if (!win.disposed) refreshGit(); }, 800);
            win.gitTimer = setInterval(() => {
                if (!win.disposed) refreshGit();
            }, 15000);

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
            // the seamless one-click paste.
            if (canReadClipboard()) {
                const onContext = async (e) => {
                    e.preventDefault();
                    if (!win.ws || win.ws.readyState !== WebSocket.OPEN) return;
                    try {
                        const text = await navigator.clipboard.readText();
                        if (text) {
                            sendChunked('paste', text);
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

