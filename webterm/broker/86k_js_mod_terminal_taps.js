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
            // A RECORD per registration, not the bare function. The same fn may
            // legitimately be registered twice (two subscriptions sharing one
            // handler), and identity-by-function made that ambiguous: off2()
            // removed whichever entry indexOf found FIRST, so the second
            // registration survived the dispatch liveness recheck and still
            // fired, while its own off() was already spent and could never
            // remove it. A record is unique by construction.
            const rec = { fn: fn };
            lists[kind].push(rec);
            let off = false;
            return function () {
                if (off) return false;
                off = true;
                const i = lists[kind].indexOf(rec);
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
                // An ARRAY is the case that made "read-only" false. The JSON
                // output frame is SERVER-supplied, so `data.data` can be `[65]`
                // -- xterm accepts it, and it was handed to the taps by
                // reference while xterm still held the same array for its
                // ASYNC write queue. One tap could then rewrite both what a
                // sibling observed and what the terminal later parsed: every
                // observer silently a transformer, which is the one thing this
                // family promises it is not.
                if (Array.isArray(data)) return data.slice();
            } catch (_) { /* fall through to the honest answer below */ }
            // Neither string, view nor array. Rather than hand out a live
            // object reference, hand out nothing: a tap cannot mutate what it
            // was not given, and "I cannot show you this safely" is a truthful
            // answer where a shared reference is not.
            return null;
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
                const rec = snapshot[i];
                if (live.indexOf(rec) === -1) continue;  // gone since the snap
                let payload;
                try { payload = make(); }
                catch (_) { return n; }
                try { rec.fn(payload); n += 1; }
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

        // A DEAD activation registers nothing, and this guard is not symmetry
        // with 86l -- it is the difference between a leak and a PERMANENT one.
        // _armTermTapRemoval degrades silently for a dead record:
        // info.onModTeardown refuses on rec.unloading, so it falls through to
        // rec.unloads.push(off) -- and _runUnloads SPLICES that list and
        // iterates the snapshot, so an entry pushed during or after the drain
        // never runs. Only info.onDispose arms, and that fires on WINDOW CLOSE.
        // So a disabled mod's tapOutput would go on receiving every PTY byte of
        // every still-open terminal for the life of that window, reachable from
        // any onUnload callback, ctx.signal abort listener, or in-flight promise
        // holding a retained info bag. _modOnAppWindowCreate already refuses the
        // same order for the same reason.
        function _modTermTap(rec, info, kind, fn) {
            if (rec && rec.unloading) return function () { return false; };
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

        // ---- info.onModesChanged (#201) -------------------------------------
        //
        // IT IS A LEVEL SURFACE WITH AN EDGE-SHAPED NAME, and #195 already owns
        // that vocabulary (_MOD_EVENT_SHAPES, 86c: every event is 'edge' or
        // 'level'). Saying the word is worth more than the paragraph below it,
        // because it makes the non-guarantee predictable rather than
        // surprising: a level surface reports WHAT IS, not WHAT HAPPENED. Two
        // changes inside one animation frame deliver ONE event carrying the
        // final state, and a change plus its reversal inside one frame deliver
        // NONE — correctly, because nothing is different afterwards. The same
        // is true across a hidden tab, where rAF does not run at all and an
        // arbitrary number of transitions collapse into the state you get back.
        // So: drive UI off it, never a one-shot side effect. Counting how many
        // times an app grabbed the mouse is not a question this surface can
        // answer, and no amount of care at the call site will make it one.
        // ONE CORE SAMPLER instead of a poll per mod. xterm exposes `term.modes`
        // as a GETTER with no change event. mods/mousemode/mousemode.js USED TO
        // sample it itself on `term.onWriteParsed` behind a
        // requestAnimationFrame; that shape is right and is kept verbatim here,
        // and the mod now subscribes to this instead of sampling (its own
        // comment records the migration). What changes is that the sampling
        // runs ONCE per terminal for N subscribers instead of once per mod:
        //
        //     ctx.windows.onTerminalCreate(function (info) {
        //         info.onModesChanged(function (modes) {
        //             modes.mouseTracking;   // 'none'|'x10'|'vt200'|'drag'|'any'
        //             modes.mouseActive;     // mouseTracking !== 'none'
        //             modes.altScreen;       // the alternate buffer is up
        //         });
        //     });
        //
        // GROUP TRANSITIONS, NOT PER-FLAG FLIPS (#154). xterm's DECRST of ANY
        // mouse-tracking mode clears the WHOLE group — coreMouseService keeps a
        // single activeProtocol, not a set of independent 1000/1002/1003 flags —
        // so per-flag events would be a fiction this surface cannot honestly
        // produce. `term.modes.mouseTrackingMode` IS the group's resolved value
        // (`switch(activeProtocol){case'X10':…}` in the vendored build), and a
        // "transition" here is a change in that one value: 'vt200'->'any' is one
        // event, and any DECRST that drops the group is exactly one event to
        // 'none'. It follows for free that RIS (which resets the mouse service)
        // reports a transition to 'none' while DECSTR (which does not) reports
        // nothing: the sampler reads STATE after the parse and never interprets
        // the escape bytes, so it cannot get that distinction wrong.
        //
        // REPLAY ON SUBSCRIBE. Subscribing fires fn once, synchronously, with
        // the CURRENT snapshot — otherwise every subscriber re-invents
        // mousemode's initial sample, which exists because onTerminalCreate
        // replays over terminals that are already running an app that may not
        // write again for minutes. A terminal that has never seen a mode
        // reports the real read of xterm's defaults: mouseTracking 'none',
        // mouseActive false, altScreen false.
        //
        // COALESCED, AND HONEST ABOUT HIDDEN TABS. onWriteParsed only ARMS a
        // rAF (the getter allocates a fresh 9-field object per read, and a
        // flooding terminal parses far faster than it paints). rAF does not run
        // in a hidden tab, so a mode change made while hidden is delivered when
        // the tab is shown again, coalesced with everything else that happened
        // meanwhile — the subscriber sees the final STATE, not the intermediate
        // ones. That is mousemode's existing behaviour, unchanged; these events
        // describe persistent state, so a missed intermediate is not a lost
        // fact. Nothing here is a suitable trigger for one-shot side effects.
        //
        // Same lifecycle as the taps above: isolated per subscriber, removed at
        // terminal dispose AND at mod teardown (_armTermTapRemoval), and nothing
        // touches `term.*` by assignment — the sampler is a SUBSCRIPTION to
        // xterm's public onWriteParsed event, so recorder's write patch and this
        // coexist.
        const _termModes = new WeakMap();   // win -> {subs, last, frame}

        function _termModesState(win, create) {
            if (!win || typeof win !== 'object') return null;
            let st = null;
            try { st = _termModes.get(win) || null; }
            catch (_) { return null; }
            if (st || !create) return st;
            st = { subs: [], last: null, frame: 0 };
            try { _termModes.set(win, st); } catch (_) { return null; }
            return st;
        }

        // The read. `null` means UNREADABLE (a getter that threw, or no term),
        // which tells us nothing about the mode — the caller keeps the last
        // known state rather than claiming 'none' and reporting a transition
        // that did not happen. mousemode takes the same posture.
        function readTermModes(win) {
            const term = win && win.term;
            if (!term) return null;
            let mouse = 'none';
            try {
                const m = term.modes;
                mouse = (m && m.mouseTrackingMode) || 'none';
            } catch (_) { return null; }
            let alt = false;
            try {
                const buf = term.buffer && term.buffer.active;
                // A build with no buffer namespace reports 'not alternate',
                // which is the truth for a terminal that cannot switch.
                alt = !!(buf && buf.type === 'alternate');
            } catch (_) { return null; }
            return { mouseTracking: mouse, mouseActive: mouse !== 'none',
                     altScreen: alt };
        }

        function _sameTermModes(a, b) {
            return !!a && !!b
                && a.mouseTracking === b.mouseTracking
                && a.mouseActive === b.mouseActive
                && a.altScreen === b.altScreen;
        }
        // A FRESH object per subscriber: one subscriber must not be able to
        // rewrite what the next one is told, and core keeps `last` for itself.
        function _copyTermModes(s) {
            return { mouseTracking: s.mouseTracking,
                     mouseActive: !!s.mouseActive, altScreen: !!s.altScreen };
        }

        // SNAPSHOT + liveness recheck, exactly as _dispatchTermTaps: a
        // subscriber that unsubscribes (or closes the window) mid-dispatch is
        // not called again, and one registered mid-dispatch does not join the
        // pass. A terminal disposed between the sample and the dispatch stops
        // the walk — the snapshot is already stale.
        function _dispatchTermModes(win, snap) {
            const st = _termModesState(win, false);
            const live = st && st.subs;
            if (!live || !live.length) return 0;
            const listed = live.slice();
            let n = 0;
            for (let i = 0; i < listed.length; i++) {
                if (win.disposed) return n;
                const rec = listed[i];
                if (live.indexOf(rec) === -1) continue;
                try { rec.fn(_copyTermModes(snap)); n += 1; }
                catch (e) {
                    try { console.error('[windows] terminal modes subscriber'
                        + ' failed:', e); } catch (_) {}
                }
            }
            return n;
        }

        // The sampler proper. Change-detected: an output frame that carried no
        // mode change costs one getter read and nothing else.
        function sampleTermModes(win) {
            if (!win || win.disposed) return 0;
            const st = _termModesState(win, false);
            if (!st) return 0;
            const now = readTermModes(win);
            if (!now) return 0;
            if (_sameTermModes(st.last, now)) return 0;
            st.last = now;
            return _dispatchTermModes(win, now);
        }

        // Core's own hook: 67 arms this from term.onWriteParsed, and cancels
        // the pending frame in win.cleanups. At most one sample per frame no
        // matter how much output arrives.
        function armTermModesSample(win) {
            const st = _termModesState(win, true);
            if (!st || st.frame) return false;
            if (typeof requestAnimationFrame !== 'function') {
                sampleTermModes(win);
                return true;
            }
            st.frame = requestAnimationFrame(function () {
                st.frame = 0;
                sampleTermModes(win);
            });
            return true;
        }
        function cancelTermModesSample(win) {
            const st = _termModesState(win, false);
            if (!st || !st.frame) return false;
            try { cancelAnimationFrame(st.frame); } catch (_) {}
            st.frame = 0;
            return true;
        }

        function _addTermModesSub(win, fn) {
            const noop = function () { return false; };
            if (typeof fn !== 'function') return noop;
            // A DISPOSED terminal takes no subscribers. The sampler already
            // checks this, but registration did not -- so a retained info bag
            // could subscribe to a dead terminal and be replayed with whatever
            // its disposed internals still happened to answer.
            if (win && win.disposed) return noop;
            const st = _termModesState(win, true);
            if (!st) return noop;
            const rec = { fn: fn };
            st.subs.push(rec);
            // off() is built and the removal is ARMED BEFORE the replay fires.
            // The replay is synchronous mod code: it may close the terminal or
            // disable its own mod, and either drains the cleanup that is
            // supposed to remove this subscription. Arming afterwards means
            // registering against a teardown that has already run, and the
            // subscription outlives the thing that owned it.
            let off = false;
            const offFn = function () {
                if (off) return false;
                off = true;
                const i = st.subs.indexOf(rec);
                if (i === -1) return false;
                st.subs.splice(i, 1);
                return true;
            };
            // THE REPLAY, now that off() exists and can be armed by the caller.
            //
            // Read LIVE when a sample is already queued: `last` is the last
            // SAMPLED state, and between a mode change and the frame that
            // observes it, `last` is stale by construction. A subscriber
            // arriving in that window was told the old value and then told the
            // new one a frame later -- a transition that never happened.
            let snap = st.last;
            if (!snap || st.frame) {
                const now = readTermModes(win);
                if (now) { snap = now; st.last = now; }
            }
            // An UNREADABLE getter replays NOTHING. Substituting a default
            // would announce 'none' for a terminal that may well be
            // mouse-active, and the next successful sample would then report a
            // transition out of a state core never observed. Saying nothing is
            // the honest form of "we do not know yet".
            if (snap) {
                try { rec.fn(_copyTermModes(snap)); }
                catch (e) {
                    try { console.error('[windows] terminal modes subscriber'
                        + ' failed:', e); } catch (_) {}
                }
            }
            return offFn;
        }

        // A SECOND extender rather than another member on _modTerminalTaps'
        // bag: the two ranges stay independently sliceable (each has its own
        // executed test), and extenders compose — this one wraps the wrapper
        // A58 installed, so a create callback receives a bag carrying
        // onModTeardown (86c), the three taps (above) and onModesChanged.
        function _modTerminalModes(rec, info) {
            if (!info || typeof info !== 'object') return info;
            const member = function (fn) {
                // Same refusal as the taps above, for the same reason: a dead
                // activation's subscription would arm only on window close,
                // because _runUnloads has already spliced the list it would be
                // pushed onto.
                if (rec && rec.unloading) return function () { return false; };
                const off = _addTermModesSub(info && info.win, fn);
                if (typeof fn === 'function') _armTermTapRemoval(rec, info, off);
                return off;
            };
            try {
                info.onModesChanged = member;
                if (info.onModesChanged === member) return info;
            } catch (_) { /* a frozen bag under strict mode: copy instead */ }
            return Object.assign({}, info, { onModesChanged: member });
        }

        function _ctxTerminalModes(ctx, rec) {
            const fam = ctx.windows;
            if (!fam || typeof fam !== 'object') return;
            const sub = fam.onTerminalCreate;
            if (typeof sub !== 'function') return;
            fam.onTerminalCreate = function (cb) {
                if (typeof cb !== 'function') return sub.call(this, cb);
                return sub.call(this, function (info) {
                    return cb(_modTerminalModes(rec, info));
                });
            };
        }
        if (typeof _registerCtxExtender === 'function') {
            _registerCtxExtender(_ctxTerminalModes);
        }
        // ---- end info.onModesChanged ----------------------------------------
