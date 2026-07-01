        // ---- mod: agent docs (S? / #120) ----------------------------------
        // The tabbed AGENTS.md + CLAUDE.md editor (with the synthetic ⚙ Sections
        // CRUD tab + the template checklist), split out of the editor mod (#120).
        // It carves out the cleanly-separable ENTRY POINTS -- the two openers below
        // and the per-terminal 📋 title-bar button -- while REUSING the editor's
        // text-editor window kind + the shared docs/Sections/template machinery
        // that stays interleaved in mods/editor/editor.js. requires:['editor']
        // guarantees that machinery (and editorFile.cap) is present, so this mod is
        // a thin front door: persistence stays byte-identical (no new appKind, the
        // same core serializeAppWindow) and core keeps zero Agent-docs knowledge.
        //
        // Both openers are top-level `function` declarations, so they HOIST across
        // the one concatenated <script> and stay reachable from the editor mod's
        // legacy-record upgrade branch (openNoteOrEditorWindow, editor.js) by bare
        // identifier -- exactly how core 67 used to call openAgentsMdEditor. They
        // call the editor/core helpers (editorFile, openAppWindow, sessions,
        // windows, placeWindowTiled, tabWindowIntoTile, showNotice, hostById,
        // localHost, joinNative, restoreWindow, bringToFront, findKeyInLayout,
        // getLayout) as free identifiers -- all resolve in the shared scope, and
        // editorFile() degrades to the hoisted core _modFileApi if the editor mod
        // ever cleared editorFile.cap, so a read/write never throws.
        //
        // The 📋 button rides the core per-terminal-window hook
        // ctx.windows.onTerminalCreate (#116, the proven git-mod seam): the callback
        // fires for every terminal already open (REPLAYED) and every future one, so
        // enabling the mod mid-session decorates all open terminals too. Per-window
        // teardown is idempotent and covers BOTH a window CLOSE (onDispose ->
        // win.cleanups) AND a mod DISABLE (ctx.onUnload drains the disposers Set) --
        // so disabling the editor mod (which cascades this one off, needs: editor)
        // or this mod alone removes every 📋 button with no orphan listeners.

        // Build (or restore) the tabbed "Agent docs" window for a folder: reads
        // <cwd>/AGENTS.md AND <cwd>/CLAUDE.md in parallel on the resolved host,
        // builds the per-tab `docs` array (+ a synthetic Sections tab is added in
        // openAppWindow), and opens the window. Shared by the titlebar opener
        // (openAgentsMdEditor) AND the legacy-record upgrade path in
        // openAppWindow (which passes stored geom/color/tiling to preserve them).
        //   opts: { id, cwd, fileHostId, geom?, color?, locked?, floatGeom?,
        //           activeTab? }
        // The /file API is host-wide (#35), so an AGENTS.md at any cwd opens;
        // not_found opens an empty buffer (saving creates it). CLAUDE.md errors
        // fall back to an empty buffer (best-effort — the AGENTS save later
        // ensures it references @AGENTS.md).
        async function openAgentDocsWindow(opts) {
            const cwd = String(opts.cwd || '');
            const fileHostId = opts.fileHostId || 'local';
            const host = hostById(fileHostId) || localHost();
            const agentsPath = joinNative(cwd, 'AGENTS.md');
            const claudePath = joinNative(cwd, 'CLAUDE.md');
            const [aRes, cRes] = await Promise.all([
                editorFile().read(agentsPath, { host: host.id }),
                editorFile().read(claudePath, { host: host.id }),
            ]);
            // A real read error (not just "doesn't exist yet") aborts.
            if (aRes && !aRes.ok && aRes.error !== 'not_found') {
                showNotice('open AGENTS.md failed: ' + ((aRes && aRes.error) || '?'));
                return;
            }
            const agentsContent = (aRes && aRes.ok) ? (aRes.content || '') : '';
            const claudeContent = (cRes && cRes.ok) ? (cRes.content || '') : '';
            const mkDoc = (name, filePath, content, isAgents, encoding) => ({
                name, filePath, content,
                wrap: true, lineNums: true, isAgents,
                encoding: encoding || 'utf-8',   // #97: round-trip source enc
            });
            const docs = [
                mkDoc('AGENTS.md', agentsPath, agentsContent, true,
                      aRes && aRes.ok ? aRes.encoding : 'utf-8'),
                mkDoc('CLAUDE.md', claudePath, claudeContent, false,
                      cRes && cRes.ok ? cRes.encoding : 'utf-8'),
            ];
            // Label remote windows with the host so two docs windows sharing a
            // cwd string stay distinguishable on the taskbar.
            const title = (fileHostId === 'local')
                ? ('Agent docs — ' + cwd)
                : ('Agent docs — ' + (host.label || fileHostId) + ':' + cwd);
            openAppWindow({
                id: opts.id,
                appKind: 'text-editor',
                title,
                docs,
                activeTab: opts.activeTab || 0,
                agentsMdCwd: cwd,
                fileHostId,
                geom: opts.geom,
                color: opts.color,
                locked: opts.locked,
                floatGeom: opts.floatGeom,
            });
        }

        // Open (or focus) the tabbed Agent-docs editor (AGENTS.md + CLAUDE.md +
        // Sections) for a terminal's working dir. Keyed by host:cwd so re-clicking
        // the titlebar 📋 button reuses one window.
        async function openAgentsMdEditor(termId) {
            const sess = sessions.get(termId);
            const cwd = sess && sess.cwd;
            if (!cwd) {
                showNotice('working directory unknown for this session');
                return;
            }
            // The terminal's cwd is an absolute path on ITS host (local OR
            // remote). Dial that broker for every /file/* op so a remote
            // terminal edits the remote host's docs, not a local one.
            const fileHostId = (sess && sess.hostId) || 'local';
            // Host-qualify the window id: local and remote terminals can share
            // a cwd string (e.g. both rooted at /home/user), so a bare cwd key
            // would collide and reuse the wrong broker's window.
            const aid = 'app:agents:' + fileHostId + ':' + cwd;
            if (windows.has(aid)) {
                const w = windows.get(aid);
                if (w.minimized) restoreWindow(aid);
                else bringToFront(aid);
                return;
            }
            await openAgentDocsWindow({ id: aid, cwd, fileHostId });
            // Land the fresh docs window as a tab in the terminal it was opened
            // from (a [terminal│AGENTS] tab group) instead of floating.
            // openAgentDocsWindow can bail before creating the window (sandbox /
            // read error), so guard on the window actually existing. Guard on the
            // terminal living in the ACTIVE workspace, not merely existing in the
            // layout: the file-read await above can interleave a workspace switch,
            // and tabbing into an inactive-workspace tile would move the
            // freshly-mounted docs DOM across workspaces (orphaning it until that
            // workspace is revisited). A floating terminal (findKeyInLayout null)
            // or one the user navigated away from mid-open leaves the docs
            // floating, as before.
            const docsWin = windows.get(aid);
            const termLoc = findKeyInLayout(termId);
            if (docsWin && termLoc && termLoc.wsIndex === getLayout().activeWs) {
                placeWindowTiled(docsWin);        // get the docs into the layout first
                tabWindowIntoTile(aid, termId);   // relocate it as a tab beside the term
            }
        }

        // ---- mod registration: the per-terminal 📋 Agent-docs button ------
        registerMod({
            id: 'agent-docs',
            version: '1.0.0',
            ctxVersion: 1,
            // #86 trust tiers: the openers do host /file/* I/O (via editorFile,
            // funnelled through the editor mod's ctx.file) and open a window.
            tiers: ['file', 'window'],
            // #121: hard dependency on the editor mod. Its shared docs/Sections/
            // template machinery + editorFile.cap live in mods/editor/editor.js, so
            // the loader keeps this mod BLOCKED (needs: editor) whenever editor is
            // inactive, and disabling editor cascades this mod off too.
            requires: ['editor'],
            init: function (ctx) {
                // Feature-detect the per-terminal-window hook (additive ctx
                // capability, #116). An older loader without ctx.windows -> the mod
                // is inert (no button), like every other mod feature-detects its
                // ctx.* before using it.
                if (!ctx.windows) return;

                // stopPropagation helper: core used the color-picker closure's
                // `stopProp`; the mod no longer shares that scope, so define a
                // local one (same idiom as the git mod).
                const stopProp = function (e) { e.stopPropagation(); };

                // Every LIVE window's idempotent teardown, so a mod DISABLE
                // (ctx.onUnload) can remove buttons on windows that are still open
                // -- win.cleanups only fires on window close/rebuild. Each teardown
                // removes itself from the set, so a window close and a mod disable
                // never double-run and the set never leaks dead entries.
                const disposers = new Set();
                // Belt-and-braces: never decorate the same win twice (a WeakSet so
                // a closed win is GC'd out of it).
                const decorated = new WeakSet();

                ctx.windows.onTerminalCreate(function (info) {
                    const win = info.win;
                    if (!win || decorated.has(win)) return;
                    decorated.add(win);

                    // The per-terminal 📋 button (moved verbatim from core 67):
                    // open (or focus) THIS terminal's Agent-docs window, keyed by
                    // cwd so re-clicking reuses one window. addTitleBarItem inserts
                    // before the min button -- its original slot (before the git
                    // widget / color / MCP), preserving today's title-bar order.
                    const btn = document.createElement('button');
                    btn.type = 'button';
                    btn.className = 'tb-btn btn-agentsmd';
                    btn.textContent = '📋';
                    btn.title = 'Edit AGENTS.md for this folder';
                    info.addTitleBarItem(btn);

                    let torn = false;
                    // Key by the terminal WINDOW id (info.win.id) -- openAgentsMd-
                    // Editor looks up sessions/windows/findKeyInLayout by it
                    // (windows.set(id, win) in core). info.wireId is the session
                    // wire id (win.sid), used only by ctx.session.*.
                    const onClick = function (e) {
                        e.stopPropagation();
                        openAgentsMdEditor(win.id);
                    };
                    btn.addEventListener('mousedown', stopProp);
                    btn.addEventListener('click', onClick);

                    // One idempotent teardown for BOTH exits (window close via
                    // onDispose, mod disable via the disposers drain). Removes the
                    // listeners + the button; self-removes from the set so a second
                    // call no-ops.
                    const teardown = function () {
                        if (torn) return;
                        torn = true;
                        disposers.delete(teardown);
                        decorated.delete(win);
                        btn.removeEventListener('mousedown', stopProp);
                        btn.removeEventListener('click', onClick);
                        try { btn.remove(); } catch (_) {}
                    };
                    disposers.add(teardown);
                    info.onDispose(teardown);
                });

                // Mod teardown: tear down every LIVE window's button (win.cleanups
                // only fires on window close, not on a mod disable). Each teardown
                // is idempotent + self-removing, so a window that closed first has
                // already left the set. The onTerminalCreate unsubscribe is
                // auto-registered by the loader, so no new windows get decorated
                // after this.
                ctx.onUnload(function () {
                    for (const t of Array.from(disposers)) {
                        try { t(); } catch (_) {}
                    }
                    disposers.clear();
                });
            },
        });
