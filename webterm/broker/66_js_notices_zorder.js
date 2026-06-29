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
        // Focus a window's content: the xterm for terminals, the textarea for
        // app windows. Tolerant of either being absent.
        function focusWin(win) {
            try {
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
                    loc.ws.focusedCol = loc.colIndex;
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
                    loc.ws.focusedCol = loc.colIndex;
                    savePrefs();
                    requestRelayout();
                    requestAnimationFrame(() => bringToFront(id, true));
                    return;
                }
                if (loc && loc.wsIndex === getLayout().activeWs) {
                    loc.ws.focusedCol = loc.colIndex;
                    savePrefs();
                    scrollColumnIntoView(loc.colIndex, true);
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
            });
        }

