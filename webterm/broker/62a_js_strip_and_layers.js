        // ---- custom tiling-strip scrollbar (Part A) -----------------------
        // A slim themed overlay bar (#strip-scrollbar, a sibling of #strip so it
        // survives teardownView) shown whenever the tiling strip can scroll. It
        // never resizes the strip (overlay → no terminal reflow). Two passes:
        //   updateStripScrollbar() — the metrics pass (reads scrollWidth, caches
        //     {maxScroll,thumbW,maxThumbX}, sets thumb width, shows/hides). Run
        //     on layout/option/resize changes, NOT on every scroll frame.
        //   positionStripThumb()  — the cheap per-scroll pass: from the cached
        //     metrics + live scrollLeft, set the thumb's left. Called from
        //     onStripScroll so a scroll burst never re-reads scrollWidth (which
        //     would force a per-frame reflow against onStripScroll's writes).
        let _sbMetrics = null;
        let _sbEls = null;       // {strip,bar,thumb} resolved once (static nodes)
        function sbEls() {
            if (_sbEls) return _sbEls;
            const strip = document.getElementById('strip');
            const bar = document.getElementById('strip-scrollbar');
            if (!strip || !bar) return null;
            const thumb = bar.querySelector('.sb-thumb');
            if (!thumb) return null;
            _sbEls = { strip, bar, thumb };
            return _sbEls;
        }
        function updateStripScrollbar() {
            const els = sbEls();
            if (!els) return;
            const { strip, bar, thumb } = els;
            const cw = strip.clientWidth;
            const sw = strip.scrollWidth;
            const on = getSettings().stripScrollbar && isTilingMode()
                && !_deactivated;
            const canScroll = (sw - cw) > 1;
            if (!on || !canScroll) {
                bar.style.display = 'none';
                _sbMetrics = null;
                return;
            }
            const maxScroll = sw - cw;
            // Clamp to cw so a very narrow viewport (cw < 24) can't make the
            // thumb overflow the track (and maxThumbX stays >= 0).
            const thumbW = Math.min(cw, Math.max(24, Math.round((cw * cw) / sw)));
            const maxThumbX = Math.max(0, cw - thumbW);
            _sbMetrics = { maxScroll, thumbW, maxThumbX };
            thumb.style.width = thumbW + 'px';
            bar.style.display = 'block';
            positionStripThumb();
        }
        function positionStripThumb() {
            const m = _sbMetrics;
            if (!m) return;                       // bar hidden → nothing to do
            const els = sbEls();
            if (!els) return;
            const frac = (m.maxScroll > 0)
                ? Math.max(0, Math.min(1, els.strip.scrollLeft / m.maxScroll)) : 0;
            // transform (compositor-only) instead of `left` so a scroll burst
            // never triggers layout for the thumb.
            els.thumb.style.transform =
                'translateX(' + Math.round(frac * m.maxThumbX) + 'px)';
        }
        // Thumb drag: map the pointer delta to strip.scrollLeft; setting it fires
        // the native scroll event → onStripScroll (floating drag) +
        // positionStripThumb, so we never move the thumb directly here. Wired
        // once at startup (the bar element is static). Document-level move/up
        // listeners mirror the other drag patterns (wireDrag).
        (function wireStripScrollbar() {
            const els = sbEls();
            if (!els) return;
            const { strip, thumb } = els;
            let dragging = false, startX = 0, startScroll = 0;
            function onMove(e) {
                if (!dragging) return;
                const m = _sbMetrics;
                if (!m || m.maxThumbX <= 0) return;
                const frac = (e.clientX - startX) / m.maxThumbX;
                strip.scrollLeft = Math.max(0, Math.min(m.maxScroll,
                    startScroll + frac * m.maxScroll));
                e.preventDefault();
            }
            function onUp() {
                if (!dragging) return;
                dragging = false;
                thumb.classList.remove('dragging');
                document.removeEventListener('mousemove', onMove, true);
                document.removeEventListener('mouseup', onUp, true);
                window.removeEventListener('blur', onUp, true);
            }
            thumb.addEventListener('mousedown', (e) => {
                if (e.button !== 0 || !_sbMetrics) return;
                dragging = true;
                startX = e.clientX;
                startScroll = strip.scrollLeft;
                thumb.classList.add('dragging');
                document.addEventListener('mousemove', onMove, true);
                document.addEventListener('mouseup', onUp, true);
                // Lost focus mid-drag (alt-tab, etc.) never delivers mouseup →
                // release on blur so the thumb can't stick in 'grabbing'.
                window.addEventListener('blur', onUp, true);
                e.preventDefault();
                e.stopPropagation();
            });
        })();

        // ---- floating scroll-lock -----------------------------------------
        // Default-unlocked floating windows travel with the strip: on every
        // strip scroll we shift each unlocked, non-minimized floating window's
        // geom.left (kept screen-relative, so drag/resize/clamp stay unchanged)
        // by the scroll delta, gluing it to the columns underneath. Locked
        // windows stay pinned to the screen. geom.left is updated in memory
        // only — pref.geom persists at the last drag/resize, so a reload
        // restores the window near where the user last placed it.
        let _lastStripScroll = 0;
        function onStripScroll() {
            const strip = document.getElementById('strip');
            if (!strip) return;
            const sl = strip.scrollLeft;
            const delta = sl - _lastStripScroll;
            _lastStripScroll = sl;
            if (!delta) return;
            for (const win of windows.values()) {
                if (win.disposed || win.tiled || win.minimized || win.locked) continue;
                win.geom.left -= delta;
                win.dom.style.left = win.geom.left + 'px';
            }
            positionStripThumb();   // cheap: cached metrics + live scrollLeft
        }
        function setWindowLocked(win, locked) {
            if (!win || win.disposed) return;
            win.locked = !!locked;
            getPref(win.id).locked = win.locked;
            win.dom.classList.toggle('scroll-locked', win.locked);
            savePrefs();
        }
        function toggleWindowLock(win) {
            if (!win || win.disposed) return;
            // App windows persist lock state in the app store — the prefs-backed
            // setWindowLocked path would write an 'app:' key the prefs GC then
            // clobbers, silently losing the pin/unpin choice across a poll.
            if (win.type === 'app') {
                win.locked = !win.locked;
                win.dom.classList.toggle('scroll-locked', win.locked);
                saveAppWindow(win);
                return;
            }
            setWindowLocked(win, !win.locked);
        }

        // ---- layer moves (float <-> tile) ---------------------------------
        // placeWindowTiled: make a window tiled NOW (synchronous relayout so a
        // freshly-opened window measures its final tiled box before the term
        // first renders). Ensures membership without resizing existing columns.
        function placeWindowTiled(win) {
            win.tiled = true;
            getPref(win.id).tiled = true;
            if (!win.floatGeom) {
                const pf = getPref(win.id).floatGeom;
                win.floatGeom = pf
                    ? Object.assign({}, pf)
                    : currentFloatGeom(win);
            }
            if (!findKeyInLayout(win.id)) {
                layoutAddColumn(win.id, DEFAULT_NEW_PRESET);
            }
            win.dom.classList.add('tiled');
            win.dom.style.left = '';
            win.dom.style.top = '';
            win.dom.style.width = '';
            win.dom.style.height = '';
            win.dom.style.zIndex = '';
            // A window whose membership is in an INACTIVE workspace (e.g. a
            // reattach after reload into a parked workspace) is parked, not
            // measured — it mounts and resizes when its workspace is shown.
            const loc = findKeyInLayout(win.id);
            if (loc && loc.wsIndex !== getLayout().activeWs) {
                parkWindow(win);
                return;
            }
            relayoutStrip();
        }
        // The window's current floating box. Uses the live laid-out rect when
        // visible; falls back to the tracked geom for a minimized/display:none
        // window (whose offset* are all 0 — capturing those would break a
        // later un-tile's geometry restore).
        function currentFloatGeom(win) {
            if (!win.minimized) {
                const w = win.dom.offsetWidth, h = win.dom.offsetHeight;
                if (w > 0 && h > 0) {
                    return { left: win.dom.offsetLeft, top: win.dom.offsetTop,
                             width: w, height: h };
                }
            }
            const g = win.geom || getPref(win.id).geom || defaultGeom();
            return { left: g.left | 0, top: g.top | 0,
                     width: g.width | 0, height: g.height | 0 };
        }
        // attachToStrip: float -> tile, snapshotting the floating geom so an
        // un-tile can restore the hand-arranged box.
        function attachToStrip(win, atIndex) {
            if (!win || win.disposed) return;
            win.floatGeom = currentFloatGeom(win);
            getPref(win.id).floatGeom = Object.assign({}, win.floatGeom);
            if (!findKeyInLayout(win.id)) {
                layoutAddColumn(win.id, DEFAULT_NEW_PRESET, atIndex);
            }
            placeWindowTiled(win);
            bringToFront(win.id);
            // App windows: snapshot the float-box to the app store (their
            // prefs 'app:' key is junk the GC drops; appStore is authoritative
            // for content/geom/lock). Tiling membership lives in _layout.
            if (win.type === 'app') saveAppWindow(win);
        }
        // detachToFloat: tile -> float, restoring the snapshotted geom (or a
        // fresh default), reparenting back above the strip.
        function detachToFloat(win) {
            if (!win || win.disposed) return;
            layoutRemoveKey(win.id);
            getPref(win.id).tiled = false;
            win.tiled = false;
            win.dom.classList.remove('tiled');
            // A window floated straight out of a tabbed column (e.g. an
            // inactive tab) must not stay display:none (task 10).
            win.dom.classList.remove('tab-hidden');
            win.dom.style.flex = '';
            const desktop = document.getElementById('desktop');
            desktop.appendChild(win.dom);
            const geom = clampGeom(win.floatGeom
                || getPref(win.id).floatGeom || defaultGeom());
            applyGeomToWindow(win, geom);
            savePrefs();
            bringToFront(win.id);
            requestRelayout();
            refitSoon(win);
        }
        // removeFromStrip: drop a key from the strip model (close/minimize),
        // collapsing its column and reflowing. The window teardown itself is
        // the caller's job.
        function removeFromStrip(key) {
            layoutRemoveKey(key);
            requestRelayout();
        }

        // ---- parking (off-screen custody) ---------------------------------
        // A window the strip is not currently mounting lives in #park
        // (display:none) — its xterm/WebSocket stay alive but the isResizable()
        // guard blocks any resize while parked (zero rect). On re-activation:
        // mount via relayout, then resize once the boxes have laid out
        // (relayout's own double-RAF tail). Core parks a minimized/dormant
        // window; the workspaces mod (#148) parks every window belonging to an
        // inactive workspace, which is what the mechanism was built for (P5).
        function parkWindow(win) {
            const park = document.getElementById('park');
            if (park && win && !win.disposed && win.dom.parentElement !== park) {
                park.appendChild(win.dom);
            }
        }
