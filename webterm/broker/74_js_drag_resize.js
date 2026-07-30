        // ---- drag / resize wiring -----------------------------------------
        // Persist a floating window's geom to the right store: app windows to
        // the separate app store (their 'app:' keys would be GC'd from prefs),
        // terminals to prefs.
        function persistWinGeom(win) {
            if (win.type === 'app') { saveAppWindow(win); return; }
            getPref(win.id).geom = Object.assign({}, win.geom);
            savePrefs();
        }

        // ---- keeping the window gestures alive over embedded content (#176) -
        // Both gestures used to track on bare `document` mousemove/mouseup.
        // Content that swallows events starves those listeners: an <iframe>'s
        // events belong to ITS document and never reach ours at all, and a
        // canvas/plugin surface can stopPropagation on the way up. The gesture
        // then stalls at the last position `document` saw and the mouseup that
        // would end it may never arrive.
        //
        // TWO mechanisms carry the gesture, and they are not alternatives --
        // each covers a half the other cannot (see raiseGestureShield below for
        // the measurement that settles it):
        //
        //   * a full-viewport SHIELD, which stops anything underneath from
        //     being hit-tested in the first place. This is what survives a
        //     cross-site iframe, where the pointer is routed into another
        //     RENDERER PROCESS before our capture is ever consulted;
        //   * setPointerCapture, which retargets every subsequent event for
        //     that pointer to the capture element. In-process that is clean
        //     routing for free, and it is what gives the gesture a
        //     pointerup / pointercancel / lostpointercapture lifecycle to hang
        //     its teardown off instead of a mouseup that may never come.
        //
        // Why the gestures still START on `mousedown`. The drag handle is the
        // entire .title-bar, and the way a title-bar child opts out of dragging
        // the window is to call stopPropagation in its OWN mousedown handler --
        // a contract core (min / close / colour / MCP) and every mod with a
        // title-bar control depends on, in-tree and out (see the mousemode
        // chip: "wireDrag treats anything that does not stopPropagation as 'the
        // bar itself'"). `pointerdown` fires BEFORE mousedown and is a separate
        // dispatch, so starting the gesture there would ignore all of those
        // guards -- press-and-hold on the close button would snap the window to
        // the grid. So mousedown stays the permission gate and the pointer id
        // is sniffed from the pointerdown that immediately precedes it.
        //
        // TOUCH is deliberately not recorded: a touch pointer's compatibility
        // mousedown (when a UA synthesises one at all) arrives only after the
        // touch has already ENDED, so its id would be dead by the time we tried
        // to capture it -- and a touch cannot start either gesture today for
        // exactly that reason. Recording only mouse/pen keeps touch on its
        // current (no-op) path instead of half-supporting it. Supporting touch
        // properly needs a pointer-era opt-out contract for title-bar children
        // (no mousedown ever arrives during a touch gesture, so the existing
        // one cannot be honoured) -- a deliberate follow-up, not a side effect
        // of this fix. `touch-action: none` IS set on both handles anyway (see
        // 10_css_root.css / 12_css_help.css): pen is a captured pointer here,
        // and without it the UA may claim a pen drag as a pan/zoom and
        // pointercancel it out from under the gesture.
        let _lastPointerDown = null;
        document.addEventListener('pointerdown', (e) => {
            // Primary mouse/pen only (above); overwritten by every press.
            _lastPointerDown = (e.isPrimary
                && (e.pointerType === 'mouse' || e.pointerType === 'pen'))
                ? { id: e.pointerId } : null;
        }, true);
        // Bound staleness: the record is cleared the moment the press it
        // describes is over, so a mousedown can only ever find the id of a
        // pointer that is still down. It is NOT cleared per-mousedown, because
        // pointerdown/pointerup fire only on the 0<->1 buttons transition: with
        // a second button already held, a left mousedown gets no pointerdown of
        // its own and must reuse the live record (see bindGestureTracking).
        document.addEventListener('pointerup', () => { _lastPointerDown = null; }, true);
        document.addEventListener('pointercancel', () => { _lastPointerDown = null; }, true);
        // READ-ONLY on purpose -- consuming the record here would contradict the
        // invariant above and silently reinstate the bug: hold the middle button,
        // press left on a title bar (record consumed), release left (buttons
        // 3->2, so no pointerup and no reset), then press left on a resize grip
        // (buttons 2->3, so no pointerdown either) and the gesture would find no
        // id and fall back to the bare document pair. A stale id cannot get
        // through instead: the record only outlives its press if the UA loses the
        // pointerup entirely, and setPointerCapture on a pointer that is no
        // longer active fails, which captureGesturePointer already treats as "no
        // capture".
        function livePointerDown() { return _lastPointerDown; }
        // Capture `pd` (from livePointerDown) for a gesture whose mousedown
        // targeted `target` inside `handle`. Returns {id, el} or null; null
        // means "no capture" and the caller tracks with the legacy mouse pair,
        // i.e. exactly the pre-#176 behaviour (synthetic MouseEvents with no
        // pointer stream behind them, an already-released pointer, a UA that
        // refuses the capture).
        //
        // It captures the ORIGINAL target rather than the handle on purpose:
        // capture retargets the compatibility mouse events too, so capturing
        // the .title-bar for a press that landed on .title-text would move that
        // press's click/dblclick onto the bar and break the app-window
        // double-click rename (editor.js).
        function captureGesturePointer(pd, target, handle) {
            if (!pd) return null;
            let el = (target instanceof Element) ? target : null;
            if (!el || !el.isConnected || !handle.contains(el)) el = handle;
            if (!el.setPointerCapture || !el.hasPointerCapture) return null;
            try {
                el.setPointerCapture(pd.id);
            } catch (_) { return null; }
            // setPointerCapture can also fail WITHOUT throwing (a pointer that
            // is no longer active); only hasPointerCapture proves it took, and
            // a false positive here would silently reinstate the bug.
            if (!el.hasPointerCapture(pd.id)) return null;
            return { id: pd.id, el: el };
        }
        // ---- the gesture shield --------------------------------------------
        // Why pointer capture is not enough, measured rather than reasoned
        // (Chrome 150, headless, over CDP): with a genuine CROSS-SITE iframe
        // under the cursor -- parent on 127.0.0.1, frame on localhost, so a real
        // out-of-process iframe and not merely a cross-ORIGIN one -- a resize
        // whose grip reported `hasPointerCapture() === true` throughout received
        // 0 of 16 pointermoves. No pointerup, no mouseup, no pointercancel: the
        // child document counted every one of them, and the gesture both stalled
        // and outlived the button (the lostpointercapture that eventually ends
        // it is deferred to the next pointer event WE see, so a stray buttonless
        // move can get in first and keep resizing). Dragging a window across
        // another window's cross-site iframe froze after 7 of 16 moves the same
        // way. A same-site iframe (in-process, still cross-origin) delivered all
        // 16 plus the release -- which is the whole story: capture is a routing
        // rule inside ONE renderer, and a cross-process frame is hit-tested by
        // the compositor before that rule is reached.
        //
        // So every gesture also raises a transparent full-viewport shield. With
        // it on top nothing underneath is hit-tested at all -- no iframe of
        // either flavour, no canvas, no plugin surface -- and every event of the
        // gesture lands on an element in OUR document. That, not the capture, is
        // what makes the issue's cases work; the capture stays for the lifecycle
        // and the in-process routing.
        //
        // A shield that outlived its gesture would leave the whole page
        // unclickable, which is far worse than the bug it fixes, so the
        // lifecycle is deliberately conservative:
        //
        //   * only raised for a gesture that HOLDS A CAPTURE, so the teardown
        //     has the pointerup / pointercancel / lostpointercapture triple
        //     behind it and not just a mouseup that may never come. It is also
        //     what keeps the no-capture fallback honest: with a shield up the
        //     compat mouseup would target the SHIELD, so the click the UA
        //     synthesises from a stationary press would go to the common
        //     ancestor (body) instead of the title bar, and double-click to
        //     rename would stop working. Under a capture that cannot happen --
        //     the compat events are retargeted to the capture element;
        //   * refcounted, because two gestures can still overlap and the first
        //     to end must not strip the other's shield. NOT from an ordinary
        //     second press -- the shield stops that reaching a handle at all --
        //     but from a synthetic mousedown, or a gesture started
        //     programmatically while one is live;
        //   * re-attached on `isConnected`, not on the refcount, so anything
        //     that rips the node out from under us (a mod, a devtools poke)
        //     self-heals on the next gesture instead of never showing again;
        //   * and bindGestureTracking's buttons-are-up sweep is the backstop
        //     for a release we never saw at all.
        let _shieldEl = null;
        let _shieldRefs = 0;
        function raiseGestureShield(handle) {
            if (!_shieldEl) {
                _shieldEl = document.createElement('div');
                _shieldEl.id = 'gesture-shield';
            }
            // Carry the handle's cursor across. The shield is what the pointer
            // is over for the whole gesture, so without this a resize would
            // drop its edge cursor for the default arrow the moment it started.
            // First raise only: with two gestures live the visible one owns the
            // shield, and a second must not repaint its cursor.
            if (!_shieldRefs) {
                let cur = '';
                try { cur = getComputedStyle(handle).cursor; } catch (_) {}
                _shieldEl.style.cursor = cur || 'default';
            }
            if (!_shieldEl.isConnected) document.body.appendChild(_shieldEl);
            _shieldRefs++;
            let lowered = false;
            return () => {
                if (lowered) return;      // idempotent, like every teardown here
                lowered = true;
                _shieldRefs = Math.max(0, _shieldRefs - 1);
                if (!_shieldRefs && _shieldEl.parentNode) {
                    _shieldEl.parentNode.removeChild(_shieldEl);
                }
            };
        }
        // Hit-test the page as if the shield were not there. The drag's
        // swap/tab mode probes for the window UNDER the cursor, and with a
        // shield on top a plain elementFromPoint would answer "the shield",
        // every time, for every position.
        //
        // elementsFromPoint + skip, rather than switching the shield's
        // pointer-events off around the probe: this is READ-ONLY. The toggle
        // would write style twice per move of a modifier drag, and if anything
        // between the two writes threw it would leave the shield inert -- which
        // is silently the exact failure the shield exists to prevent. Hit-testing
        // by geometry was the third option and would mean re-deriving a z-order
        // the DOM already knows (windows, tiers, pinned notes, mods). The toggle
        // survives only as the fallback for a UA without elementsFromPoint.
        function hitTestUnderShield(x, y) {
            if (!_shieldEl || !_shieldEl.parentNode) {
                return document.elementFromPoint(x, y);
            }
            if (document.elementsFromPoint) {
                const stack = document.elementsFromPoint(x, y);
                for (let i = 0; i < stack.length; i++) {
                    if (stack[i] !== _shieldEl) return stack[i];
                }
                return null;
            }
            const prev = _shieldEl.style.pointerEvents;
            _shieldEl.style.pointerEvents = 'none';
            try { return document.elementFromPoint(x, y); }
            finally { _shieldEl.style.pointerEvents = prev; }
        }
        // Bind one gesture's tracking listeners; returns the unbind.
        //
        // With a capture held we track POINTER events, in the CAPTURE phase on
        // document so nothing in between can stop them, filtered to the
        // captured pointer id -- a second pointer (multi-touch, a second
        // device) is ignored instead of corrupting the gesture. `onCancel` runs
        // for pointercancel (a system/touch interruption, which never sends a
        // pointerup) and for an unexpected lostpointercapture, both of which
        // must end the gesture the same way a lost mouseup does.
        //
        // Without a capture we bind the legacy document mousemove/mouseup pair
        // in the bubble phase, and raise NO shield -- unchanged, deliberately,
        // so the fallback behaves exactly as it did before #176 (see
        // raiseGestureShield for why a shield without a capture behind it is a
        // bad trade).
        //
        // The shield is raised HERE, so that the unbind this returns is what
        // lowers it: the drag's teardown and the resize's finish both call it,
        // and between them they cover pointerup, a plain mouseup, pointercancel,
        // a lost capture, window blur, window disposal, a stale gesture
        // displaced by a new press, and (in snap mode only) Escape.
        function bindGestureTracking(cap, handle, onMove, onUp, onCancel) {
            if (!cap) {
                document.addEventListener('mousemove', onMove);
                document.addEventListener('mouseup', onUp);
                return () => {
                    document.removeEventListener('mousemove', onMove);
                    document.removeEventListener('mouseup', onUp);
                };
            }
            const lowerShield = raiseGestureShield(handle);
            const mine = (fn) => (ev) => { if (ev.pointerId === cap.id) fn(ev); };
            // Backstop for a release nobody delivered. No button is down during
            // a move that belongs to a live gesture, so a move that reports
            // buttons === 0 means the up was lost -- end on the cancel path
            // rather than run on (which, with a shield up, would leave the page
            // unclickable until something else tore the gesture down).
            const pMove = mine((ev) => {
                if (ev.buttons === 0) { onCancel(ev); return; }
                onMove(ev);
            });
            const pUp = mine(onUp);
            const pCancel = mine(onCancel);
            document.addEventListener('pointermove', pMove, true);
            document.addEventListener('pointerup', pUp, true);
            document.addEventListener('pointercancel', pCancel, true);
            document.addEventListener('lostpointercapture', pCancel, true);
            // ALSO end on a plain mouseup of the PRIMARY button.
            // pointerdown/pointerup fire only on the 0<->1 buttons transition,
            // so when a second button is held (chording) the release of the one
            // that started this gesture produces a mouseup and no pointerup --
            // without this the gesture would run on until the last button came
            // up, where today it ends on the first. Capture retargets the
            // compatibility mouse events too, so this listener is not starved by
            // whatever the pointer is over. onUp is idempotent, so the normal
            // pointerup-then-mouseup order costs nothing.
            //
            // ev.button === 0 because this listener is deliberately the one that
            // DOES see everything: a right-click landing mid-drag releases too,
            // and that release is not this gesture's. onDown already refuses to
            // let a right mousedown kill a live drag; this is the same rule for
            // the way out.
            const mUp = (ev) => { if (ev.button === 0) onUp(ev); };
            document.addEventListener('mouseup', mUp, true);
            // Last-resort recovery. Every listener above is filtered to OUR
            // pointer, and with the shield up a fresh press cannot reach a
            // handle, so wireDrag / wireResize's `if (endGesture) endGesture()`
            // sweep is unreachable while a gesture is live. If a release is lost
            // on a stream whose events we filter out, a pointerdown from some
            // OTHER pointer is the only thing left that can free the page --
            // so it ends this gesture rather than being ignored. Chording does
            // not trip it (a second button on the same pointer produces no
            // pointerdown at all), and neither does this gesture's own press:
            // that pointerdown was dispatched before we got here.
            const pDown = (ev) => { onCancel(ev); };
            document.addEventListener('pointerdown', pDown, true);
            return () => {
                // Listeners go FIRST: releasePointerCapture fires
                // lostpointercapture, which must not re-enter onCancel.
                document.removeEventListener('pointermove', pMove, true);
                document.removeEventListener('pointerup', pUp, true);
                document.removeEventListener('pointercancel', pCancel, true);
                document.removeEventListener('lostpointercapture', pCancel, true);
                document.removeEventListener('mouseup', mUp, true);
                document.removeEventListener('pointerdown', pDown, true);
                lowerShield();
                // pointerup / pointercancel release implicitly; this is for
                // every OTHER exit (Escape, blur, the window being disposed
                // mid-gesture), which must not leave the pointer captured.
                try {
                    if (cap.el.hasPointerCapture(cap.id)) {
                        cap.el.releasePointerCapture(cap.id);
                    }
                } catch (_) {}
            };
        }
        // A captured pointerup retargets the compatibility click to the capture
        // element, so a gesture RELEASED somewhere else still lands a click on
        // the element it started from, where uncaptured it would have gone to
        // the common ancestor instead. Swallow that one, at the very top of the
        // propagation path so nothing downstream sees it.
        //
        // dblclick needs a LONGER guard than the click that follows the
        // release: swallowing a click does not un-count it for the UA, so the
        // next plain click on the same element -- a drag, then a click on the
        // title -- would pair with it into a dblclick and open the rename
        // editor. Hold the dblclick guard for one double-click interval.
        //
        // Only ever called for a gesture that actually MOVED, and only for
        // TRUSTED events, so a press that stayed put keeps its click/dblclick
        // (double-click-to-rename still works) and a programmatic .click() from
        // some handler in the same task is left alone.
        //
        // SCOPED to `root`, the gesture's own handle -- its title bar, and only
        // events inside it. Unscoped, for 700ms after any drag every trusted
        // double-click in the page died: a browse-pane row
        // (70_js_browse_pane.js), a scratchpad tab, and the app-window rename on
        // some OTHER window, which is the very thing the guard was written to
        // protect.
        //
        // The handle rather than the capture element itself, even though the
        // retargeted click is dispatched AT the capture element: swallowing a
        // click does not un-count it, so the pair that reaches dblclick can be
        // (retargeted click on the ID badge, then a click on the title text)
        // and the dblclick then lands on their common ancestor. Everything that
        // pairing can reach is inside the handle, and nothing outside this one
        // window's title bar has any business being suppressed.
        const DBLCLICK_GUARD_MS = 700;
        function swallowClickAfterGesture(root) {
            if (!root) return;
            const eat = (ev) => {
                if (!ev.isTrusted || !root.contains(ev.target)) return;
                ev.preventDefault();
                ev.stopPropagation();
                ev.stopImmediatePropagation();
            };
            window.addEventListener('click', eat, true);
            window.addEventListener('dblclick', eat, true);
            setTimeout(() => window.removeEventListener('click', eat, true), 0);
            setTimeout(() => window.removeEventListener('dblclick', eat, true),
                       DBLCLICK_GUARD_MS);
        }

        function wireDrag(win, handle) {
            // Set while a gesture is live so window teardown can end it. A
            // capture element removed from the DOM does NOT reliably fire
            // lostpointercapture (the spec defers it to the next pointer
            // event), so disposal must not be left to that.
            let endGesture = null;
            const onDown = (e) => {
                // Ahead of the stale-gesture sweep below on purpose: a
                // right-click lands a mousedown too, and it must not kill the
                // drag it lands in the middle of.
                if (e.button !== 0) return;
                // Tiled windows: title-bar drag reorders / consumes / detaches
                // via the strip engine (overlays during drag, one mutation on
                // drop). The floating absolute-move below would be a no-op on a
                // static box, so hand off and return.
                if (win.tiled) { startTiledDrag(win, e); return; }
                // A gesture still live on this handle can only be one that lost
                // its release (the fallback path over event-eating content).
                // Abandon it rather than run two gestures on one window.
                if (endGesture) endGesture();
                // Interactive children (tb-btn, incl. the color button)
                // stopPropagation in their own mousedown handlers; title-text has
                // pointer-events:none. So we only get here for the bar itself,
                // the id badge, or the title text area.
                e.preventDefault();
                bringToFront(win.id);
                const startX = e.clientX, startY = e.clientY;
                const startLeft = win.dom.offsetLeft;
                const startTop = win.dom.offsetTop;
                const desktop = document.getElementById('desktop');
                const dRect = desktop.getBoundingClientRect();
                const originalGeom = {
                    left: startLeft,
                    top: startTop,
                    width: win.dom.offsetWidth,
                    height: win.dom.offsetHeight,
                };
                // Capture for lifecycle + in-process routing; the shield that
                // bindGestureTracking raises below is what actually survives
                // event-eating content, cross-process included (#176).
                const cap = captureGesturePointer(livePointerDown(), e.target, handle);
                // elementFromPoint ONLY: swap/tab mode probes for the window
                // UNDER the cursor, so the dragged window must not hit-test.
                // This is NOT what keeps the gesture alive over event-eating
                // content -- the shield is. It only ever looked like drag
                // safety: neutralising the dragged window's own subtree happens
                // to hide the iframe INSIDE it, which is why dragging an
                // embedding window worked while resizing it did not, and
                // reading that as safety is how the resize path (which never
                // had this line) kept stalling. Do not delete it as redundant
                // -- hitTestUnderShield still needs the dragged window
                // transparent to find what is beneath it -- and do not rely on
                // it for anything else.
                const prevPointerEvents = win.dom.style.pointerEvents;
                win.dom.style.pointerEvents = 'none';
                let swapCandidate = null;
                let candClass = 'swap-target';   // swap (Shift) vs tab (Alt)
                const setCandidate = (next, cls) => {
                    if (next === swapCandidate && cls === candClass) return;
                    if (swapCandidate) swapCandidate.dom.classList.remove(candClass);
                    candClass = cls || 'swap-target';
                    if (next) next.dom.classList.add(candClass);
                    swapCandidate = next;
                };
                // Hold-to-snap state. lastX/Y track the cursor; dwellX/Y is the
                // anchor a still hold is measured from; snapMode flips on when the
                // dwell timer fires; modHeld mirrors Shift/Alt so a timer that
                // fires while a modifier is held re-arms instead of stealing the
                // swap/tab gesture.
                let lastX = startX, lastY = startY;
                let dwellX = startX, dwellY = startY;
                let snapMode = false, snapTarget = null, holdTimer = 0;
                let modHeld = !!(e.shiftKey || e.altKey);
                // #38: dwell delay captured ONCE at grab (deterministic for the
                // whole gesture even if a settings edit / remote prefetch lands
                // mid-drag); 0 disables the snap dwell.
                const holdMs = snapHoldMsFor(win);
                // `ended` makes every exit path idempotent: pointerup and the
                // implicit lostpointercapture that follows it, a blur racing a
                // pointercancel, disposal racing either.
                let ended = false;
                let unbind = null;
                const clearHold = () => {
                    if (holdTimer) { clearTimeout(holdTimer); holdTimer = 0; }
                };
                const enterSnap = () => {
                    holdTimer = 0;
                    // Disposed mid-hold: tear the drag down now so the document
                    // listeners don't linger until some later stray event.
                    if (win.disposed) { teardown(); return; }
                    // A modifier owns swap/tab drags — don't steal the gesture;
                    // re-arm so snap can engage once it's released and re-dwelled.
                    if (modHeld) { armDwell(); return; }
                    snapMode = true;
                    setCandidate(null);
                    win.dom.classList.remove('swap-source');
                    document.body.classList.add('tiled-dragging');
                    win.dom.classList.add('drag-source');
                    showSnapCancel();
                    // Show the preview immediately at the parked spot (no wiggle).
                    snapTarget = computeDropTarget(lastX, lastY, false);
                    showDrop(inSnapCancel(lastX, lastY) ? null : snapTarget);
                };
                const armDwell = () => {
                    clearHold();
                    dwellX = lastX; dwellY = lastY;
                    if (holdMs <= 0) return;      // 0 = snap dwell disabled
                    holdTimer = setTimeout(enterSnap, holdMs);
                };
                // Drops every listener this gesture owns (tracking, keys, blur)
                // and releases the capture. Idempotent.
                const teardown = () => {
                    if (ended) return;
                    ended = true;
                    endGesture = null;
                    clearHold();
                    if (unbind) unbind();
                    document.removeEventListener('keydown', onKey, true);
                    document.removeEventListener('keyup', onKeyUp, true);
                    window.removeEventListener('blur', onAbort);
                    win.dom.classList.remove('swap-source');
                    if (snapMode) {
                        document.body.classList.remove('tiled-dragging');
                        win.dom.classList.remove('drag-source');
                        hideDropEls();
                        hideSnapCancel();
                    }
                    snapMode = false;
                };
                // True when the pointer ended far enough from where it started
                // that the release should not also read as a click (see
                // swallowClickAfterGesture). Measured from the RELEASE event,
                // whose coordinates can be newer than the last move we saw. A
                // drag that returns to where it began keeps its click, which is
                // what would have happened without a capture anyway.
                const dragged = (ev) => {
                    const x = (ev && typeof ev.clientX === 'number') ? ev.clientX : lastX;
                    const y = (ev && typeof ev.clientY === 'number') ? ev.clientY : lastY;
                    return Math.hypot(x - startX, y - startY) > DRAG_THRESHOLD;
                };
                const onMove = (ev) => {
                    if (win.disposed) { setCandidate(null); teardown(); return; }
                    lastX = ev.clientX; lastY = ev.clientY;
                    modHeld = !!(ev.shiftKey || ev.altKey);
                    // The box follows the cursor in BOTH modes.
                    let nl = startLeft + (ev.clientX - startX);
                    let nt = startTop + (ev.clientY - startY);
                    nl = Math.max(0, Math.min(nl, dRect.width - 80));
                    nt = Math.max(0, Math.min(nt, dRect.height - 30));
                    win.dom.style.left = nl + 'px';
                    win.dom.style.top = nt + 'px';
                    if (snapMode) {
                        // Snap mode ignores modifiers; track the grid target,
                        // hiding the grid preview while over the cancel zone so
                        // only the "return to floating" affordance shows. Movement
                        // does NOT disarm snap.
                        snapTarget = computeDropTarget(ev.clientX, ev.clientY, false);
                        showDrop(inSnapCancel(ev.clientX, ev.clientY) ? null : snapTarget);
                        return;
                    }
                    win.dom.classList.toggle('swap-source', !!ev.shiftKey);
                    // Shift = swap, Alt (without Shift) = tab; both hit-test the
                    // window under the cursor (the dragged node has
                    // pointer-events:none so it's transparent to the probe, and
                    // hitTestUnderShield sees past the gesture shield, which is
                    // over everything for the duration -- #176).
                    if (ev.shiftKey || ev.altKey) {
                        clearHold();   // modifier drags own the gesture; no dwell
                        const hit = hitTestUnderShield(ev.clientX, ev.clientY);
                        const dom = hit && hit.closest && hit.closest('.term-window');
                        let cand = null;
                        if (dom) {
                            const other = windows.get(dom.dataset.sessionId);
                            if (other && other !== win && !other.minimized && !other.disposed) {
                                cand = other;
                            }
                        }
                        setCandidate(cand, ev.shiftKey ? 'swap-target' : 'tab-target');
                    } else {
                        setCandidate(null);
                        // Dwell reset: meaningful movement re-arms the timer +
                        // anchor, so snap engages only on a deliberate park.
                        if (Math.hypot(ev.clientX - dwellX, ev.clientY - dwellY) > DWELL_MOVE) {
                            armDwell();
                        }
                    }
                };
                const onUp = (ev) => {
                    if (ended) return;
                    const wasSnap = snapMode;
                    const wasDrag = dragged(ev);
                    teardown();
                    if (cap && wasDrag) swallowClickAfterGesture(handle);
                    if (win.disposed) { setCandidate(null); return; }
                    win.dom.style.pointerEvents = prevPointerEvents;
                    if (wasSnap) {
                        // Cancel (top-left zone / no target) returns the window to
                        // its pre-drag box; otherwise commit the snap: float->tile
                        // attach FIRST (the layout mutators bail on a key not yet
                        // in _layout), then route through the shared commitDrop.
                        if (inSnapCancel(ev.clientX, ev.clientY) || !snapTarget) {
                            applyGeomToWindow(win, originalGeom);
                            savePrefs();
                        } else {
                            attachToStrip(win);
                            commitDrop(win, snapTarget, ev);
                        }
                        return;
                    }
                    const finalCandidate = swapCandidate;
                    setCandidate(null);
                    // Revalidate target — it may have been closed/minimized
                    // between the last move and the release.
                    const candValid = finalCandidate
                        && !finalCandidate.disposed
                        && !finalCandidate.minimized
                        && windows.get(finalCandidate.id) === finalCandidate;
                    if (ev.shiftKey && candValid) {
                        swapWindows(win, finalCandidate, originalGeom);
                        savePrefs();
                    } else if (ev.shiftKey) {
                        // Shift held but no valid target — snap dragged window back.
                        applyGeomToWindow(win, originalGeom);
                        savePrefs();
                    } else if (ev.altKey && candValid) {
                        // Alt = dock this window as a tab in the target's column
                        // (task 10). tabWindowInto tiles both as needed.
                        tabWindowInto(win, finalCandidate);
                    } else {
                        win.geom.left = win.dom.offsetLeft;
                        win.geom.top = win.dom.offsetTop;
                        persistWinGeom(win);
                    }
                };
                // Escape cancels snap back to the pre-drag box. Capture +
                // stopPropagation so the global Escape handler doesn't also close
                // settings / the context menu. Also mirrors modifier state so a
                // Shift/Alt pressed while still doesn't let a pending dwell snap.
                const onKey = (ev) => {
                    modHeld = !!(ev.shiftKey || ev.altKey);
                    if (snapMode && ev.key === 'Escape') {
                        ev.preventDefault();
                        ev.stopPropagation();
                        teardown();
                        if (win.disposed) return;
                        win.dom.style.pointerEvents = prevPointerEvents;
                        applyGeomToWindow(win, originalGeom);
                        savePrefs();
                    }
                };
                // Mirror modifier RELEASE too — without this, a Shift/Alt pressed
                // while parked (then released without moving) would leave modHeld
                // stuck true and suppress the dwell forever.
                const onKeyUp = (ev) => { modHeld = !!(ev.shiftKey || ev.altKey); };
                // Every abnormal end of the gesture: a lost mouseup (window blur
                // / alt-tab), a pointercancel (the system took the pointer over
                // — a touch or pen interruption never sends pointerup), or an
                // unexpected lostpointercapture. None of them may leave the drag
                // half-alive (listeners live, pointer-events:none, stale swap
                // UI). Always tear down + restore pointer-events. In snap mode
                // this is a cancel back to the pre-drag box (undo the gesture);
                // pre-snap it drops the window where it currently sits,
                // mirroring the missing no-modifier release so the existing
                // floating-drag isn't regressed.
                const onAbort = () => {
                    if (ended) return;
                    const wasSnap = snapMode;
                    teardown();
                    if (win.disposed) { setCandidate(null); return; }
                    win.dom.style.pointerEvents = prevPointerEvents;
                    setCandidate(null);
                    if (wasSnap) {
                        applyGeomToWindow(win, originalGeom);
                        savePrefs();
                    } else {
                        win.geom.left = win.dom.offsetLeft;
                        win.geom.top = win.dom.offsetTop;
                        persistWinGeom(win);
                    }
                };
                unbind = bindGestureTracking(cap, handle, onMove, onUp, onAbort);
                document.addEventListener('keydown', onKey, true);
                document.addEventListener('keyup', onKeyUp, true);
                window.addEventListener('blur', onAbort);
                // Window teardown (or a stale gesture displaced by a new press):
                // drop the gesture without committing geom, but DO put
                // pointer-events back -- win.cleanups also runs on the
                // active-view rebuild, and a live window left non-interactive
                // would be unclickable.
                endGesture = () => {
                    teardown();
                    setCandidate(null);
                    win.dom.style.pointerEvents = prevPointerEvents;
                };
                armDwell();   // start the dwell clock at grab
            };
            handle.addEventListener('mousedown', onDown);
            win.cleanups.push(() => {
                handle.removeEventListener('mousedown', onDown);
                if (endGesture) endGesture();
            });
        }

        function wireResize(win, handle, dir) {
            let endGesture = null;
            const onDown = (e) => {
                if (e.button !== 0) return;
                if (win.tiled) return;       // flex-sized; handles are hidden
                if (isSizeLocked()) return;
                // See wireDrag: only a gesture that lost its release can still
                // be live here, and two of them must not share one window.
                if (endGesture) endGesture();
                e.preventDefault();
                e.stopPropagation();
                bringToFront(win.id);
                const startX = e.clientX, startY = e.clientY;
                const startLeft = win.dom.offsetLeft;
                const startTop = win.dom.offsetTop;
                const startW = win.dom.offsetWidth;
                const startH = win.dom.offsetHeight;
                const desktop = document.getElementById('desktop');
                const dRect = desktop.getBoundingClientRect();
                // Same capture + shield as the drag (#176) — and the reason the
                // resize path needed this fix most: it has no pointer-events
                // neutralisation of any kind, so it stalled the moment the
                // cursor crossed the window's OWN embedded content, which the
                // drag's neutralisation was accidentally hiding.
                const cap = captureGesturePointer(livePointerDown(), e.target, handle);
                let ended = false;
                let unbind = null;
                // Single exit for the whole gesture: drops the tracking
                // listeners, releases the capture, and commits the box unless
                // the window went away under us (nothing worth persisting, and
                // no session left to resize). Idempotent.
                const finish = (commit) => {
                    if (ended) return;
                    ended = true;
                    endGesture = null;
                    if (unbind) unbind();
                    window.removeEventListener('blur', onAbort);
                    if (!commit || win.disposed) return;
                    win.geom = {
                        left: win.dom.offsetLeft,
                        top: win.dom.offsetTop,
                        width: win.dom.offsetWidth,
                        height: win.dom.offsetHeight,
                    };
                    persistWinGeom(win);
                    sendResize(win, true);
                };
                const onMove = (ev) => {
                    if (win.disposed) { finish(false); return; }
                    let nl = startLeft, nt = startTop, nw = startW, nh = startH;
                    const dx = ev.clientX - startX;
                    const dy = ev.clientY - startY;
                    if (dir.indexOf('e') !== -1) nw = Math.max(MIN_W, startW + dx);
                    if (dir.indexOf('s') !== -1) nh = Math.max(MIN_H, startH + dy);
                    if (dir.indexOf('w') !== -1) {
                        const cw = Math.max(MIN_W, startW - dx);
                        nl = startLeft + (startW - cw);
                        nw = cw;
                    }
                    if (dir.indexOf('n') !== -1) {
                        const ch = Math.max(MIN_H, startH - dy);
                        nt = startTop + (startH - ch);
                        nh = ch;
                    }
                    nl = Math.max(0, Math.min(nl, dRect.width - MIN_W));
                    nt = Math.max(0, Math.min(nt, dRect.height - MIN_H));
                    nw = Math.min(nw, dRect.width - nl);
                    nh = Math.min(nh, dRect.height - nt);
                    win.dom.style.left = nl + 'px';
                    win.dom.style.top = nt + 'px';
                    win.dom.style.width = nw + 'px';
                    win.dom.style.height = nh + 'px';
                    scheduleResize(win);
                };
                // No swallowClickAfterGesture here, unlike the drag. This
                // gesture's capture element IS the original mousedown target --
                // the .rh grip -- so the retargeted compat click lands on a
                // grip, and the retargeting has moved it no closer to anything
                // that reacts: nothing on the grip, the window, the desktop or
                // the document listens for click or dblclick, and a grip is not
                // a rename affordance. There is nothing to guard, and the guard
                // was pure collateral here.
                const onUp = () => { finish(true); };
                // pointercancel / an unexpected lostpointercapture / window blur
                // (alt-tab with the button still down). Before #176 a blur mid-
                // resize left both document listeners bound forever, so a later
                // BUTTONLESS mousemove kept resizing the window. Commit the box
                // where it got to — same choice the drag's abort path makes.
                const onAbort = () => { finish(true); };
                unbind = bindGestureTracking(cap, handle, onMove, onUp, onAbort);
                window.addEventListener('blur', onAbort);
                endGesture = () => finish(false);
            };
            handle.addEventListener('mousedown', onDown);
            win.cleanups.push(() => {
                handle.removeEventListener('mousedown', onDown);
                if (endGesture) endGesture();
            });
        }

