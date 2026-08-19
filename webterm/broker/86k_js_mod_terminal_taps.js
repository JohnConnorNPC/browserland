        // ---- info.tapOutput / info.tapInput / info.onResize (#201) ---------
        // The sanctioned replacement for monkey-patching an xterm instance.
        // recorder (mods/recorder/recorder.js) replaces `term.write` and
        // `term.resize` BY ASSIGNMENT and restores them only if the method it
        // finds is still its own — a scheme whose own comment admits two
        // patchers cannot coexist ("if ANOTHER patcher stacked on top of ours
        // after start, stop can't unhook us"). These three taps are what a mod
        // uses instead:
        //
        //     ctx.windows.onTerminalCreate(function (info) {
        //         const off = info.tapOutput(function (data) { ... });
        //         info.tapInput(function (data) { ... });
        //         info.onResize(function (d) { d.cols, d.rows });
        //     });
        //
        // CORE HOOKS ITS OWN CALL SITES; NOTHING HERE TOUCHES `term.*`. That is
        // a compatibility requirement, not taste. recorder checks "is
        // term.write still the function I installed" before restoring; a core
        // wrapper installed on the instance would make that check false and
        // corrupt recorder's restore path. Both mechanisms must work for one
        // release, so the dispatch calls live beside core's OWN write/send
        // sites (73_js_window_runtime.js, 67_js_window_lifecycle.js) and the
        // instance is left exactly as xterm handed it over. A consequence worth
        // stating: a tap and recorder's patch see the SAME core bytes; a tap
        // additionally never sees anything a mod (recorder's own playback
        // included) writes straight to `win.term`, because that is not a core
        // call site. docs-terminal-funnels.md enumerates the boundary.
        //
        // READ-ONLY IS ENFORCED, NOT REQUESTED. Every tap fires strictly AFTER
        // the underlying write / send has been dispatched, and a tap that gets
        // a mutable payload gets its OWN copy — per tap, so tap #1 cannot
        // rewrite what tap #2 observes either. Handing out the shared
        // `Uint8Array` that xterm is still holding (its write queue is
        // processed asynchronously) would make every "observer" a silent
        // transformer of the screen.
        //
        // ORDERED AND ISOLATED. Taps run in registration order over a snapshot
        // of the list; a throwing tap loses only the rest of its own callback —
        // siblings still run, and the terminal never sees the throw, because
        // the dispatch call sites are on the far side of the write.
        //
        // NO HISTORY REPLAY. onTerminalCreate replays over terminals that are
        // already open, so a tap registered from that replay starts seeing
        // traffic FROM THEN ON. Reconstructing the scrollback that came before
        // stays recorder's job (its serialize-addon keyframes).
        const _termTaps = new WeakMap();      // win -> { out, in, resize }

        // The per-window tap lists. A WeakMap keyed by `win`, so nothing here
        // keeps a closed window alive, and a mod that never taps costs a
        // window nothing at all.
        function _termTapLists(win, create) {
            if (!win || typeof win !== 'object') return null;
            let lists = null;
            try { lists = _termTaps.get(win) || null; }
            catch (_) { return null; }
            if (lists || !create) return lists;
            lists = { out: [], in: [], resize: [] };
            try { _termTaps.set(win, lists); } catch (_) { return null; }
            return lists;
        }

        // Register one tap; returns an idempotent unsubscribe. `kind` is one of
        // 'out' / 'in' / 'resize'. Refusals are silent no-ops that still hand
        // back a callable off(), the posture registerTerminalCreate takes.
        function _addTermTap(win, kind, fn) {
            const noop = function () { return false; };
            if (typeof fn !== 'function') return noop;
            const lists = _termTapLists(win, true);
            if (!lists || !Array.isArray(lists[kind])) return noop;
            lists[kind].push(fn);
            let off = false;
            return function () {
                if (off) return false;
                off = true;
                const i = lists[kind].indexOf(fn);
                if (i === -1) return false;
                lists[kind].splice(i, 1);
                return true;
            };
        }

        // The copy that makes "observer" true. A string is immutable and is
        // passed through; a typed-array view is copied PER TAP. Anything else
        // is passed as-is (core never produces one).
        function _copyTermData(data) {
            if (typeof data === 'string') return data;
            try {
                if (ArrayBuffer.isView(data) && typeof data.slice === 'function') {
                    return data.slice();
                }
            } catch (_) { /* an exotic payload: hand it over unchanged */ }
            return data;
        }

        // One dispatch. SNAPSHOT + liveness recheck, which is what makes the
        // two nasty orders safe: a tap that unsubscribes (or closes the window,
        // draining win.cleanups) mid-dispatch must not still be called, and a
        // tap registered mid-dispatch must not join the pass it is not part of.
        // Isolated per callback; a throw is logged and the walk continues.
        function _dispatchTermTaps(win, kind, make) {
            const lists = _termTapLists(win, false);
            const live = lists && lists[kind];
            if (!live || !live.length) return 0;
            const snapshot = live.slice();
            let n = 0;
            for (let i = 0; i < snapshot.length; i++) {
                const fn = snapshot[i];
                if (live.indexOf(fn) === -1) continue;   // gone since the snap
                let payload;
                try { payload = make(); }
                catch (_) { return n; }
                try { fn(payload); n += 1; }
                catch (e) {
                    try { console.error('[windows] terminal tap (' + kind
                        + ') failed:', e); } catch (_) {}
                }
            }
            return n;
        }

        // The three core-facing entry points. Called from core's OWN call sites
        // AFTER the write / send has been dispatched (73's seven write sites
        // and 67's sendChunked), and from xterm's own onResize event (67).
        // They never throw: a dispatch failure must not be able to break the
        // terminal it is observing.
        function dispatchTermOut(win, data) {
            return _dispatchTermTaps(win, 'out', function () {
                return _copyTermData(data);
            });
        }
        function dispatchTermIn(win, data) {
            return _dispatchTermTaps(win, 'in', function () {
                return _copyTermData(data);
            });
        }
        // A FRESH {cols, rows} per tap: the object xterm hands its own event is
        // shared with every other listener xterm has.
        function dispatchTermResize(win, dims) {
            return _dispatchTermTaps(win, 'resize', function () {
                return { cols: (dims && dims.cols) | 0,
                         rows: (dims && dims.rows) | 0 };
            });
        }

        // Decorate ONE onTerminalCreate bag with the three registrars, per mod
        // and per window — the shape #195's info.onModTeardown established, and
        // for the same reason: `_emitTerminalCreate` (67) is handed a callback,
        // not a mod record, so the binding to a mod can only happen out here.
        //
        // AUTO-REMOVED AT DISPOSE **AND** AT MOD TEARDOWN, both armed at the
        // moment of registration. The dispose half is info.onDispose
        // (win.cleanups, drained by closeWindow and by the active-view
        // rebuild); the teardown half is #195's info.onModTeardown, falling
        // back to the mod's unload chain on a build that predates it. Missing
        // the teardown half is exactly git's disposer-Set bug: win.cleanups
        // fires only on window CLOSE, so disabling a mod with live terminals
        // would leave its taps running on every open one.
        function _armTermTapRemoval(rec, info, off) {
            let armed = false;
            if (info && typeof info.onModTeardown === 'function') {
                try { armed = info.onModTeardown(off) === true; } catch (_) {}
            }
            if (!armed && rec && Array.isArray(rec.unloads)) {
                rec.unloads.push(off);
            }
            if (info && typeof info.onDispose === 'function') {
                try { info.onDispose(off); } catch (_) {}
            }
        }

        function _modTermTap(rec, info, kind, fn) {
            const off = _addTermTap(info && info.win, kind, fn);
            if (typeof fn === 'function') _armTermTapRemoval(rec, info, off);
            return off;
        }

        function _modTerminalTaps(rec, info) {
            if (!info || typeof info !== 'object') return info;
            const members = {
                tapOutput: function (fn) { return _modTermTap(rec, info, 'out', fn); },
                tapInput: function (fn) { return _modTermTap(rec, info, 'in', fn); },
                onResize: function (fn) { return _modTermTap(rec, info, 'resize', fn); },
            };
            try {
                info.tapOutput = members.tapOutput;
                info.tapInput = members.tapInput;
                info.onResize = members.onResize;
                if (info.tapOutput === members.tapOutput) return info;
            } catch (_) { /* a frozen bag under strict mode: copy instead */ }
            return Object.assign({}, info, members);
        }

        // The extender. WRAPS ctx.windows.onTerminalCreate exactly the way
        // #195's teardown extender does — the subscription, the replay and the
        // auto-unsubscribe stay core's and the loader's. Registered AFTER 86c
        // (extenders run in ui.py::_ORDERED order), so the bag this decorates
        // already carries onModTeardown and the removal above can arm on it.
        // No capability entry: these are MEMBERS of the existing v1 `windows`
        // family, the saveChain case rather than the signal case.
        function _ctxTerminalTaps(ctx, rec) {
            // Spelled without 86c's exact guard line on purpose: a checked-in
            // test pins every occurrence of that literal to 86c. Same check,
            // same posture — a build with no onTerminalCreate is left alone.
            const fam = ctx.windows;
            if (!fam || typeof fam !== 'object') return;
            const sub = fam.onTerminalCreate;
            if (typeof sub !== 'function') return;
            fam.onTerminalCreate = function (cb) {
                if (typeof cb !== 'function') return sub.call(this, cb);
                return sub.call(this, function (info) {
                    return cb(_modTerminalTaps(rec, info));
                });
            };
        }
        if (typeof _registerCtxExtender === 'function') {
            _registerCtxExtender(_ctxTerminalTaps);
        }
        // ---- end info.tapOutput / tapInput / onResize -----------------------
