        // ---- notices ------------------------------------------------------
        // Back-compat: showNotice(text) / showNotice(text, ms) auto-dismiss
        // after ms (default 4000). An options object { ms, sticky, type } makes
        // a sticky error (type:'error') that renders a × close button and NEVER
        // auto-dismisses — used for terminate failures the user must see and act
        // on (#64), since the old 4s toast vanished before it could be read.
        const STICKY_NOTICE_MAX = 4;
        function showNotice(text, opts) {
            const host = document.getElementById('notice-host');
            let ms = 4000, sticky = false, type = null;
            if (typeof opts === 'number') {
                ms = opts;                                  // legacy (text, ms)
            } else if (opts && typeof opts === 'object') {
                if (typeof opts.ms === 'number') ms = opts.ms;
                sticky = !!opts.sticky;
                type = opts.type || null;
            }
            if (sticky) {
                // Dedupe: identical sticky text already up -> don't stack it
                // again (repeated Terminate clicks must not pile up).
                const dup = Array.from(host.querySelectorAll('.notice-sticky'))
                    .find(n => n._noticeText === text);
                if (dup) return;
                const div = document.createElement('div');
                div.className = 'notice notice-sticky'
                    + (type === 'error' ? ' notice-error' : '');
                div._noticeText = text;
                const span = document.createElement('span');
                span.textContent = text;
                div.appendChild(span);
                const close = document.createElement('button');
                close.type = 'button';
                close.className = 'notice-close';
                close.textContent = '×';
                close.title = 'dismiss';
                close.addEventListener('click', () => div.remove());
                div.appendChild(close);
                host.appendChild(div);
                // Cap accumulated sticky notices — drop the oldest beyond MAX.
                const all = host.querySelectorAll('.notice-sticky');
                for (let i = 0; i < all.length - STICKY_NOTICE_MAX; i++) {
                    all[i].remove();
                }
                return;
            }
            const div = document.createElement('div');
            div.className = 'notice' + (type === 'error' ? ' notice-error' : '');
            div.textContent = text;
            host.appendChild(div);
            setTimeout(() => { div.remove(); }, ms);
        }

        // ---- z-order ------------------------------------------------------
        // #262: an open or arrange the user did not ask for (an MCP launch,
        // a relayed desktop op other than `focus`) must never take keyboard
        // focus: whatever had it keeps it, lastTermId and frontId do not
        // move, and the strip does not scroll to the window. withoutFocus
        // runs fn with bringToFront (and layoutAddColumn's focusedCol) held
        // QUIET for `keys` only -- every other window focuses as usual --
        // and keeps them quiet a little longer, for the bringToFront a
        // mutator defers to the next frame (a tab switch re-enters after its
        // relayout): until two frames have passed or QUIET_RELEASE_MS has,
        // whichever is first, so a hidden tab (rAF paused) releases too. A
        // quiet float still stacks. Quiet never swallows a focus somebody
        // asked for: the `focus` op releases its key first
        // (clearFocusQuiet), and a click or key press inside a quiet window
        // releases that window. A mutator that MOVES the focused window's
        // node (float <-> tile reparents it) makes the browser drop focus to
        // <body>; the element that had focus gets it back, at the end of fn
        // and again at the release -- but never once focus moved somewhere
        // else (a deliberate blur to <body> inside that short window is
        // undone too: accepted). Synchronous by contract: fn must not await.
        // Accepted: under main-thread jank the 50 ms timer can release before
        // the deferred frame lands (worst case one unwanted focus).
        const QUIET_RELEASE_MS = 50;
        // key -> the Set of scopes holding it quiet. Per SCOPE, not a count:
        // a scope's release drops only its own token, so a release that
        // runs after clearFocusQuiet (or a user event) emptied the key can
        // never cut short a newer scope on the same key.
        const _quietKeys = new Map();
        function withoutFocus(keys, fn) {
            const held = (keys || []).filter(k => typeof k === 'string');
            const token = {};
            for (const k of held) {
                if (!_quietKeys.has(k)) _quietKeys.set(k, new Set());
                _quietKeys.get(k).add(token);
            }
            const prev = document.activeElement;
            const giveBack = () => {
                try {
                    const now = document.activeElement;
                    if (prev && prev !== document.body && prev !== now
                            && prev.isConnected
                            && (!now || now === document.body)) {
                        prev.focus({ preventScroll: true });
                    }
                } catch (_) {}
            };
            let released = false;
            const release = () => {
                if (released) return;
                released = true;
                for (const k of held) {
                    const scopes = _quietKeys.get(k);
                    if (!scopes) continue;
                    scopes.delete(token);
                    if (!scopes.size) _quietKeys.delete(k);
                }
                giveBack();
            };
            try {
                return fn();
            } finally {
                giveBack();
                requestAnimationFrame(() => requestAnimationFrame(release));
                setTimeout(release, QUIET_RELEASE_MS);
            }
        }
        function focusIsQuiet(id) {
            return _quietKeys.has(String(id));
        }
        // Releases every scope holding THIS key now (the focus op asked for
        // it); other keys, and scopes opened on this key later, keep theirs.
        function clearFocusQuiet(id) {
            _quietKeys.delete(String(id));
        }
        // The user acting INSIDE a quiet window (a click, a key press) asked
        // for it: release that window only, before its own handlers run.
        // Accepted: the capture flag is unpinned, and mousedown is a belt for
        // a page without pointer events (pointerdown precedes it elsewhere).
        function _releaseQuietAt(ev) {
            if (!_quietKeys.size || !ev || !ev.target) return;
            for (const k of Array.from(_quietKeys.keys())) {
                const win = windows.get(k);
                try {
                    if (win && win.dom && win.dom.contains(ev.target)) {
                        _quietKeys.delete(k);
                    }
                } catch (_) {}
            }
        }
        if (typeof document !== 'undefined' && document.addEventListener) {
            document.addEventListener('pointerdown', _releaseQuietAt, true);
            document.addEventListener('mousedown', _releaseQuietAt, true);
            document.addEventListener('keydown', _releaseQuietAt, true);
        }
        // Focus a window's content: the xterm for terminals, the textarea for
        // app windows. Tolerant of either being absent.
        function focusWin(win) {
            try {
                // #239: a controlled window's keys go nowhere (it has no
                // socket), so focus lands on its one action instead, or on the
                // overlay while that button is busy.
                if (win.controlled) {
                    const b = win.ctlBtn;
                    if (b && b.isConnected && !b.disabled) b.focus();
                    else if (win.ctlOverlay) win.ctlOverlay.focus();
                    return;
                }
                if (win.term) win.term.focus();
                // Editors/notes expose focusEditor (routes to the CodeMirror view
                // when one is mounted, else the textarea); fall back to win.body.
                else if (win.focusEditor) win.focusEditor();
                else if (win.body && win.body.focus) win.body.focus();
            } catch (_) {}
        }
        function bringToFront(id, _retry) {
            const win = windows.get(id);
            if (!win || win.minimized || hostHidden(win.hostId)) return;
            // A floating window masked off the active workspace (task 8) can't
            // take focus — its taskbar click switches workspace first.
            if (!win.tiled && win.dom.classList.contains('ws-hidden')) return;
            // #262: quiet -- a float still stacks; nothing else moves.
            if (focusIsQuiet(id)) {
                if (!win.tiled) win.dom.style.zIndex = String(floatZIndex(win));
                return;
            }
            // Remember the active TERMINAL (not an app window) so the file tools
            // can open "where I am" — at its cwd, on its host (#35).
            if (win.type !== 'app') lastTermId = id;
            if (win.tiled) {
                // Tiled windows live in normal flow (z-index is inert on a
                // static box): "front" means make their column the focused one
                // and scroll it into view. Focus follows.
                const loc = findKeyInLayout(id);
                // Tabbed tile: focusing a window that is currently a hidden
                // (inactive) tab must first make it the active tab and relayout,
                // else focus lands on a display:none node. Re-enter after the
                // relayout frame, when it is the active visible tab.
                if (loc && loc.row.mode === 'tabbed' && loc.row.keys.length > 1
                    && loc.row.activeTab !== id) {
                    // Already re-entered once and STILL not the active tab: a
                    // sibling tab's own bringToFront won the activeTab (e.g. all
                    // tabs grabbing focus as they reconnect on reload). Stop here
                    // — re-entering again would ping-pong activeTab between
                    // siblings forever, relayouting every frame and detaching the
                    // tab strip so real clicks never land. Concede focus quietly.
                    if (_retry) return;
                    loc.row.activeTab = id;
                    loc.desktop.focusedCol = visibleColIndex(loc.col);
                    savePrefs();
                    requestRelayout();
                    requestAnimationFrame(() => bringToFront(id, true));
                    return;
                }
                // (F-NESTSPLIT) Same reveal for a key that is a HIDDEN tab inside a
                // split row's GROUP cell: make it the cell's active tab and relayout
                // first, else focus lands on a display:none node. Same _retry guard
                // against activeTab ping-pong between sibling reconnecting tabs.
                if (loc && loc.cell && Array.isArray(loc.cell.keys)
                    && loc.cell.keys.length > 1 && loc.cell.activeTab !== id) {
                    if (_retry) return;
                    loc.cell.activeTab = id;
                    loc.desktop.focusedCol = visibleColIndex(loc.col);
                    savePrefs();
                    requestRelayout();
                    requestAnimationFrame(() => bringToFront(id, true));
                    return;
                }
                // Only when the column is actually ON SCREEN: a filtered-out
                // column (the workspaces mod hides the other workspaces') has
                // no visible slot to focus or scroll to (#148).
                const vci = loc ? visibleColIndex(loc.col) : -1;
                if (vci !== -1) {
                    loc.desktop.focusedCol = vci;
                    savePrefs();
                    scrollColumnIntoView(vci, true);
                }
                frontId = id;
                updateTaskbarActive();
                focusWin(win);
                return;
            }
            win.dom.style.zIndex = String(floatZIndex(win));
            frontId = id;
            updateTaskbarActive();
            focusWin(win);
        }
        function updateTaskbarActive() {
            document.querySelectorAll('.taskbar-item').forEach(el => {
                const id = el.dataset.sessionId;
                const win = windows.get(id);
                el.classList.toggle('active', !!(win && !win.minimized && id === frontId));
                el.classList.toggle('minimized', !!(win && win.minimized));
                // #226: out in its own browser window (a click focuses it).
                el.classList.toggle('detached', !!(win && win.detached));
                // #239: another device drives it. From the live window when
                // one is open, else the record, so a chip with no window (and
                // one the poll rebuilt) is marked too: every tick ends here.
                el.classList.toggle('controlled', !!controlledViewFor(id));
            });
        }

