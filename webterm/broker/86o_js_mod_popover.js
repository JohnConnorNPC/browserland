        // ---- ctx.popover: the core-owned, anchored, self-dismissing popover (#202)
        //
        //     const p = ctx.popover.anchor(node, anchorEl, {
        //         placement: 'bottom-start',   // | bottom-end | bottom
        //                                      // | top-start | top-end | top
        //         gap: 2,                      // px between anchor and node
        //         onClose: function (why) {},  // 'close' | 'outside' | 'escape'
        //                                      // | 'anchor-gone' | 'teardown'
        //     });
        //     p.close();          // acts on THIS popover only
        //     p.reposition();     // re-measure now (after a synchronous fill)
        //     p.isOpen();         // false once anything has dismissed it
        //
        // WHAT IT REPLACES. mods/git hand-anchors its branch popover off
        // `gitBtn.offsetLeft/offsetTop` inside the title bar and installs its
        // OWN document-level capture listeners for mousedown and Escape, plus a
        // `torn` flag and a teardown that has to remember to close the popover
        // so those document listeners are dropped. mods/workspaces appends its
        // preview box to document.body and clamps by hand:
        //     left = Math.max(6, Math.min(left, window.innerWidth - bw - 6))
        // with a flip to `ar.bottom + 8` when it would run off the top. Both of
        // those are this fragment's job now. THE MIGRATION IS #204's, NOT THIS
        // ONE'S: nothing here changes a mod, and both hand-rolled popovers keep
        // working exactly as they do today.
        //
        // NO ANIMATION, DELIBERATELY. Not "animate unless prefers-reduced-
        // motion" -- that is the shape that reads as broken-or-fine depending
        // on who is testing, which is the argument 15_css_dialogs.css already
        // makes for the Control Panel dress: "anything gated on
        // prefers-reduced-motion is simply instant for a viewer who has it set".
        // A popover appears and disappears. There is no entrance transition, no
        // exit transition, no keyframe, and nothing in this range reads
        // matchMedia -- so there is no OS query for an app setting to have to
        // beat, and no way for a reduced-motion viewer to see a different
        // popover from anyone else. (The repo's one motion knob, #125's
        // `slideScreenMs`, is about the tiled-strip slide and is not consulted
        // here: this chrome has no motion for it to scale.)
        //
        // ONE rAF WATCH, NOT FIVE LISTENERS. Every way an anchored box goes
        // wrong is the same failure -- the anchor's rect moved, or the node's
        // size changed, and nobody recomputed. A window `resize` listener, a
        // capture-phase `scroll` listener and a ResizeObserver are three
        // partial answers to that one question; a per-frame re-measure while at
        // least one popover is open is the whole answer, and it costs nothing
        // when none is (the loop is not running) and nothing in a background
        // tab (rAF does not fire). It is what covers a window being DRAGGED,
        // which fires neither resize nor scroll.
        //
        // Its own fragment for the reason 86e-86n each got one: 86c is at the
        // #68 2500-line cap and the rule there is split, never trim. Ordered
        // after 86c because it uses that fragment's extender registry and
        // capability map, and before the mod-script splice because a mod's init
        // reads the finished ctx.

        // Every open popover on the page, from every mod, in open order. The
        // list is the z-order (last = topmost) and the outside-click order.
        const _MOD_POPOVERS = [];
        let _modPopoverRaf = null;
        let _modPopoverDocBound = false;

        // The viewport inset the clamp keeps. workspaces' hand-rolled clamp
        // used 6px; this is that number, owned in one place.
        const _MOD_POPOVER_EDGE = 6;

        function _modPopoverViewport() {
            let w = 0, h = 0;
            try {
                w = window.innerWidth || 0;
                h = window.innerHeight || 0;
            } catch (_) { w = 0; h = 0; }
            return { w: w || 1024, h: h || 768 };
        }

        function _modPopoverRect(el) {
            try {
                const r = el.getBoundingClientRect();
                if (!r) return null;
                return { left: r.left, top: r.top, right: r.right,
                         bottom: r.bottom, width: r.width, height: r.height };
            } catch (_) { return null; }
        }

        // Is the anchor still on the page? An anchor ripped out from under an
        // open popover is the case a hand-positioned popover never handled: it
        // leaves a box floating at coordinates that no longer mean anything,
        // pointing at a button that is gone. `isConnected` is the check (never
        // a parentNode walk: a detached subtree still has parents).
        function _modPopoverNodeLive(entry) {
            try {
                const n = entry && entry.node;
                if (!n) return false;
                if (typeof n.isConnected === 'boolean') return n.isConnected;
                return true;            // an engine without isConnected
            } catch (_) { return false; }
        }
        function _modPopoverAnchorLive(entry) {
            const a = entry.anchor;
            if (!a) return false;
            try {
                if (typeof a.isConnected === 'boolean') return a.isConnected;
            } catch (_) { return false; }
            return true;
        }

        // Place ONE popover. Reads the anchor's CURRENT rect and the node's
        // CURRENT measured size every time, so a node the mod refilled after
        // anchoring is clamped at its new size rather than its old one.
        //
        // Clamping is core's job here, and it is a clamp with a FLIP: a
        // bottom-placed popover that would run off the bottom is put above the
        // anchor instead (and vice versa), because sliding it up would cover
        // the very control it is anchored to. Only after the flip is the
        // remaining overflow clamped, and the horizontal axis is a pure clamp.
        function _modPopoverPlace(entry) {
            const ar = _modPopoverRect(entry.anchor);
            if (!ar) return false;
            const node = entry.node;
            let nw = 0, nh = 0;
            try {
                nw = node.offsetWidth || 0;
                nh = node.offsetHeight || 0;
            } catch (_) { nw = 0; nh = 0; }
            if (!nw || !nh) {
                const nr = _modPopoverRect(node);
                if (nr) { nw = nw || nr.width; nh = nh || nr.height; }
            }
            const vp = _modPopoverViewport();
            const gap = entry.gap;
            const p = entry.placement;
            const edge = _MOD_POPOVER_EDGE;

            let left;
            if (p === 'bottom-end' || p === 'top-end') {
                left = ar.right - nw;
            } else if (p === 'bottom' || p === 'top') {
                left = ar.left + ar.width / 2 - nw / 2;
            } else {
                left = ar.left;                       // *-start (the default)
            }
            const maxLeft = vp.w - nw - edge;
            left = Math.max(edge, (maxLeft < edge) ? edge : Math.min(left, maxLeft));

            const wantsTop = (p === 'top' || p === 'top-start' || p === 'top-end');
            let top = wantsTop ? (ar.top - nh - gap) : (ar.bottom + gap);
            if (wantsTop) {
                // Would run off the top -> flip below, if below actually fits.
                if (top < edge && ar.bottom + gap + nh <= vp.h - edge) {
                    top = ar.bottom + gap;
                }
            } else if (top + nh > vp.h - edge && ar.top - gap - nh >= edge) {
                top = ar.top - nh - gap;              // flip above
            }
            const maxTop = vp.h - nh - edge;
            top = Math.max(edge, (maxTop < edge) ? edge : Math.min(top, maxTop));

            left = Math.round(left);
            top = Math.round(top);
            if (entry.left === left && entry.top === top
                && entry.w === nw && entry.h === nh) return false;
            entry.left = left; entry.top = top; entry.w = nw; entry.h = nh;
            try {
                node.style.position = 'fixed';
                node.style.left = left + 'px';
                node.style.top = top + 'px';
            } catch (_) {}
            return true;
        }

        // Close ONE popover, exactly once. Removing the node is core's job too:
        // a popover must go away when its mod goes away, and the mod that is
        // gone is in no position to remove anything.
        function _modPopoverClose(entry, why) {
            if (!entry || entry.closed) return false;
            entry.closed = true;
            const i = _MOD_POPOVERS.indexOf(entry);
            if (i >= 0) _MOD_POPOVERS.splice(i, 1);
            try {
                const n = entry.node;
                if (n && n.parentNode) n.parentNode.removeChild(n);
            } catch (_) {}
            entry.node = null;
            entry.anchor = null;
            const cb = entry.onClose;
            entry.onClose = null;
            _modPopoverSync();
            if (typeof cb === 'function') {
                try { cb(why || 'close'); } catch (e) {
                    try { console.error('[popover] onClose failed:', e); } catch (_) {}
                }
            }
            return true;
        }

        // The per-frame pass: drop popovers whose anchor left the page, and
        // re-place the rest. This single loop is what covers a scroll, a
        // resize, a window DRAG, and a node the mod mutated after anchoring.
        function _modPopoverTick() {
            _modPopoverRaf = null;
            for (const entry of _MOD_POPOVERS.slice()) {
                if (entry.closed) continue;
                // The NODE is checked as well as the anchor. A mod that removes
                // its own popover node while the anchor is still on screen
                // would otherwise leave the entry live forever: isOpen() true,
                // onClose never fired, the document listeners still bound, and
                // this loop rescheduling itself for the life of the page to
                // measure and style a detached element.
                if (!_modPopoverNodeLive(entry)) {
                    _modPopoverClose(entry, 'node-gone');
                    continue;
                }
                if (!_modPopoverAnchorLive(entry)) {
                    _modPopoverClose(entry, 'anchor-gone');
                    continue;
                }
                _modPopoverPlace(entry);
            }
            _modPopoverSync();
        }

        // Arm the loop and the document listeners while anything is open;
        // disarm both when the last popover closes, so an idle page carries
        // neither a running rAF chain nor a capture listener.
        function _modPopoverSync() {
            const want = _MOD_POPOVERS.length > 0;
            if (want && _modPopoverRaf === null) {
                if (typeof requestAnimationFrame === 'function') {
                    _modPopoverRaf = requestAnimationFrame(_modPopoverTick);
                } else {
                    _modPopoverRaf = setTimeout(_modPopoverTick, 16);
                }
            } else if (!want && _modPopoverRaf !== null) {
                try {
                    if (typeof cancelAnimationFrame === 'function') {
                        cancelAnimationFrame(_modPopoverRaf);
                    } else { clearTimeout(_modPopoverRaf); }
                } catch (_) {}
                _modPopoverRaf = null;
            }
            if (want && !_modPopoverDocBound) {
                document.addEventListener('mousedown', _modPopoverOutside, true);
                document.addEventListener('keydown', _modPopoverKey, true);
                _modPopoverDocBound = true;
            } else if (!want && _modPopoverDocBound) {
                document.removeEventListener('mousedown', _modPopoverOutside, true);
                document.removeEventListener('keydown', _modPopoverKey, true);
                _modPopoverDocBound = false;
            }
        }

        function _modPopoverContains(el, target) {
            if (!el || !target) return false;
            if (el === target) return true;
            try {
                return !!(el.contains && el.contains(target));
            } catch (_) { return false; }
        }

        // Outside-click dismissal, ONE listener for the whole page rather than
        // a pair per popover. The anchor counts as inside: an anchor is nearly
        // always a toggle, and treating a click on it as "outside" makes the
        // toggle close-then-reopen and never close (git's popover carries a
        // hand-written special case for exactly this).
        //
        // With two mods' popovers open at once, a click inside one closes the
        // OTHER and leaves the one it landed in -- each popover is judged
        // against its own node, not against "the" popover.
        function _modPopoverOutside(e) {
            const target = e && e.target;
            for (const entry of _MOD_POPOVERS.slice()) {
                if (entry.closed) continue;
                if (_modPopoverContains(entry.node, target)) continue;
                if (_modPopoverContains(entry.anchor, target)) continue;
                _modPopoverClose(entry, 'outside');
            }
        }

        // Escape closes the TOPMOST popover only, and swallows the key so it
        // does not also reach whatever is behind it. Peeling one layer per
        // press is what makes two stacked popovers dismissable in the order a
        // reader expects.
        function _modPopoverKey(e) {
            if (!e || e.key !== 'Escape') return;
            const entry = _MOD_POPOVERS[_MOD_POPOVERS.length - 1];
            if (!entry) return;
            try { e.preventDefault(); } catch (_) {}
            // stopImmediatePropagation, not stopPropagation: the listeners
            // that matter here are on the SAME document node (a hand-rolled
            // mod popover's own capture listener is the case), and
            // stopPropagation does not stop those -- so Escape would peel
            // two layers at once while claiming to peel the topmost.
            try { e.stopImmediatePropagation(); } catch (_) {}
            _modPopoverClose(entry, 'escape');
        }

        // Teardown. `ctx.onUnload` and NOT #195's `info.onModTeardown`: that
        // one is per WINDOW, delivered to a registered window kind, and a
        // popover is not owned by a window at all -- workspaces anchors its
        // preview off a taskbar dot with no window anywhere in the chain. The
        // per-MOD disposer is the one whose lifetime actually matches "every
        // popover this activation opened", and it fires for a disable, a
        // reload and an uninstall alike.
        function _dropModPopovers(rec) {
            let n = 0;
            for (const entry of _MOD_POPOVERS.slice()) {
                if (entry.rec !== rec) continue;
                if (_modPopoverClose(entry, 'teardown')) n++;
            }
            return n;
        }

        function _modPopoverPlacement(v) {
            switch (v) {
                case 'bottom': case 'bottom-end':
                case 'top': case 'top-start': case 'top-end':
                case 'bottom-start':
                    return v;
                default:
                    return 'bottom-start';
            }
        }

        // anchor(). The handle comes back synchronously and always: a refused
        // anchor (dead activation, missing node or anchor) answers with a
        // closed handle rather than a null, so a caller never has to branch on
        // whether it got one.
        function _anchorModPopover(rec, node, anchorEl, opts) {
            const o = (opts && typeof opts === 'object') ? opts : {};
            const entry = {
                rec: rec,
                owner: (rec && typeof rec.id === 'string') ? rec.id : '',
                node: node || null,
                anchor: anchorEl || null,
                placement: _modPopoverPlacement(o.placement),
                gap: (typeof o.gap === 'number' && isFinite(o.gap))
                    ? Math.max(0, Math.min(64, o.gap)) : 2,
                onClose: (typeof o.onClose === 'function') ? o.onClose : null,
                closed: false,
                left: null, top: null, w: null, h: null,
            };
            const handle = {
                close: function () { return _modPopoverClose(entry, 'close'); },
                reposition: function () {
                    if (entry.closed) return false;
                    if (!_modPopoverAnchorLive(entry)) {
                        _modPopoverClose(entry, 'anchor-gone');
                        return false;
                    }
                    return _modPopoverPlace(entry);
                },
                isOpen: function () { return !entry.closed; },
            };
            if (!node || !anchorEl || (rec && rec.unloading)) {
                entry.closed = true;
                entry.node = null;
                entry.anchor = null;
                entry.onClose = null;
                return handle;
            }
            // Re-anchoring a node that is already in an open popover replaces
            // that popover rather than tracking one node from two entries --
            // two entries would fight over the same left/top every frame.
            for (const other of _MOD_POPOVERS.slice()) {
                if (other.node === node) _modPopoverClose(other, 'close');
            }
            try {
                node.classList.add('mod-popover');
                node.style.position = 'fixed';
                // Off-screen for the measuring frame: the node is in the tree
                // (so it has a size) but never painted at 0,0 first.
                node.style.left = '-10000px';
                node.style.top = '-10000px';
                if (!node.parentNode) document.body.appendChild(node);
            } catch (e) {
                try { console.error('[popover] anchor failed:', e); } catch (_) {}
                entry.closed = true;
                return handle;
            }
            _MOD_POPOVERS.push(entry);
            _modPopoverPlace(entry);
            _modPopoverSync();
            return handle;
        }

        // The extender. The teardown disposer is registered BEFORE the surface
        // that can open anything (the house rule: register the disposer before
        // anything that can throw), so there is no window in which a mod can
        // anchor a popover that nothing would ever remove.
        function _ctxPopover(ctx, rec) {
            if (typeof ctx.onUnload === 'function') {
                ctx.onUnload(function () { _dropModPopovers(rec); });
            }
            ctx.popover = {
                anchor: function (node, anchorEl, opts) {
                    return _anchorModPopover(rec, node, anchorEl, opts);
                },
            };
        }
        // Guarded like every other registration in an extension fragment: a
        // page assembled without #194's registry or #197's capability map must
        // not throw at load.
        if (typeof _registerCtxExtender === 'function') {
            _registerCtxExtender(_ctxPopover);
        }
        // A NEW top-level family, so it registers its own capability entry
        // beside the surface it names (#197: the map is a true inventory, and
        // the v1 seed loop must stay byte-equal to makeCtx's literal).
        if (typeof _registerModCapability === 'function') {
            _registerModCapability('popover', 1);
        }
        // ---- end ctx.popover -------------------------------------------------
