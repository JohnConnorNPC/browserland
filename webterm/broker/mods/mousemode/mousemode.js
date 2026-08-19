        // ---- mod: mouse mode chip (#155) -----------------------------------
        // The ambient half of "TUIs feel broken for copy/paste". When lazygit /
        // btop / mc enable mouse reporting, xterm hands every click, drag and
        // wheel event to the app and DISABLES its own selection service, so
        // drag-select silently stops highlighting anything. Nothing in the UI
        // says why. This mod puts a 🖱 in that terminal's title bar for exactly
        // as long as the app owns the mouse, and names the escape gesture in the
        // tooltip — the same sentence wiki/Keyboard-Shortcuts.md carries, at the
        // moment of confusion instead of on a wiki page.
        //
        // Deliberately AMBIENT, not eventful (#155 rejected a detect-and-coach
        // toast): mouse tracking is persistent STATE, and an app enables it
        // during its splash output — a one-shot toast fires while the user is
        // still reading the splash and is long gone by the time they first try to
        // select something. A chip cannot misfire: it is true precisely when
        // tracking is on. Scope is a chip and a tooltip — no popover, no click
        // action, no settings (a click action would be a separate issue).
        //
        // SAMPLING IS CORE'S NOW (#201). xterm exposes `term.modes` as a GETTER
        // with no change event:
        //     get modes(){ … switch(coreMouseService.activeProtocol){ … }
        //                  return { …, mouseTrackingMode:t, … } }   // 'none'|'x10'|'vt200'|'drag'|'any'
        // so SOMETHING has to sample it. This mod used to be that something: it
        // subscribed to `term.onWriteParsed` and read the getter behind a
        // requestAnimationFrame. #201 moved that exact shape into core as
        // `info.onModesChanged` (86k_js_mod_terminal_taps.js) — one sampler per
        // terminal for N subscribers instead of one poll per mod — and this mod
        // is the reference migration: the poll is DELETED and the chip is driven
        // by the event. Nothing about what the chip shows changed.
        //
        // What the core surface keeps, verbatim, so the migration is behaviour-
        // preserving rather than merely tidier:
        //   · the trigger is still `term.onWriteParsed`, which fires only when
        //     output was actually PARSED — an idle terminal costs nothing, and
        //     every mode change arrives as parsed output by definition
        //     (DECSET/DECRST from the app, the RIS an exiting app leaves behind,
        //     and the mouse modes the agent RE-ASSERTS in a reattach snapshot's
        //     postamble — webterm/agent/agent.py, #154 — so a reload or a
        //     lost-lease rebuild re-derives the chip instead of guessing);
        //   · the rAF coalescing, because the getter allocates a fresh 9-field
        //     object every access and a flooding terminal parses writes far
        //     faster than it paints — at most one sample per frame, and the DOM
        //     write still lands at paint time;
        //   · the hidden-tab consequence: rAF does not run in a hidden tab, so a
        //     mode change made while hidden is delivered, coalesced, when the tab
        //     comes back. These are STATE notifications, so a skipped
        //     intermediate is not a lost fact — the chip is right when it is
        //     visible, which is the only time it can be wrong;
        //   · the "unreadable getter" posture: a getter that threw reports
        //     NOTHING, so the chip keeps whatever is on screen instead of
        //     claiming 'none' and hiding a chip that should be up;
        //   · the initial sample — subscribing REPLAYS the current snapshot
        //     synchronously, which is exactly why the mod's own `paint()`-once
        //     line is gone rather than merely moved. It exists for the same
        //     reason it always did: onTerminalCreate replays over terminals that
        //     are already running an app that may not write again for minutes.
        // The payload names the group's resolved protocol as `mouseTracking`
        // (#154: xterm keeps ONE activeProtocol, not independent 1000/1002/1003
        // flags, so a DECRST of any member is one transition to 'none').
        //
        // Rejected, still: watching the `enable-mouse-events` class xterm toggles
        // on term.element — it fires exactly on the transition, but it is an
        // internal implementation detail, and a renamed class would silently
        // freeze the chip, whereas `modes` and `onWriteParsed` (which the core
        // sampler rides) are public API. Also rejected: a per-window timer, which
        // #155 rules out in spirit — there is nothing a 1 Hz poll would catch
        // that a parsed write does not, and it would mask an xterm API skew
        // instead of surfacing it.
        //
        // Ships default-ON: it is invisible until an app grabs the mouse, so a
        // shell-only user pays one getter read per output frame and sees nothing.
        // Per-window state lives in the onTerminalCreate closure (never fields on
        // win), and teardown is idempotent for BOTH exits — a window close
        // (info.onDispose → win.cleanups, drained by closeWindow and by the
        // lost-lease view rebuild) and a mod disable (ctx.onUnload drains every
        // live window's teardown) — following the git mod (#116) exactly.
        registerMod({
            id: 'mousemode',
            version: '1.0.0',
            ctxVersion: 1,
            tiers: ['window'],
            init: function (ctx) {
                // Feature-detect the per-terminal-window hook (additive ctx
                // capability, #116): an older loader without ctx.windows leaves
                // the mod inert, as the git / termfont mods do.
                if (!ctx.windows) return;

                // How much of the mouse the running program asked for, in a
                // terminal user's words rather than xterm's protocol names. An
                // unknown value (a future xterm growing a fifth protocol) still
                // shows the chip with a generic phrase: the chip's claim is true
                // for every non-'none' value.
                const MODE_TEXT = {
                    x10: 'clicks',
                    vt200: 'clicks',
                    drag: 'clicks and drags',
                    any: 'clicks, drags and every mouse move',
                };
                // The tooltip states the MEASURED state — "mouse reporting is
                // on" — never "this app is reading the mouse". They are not the
                // same claim: a crashed or killed TUI, a buggy one that skips its
                // cleanup, or a `cat` of a file containing DECSET 1002 all leave
                // reporting on with nobody listening. The chip is honest about
                // what it can actually see.
                //
                // Both gestures are named, with no platform detection. xterm's
                // shouldForceSelection is `shiftKey` everywhere and `altKey` on
                // macOS (gated on the macOptionClickForcesSelection option core
                // sets, #154), so naming both is complete; sniffing
                // navigator.platform to name one would add a deprecated,
                // spoofable, iPad-confusing failure mode to save four words.
                const tooltipFor = function (mode) {
                    return 'Mouse reporting is on — '
                        + (MODE_TEXT[mode] || 'mouse events')
                        + ' go to the program, not the browser. '
                        + 'Shift-drag selects text anyway'
                        + ' (Option-drag on macOS)'
                        + ' · Shift+scroll scrolls the terminal'
                        + ' · Ctrl+Shift+C copies the selection';
                };

                // Every LIVE window's idempotent teardown, so a mod DISABLE can
                // tear down chips on windows that are still open (win.cleanups
                // only fires on window close / view rebuild). Each teardown
                // self-removes, so the two exits never double-run.
                const disposers = new Set();
                // Never decorate the same win twice. Replay and the create-time
                // emit are mutually exclusive for one subscription; the WeakSet
                // keeps that robust and self-cleaning (a closed win is GC'd out).
                const decorated = new WeakSet();

                ctx.windows.onTerminalCreate(function (info) {
                    const win = info.win;
                    // No term = nothing to sample. App windows never reach this
                    // hook, but a guard keeps the mod honest if that changes.
                    if (!win || !win.term || decorated.has(win)) return;
                    // info.onModesChanged is the whole mechanism: without it the
                    // chip could only go stale, and #155 rules out a timer. Bail
                    // BEFORE adding a node so a build that predates the surface
                    // leaves no dead chip pinned to whatever the mode was at
                    // open — the same posture the old onWriteParsed guard took.
                    if (typeof info.onModesChanged !== 'function') return;
                    decorated.add(win);

                    // A passive <span>, not a .tb-btn: no click action, and no
                    // mousedown handler either, so a mousedown on it bubbles to
                    // the title bar and drags the window like the title text and
                    // the git branch label already do (wireDrag treats anything
                    // that does not stopPropagation as "the bar itself"). The
                    // class is mod-prefixed so a future mod cannot collide with
                    // it. role/aria-label carry the same sentence as the tooltip,
                    // since a `title` alone reaches neither a screen reader
                    // reading the title bar nor a touch user with no hover.
                    const chip = document.createElement('span');
                    chip.className = 'mousemode-chip';
                    chip.setAttribute('role', 'img');
                    chip.textContent = '🖱';   // 🖱 U+1F5B1
                    chip.style.display = 'none';         // ambient: hidden at 'none'
                    info.addTitleBarItem(chip);

                    let last = null;      // last mode PAINTED (null = never)
                    let torn = false;
                    // The event carries three fields; the chip is about exactly
                    // one of them. Keep the mod's OWN change-detection on the
                    // mode it paints, so an altScreen-only transition (a real
                    // event on this surface) costs no DOM work here.
                    const paint = function (modes) {
                        if (torn || win.disposed) return;
                        const mode = (modes && modes.mouseTracking) || 'none';
                        if (mode === last) return;   // change-detected: no DOM work
                        last = mode;
                        if (mode === 'none') {
                            chip.style.display = 'none';
                            // Drop the stale tooltip with it: a hidden node is
                            // still visible to devtools and a11y trees.
                            chip.removeAttribute('title');
                            chip.removeAttribute('aria-label');
                            return;
                        }
                        const text = tooltipFor(mode);
                        chip.title = text;
                        chip.setAttribute('aria-label', text);
                        chip.style.display = '';
                    };
                    // THE SUBSCRIPTION, which is the whole mechanism. It fires
                    // ONCE SYNCHRONOUSLY right here with the current snapshot —
                    // that replay IS the old initial sample, so a terminal that
                    // was already running a moused app when the mod was enabled
                    // gets its chip before this call returns — and thereafter on
                    // every mode transition, coalesced to one per frame by core.
                    // The returned off() is idempotent, and core additionally
                    // arms it on dispose + mod teardown; the local teardown below
                    // still calls it so the chip and the subscription always go
                    // together.
                    let off = null;
                    try { off = info.onModesChanged(paint); }
                    catch (_) { off = null; }

                    const teardown = function () {
                        if (torn) return;
                        torn = true;
                        disposers.delete(teardown);
                        // Drop the decorate-once guard so IF this exact win were
                        // ever re-emitted (it is not today — closeWindow and
                        // teardownView both discard the win first) a future
                        // create would re-decorate rather than silently skip.
                        decorated.delete(win);
                        if (off) {
                            try { off(); } catch (_) {}
                            off = null;
                        }
                        // No pending frame to cancel any more: the rAF belongs
                        // to core's one-per-terminal sampler, which cancels it in
                        // win.cleanups. `torn` still guards the window between a
                        // mod-teardown that ran before core's removal did.
                        try { chip.remove(); } catch (_) {}
                    };
                    disposers.add(teardown);
                    info.onDispose(teardown);
                });

                // Mod teardown: every live window's chip goes now. The
                // onTerminalCreate unsubscribe is auto-registered by the loader
                // (rec.unloads), so no new terminal is decorated after this.
                ctx.onUnload(function () {
                    for (const t of Array.from(disposers)) {
                        try { t(); } catch (_) {}
                    }
                    disposers.clear();
                });
            },
        });
