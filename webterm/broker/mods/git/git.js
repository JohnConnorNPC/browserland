        // ---- mod: git status (S14 / #116) ---------------------------------
        // The per-terminal git-status title-bar widget — the ⎇ button, the branch
        // label + dirty badge, and the click-to-open status popover — extracted
        // from core (67_js_window_lifecycle.js) as a per-terminal-window mod (#116).
        // Unlike the old ALWAYS-ON core widget it ships DISABLED by default
        // (registerMod defaultEnabled:false, the #112/#116 loader capability):
        // enable it in Control Panel → Mods.
        //
        // It rides the core per-terminal-window hook ctx.windows.onTerminalCreate
        // (#116): the callback fires for every terminal already open (REPLAYED) and
        // for every future one, so enabling the mod mid-session decorates all open
        // terminals too. The backend is UNCHANGED — each poll is
        // ctx.session.git(wireId, {host}) → the host-routed POST /session/git the
        // old inline gitPost used, byte-identical ({status,json}, fail-open).
        //
        // Per-window state lives in the callback's CLOSURE (one per terminal), not
        // on the win object. Teardown covers BOTH exits through ONE registration:
        // info.onModTeardown (#195), which fires exactly once on the first of a
        // window CLOSE (win.cleanups, drained by closeWindow / the active-view
        // rebuild) and a mod DISABLE — so no stray 15s interval, no orphan document
        // listener, and the ⎇ button + label are removed in either case. This mod
        // is #195's reference migration for that hook: it used to hand-ride the two
        // chains itself, with a Set of live-window teardowns drained from
        // ctx.onUnload beside an info.onDispose, and a decorate-once WeakSet in
        // front of both. Core keeps all three now.
        registerMod({
            id: 'git',
            version: '1.0.0',
            ctxVersion: 1,
            defaultEnabled: false,   // #116: ship OFF — opt in via the Mods pane
            tiers: ['session', 'window'],
            init: function (ctx) {
                // Feature-detect the per-terminal-window hook (additive ctx
                // capability, #116). An older loader without ctx.windows -> the mod
                // is inert (no widget), exactly like the other mods feature-detect
                // ctx.file / ctx.session before using them.
                if (!ctx.windows) return;

                // stopPropagation helper: the core git block borrowed `stopProp`
                // from the color-picker closure (67); the mod no longer shares that
                // scope, so define a local one.
                const stopProp = function (e) { e.stopPropagation(); };

                // NO `needs` on the decl for this: `needs` names a path on CTX
                // (#197 resolves 'windows.onTerminalCreate'), and onModTeardown is
                // a member of the per-delivery info BAG, which no ctx path can
                // reach. The bail above is what covers an absent ctx.windows, and
                // the per-registration check below covers an absent hook.

                // Feature-detected {stop}-shaped interval: a runtime-installed
                // copy of this mod can run against an older core with no
                // ctx.visibility, in which case this degrades to a plain
                // setInterval wrapped in the same { stop } shape so every
                // teardown site below can call .stop() unconditionally.
                function pausable(fn, ms) {
                    return ctx.visibility
                        ? ctx.visibility.pausableInterval(fn, ms)
                        : (function () {
                            const id = setInterval(fn, ms);
                            return { stop: function () { clearInterval(id); } };
                        })();
                }

                ctx.windows.onTerminalCreate(function (info) {
                    const win = info.win;
                    const titleBar = info.titleBar;
                    // No decorate-once guard of this mod's own: one subscription's
                    // replay and its create-time emit are mutually exclusive, so
                    // core delivers each window to this callback exactly once
                    // (#116) — the WeakSet that used to sit here was belt-and-braces
                    // over a guarantee core already makes.
                    if (!win || !titleBar) return;

                    // ---- per-window title-bar UI (moved from core) ------------
                    // Starts muted (no status known yet); hidden when the cwd is
                    // not a repo or the broker is too old to have /session/git.
                    const gitBtn = document.createElement('button');
                    gitBtn.type = 'button';
                    gitBtn.className = 'tb-btn btn-git muted';
                    // #119: the app-icon git branch-nodes glyph (trusted, hardcoded
                    // SVG from the APP_ICON_SVG registry) replaces the ⎇ character;
                    // the .muted (not-a-repo) state still dims it via opacity.
                    gitBtn.innerHTML = appIconSvg('git');
                    gitBtn.title = 'Git status';
                    const gitLabel = document.createElement('span');
                    gitLabel.className = 'git-label';
                    // addTitleBarItem inserts before the min button, so the ⎇ button
                    // + label land in their original slot (after AGENTS.md, before
                    // color/MCP/min) — preserving today's title-bar order.
                    info.addTitleBarItem(gitBtn);
                    info.addTitleBarItem(gitLabel);

                    // ---- per-window state (closure, not fields on win) --------
                    // gitStatus: last successful {ok:true,...} payload (or null);
                    // gitState: 'unknown'|'repo'|'norepo'|'unavailable' so the
                    // button renders muted/hidden without a status toast.
                    let gitStatus = null;
                    let gitState = 'unknown';
                    let gitPopover = null;
                    let gitFetching = false;
                    let gitTimer = null;
                    let gitSeq = 0;
                    let torn = false;

                    // Host-aware POST /session/git via the reviewed ctx.session
                    // capability (#116): the AGENT runs git in its own live cwd, so
                    // we send only the bare wire id + the window's host id. Same
                    // {status, json} envelope as the old inline gitPost — a non-repo
                    // / 404 / network error resolves (never throws) so a routine
                    // terminal never toasts.
                    const gitPost = function () {
                        return ctx.session.git(info.wireId, { host: win.hostId });
                    };
                    // Paint the button + label from gitState/gitStatus. Muted when
                    // not-a-repo / unknown; HIDDEN when the route is unavailable
                    // (404 on an old broker). Branch name + a dirty badge alongside.
                    const renderGit = function () {
                        if (win.disposed || torn) return;
                        if (gitState === 'unavailable') {
                            gitBtn.style.display = 'none';
                            gitLabel.style.display = 'none';
                            return;
                        }
                        gitBtn.style.display = '';
                        const st = gitStatus;
                        const isRepo = gitState === 'repo' && st && st.ok;
                        gitBtn.classList.toggle('muted', !isRepo);
                        if (!isRepo) {
                            gitLabel.style.display = 'none';
                            gitLabel.textContent = '';
                            gitBtn.title = (gitState === 'norepo')
                                ? 'Git: not a repository' : 'Git status';
                            return;
                        }
                        const branch = st.detached ? 'detached'
                            : (st.branch || '(no branch)');
                        gitLabel.style.display = '';
                        gitLabel.textContent = branch;
                        gitLabel.classList.toggle('git-dirty', !!st.dirty);
                        // A small dirty badge: the change count when known, else a dot.
                        let badge = '';
                        if (st.dirty) {
                            badge = (typeof st.dirty_count === 'number'
                                     && st.dirty_count > 0)
                                ? (' ●' + st.dirty_count) : ' ●';
                        }
                        gitLabel.textContent = branch + badge;
                        const ab = [];
                        if (st.ahead) ab.push('↑' + st.ahead);
                        if (st.behind) ab.push('↓' + st.behind);
                        gitBtn.title = 'Git: ' + branch
                            + (ab.length ? (' ' + ab.join(' ')) : '')
                            + (st.dirty ? ' (dirty)' : ' (clean)');
                        // If the popover is open, keep it in sync with the new status.
                        if (gitPopover) fillGitPopover();
                    };
                    // Fetch + classify. Never throws. 404/no route -> 'unavailable'
                    // (hide forever this session); not_a_repo/no_cwd -> 'norepo'
                    // (muted); ok -> 'repo'.
                    const refreshGit = async function () {
                        if (win.disposed || torn || gitFetching) return;
                        gitFetching = true;
                        // Monotonic token: a slow earlier reply must not paint over
                        // a newer one (gitFetching blocks overlap from THIS caller,
                        // but the token is the durable guard).
                        const seq = ++gitSeq;
                        let res;
                        try { res = await gitPost(); }
                        catch (_) { res = { status: 0, json: { ok: false } }; }
                        finally { gitFetching = false; }
                        if (win.disposed || torn || seq !== gitSeq) return;
                        const j = res.json || {};
                        if (res.status === 404) {
                            gitState = 'unavailable';
                            // Old broker without the route: stop the keep-alive poll
                            // — it will only keep 404ing. The button stays hidden.
                            if (gitTimer) { gitTimer.stop(); gitTimer = null; }
                        } else if (j.ok) {
                            gitState = 'repo';
                            gitStatus = j;
                        } else if (j.error === 'not_a_repo' || j.error === 'no_cwd') {
                            gitState = 'norepo';
                            gitStatus = null;
                        } else {
                            // Transient/unknown error: stay muted, don't toast,
                            // don't hide (a later refresh may succeed).
                            if (gitState === 'unknown') gitState = 'norepo';
                        }
                        renderGit();
                    };
                    // Status popover anchored under the button: branch/detached,
                    // ahead/behind, the four index counts, + a Refresh button.
                    // Closes on outside-click / Escape.
                    const fillGitPopover = function () {
                        const pop = gitPopover;
                        if (!pop) return;
                        const st = gitStatus;
                        pop.innerHTML = '';
                        const head = document.createElement('div');
                        head.className = 'git-pop-head';
                        if (gitState === 'repo' && st && st.ok) {
                            head.textContent = st.detached
                                ? 'detached HEAD' : (st.branch || '(no branch)');
                        } else if (gitState === 'norepo') {
                            head.textContent = 'not a git repository';
                        } else {
                            head.textContent = 'git status unavailable';
                        }
                        pop.appendChild(head);
                        if (gitState === 'repo' && st && st.ok) {
                            const ab = document.createElement('div');
                            ab.className = 'git-pop-row';
                            ab.textContent = 'ahead ↑' + (st.ahead || 0)
                                + '   behind ↓' + (st.behind || 0);
                            pop.appendChild(ab);
                            const counts = [
                                ['staged', st.staged], ['unstaged', st.unstaged],
                                ['untracked', st.untracked], ['conflicts', st.conflicts],
                            ];
                            for (const [k, v] of counts) {
                                const r = document.createElement('div');
                                r.className = 'git-pop-row';
                                r.textContent = k + ': ' + (v || 0);
                                if (k === 'conflicts' && v) r.classList.add('git-bad');
                                pop.appendChild(r);
                            }
                            const dirty = document.createElement('div');
                            dirty.className = 'git-pop-row '
                                + (st.dirty ? 'git-dirty' : '');
                            dirty.textContent = st.dirty
                                ? ('dirty (' + (st.dirty_count || 0) + ')') : 'clean';
                            pop.appendChild(dirty);
                        }
                        const foot = document.createElement('div');
                        foot.className = 'git-pop-foot';
                        const refreshBtn = document.createElement('button');
                        refreshBtn.type = 'button';
                        refreshBtn.className = 'tb-btn';
                        refreshBtn.style.width = 'auto';
                        refreshBtn.style.padding = '0 8px';
                        refreshBtn.textContent = 'Refresh';
                        refreshBtn.addEventListener('mousedown', stopProp);
                        refreshBtn.addEventListener('click', function (e) {
                            e.stopPropagation();
                            refreshGit();
                        });
                        foot.appendChild(refreshBtn);
                        pop.appendChild(foot);
                    };
                    const closeGitPopover = function () {
                        if (!gitPopover) return;
                        document.removeEventListener('mousedown', onGitOutside, true);
                        document.removeEventListener('keydown', onGitKey, true);
                        try { gitPopover.remove(); } catch (_) {}
                        gitPopover = null;
                    };
                    const onGitOutside = function (e) {
                        // The button + its branch label form one affordance: a click
                        // on either is "inside" (the button toggles, popover stays).
                        if (gitPopover && !gitPopover.contains(e.target)
                            && e.target !== gitBtn && e.target !== gitLabel) {
                            closeGitPopover();
                        }
                    };
                    const onGitKey = function (e) {
                        if (e.key === 'Escape') {
                            e.preventDefault(); e.stopPropagation(); closeGitPopover();
                        }
                    };
                    const openGitPopover = function () {
                        if (gitPopover) { closeGitPopover(); return; }
                        const pop = document.createElement('div');
                        pop.className = 'git-popover';
                        titleBar.appendChild(pop);
                        gitPopover = pop;
                        fillGitPopover();
                        // Anchor under the button within the (relative) title bar.
                        pop.style.left = Math.max(0, gitBtn.offsetLeft) + 'px';
                        pop.style.top = (gitBtn.offsetTop + gitBtn.offsetHeight + 2) + 'px';
                        document.addEventListener('mousedown', onGitOutside, true);
                        document.addEventListener('keydown', onGitKey, true);
                        // Always refresh on open (cheap, keeps the popover live).
                        refreshGit();
                    };
                    const onGitClick = function (e) {
                        e.stopPropagation();
                        openGitPopover();
                    };
                    gitBtn.addEventListener('mousedown', stopProp);
                    gitBtn.addEventListener('click', onGitClick);

                    // One teardown for BOTH exits. Removes the listeners, closes the
                    // popover (dropping its document-level listeners), clears the
                    // keep-alive interval, and removes the DOM nodes. `torn` is the
                    // flag the async refreshGit and the two timers already read to
                    // tell a dead window from a live one, so it doubles as the
                    // idempotence guard the fallback path below still wants.
                    const teardown = function () {
                        if (torn) return;
                        torn = true;
                        gitBtn.removeEventListener('mousedown', stopProp);
                        gitBtn.removeEventListener('click', onGitClick);
                        closeGitPopover();
                        if (gitTimer) { gitTimer.stop(); gitTimer = null; }
                        try { gitBtn.remove(); } catch (_) {}
                        try { gitLabel.remove(); } catch (_) {}
                    };
                    // #195: ONE registration, both exits. onModTeardown fires on the
                    // first of the window closing and this mod being disabled, and a
                    // disable removes the widget WITHOUT closing the terminal (the
                    // window is core's, not this mod's). It returns false when it
                    // would never fire at all — a build with no 86c, a window already
                    // gone, this mod mid-teardown — and then the window-close half is
                    // still worth arming, because a leaked 15s interval plus a live
                    // document listener is the worse failure. It is the one line this
                    // rode before, not a second set.
                    if (!(typeof info.onModTeardown === 'function'
                          && info.onModTeardown(teardown))) {
                        info.onDispose(teardown);
                    }

                    // Initial fetch shortly after open, plus a slow keep-alive poll.
                    // Both best-effort; refreshGit never throws and self-guards on
                    // disposed/torn. Each call runs a git subprocess on the agent,
                    // so keep the interval slow.
                    setTimeout(function () {
                        if (!win.disposed && !torn) refreshGit();
                    }, 800);
                    gitTimer = pausable(function () {
                        if (!win.disposed && !torn) refreshGit();
                    }, 15000);
                });

                // No ctx.onUnload drain here any more: the mod-disable half of every
                // window's teardown is what info.onModTeardown registered above, and
                // the onTerminalCreate unsubscribe is auto-registered by the loader
                // (rec.unloads), so no terminal opened after a disable gets a widget.
            },
        });
