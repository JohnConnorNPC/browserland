        // ---- info.cellDims / info.setFont / ctx.terminals (#201) -----------
        // Two gaps closed together, because they are the same gap: a mod that
        // wants to know how big a cell is, or to change what a terminal is
        // rendered in, has had to reach INTO xterm and guess.
        //
        //     ctx.windows.onTerminalCreate(function (info) {
        //         const d = info.cellDims();      // {width, height} | null
        //         info.setFont('Iosevka, monospace');
        //         info.setFont(null);             // drop MY override
        //     });
        //     ctx.terminals.defaults.fontFamily;  // the core baseline
        //
        // cellDims() RETURNS null BEFORE THE FIRST RENDER. There is no 9x17
        // fallback here and there must never be one: recorder
        // (mods/recorder/recorder.js) reads
        // `term._core._renderService.dimensions` and falls back to a hardcoded
        // 9x17 when it is not there yet, which is not a measurement, it is a
        // number that happens to be close on one machine. A caller that needs
        // a cell box before anything has been rendered has to handle null --
        // that is the honest answer, and it is the one this returns. Nothing
        // is cached into prefs on the way out either: 73's readCellDims writes
        // the measurement into settings because CORE uses it to size a window
        // before one is open; a MOD asking a question must not move that
        // stored value.
        //
        // setFont IS LAST-WRITER-WINS **WITH AN OWNER RECORD**. Core keeps,
        // per terminal, the ordered list of which mod set which family; the
        // last entry is what is on screen. That record is the whole point:
        // when a mod is disabled, its entry is removed and the terminal
        // reverts TO THE SURVIVING WRITER IF ONE REMAINS, ELSE TO THE
        // BASELINE. A plain "remember the previous value and restore it on
        // unload" scheme -- which is what a mod can implement for itself, and
        // what termfont does -- gets both of the interesting orders wrong: two
        // font mods on one terminal either strand a dead mod's font on screen
        // or stomp a live mod's font back to the baseline, depending on which
        // one is turned off. Ownership is by MOD id, so a mod calling setFont
        // twice on one terminal updates its own entry rather than stacking.
        //
        // CORE OWNS THE APPLY. The override is written to `term.options` and
        // followed by a refit, here, once -- so the revert path is the same
        // code as the set path and cannot drift from it. The refit is
        // CORE'S OWN `refitSoon(win)` (73), deliberately NOT `fitAddon.fit()`:
        // core loads exactly one addon and never drives it (a checked-in test
        // pins that, because an addon-driven resize is invisible to core's
        // call-site hooks), and termfont's fit() is recorded in
        // docs-terminal-funnels.md as a named LIMIT rather than a pattern to
        // copy. refitSoon re-measures the cell box and goes out through the
        // existing resize funnel, so this adds no new funnel call site.
        const _termFontOwners = new WeakMap();   // win -> [{owner, family}]

        // The per-window owner stack. A WeakMap keyed by `win`, the shape 86k
        // uses, so a terminal that no mod ever styled costs nothing and a
        // closed window is not held alive by its font history.
        function _termFontStack(win, create) {
            if (!win || typeof win !== 'object') return null;
            let stack = null;
            try { stack = _termFontOwners.get(win) || null; }
            catch (_) { return null; }
            if (stack || !create) return stack;
            stack = [];
            try { _termFontOwners.set(win, stack); } catch (_) { return null; }
            return stack;
        }

        // What SHOULD be on screen right now: the last surviving writer's
        // family, or core's baseline when nobody owns this terminal. The
        // baseline is read from 67's `TERM_FONT_BASELINE` -- the single
        // source; no second copy of the literal exists in core.
        function _termFontWanted(win) {
            const stack = _termFontStack(win, false);
            if (stack && stack.length) return stack[stack.length - 1].family;
            return TERM_FONT_BASELINE;
        }

        // The apply path. Idempotent (xterm re-measures on an options write,
        // so setting the family it already has is not free) and total: a
        // disposed window is skipped, and a build whose Terminal has no
        // `options` bag is left alone rather than thrown through.
        function _termFontApply(win) {
            if (!win || win.disposed) return false;
            const term = win.term;
            if (!term || !term.options) return false;
            const family = _termFontWanted(win);
            try {
                if (term.options.fontFamily === family) return false;
                term.options.fontFamily = family;
            } catch (e) {
                try { console.error('[windows] setFont failed:', e); }
                catch (_) {}
                return false;
            }
            // Cell metrics just changed, so the grid must be recomputed or the
            // terminal keeps a cols/rows that no longer fits its box.
            // refitSoon defers two frames and bails on a disposed window; a
            // hidden / 0x0 terminal is dropped inside sendResize, so this is
            // safe to call before anything has rendered.
            try {
                if (typeof refitSoon === 'function') refitSoon(win);
            } catch (_) { /* nothing measurable yet: the next refit covers it */ }
            return true;
        }

        // Record `owner`'s override and apply. An empty / non-string family
        // means "drop mine", which is the same code path as a teardown --
        // deliberately, so a mod cannot get a revert that core's own unload
        // does not also get.
        function _termFontSet(win, owner, family, rec) {
            if (!win || typeof owner !== 'string' || !owner) return false;
            // A DEAD activation may not take ownership. Teardown releases the
            // owner but the arming is one-shot, so a retained bag calling
            // setFont from a stray callback after its mod was disabled used to
            // re-create the owner entry -- with no teardown left to ever remove
            // it, and a live terminal wearing a dead mod's font permanently.
            if (rec && rec.unloading) return false;
            const fam = (typeof family === 'string') ? family.trim() : '';
            if (!fam) return _termFontRelease(win, owner);
            const stack = _termFontStack(win, true);
            if (!stack) return false;
            for (let i = 0; i < stack.length; i++) {
                if (stack[i].owner === owner) { stack.splice(i, 1); break; }
            }
            stack.push({ owner: owner, family: fam });
            _termFontApply(win);
            return true;
        }

        // Remove `owner`'s entry and re-apply whatever is left -- the revert
        // chain. Called by setFont(null), by info.onDispose (where the window
        // is going away, so the removal is bookkeeping only) and by
        // info.onModTeardown (where the terminal is very much alive and the
        // next-surviving font has to land on it).
        function _termFontRelease(win, owner) {
            const stack = _termFontStack(win, false);
            if (!stack || !stack.length) return false;
            let hit = false;
            for (let i = stack.length - 1; i >= 0; i--) {
                if (stack[i].owner === owner) { stack.splice(i, 1); hit = true; }
            }
            if (!hit) return false;
            _termFontApply(win);
            return true;
        }

        // The cell box, or null. Guarded the way 73's readCellDims is (xterm
        // 5.3 private API, shape varies by version, absent until first
        // render), and both spellings are read because the vendored build
        // carries `css.cell` while older ones carry `actualCell*`. A zero
        // width or height is PRE-RENDER, not a measurement -- FitAddon's own
        // proposeDimensions bails on exactly that check.
        function _termCellDims(win) {
            try {
                // A DISPOSED terminal measures nothing. xterm can leave the
                // last dimensions object resident on a disposed render
                // service, so a retained bag would otherwise answer with the
                // box the terminal had when it died -- a stale measurement
                // presented as a current one, which is the same class of lie
                // as the 9x17 fallback this replaces.
                if (!win || win.disposed) return null;
                const term = win && win.term;
                const core = term && term._core;
                const rs = core && core._renderService;
                const dims = rs && rs.dimensions;
                if (!dims) return null;
                const cell = dims.css && dims.css.cell;
                const w = dims.actualCellWidth || (cell && cell.width);
                const h = dims.actualCellHeight || (cell && cell.height);
                if (!w || !h) return null;
                return { width: w, height: h };
            } catch (_) {
                return null;
            }
        }

        // Arm the revert on BOTH ends, at the moment the override is set --
        // 86k's `_armTermTapRemoval`, for the same reason: win.cleanups fires
        // only on window CLOSE, so a mod disabled while its terminals are open
        // would otherwise leave its font on screen forever.
        // The armed set is keyed on the BAG, in a WeakSet this fragment owns --
        // not a property ON the bag. `info` is handed to mod code and to every
        // other extender, so an internal lifecycle flag living there is one
        // `info._fontArmed = 1` away from silently disabling cleanup
        // registration, after which a disabled mod leaves its font on screen
        // for good. A WeakSet cannot be reached by a caller and dies with the
        // bag.
        const _termFontArmed = new WeakSet();
        function _armTermFontRelease(rec, info, win, owner) {
            if (!info) return;
            if (_termFontArmed.has(info)) return;
            const release = function () { return _termFontRelease(win, owner); };
            let armed = false;
            if (info && typeof info.onModTeardown === 'function') {
                try { armed = info.onModTeardown(release) === true; } catch (_) {}
            }
            if (!armed && rec && Array.isArray(rec.unloads)) {
                rec.unloads.push(release);
            }
            if (info && typeof info.onDispose === 'function') {
                try { info.onDispose(release); } catch (_) {}
            }
            try { _termFontArmed.add(info); } catch (_) {}
        }

        // Decorate ONE onTerminalCreate bag, per mod and per window -- the
        // shape #195's info.onModTeardown established and 86k follows: the bag
        // is handed a callback, not a mod record, so the binding to a mod id
        // can only happen out here. That id IS the owner record.
        function _modTerminalFont(rec, info) {
            if (!info || typeof info !== 'object') return info;
            const owner = (rec && typeof rec.id === 'string') ? rec.id : '';
            const members = {
                cellDims: function () { return _termCellDims(info.win); },
                setFont: function (family) {
                    if (!owner) return false;
                    // The record is passed so the set can refuse a DEAD
                    // activation. Arming first would otherwise register a
                    // release on a teardown that has already run.
                    if (rec && rec.unloading) return false;
                    _armTermFontRelease(rec, info, info.win, owner);
                    return _termFontSet(info.win, owner, family, rec);
                },
            };
            try {
                info.cellDims = members.cellDims;
                info.setFont = members.setFont;
                if (info.cellDims === members.cellDims) return info;
            } catch (_) { /* a frozen bag under strict mode: copy instead */ }
            return Object.assign({}, info, members);
        }

        // The extender. Wraps ctx.windows.onTerminalCreate exactly as 86k and
        // #195's teardown extender do, and adds the ONE new top-level family:
        // ctx.terminals. It is a family and not a member of `windows` because
        // it is about terminals that are not (yet) a window handed to this
        // mod -- `defaults.fontFamily` is readable with no window at all,
        // which is the whole point of retiring the duplicated constant.
        function _ctxTerminalFont(ctx, rec) {
            ctx.terminals = Object.freeze({
                defaults: Object.freeze({ fontFamily: TERM_FONT_BASELINE }),
            });
            const fam = ctx.windows;
            if (!fam || typeof fam !== 'object') return;
            const sub = fam.onTerminalCreate;
            if (typeof sub !== 'function') return;
            fam.onTerminalCreate = function (cb) {
                if (typeof cb !== 'function') return sub.call(this, cb);
                return sub.call(this, function (info) {
                    return cb(_modTerminalFont(rec, info));
                });
            };
        }
        if (typeof _registerCtxExtender === 'function') {
            _registerCtxExtender(_ctxTerminalFont);
        }
        // A NEW top-level family, so it registers its own capability entry
        // beside the surface it names (#197: the map is a true inventory, and
        // the v1 seed loop must stay byte-equal to makeCtx's literal).
        if (typeof _registerModCapability === 'function') {
            _registerModCapability('terminals', 1);
        }
        // ---- end info.cellDims / info.setFont / ctx.terminals --------------
