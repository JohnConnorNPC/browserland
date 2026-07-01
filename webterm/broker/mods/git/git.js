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
        // on the win object. Teardown is idempotent and covers BOTH exits: a window
        // CLOSE (onDispose → win.cleanups, drained by closeWindow / the active-view
        // rebuild) AND a mod DISABLE (ctx.onUnload drains a Set holding every live
        // window's teardown) — so no stray 15s interval, no orphan document
        // listener, and the ⎇ button + label are removed in either case.
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

                // Every LIVE window's idempotent teardown, so a mod DISABLE
                // (ctx.onUnload) can tear down widgets on windows that are still
                // open — win.cleanups only fires on window close/rebuild. Each
                // teardown removes itself from the set, so a window close and a mod
                // disable never double-run and the set never leaks dead entries.
                const disposers = new Set();
                // Belt-and-braces: never decorate the same win twice. Replay and
                // the create-time emit are mutually exclusive for one subscription,
                // but a WeakSet keyed by win keeps it robust (and self-cleaning: a
                // closed win is GC'd out of the set).
                const decorated = new WeakSet();

                ctx.windows.onTerminalCreate(function (info) {
                    const win = info.win;
                    const titleBar = info.titleBar;
                    if (!win || !titleBar || decorated.has(win)) return;
                    decorated.add(win);

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
                            if (gitTimer) { clearInterval(gitTimer); gitTimer = null; }
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

                    // One idempotent teardown for BOTH exits (window close via
                    // onDispose, mod disable via the disposers drain). Removes the
                    // listeners, closes the popover (dropping its document-level
                    // listeners), clears the keep-alive interval, and removes the
                    // DOM nodes. Self-removes from the set so a second call no-ops.
                    const teardown = function () {
                        if (torn) return;
                        torn = true;
                        disposers.delete(teardown);
                        // Drop the decorate-once guard so IF this exact win object
                        // were ever re-emitted (it is not today — closeWindow /
                        // teardownView both discard the win before any replay), a
                        // future create would re-decorate rather than silently skip.
                        decorated.delete(win);
                        gitBtn.removeEventListener('mousedown', stopProp);
                        gitBtn.removeEventListener('click', onGitClick);
                        closeGitPopover();
                        if (gitTimer) { clearInterval(gitTimer); gitTimer = null; }
                        try { gitBtn.remove(); } catch (_) {}
                        try { gitLabel.remove(); } catch (_) {}
                    };
                    disposers.add(teardown);
                    info.onDispose(teardown);

                    // Initial fetch shortly after open, plus a slow keep-alive poll.
                    // Both best-effort; refreshGit never throws and self-guards on
                    // disposed/torn. Each call runs a git subprocess on the agent,
                    // so keep the interval slow.
                    setTimeout(function () {
                        if (!win.disposed && !torn) refreshGit();
                    }, 800);
                    gitTimer = setInterval(function () {
                        if (!win.disposed && !torn) refreshGit();
                    }, 15000);
                });

                // Mod teardown: tear down every LIVE window's widget (win.cleanups
                // only fires on window close, not on a mod disable). Each teardown
                // is idempotent + self-removing, so a window that closes first has
                // already left the set. The onTerminalCreate unsubscribe is
                // auto-registered by the loader (rec.unloads), so no new windows get
                // decorated after this.
                ctx.onUnload(function () {
                    for (const t of Array.from(disposers)) {
                        try { t(); } catch (_) {}
                    }
                    disposers.clear();
                });
            },
        });
