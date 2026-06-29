        function openTaskManagerWindow(appData) {
            const id = String(appData.id);
            const title = appData.title || 'Task manager';
            const geom = clampGeom(appData.geom || appDefaultGeom('text-editor'));
            const color = normalizeHex(appData.color || defaultColor(id));
            const locked = appData.locked !== undefined ? !!appData.locked : true;

            // Shared chrome (#79): .term-window shell + title bar (_ / ×) + the
            // eight resize handles, built + wired by the window-runtime factory.
            const chrome = buildAppChrome({
                id, appClass: 'app-tm', badge: '#tm', geom, color, locked, title,
            });
            const { dom, titleText } = chrome;

            const toolbar = document.createElement('div');
            toolbar.className = 'app-toolbar app-tm-toolbar';
            const refreshBtn = document.createElement('button');
            refreshBtn.type = 'button';
            refreshBtn.textContent = 'Refresh';
            refreshBtn.title = 'reload the session list + expanded process trees';
            toolbar.appendChild(refreshBtn);

            const tmBody = document.createElement('div');
            tmBody.className = 'tm-body';

            dom.appendChild(toolbar);
            dom.appendChild(tmBody);
            addResizeHandles(dom);   // last children: edge/corner hit zones on top

            document.getElementById('desktop').appendChild(dom);
            document.getElementById('desktop').classList.remove('empty');

            const win = {
                id, sid: 'tm', hostId: 'app',
                type: 'app', appKind: 'task-manager',
                dom, body: tmBody, titleText,
                term: null, fitAddon: null,
                ws: null, wsOpen: false, termReady: false,
                minimized: false, disposed: false,
                geom, name: title, color,
                resizeTimer: null, lastSentDims: null,
                staleSession: false, authFailed: false,
                reattachAttempts: 0, reattachAt: 0, lastOpenAt: 0, missingPolls: 0,
                cleanups: [],
                tiled: false,
                floatGeom: appData.floatGeom
                    ? Object.assign({}, appData.floatGeom) : null,
                locked,
                dirty: false,
            };
            windows.set(id, win);

            // stopProp is shared by the toolbar/destroy/kill button handlers below
            // (the dom-mousedown raise + min/close are wired by wireAppChrome).
            const stopProp = (e) => e.stopPropagation();

            // ---- per-window live state (NOT persisted) ----
            // Keyed by sess.key ('<hostId>:<windowId>') — bare wire ids collide
            // across hosts, so never key by sess.id.
            const expanded = new Set();           // sess.key currently expanded
            const procCache = new Map();          // sess.key -> { procs } | { error }
            const procSeq = new Map();            // sess.key -> latest fetch seq
            const inFlight = new Set();           // sess.key with a fetch running
            const authBad = new Set();            // sess.key 401'd -> stop polling
            const busyOps = new Set();            // sess.key|pid OR sess.key|destroy
            // Sessions we just destroyed: hide them immediately rather than wait
            // for the next /sessions poll to drop them (closeWindow tears down the
            // terminal window but leaves the `sessions` entry for the poll reaper).
            const destroyed = new Set();          // sess.key

            const liveSessions = () => Array.from(sessions.values())
                .filter(s => s && s.kind !== 'app' && !destroyed.has(s.key));

            // Host-aware POST to a /session/* endpoint for one session. Returns
            // { status, json } and never rejects. A removed host yields a
            // synthetic no-host result so callers can render "unavailable".
            const sessionPost = (sess, path, extra) => {
                const host = hostById(sess.hostId);
                if (!host) {
                    return Promise.resolve(
                        { status: 0, json: { ok: false, error: 'no_host' } });
                }
                return fetch(hostHttpUrl(host, path), {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(Object.assign({ id: sess.id }, extra || {})),
                }).then(r => r.json()
                    .then(j => ({ status: r.status, json: j }))
                    .catch(() => ({ status: r.status,
                                    json: { ok: false, error: 'HTTP ' + r.status } })))
                 .catch(e => ({ status: 0, json: { ok: false, error: String(e) } }));
            };

            // Fetch procs for one expanded session. Sequence-guarded per key so a
            // slow reply can't paint over a newer collapse/expand or a closed
            // window; skips if a fetch is already in flight or the host 401'd.
            const fetchProcs = async (sess) => {
                const key = sess.key;
                if (inFlight.has(key) || authBad.has(key)) return;
                inFlight.add(key);
                const seq = (procSeq.get(key) || 0) + 1;
                procSeq.set(key, seq);
                let res;
                try {
                    res = await sessionPost(sess, '/session/procs');
                } catch (_) {
                    res = { status: 0, json: { ok: false, error: 'error' } };
                } finally {
                    inFlight.delete(key);
                }
                // Drop a stale result: window closed, collapsed, or superseded.
                if (win.disposed || !expanded.has(key)
                    || procSeq.get(key) !== seq) return;
                if (res.status === 401) {
                    authBad.add(key);
                    procCache.set(key, { error: 'auth required (refresh to retry)' });
                    render();
                    return;
                }
                if (res.json && res.json.ok && Array.isArray(res.json.procs)) {
                    procCache.set(key, { procs: res.json.procs });
                } else {
                    const err = (res.json && res.json.error)
                        || (res.status ? 'HTTP ' + res.status : 'unreachable');
                    procCache.set(key, { error: err });
                }
                render();
            };

            // Destroy a whole session: kill its shell pid (sess.pid). Killing the
            // shell tears the agent down before it can reply, so the broker
            // returns HTTP 409 + error:'session_gone' — that is SUCCESS, same as
            // a 200 ok:true. Either way close the local window (by sess.key) and
            // drop its state. Used by the row Destroy button AND by End-process
            // when the targeted pid IS the shell pid.
            const destroySession = async (sess) => {
                const opKey = sess.key + '|destroy';
                if (busyOps.has(opKey)) return;
                busyOps.add(opKey);
                render();
                let res;
                try {
                    res = await sessionPost(sess, '/session/kill', { pid: sess.pid });
                } finally {
                    busyOps.delete(opKey);
                }
                if (win.disposed) return;
                const j = res.json || {};
                const gone = res.status === 409 && j.error === 'session_gone';
                const ok = (res.status === 200 && j.ok) || gone;
                if (!ok) {
                    showNotice('destroy failed: '
                        + (j.error || ('HTTP ' + res.status)));
                    render();
                    return;
                }
                // Forget all local state for this session, then tear the window
                // down (terminals are keyed by the host-qualified sess.key).
                expanded.delete(sess.key);
                procCache.delete(sess.key);
                procSeq.delete(sess.key);
                authBad.delete(sess.key);
                destroyed.add(sess.key);          // hide the row at once
                if (windows.has(sess.key)) closeWindow(sess.key);
                showNotice('destroyed ' + (sess.title || ('#' + sess.id)));
                render();
            };

            // End ONE process. If it's the shell pid, that's a destroy — route
            // through destroySession so the 409-as-success rule lives in one
            // place. Otherwise only a 200 ok:true counts as success; a 409 here
            // means the session vanished mid-kill, which is an error (not "ended").
            const endProcess = async (sess, pid) => {
                if (pid === sess.pid) { return destroySession(sess); }
                const opKey = sess.key + '|' + pid;
                if (busyOps.has(opKey)) return;
                busyOps.add(opKey);
                render();
                let res;
                try {
                    res = await sessionPost(sess, '/session/kill', { pid });
                } finally {
                    busyOps.delete(opKey);
                }
                if (win.disposed) return;
                const j = res.json || {};
                if (res.status === 200 && j.ok) {
                    showNotice('ended pid ' + pid);
                    fetchProcs(sess);          // re-fetch this session's tree
                } else {
                    showNotice('end process failed: '
                        + (j.error || ('HTTP ' + res.status)));
                }
                render();
            };

            // Build the indented parent->child process tree from a flat list.
            // Root = the shell (pid === sess.pid). Orphans (whose ppid isn't in
            // the list, e.g. a partial/old reply that omits the shell) are also
            // shown as roots so an expanded tree is never silently empty.
            const buildProcTree = (procs, sess) => {
                const byPid = new Map();
                for (const p of procs) byPid.set(p.pid, p);
                const kids = new Map();           // ppid -> [proc]
                const roots = [];
                for (const p of procs) {
                    if (p.pid === sess.pid || !byPid.has(p.ppid) || p.ppid === p.pid) {
                        roots.push(p);
                    } else {
                        if (!kids.has(p.ppid)) kids.set(p.ppid, []);
                        kids.get(p.ppid).push(p);
                    }
                }
                // Shell first among roots so the highlighted root is on top.
                roots.sort((a, b) => (a.pid === sess.pid ? -1
                    : b.pid === sess.pid ? 1 : a.pid - b.pid));
                const out = [];
                const seen = new Set();
                const walk = (p, depth) => {
                    if (seen.has(p.pid)) return;  // guard cycles
                    seen.add(p.pid);
                    out.push({ proc: p, depth });
                    const cs = (kids.get(p.pid) || []).sort((a, b) => a.pid - b.pid);
                    for (const c of cs) walk(c, depth + 1);
                };
                for (const r of roots) walk(r, 0);
                return out;
            };

            const truncate = (s, n) => {
                s = String(s || '');
                return s.length > n ? s.slice(0, n - 1) + '…' : s;
            };

            // Render the process subtree for an expanded session into `host`.
            const renderProcs = (sess, host) => {
                host.innerHTML = '';
                const cached = procCache.get(sess.key);
                if (authBad.has(sess.key)) {
                    const n = document.createElement('div');
                    n.className = 'tm-proc-note';
                    n.textContent = 'auth required — Refresh to retry';
                    host.appendChild(n);
                    return;
                }
                if (!cached) {
                    const n = document.createElement('div');
                    n.className = 'tm-proc-note';
                    n.textContent = 'loading…';
                    host.appendChild(n);
                    return;
                }
                if (cached.error) {
                    const n = document.createElement('div');
                    n.className = 'tm-proc-note';
                    n.textContent = '⚠ ' + cached.error;
                    host.appendChild(n);
                    return;
                }
                const procs = cached.procs || [];
                if (!procs.length) {
                    const n = document.createElement('div');
                    n.className = 'tm-proc-note';
                    n.textContent = 'no processes';
                    host.appendChild(n);
                    return;
                }
                for (const { proc, depth } of buildProcTree(procs, sess)) {
                    const isRoot = proc.pid === sess.pid;
                    const row = document.createElement('div');
                    row.className = 'tm-proc' + (isRoot ? ' tm-root' : '');
                    const pidEl = document.createElement('span');
                    pidEl.className = 'tm-pid';
                    pidEl.style.paddingLeft = (depth * 12) + 'px';
                    pidEl.textContent = String(proc.pid);
                    const nameEl = document.createElement('span');
                    nameEl.className = 'tm-pname';
                    nameEl.textContent = (isRoot ? '▣ ' : '') + (proc.name || '?');
                    const cmdEl = document.createElement('span');
                    cmdEl.className = 'tm-cmd';
                    cmdEl.textContent = truncate(proc.cmdline || proc.name || '', 200);
                    cmdEl.title = String(proc.cmdline || '');
                    const metaEl = document.createElement('span');
                    metaEl.className = 'tm-meta';
                    const bits = [];
                    if (proc.mem_mb != null) bits.push(Number(proc.mem_mb).toFixed(0) + ' MB');
                    if (proc.cpu != null) bits.push(Number(proc.cpu).toFixed(0) + '%');
                    if (proc.status) bits.push(String(proc.status));
                    metaEl.textContent = bits.join('  ');
                    const killBtn = document.createElement('button');
                    killBtn.type = 'button';
                    killBtn.className = 'tm-kill';
                    killBtn.textContent = isRoot ? 'kill shell' : 'End';
                    killBtn.title = isRoot
                        ? 'kill the shell pid (destroys this window)'
                        : 'end this process';
                    const opKey = sess.key + '|' + proc.pid;
                    const destroyKey = sess.key + '|destroy';
                    if (busyOps.has(opKey) || (isRoot && busyOps.has(destroyKey))) {
                        killBtn.disabled = true;
                        killBtn.textContent = '…';
                    }
                    killBtn.addEventListener('mousedown', stopProp);
                    killBtn.addEventListener('click', (e) => {
                        e.stopPropagation();
                        if (isRoot) {
                            if (!confirm('Kill the shell of "'
                                + (sess.title || ('#' + sess.id))
                                + '"? This destroys the window.')) return;
                        } else if (!confirm('End process ' + proc.pid
                                + ' (' + (proc.name || '?') + ')?')) {
                            return;
                        }
                        endProcess(sess, proc.pid);
                    });
                    row.appendChild(pidEl);
                    row.appendChild(nameEl);
                    row.appendChild(cmdEl);
                    row.appendChild(metaEl);
                    row.appendChild(killBtn);
                    host.appendChild(row);
                }
            };

            const multiHost = () => getHosts().length > 1;

            // Full re-render of the session list. Cheap + idempotent: it rebuilds
            // tmBody from `sessions` (kept fresh by the main poll) and paints
            // cached procs for expanded rows. Scroll is snapshotted/restored so
            // the periodic tick doesn't jump the view. Expand state lives in the
            // `expanded` Set (keyed by sess.key), so it survives re-renders.
            const render = () => {
                const scrollTop = tmBody.scrollTop;
                tmBody.innerHTML = '';
                const list = liveSessions();
                // Prune per-session state for sessions that vanished outside the
                // TM destroy path (terminal exited, closed elsewhere, or poll-
                // reaped) so the maps/sets don't grow unbounded while the TM is
                // open. Keyed by sess.key; in-flight fetches self-discard via the
                // disposed/seq guards, so dropping their guard keys here is safe.
                const liveKeys = new Set(list.map(s => s.key));
                for (const k of Array.from(expanded)) if (!liveKeys.has(k)) expanded.delete(k);
                for (const k of Array.from(procCache.keys())) if (!liveKeys.has(k)) procCache.delete(k);
                for (const k of Array.from(procSeq.keys())) if (!liveKeys.has(k)) procSeq.delete(k);
                for (const k of Array.from(authBad)) if (!liveKeys.has(k)) authBad.delete(k);
                // `destroyed` keys stay until the underlying session is gone from
                // the map (otherwise the row would flash back); once the poll
                // drops it, sessions.get won't return it so it falls out of use.
                for (const k of Array.from(destroyed)) if (!sessions.has(k)) destroyed.delete(k);
                if (!list.length) {
                    const empty = document.createElement('div');
                    empty.className = 'tm-empty';
                    empty.textContent = 'no sessions';
                    tmBody.appendChild(empty);
                    return;
                }
                // Stable order: host label, then numeric-ish id.
                list.sort((a, b) => {
                    const hl = String(a.hostLabel || '').localeCompare(
                        String(b.hostLabel || ''));
                    if (hl) return hl;
                    return String(a.id).localeCompare(String(b.id), undefined,
                        { numeric: true });
                });
                for (const sess of list) {
                    const key = sess.key;
                    const isOpen = expanded.has(key);
                    const block = document.createElement('div');
                    block.className = 'tm-sess';

                    const row = document.createElement('div');
                    row.className = 'tm-sess-row';
                    const tri = document.createElement('span');
                    tri.className = 'tm-tri';
                    tri.textContent = isOpen ? '▾' : '▸';
                    const icon = document.createElement('span');
                    icon.className = 'tm-icon';
                    icon.textContent = sess.agent ? '🤖' : '🖥';
                    const titleEl = document.createElement('span');
                    titleEl.className = 'tm-title';
                    titleEl.textContent = sess.title || ('#' + sess.id);
                    titleEl.title = sess.title || ('#' + sess.id);
                    const widEl = document.createElement('span');
                    widEl.className = 'tm-wid';
                    widEl.textContent = '#' + sess.id;
                    row.appendChild(tri);
                    row.appendChild(icon);
                    row.appendChild(titleEl);
                    row.appendChild(widEl);
                    if (multiHost() && sess.hostLabel) {
                        const hostEl = document.createElement('span');
                        hostEl.className = 'tm-host';
                        hostEl.textContent = sess.hostLabel;
                        row.appendChild(hostEl);
                    }
                    if (sess.agent) {
                        const badge = document.createElement('span');
                        badge.className = 'tm-badge';
                        badge.textContent = sess.agent;
                        row.appendChild(badge);
                    }
                    const destroyBtn = document.createElement('button');
                    destroyBtn.type = 'button';
                    destroyBtn.className = 'tm-btn';
                    destroyBtn.textContent = '✕ destroy';
                    destroyBtn.title = 'destroy this window (kills its shell)';
                    if (busyOps.has(key + '|destroy')) {
                        destroyBtn.disabled = true;
                        destroyBtn.textContent = '…';
                    }
                    destroyBtn.addEventListener('mousedown', stopProp);
                    destroyBtn.addEventListener('click', (e) => {
                        e.stopPropagation();
                        if (!confirm('Destroy "' + (sess.title || ('#' + sess.id))
                            + '"? This kills the session.')) return;
                        destroySession(sess);
                    });
                    row.appendChild(destroyBtn);

                    const toggle = () => {
                        if (expanded.has(key)) {
                            expanded.delete(key);
                        } else {
                            expanded.add(key);
                            authBad.delete(key);     // re-arm on explicit expand
                            fetchProcs(sess);
                        }
                        render();
                    };
                    row.addEventListener('click', toggle);

                    block.appendChild(row);
                    if (isOpen) {
                        const procsHost = document.createElement('div');
                        procsHost.className = 'tm-procs';
                        renderProcs(sess, procsHost);
                        block.appendChild(procsHost);
                    }
                    tmBody.appendChild(block);
                }
                tmBody.scrollTop = scrollTop;
            };

            // ---- toolbar / title wiring (same pattern as the file manager) ----
            const wireBtn = (btn, fn) => {
                const onClick = (e) => { e.stopPropagation(); fn(); };
                btn.addEventListener('mousedown', stopProp);
                btn.addEventListener('click', onClick);
                win.cleanups.push(() => {
                    btn.removeEventListener('mousedown', stopProp);
                    btn.removeEventListener('click', onClick);
                });
            };
            wireBtn(refreshBtn, () => {
                // Manual refresh: clear sticky auth flags + re-pull expanded trees.
                for (const k of Array.from(authBad)) authBad.delete(k);
                render();
                for (const sess of liveSessions()) {
                    if (expanded.has(sess.key)) fetchProcs(sess);
                }
            });

            // Raise / minimize / close / drag / 8-way resize / WM context menu.
            wireAppChrome(win, chrome);

            // Manual taskbar item — app windows are never poll-managed (same as
            // openFileManagerWindow). The synthetic kind:'app' session keeps the
            // poll reaper from closing this window + lets formatTitle render it.
            const appSess = { key: id, sid: 'tm', id, title, stale: false,
                              kind: 'app', hostId: 'app' };
            sessions.set(id, appSess);
            const itemsHost = document.getElementById('taskbar-items');
            if (!itemsHost.querySelector(
                    '.taskbar-item[data-session-id="' + cssEscape(id) + '"]')) {
                itemsHost.appendChild(buildTaskbarItem(appSess));
            }
            updateTaskbarColor(id);
            updateTaskbarLabel(id);
            const emptyMsg = document.getElementById('taskbar-empty');
            if (emptyMsg) emptyMsg.remove();

            // Periodic refresh (kept off appStore — this is a live monitor). Each
            // tick re-renders the list from `sessions` and re-pulls procs only for
            // EXPANDED sessions whose fetch isn't already in flight / auth-blocked.
            // Interval is cleared via win.cleanups (closeWindow runs them).
            const timer = setInterval(() => {
                if (win.disposed) return;
                try {
                    render();
                    for (const sess of liveSessions()) {
                        if (expanded.has(sess.key)
                            && !inFlight.has(sess.key)
                            && !authBad.has(sess.key)) {
                            fetchProcs(sess);
                        }
                    }
                } catch (_) { /* never let a tick throw */ }
            }, 2500);
            win.cleanups.push(() => clearInterval(timer));

            render();
            if (findKeyInLayout(id)) placeWindowTiled(win);
            else bringToFront(id);
            return win;
        }

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
