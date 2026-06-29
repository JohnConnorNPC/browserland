        // ---- drag / resize wiring -----------------------------------------
        // Persist a floating window's geom to the right store: app windows to
        // the separate app store (their 'app:' keys would be GC'd from prefs),
        // terminals to prefs.
        function persistWinGeom(win) {
            if (win.type === 'app') { saveAppWindow(win); return; }
            getPref(win.id).geom = Object.assign({}, win.geom);
            savePrefs();
        }
        function wireDrag(win, handle) {
            const onDown = (e) => {
                if (e.button !== 0) return;
                // Tiled windows: title-bar drag reorders / consumes / detaches
                // via the strip engine (overlays during drag, one mutation on
                // drop). The floating absolute-move below would be a no-op on a
                // static box, so hand off and return.
                if (win.tiled) { startTiledDrag(win, e); return; }
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
                // Take dragged window out of elementFromPoint hit-testing so
                // we can find the window underneath the cursor for swap mode.
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
                const teardown = () => {
                    clearHold();
                    document.removeEventListener('mousemove', onMove);
                    document.removeEventListener('mouseup', onUp);
                    document.removeEventListener('keydown', onKey, true);
                    document.removeEventListener('keyup', onKeyUp, true);
                    window.removeEventListener('blur', onBlur);
                    win.dom.classList.remove('swap-source');
                    if (snapMode) {
                        document.body.classList.remove('tiled-dragging');
                        win.dom.classList.remove('drag-source');
                        hideDropEls();
                        hideSnapCancel();
                    }
                    snapMode = false;
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
                    // pointer-events:none so it's transparent to the probe).
                    if (ev.shiftKey || ev.altKey) {
                        clearHold();   // modifier drags own the gesture; no dwell
                        const hit = document.elementFromPoint(ev.clientX, ev.clientY);
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
                    const wasSnap = snapMode;
                    teardown();
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
                    // between the last mousemove and mouseup.
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
                // A lost mouseup (window blur / alt-tab) must not leave the drag
                // half-alive (listeners live, pointer-events:none, stale swap UI).
                // Always tear down + restore pointer-events. In snap mode this is
                // a cancel back to the pre-drag box (undo the gesture); pre-snap it
                // drops the window where it currently sits, mirroring the missing
                // no-modifier mouseup so the existing floating-drag isn't regressed.
                const onBlur = () => {
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
                document.addEventListener('mousemove', onMove);
                document.addEventListener('mouseup', onUp);
                document.addEventListener('keydown', onKey, true);
                document.addEventListener('keyup', onKeyUp, true);
                window.addEventListener('blur', onBlur);
                armDwell();   // start the dwell clock at grab
            };
            handle.addEventListener('mousedown', onDown);
            win.cleanups.push(() => handle.removeEventListener('mousedown', onDown));
        }

        function wireResize(win, handle, dir) {
            const onDown = (e) => {
                if (e.button !== 0) return;
                if (win.tiled) return;       // flex-sized; handles are hidden
                if (isSizeLocked()) return;
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
                const onMove = (ev) => {
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
                const onUp = () => {
                    document.removeEventListener('mousemove', onMove);
                    document.removeEventListener('mouseup', onUp);
                    win.geom = {
                        left: win.dom.offsetLeft,
                        top: win.dom.offsetTop,
                        width: win.dom.offsetWidth,
                        height: win.dom.offsetHeight,
                    };
                    persistWinGeom(win);
                    sendResize(win, true);
                };
                document.addEventListener('mousemove', onMove);
                document.addEventListener('mouseup', onUp);
            };
            handle.addEventListener('mousedown', onDown);
            win.cleanups.push(() => handle.removeEventListener('mousedown', onDown));
        }

