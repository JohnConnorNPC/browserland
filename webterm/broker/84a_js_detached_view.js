        // ---- detached terminal view (#226) ----------------------------------
        // "Open in new window": one terminal leaves the desktop for a browser
        // window of its own. The PTY never moves — the broker already fans one
        // terminal out to any number of /ws subscribers — so the whole feature
        // is about which PAGE is allowed to be attached, and the answer is
        // exactly one at a time. Two attached xterms are two WRITERS: xterm
        // answers DA/DSR/CPR queries through term.onData, which core forwards
        // to the PTY unconditionally, and relay.py accepts input from both
        // sockets because both carry the same CLIENT_ID. Hiding the desktop
        // copy would not stop it replying, so a TUI would see every query
        // answered twice.
        //
        // Two surfaces share this fragment (SURFACE, 50):
        //   desktop  — detachWindow() opens the child and nothing else. When
        //              the child CLAIMS the terminal the desktop closes its own
        //              socket, marks the window `detached`, minimizes it
        //              (column membership and the taskbar chip are retained;
        //              the resize guard already skips a minimized window) and
        //              only then answers `released`, so the child attaches to
        //              a PTY nobody else is attached to. Every un-minimize
        //              path funnels through restoreWindow, which focuses the
        //              child instead. Return = reattach + reveal.
        //   detached — `?detach=<hostId:sid>` boots bootDetachedView(): no
        //              taskbar, no restore queue, no app windows, no shared-
        //              state writes (savePrefs/pushState are no-ops on this
        //              surface, 51/52), the layout is forced floating, and the
        //              one window is fitted to the viewport and OWNS the PTY
        //              size.
        //
        // Ownership is a Web Lock, not a heartbeat. The child holds
        // `bl-detach:<brokerId:sid>` for its whole document life; the browser
        // releases it on close, crash, discard or navigation, a document
        // holding one is never bfcache-eligible, and acquisition is atomic
        // across tabs. So the desktop learns the child is gone by WAITING on
        // the lock, a second child for the same terminal fails to acquire and
        // says so, and a desktop that (re)loads asks navigator.locks.query()
        // before it attaches. The name is the BROKER's identity plus the bare
        // wire id, not the host alias: two host records can address the same
        // broker, and one PTY must have one lock. Without navigator.locks (a
        // plain-http page that is not localhost) the feature is off — there
        // is no honest way to promise a single writer with heartbeats.
        //
        // The BroadcastChannel carries the handshake only: claim / released /
        // ready / bye / return / focus. Every frame names the canonical key
        // and the sender's per-document instance id; the desktop acts on
        // `bye` only from the instance it yielded to, the child acts on
        // `return` only while it is the owner, and a late frame from an
        // earlier generation of the same terminal is ignored.
        const DETACH_CHANNEL_NAME = 'bl-detach';
        const DETACH_INST = 'd-' + Date.now().toString(36) + '-'
            + Math.random().toString(36).slice(2, 8);
        // Mods the detached surface never runs. Workspaces adopts any float
        // with no workspace record into the ACTIVE workspace and writes that to
        // the shared prefs._floatWs; the active workspace comes from the synced
        // layout, so a workspace switch on the desktop would hide this page's
        // only terminal. Everything else loads (owner's call, #226).
        const DETACHED_SKIP_MODS = new Set(['workspaces']);
        // Core key actions that act on a desktop this page does not have.
        const DETACHED_DENY_ACTIONS = new Set([
            'new-terminal', 'toggle-tiling', 'focus-col-left', 'focus-col-right',
            'move-col-left', 'move-col-right', 'open-control-panel',
            'minimize-window', 'close-window',
        ]);
        // The mod loader reads pins through this so the surface skip costs it
        // no line (86 sits at the fragment cap): a pinned-off answer for a mod
        // the detached surface never runs, else the broker's own pin.
        function _surfacePin(id) {
            if (isDetachedSurface() && DETACHED_SKIP_MODS.has(id)) return false;
            return _pin(id);
        }
        function _hasWebLocks() {
            try { return !!(navigator.locks && navigator.locks.request && navigator.locks.query); }
            catch (_) { return false; }
        }
        // '<hostId>:<sid>' -> '<brokerId>:<sid>' when the host's broker identity
        // is known (83 learns it from /info), else the alias. Both pages read
        // the same host records, so they agree.
        function detachCanonical(key) {
            key = String(key);
            const i = key.indexOf(':');
            const hid = i === -1 ? 'local' : key.slice(0, i);
            const sid = i === -1 ? key : key.slice(i + 1);
            let h = null;
            try { h = hostById(hid); } catch (_) { h = null; }
            return ((h && h.brokerId) || hid) + ':' + sid;
        }
        function detachLockName(ckey) { return 'bl-detach:' + ckey; }
        // Collision-free: hex of the key, so 'a.b:1' and 'a_b:1' never share
        // a browsing-context name.
        function detachWindowName(key) {
            let hex = '';
            for (const ch of String(key)) hex += ch.codePointAt(0).toString(16) + '-';
            return 'bl-detach-' + hex;
        }
        let _detachChannel = null;
        function _detachBus() {
            if (_detachChannel) return _detachChannel;
            try { _detachChannel = new BroadcastChannel(DETACH_CHANNEL_NAME); }
            catch (_) { return null; }
            _detachChannel.onmessage = (ev) => {
                const m = ev && ev.data;
                if (!m || typeof m !== 'object' || typeof m.ckey !== 'string') return;
                if (m.inst === DETACH_INST) return;          // our own echo
                try {
                    if (isDetachedSurface()) _onDetachFrameChild(m);
                    else _onDetachFrameDesktop(m);
                } catch (e) { console.warn('[detach] frame', e); }
            };
            return _detachChannel;
        }
        function _detachPost(frame) {
            const bus = _detachBus();
            if (!bus) return false;
            try { bus.postMessage(Object.assign({ inst: DETACH_INST }, frame)); }
            catch (_) { return false; }
            return true;
        }

        // ================= desktop side =====================================
        // Browser-local record of the keys that are out, so a reload of THIS
        // desktop rebuilds them detached instead of attaching (verified against
        // the lock right after). Placement is per-browser (the same argument as
        // prefs._floatWs): it rides the localStorage blob, never _stateBlob.
        function _detachedSet() {
            if (!prefs._detached || typeof prefs._detached !== 'object'
                    || Array.isArray(prefs._detached)) {
                prefs._detached = {};
            }
            return prefs._detached;
        }
        function detachedKeyActive(key) {
            const set = prefs._detached;
            return !!(set && typeof set === 'object' && set[String(key)]);
        }
        // Every terminal window on this desktop that shows the given PTY
        // (aliases included).
        function _windowsForCanonical(ckey) {
            const out = [];
            for (const win of windows.values()) {
                if (win.disposed || win.type === 'app') continue;
                if (detachCanonical(win.id) === ckey) out.push(win);
            }
            return out;
        }
        // The lock answers "is a child alive" exactly.
        async function _childAlive(key) {
            if (!_hasWebLocks()) return false;
            try {
                const st = await navigator.locks.query();
                const name = detachLockName(detachCanonical(key));
                return !!(st && st.held && st.held.some((l) => l.name === name));
            } catch (_) { return false; }
        }
        function _closeWinSocket(win) {
            if (win.connectTimer) { clearTimeout(win.connectTimer); win.connectTimer = null; }
            if (win.ws) {
                try {
                    win.ws.onopen = win.ws.onmessage = null;
                    win.ws.onclose = win.ws.onerror = null;
                } catch (_) {}
                try { win.ws.close(); } catch (_) {}
                win.ws = null;
            }
            win.wsOpen = false;
            win.reattachAt = 0;
        }
        // Wait for the child to go away. A queued exclusive request is granted
        // the moment the child's document releases (and is dropped for free
        // if THIS page goes away first). Keyed on the RECORD, not the window:
        // a window this desktop closes while it is out (menu Close forgets it)
        // must still come back when its only view dies. A return or a
        // takeover aborts the previous watcher, and an abort is NOT a death;
        // a watcher that resolves after its record is gone is a no-op.
        const _recordWatchers = new Map();      // key -> AbortController
        function _watchRecord(key) {
            key = String(key);
            const prev = _recordWatchers.get(key);
            if (prev) { try { prev.abort(); } catch (_) {} }
            const ctrl = new AbortController();
            _recordWatchers.set(key, ctrl);
            const name = detachLockName(detachCanonical(key));
            navigator.locks.request(name, { mode: 'exclusive', signal: ctrl.signal },
                () => { /* granted == the child released */ })
                .then(() => {
                    if (_recordWatchers.get(key) === ctrl) _recordWatchers.delete(key);
                    if (!_detachedSet()[key]) return;
                    returnDetached(key);
                }, () => { /* aborted or unavailable: no verdict */ });
        }
        function _unwatchRecord(key) {
            const ctrl = _recordWatchers.get(String(key));
            if (!ctrl) return;
            _recordWatchers.delete(String(key));
            try { ctrl.abort(); } catch (_) {}
        }
        // The child holds the lock and wants the PTY: stop being a writer,
        // then say so. Idempotent for the same instance; a NEW instance while
        // we are detached means the previous child is gone (the lock is
        // exclusive) and this one is taking over.
        function _yieldToChild(m) {
            let any = false;
            for (const win of _windowsForCanonical(m.ckey)) {
                any = true;
                if (win.detached && win.detachInst === m.inst) continue;
                const key = String(win.id);
                _closeWinSocket(win);
                win.detached = true;
                win.detachInst = m.inst;
                win.detachGen = (win.detachGen | 0) + 1;
                if (!win.minimized) minimizeWindow(key);
                _detachedSet()[key] = { inst: m.inst, ckey: m.ckey, ts: Date.now() };
                savePrefsLocal();
                updateTaskbarActive();
                _watchRecord(key);
            }
            if (any || m.t === 'claim') {
                _detachPost({ t: 'released', ckey: m.ckey, to: m.inst });
            }
        }
        function _onDetachFrameDesktop(m) {
            if (m.t === 'claim' || m.t === 'ready') { _yieldToChild(m); return; }
            if (m.t === 'bye') {
                // Only the instance we yielded to may hand the terminal back.
                for (const win of _windowsForCanonical(m.ckey)) {
                    if (win.detached && win.detachInst === m.inst) returnDetached(win.id);
                }
                // A window this desktop CLOSED while it was out (menu Close
                // forgets it) still has its record: the terminal is running
                // and its only view just went away, so bring it back.
                const set = _detachedSet();
                for (const key of Object.keys(set)) {
                    const rec = set[key];
                    if (rec && rec.inst === m.inst && !windows.has(key)) returnDetached(key);
                }
            }
        }
        // Return = the window's own reattach path plus the placement-aware
        // reveal (a float parked on another workspace must be navigated to,
        // which bringToFront alone refuses). refitSoon and the reattach's
        // initial resize are both forced, so the PTY takes the desktop's dims
        // again no matter what the child left.
        function returnDetached(key) {
            key = String(key);
            _unwatchRecord(key);
            const set = _detachedSet();
            if (set[key]) { delete set[key]; savePrefsLocal(); }
            const win = windows.get(key);
            if (_deactivated) return;          // rebuildView reopens it normally
            if (!win || win.disposed) {
                pendingOpens.set(key, Date.now() + AUTO_OPEN_TIMEOUT_MS);
                startFastPoll();
                return;
            }
            if (!win.detached) return;         // already back (a second verdict)
            win.detached = false;
            win.detachInst = null;
            win.detachProxy = null;
            win.detachGen = (win.detachGen | 0) + 1;
            reattachWindow(win);
            revealAndFocusWindow(key);
            updateTaskbarActive();
        }
        // The entry point: the title-bar item, the key action, the command.
        // Must run in the user's gesture (window.open). NOTHING is hidden here —
        // a blocked popup returns null and changes nothing, and a child that
        // never claims leaves the desktop exactly as it was.
        function detachWindow(id) {
            if (isDetachedSurface()) return false;
            const win = windows.get(id);
            if (!win || win.disposed || win.type === 'app' || win.detached) return false;
            if (!_hasWebLocks()) {
                showNotice('opening a terminal in its own window needs an https '
                    + 'or localhost page', { type: 'error' });
                return false;
            }
            const key = String(id);
            const w = Math.max(480, Math.min(win.dom.offsetWidth || DEFAULT_W, screen.availWidth || 1600));
            const h = Math.max(320, Math.min((win.dom.offsetHeight || DEFAULT_H) + 40, screen.availHeight || 900));
            const url = '/?detach=' + encodeURIComponent(key);
            let proxy = null;
            try {
                proxy = window.open(url, detachWindowName(key),
                    'popup=yes,width=' + w + ',height=' + h);
            } catch (_) { proxy = null; }
            if (!proxy) {
                showNotice('popup blocked — allow popups for this site to open a '
                    + 'terminal in its own window', { type: 'error' });
                return false;
            }
            win.detachProxy = proxy;
            _detachBus();          // make sure we are listening for the claim
            return true;
        }
        // restoreWindow's detached branch: focus the child rather than
        // un-minimize. Our own WindowProxy when we still hold one; otherwise a
        // `focus` frame (the child raises itself where the browser allows it)
        // plus a name lookup — which CREATES a blank window when the child is
        // in another browsing-context group (a bookmark, another desktop's
        // popup), so a blank result is closed again on the spot.
        function focusDetachedChild(win) {
            const key = String(win.id);
            const ckey = detachCanonical(key);
            let p = win.detachProxy;
            let closed = true;
            try { closed = !p || !!p.closed; } catch (_) { closed = true; }
            if (!closed) { try { p.focus(); } catch (_) {} return; }
            win.detachProxy = null;
            _childAlive(key).then((alive) => {
                if (win.disposed || !win.detached) return;
                if (!alive) { returnDetached(key); return; }
                _detachPost({ t: 'focus', ckey });
                try { p = window.open('', detachWindowName(key)); } catch (_) { p = null; }
                if (!p) return;
                let blank = false;
                try { blank = p.location.href === 'about:blank'; } catch (_) { blank = false; }
                if (blank) {
                    try { p.close(); } catch (_) {}
                    showNotice('this terminal is open in another browser window');
                    return;
                }
                win.detachProxy = p;
                try { p.focus(); } catch (_) {}
            });
        }
        // openWindow's tail on the desktop. A key this browser's record says
        // is out is built WITHOUT attaching, minimized and marked, and the lock
        // is asked; otherwise attach now and ask the lock afterwards — another
        // desktop of this browser may have handed the terminal over without
        // this page's record knowing — and yield if a child is alive.
        function detachGateAttach(win) {
            const key = String(win.id);
            if (!_hasWebLocks()) { attachWebSocket(win); bringToFront(key); return; }
            const recorded = detachedKeyActive(key);
            if (recorded) {
                win.detached = true;
                win.detachInst = (_detachedSet()[key] || {}).inst || null;
                win.detachGen = (win.detachGen | 0) + 1;
                minimizeWindow(key);
            } else {
                attachWebSocket(win);
                bringToFront(key);
            }
            const gen = win.detachGen | 0;
            _childAlive(key).then((alive) => {
                if (win.disposed || (win.detachGen | 0) !== gen) return;
                if (alive) {
                    if (!win.detached) {
                        _closeWinSocket(win);
                        win.detached = true;
                        win.detachGen = (win.detachGen | 0) + 1;
                        minimizeWindow(key);
                        _detachedSet()[key] = { inst: null, ckey: detachCanonical(key), ts: Date.now() };
                        savePrefsLocal();
                    }
                    updateTaskbarActive();
                    _watchRecord(key);
                } else if (win.detached) {
                    returnDetached(key);
                }
            });
        }
        // Boot hygiene: records whose child died while this page was away.
        async function pruneDetachedSet() {
            if (isDetachedSurface()) return;
            const set = prefs._detached;
            if (!set || typeof set !== 'object') return;
            for (const key of Object.keys(set)) {
                if (windows.has(key)) continue;
                if (!(await _childAlive(key))) { delete set[key]; savePrefsLocal(); }
            }
        }

        // ================= detached (child) side =============================
        let _detachLockHeld = false;
        let _detachLockRelease = null;
        let _detachLockPromise = null;
        let _detachReturned = false;
        let _detachChildSeen = false;
        let _detachEndWatch = null;
        let _detachReleasedWaiter = null;
        function _detachCkey() { return detachCanonical(DETACH_KEY); }
        function _detachIsOwner() { return _detachLockHeld && !_detachReturned; }
        // Fail closed: an API error is neither ownership nor a free pass.
        function _acquireDetachLock() {
            if (_detachLockPromise) return _detachLockPromise;
            _detachLockPromise = new Promise((resolve) => {
                let settled = false;
                let req;
                try {
                    req = navigator.locks.request(detachLockName(_detachCkey()),
                        { ifAvailable: true }, (lock) => {
                            if (!lock) { settled = true; resolve(false); return; }
                            _detachLockHeld = true;
                            settled = true;
                            resolve(true);
                            // Hold until Return releases it or the document dies.
                            return new Promise((rel) => { _detachLockRelease = rel; });
                        });
                } catch (_) { settled = true; resolve(false); return; }
                req.catch(() => { if (!settled) { settled = true; resolve(false); } });
            });
            return _detachLockPromise;
        }
        function _releaseDetachLock() {
            _detachLockHeld = false;
            if (_detachLockRelease) { try { _detachLockRelease(); } catch (_) {} }
            _detachLockRelease = null;
        }
        function _fitDetachedWindow(win) {
            if (!win || win.disposed) return;
            const W = Math.max(MIN_W, window.innerWidth);
            const H = Math.max(MIN_H, window.innerHeight);
            win.dom.style.left = '0px';
            win.dom.style.top = '0px';
            // .term-window is content-box with 2px borders: style = outer - 4.
            win.dom.style.width = (W - 4) + 'px';
            win.dom.style.height = (H - 4) + 'px';
            win.geom = { left: 0, top: 0, width: W, height: H };
            refitSoon(win);
        }
        // The one window this page shows: pinned, viewport-sized, undraggable.
        function _adoptChildWindow(win) {
            if (!isDetachedSurface() || !win || win.type === 'app') return;
            if (String(win.id) !== DETACH_KEY) return;
            _detachChildSeen = true;
            win.locked = true;
            win.detachedMain = true;
            win.dom.classList.add('detached-main');
            _fitDetachedWindow(win);
            const onResize = () => _fitDetachedWindow(win);
            window.addEventListener('resize', onResize);
            win.cleanups.push(() => window.removeEventListener('resize', onResize));
            document.title = (win.name || 'terminal') + ' — Browserland';
            // `ready` once attached, measured and sized, and again every few
            // seconds for as long as this page owns and is attached: a desktop
            // that (re)loaded meanwhile, or one that never saw the claim, still
            // learns who owns the PTY. A yield is idempotent on a desktop
            // already detached to this instance.
            const ckey = _detachCkey();
            const tick = () => {
                if (win.disposed || !_detachIsOwner()) return;
                if (win.wsOpen && win.termReady && win.lastSentDims) {
                    _detachPost({ t: 'ready', key: DETACH_KEY, ckey });
                    setTimeout(tick, 5000);
                    return;
                }
                setTimeout(tick, 120);
            };
            tick();
        }
        function _detachedStickyNotice(text) {
            showNotice(text, { sticky: true });
        }
        // Claim the PTY: tell any desktop showing it to stop being a writer,
        // wait (briefly) for its `released`, THEN open through the same door a
        // ?session= deep link uses so the broker's replay lands in a page that
        // is the only one attached. No desktop answering (a bookmark, a
        // desktop that is torn down) is fine: nobody else is attached either.
        function _claimAndOpen() {
            if (_detachReturned || !_detachIsOwner()) return;
            const ckey = _detachCkey();
            const open = () => {
                _detachReleasedWaiter = null;
                if (_detachReturned || !_detachIsOwner()) return;
                if (windows.has(DETACH_KEY) || pendingOpens.has(DETACH_KEY)) return;
                pendingOpens.set(DETACH_KEY, Date.now() + AUTO_OPEN_TIMEOUT_MS);
                startFastPoll();
                startSlowPoll();
                _startDetachEndWatch();
            };
            const timer = setTimeout(open, 700);
            _detachReleasedWaiter = () => { clearTimeout(timer); open(); };
            _detachPost({ t: 'claim', key: DETACH_KEY, ckey });
        }
        // The session ending (exit frame, reaper) closes the window; there is
        // nothing else on this page, so say so and let the lock go.
        function _startDetachEndWatch() {
            if (_detachEndWatch) return;
            _detachEndWatch = setInterval(() => {
                if (_detachReturned) { clearInterval(_detachEndWatch); _detachEndWatch = null; return; }
                if (!_detachChildSeen || _deactivated) return;
                if (windows.has(DETACH_KEY) || pendingOpens.has(DETACH_KEY)) return;
                clearInterval(_detachEndWatch); _detachEndWatch = null;
                _detachReturned = true;
                _detachedStickyNotice('session ended — you can close this window');
                document.title = 'session ended — Browserland';
                _releaseDetachLock();
            }, 1000);
        }
        function _onDetachFrameChild(m) {
            if (m.ckey !== _detachCkey()) return;
            if (m.t === 'released') {
                if (m.to === DETACH_INST && _detachReleasedWaiter) _detachReleasedWaiter();
                return;
            }
            if (!_detachIsOwner()) return;         // refused / returned: not ours
            if (m.t === 'return') { detachedChildReturn(); return; }
            if (m.t === 'focus') { try { window.focus(); } catch (_) {} }
        }
        // Boot on this surface (called by bootActiveView on the first HOME
        // {active:true}, after the /state adopt).
        async function bootDetachedView(epoch) {
            _detachBus();
            if (!_hasWebLocks()) {
                _detachedStickyNotice('opening a terminal in its own window needs an '
                    + 'https or localhost page');
                document.title = 'not available — Browserland';
                return;
            }
            const ok = await _acquireDetachLock();
            if (!ok) {
                _detachedStickyNotice('This terminal is already open in another window.');
                document.title = 'already open — Browserland';
                return;
            }
            // A lease flip mid-acquire: the rebuild path waits on the same
            // promise and opens, so a stale boot simply stops here.
            if (_deactivated || epoch !== _viewEpoch) return;
            _claimAndOpen();
        }
        // After a HOME lease loss + regain the ordinary rebuild is restore-queue
        // driven and would open nothing here; re-claim and re-open the one
        // window. The lock is document-scoped and survived the teardown, and a
        // boot still acquiring it is waited for rather than raced.
        function rebuildDetachedView() {
            if (_detachReturned) return;
            _acquireDetachLock().then((ok) => {
                if (!ok || _deactivated || _detachReturned) return;
                _claimAndOpen();
            });
        }
        // Explicit Return: stop being a writer FIRST (the ordinary close path,
        // which also disposes the xterm and its listeners — a no-op for shared
        // state on this surface), then tell the desktop, release, and close.
        // A page the script may not close (opened by hand) is told so instead,
        // and stays returned: no rebuild, no claim, no answer to a `return`.
        function detachedChildReturn() {
            if (_detachReturned) return;
            _detachReturned = true;
            const win = windows.get(DETACH_KEY);
            if (win && !win.disposed) {
                _closeWinSocket(win);
                try { closeWindow(DETACH_KEY); } catch (_) {}
            }
            pendingOpens.delete(DETACH_KEY);
            _detachPost({ t: 'bye', key: DETACH_KEY, ckey: _detachCkey() });
            _releaseDetachLock();
            try { window.close(); } catch (_) {}
            setTimeout(() => {
                if (!window.closed) {
                    _detachedStickyNotice('returned to the desktop — you can close this window');
                    document.title = 'returned — Browserland';
                }
            }, 400);
        }
        // Title-bar right-click on this surface: what a page with one window
        // and no desktop can actually do.
        function buildDetachedWindowMenu(win, x, y) {
            const items = [
                { label: 'Return to desktop', enabled: true,
                  action: () => detachedChildReturn() },
                { sep: true },
                { label: 'Terminate', enabled: true, action: () => {
                    openConfirmDialog({
                        title: 'Terminate session',
                        message: 'Terminate this session? The shell process tree will be killed.',
                        okLabel: 'Terminate', danger: true,
                    }).then((ok) => { if (ok && windows.has(win.id)) terminateWindow(win.id); });
                } },
            ];
            renderMenu(items, x, y);
        }
        // A remote broker whose lease another browser holds masks its windows;
        // on the desktop that leaves a chip to take it back, here it would leave
        // a blank page. Say what happened.
        function detachedRemoteLeaseNotice(hostId, active) {
            if (!isDetachedSurface()) return;
            const h = hostById(hostId);
            const label = (h && h.label) || hostId;
            const host = document.getElementById('notice-host');
            if (host) {
                for (const n of Array.from(host.querySelectorAll('.notice-sticky'))) {
                    if (n._noticeText && n._noticeText.indexOf('another browser is active on ') === 0) n.remove();
                }
            }
            if (!active) {
                showNotice('another browser is active on ' + label
                    + ' — take the lease back from the desktop to keep typing here',
                    { sticky: true, type: 'error' });
            }
        }
        if (isDetachedSurface()) {
            document.body.classList.add('detached-surface');
            document.title = 'terminal — Browserland';
            registerTerminalCreate((info) => { _adoptChildWindow(info.win); });
        } else {
            _detachBus();
        }
