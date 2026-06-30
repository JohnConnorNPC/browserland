        // ---- mod: file manager (S11 / #84) --------------------------------
        // The dual-pane file manager, extracted from core (fragment
        // 71_js_file_manager.js) as a ctx.registerWindowKind mod (#84). It was a
        // core built-in in #80's window-kind registry; the same spec now ships
        // here and registers through the mod's ctx, so a file manager is a
        // first-class window everywhere the registry is consulted (open /
        // serialize / restore / close / the (+) launch menu). Only its OWNER
        // moved from core to a mod. HIGHEST review tier — it performs DESTRUCTIVE
        // delete (move = copy + delete-source) and upload (overwrite).
        //
        // What moved here, verbatim except the I/O swap below: the dual-pane
        // builder openFileManagerWindow (list/preview, per-pane host picker #46,
        // open-in-editor handoff, OS-file drop-to-upload, cross-pane copy/move
        // straddling two hosts, the removed-host / re-auth lifecycle hooks) and
        // the (+) launcher launchFileManager (moved from core 76). Both are
        // top-level `function` declarations, so they HOIST across the one
        // concatenated <script> and stay reachable from core regardless of
        // mods_enabled — the same posture the editor/sticky mods use.
        //
        // PERSISTENCE IS BYTE-IDENTICAL (the issue's hard requirement): the spec
        // reuses the EXACT core serializer serializeAppWindow (still core, shared
        // with the editor + sticky mods), so every serialized field — fmLeft,
        // fmRight, fmLeftHostId, fmRightHostId, fileHostId — round-trips unchanged
        // through webterm:appwindows:v1.
        //
        // FILE I/O rides ctx.file (#82): every /file/* call goes through the
        // fmFile() accessor (below) instead of fileApiPost / a raw upload fetch,
        // so all filesystem access — including the DESTRUCTIVE delete + upload —
        // is funneled through the one reviewed capability. Host routing is
        // byte-identical — each call passes the host *id* of the host object the
        // pane already resolved (paneHost(...).id), which ctx.file / _modFileApi
        // re-resolve to the SAME cached object. The upload-clash check moves from
        // the raw HTTP 409 status to the parsed body field error === 'exists'
        // (app.py always pairs a 409 with {ok:false,error:'exists'}), so the
        // overwrite prompt is preserved.
        //
        // mods_enabled=false posture: the file-manager kind is simply not
        // REGISTERED, so the (+) "File manager" launcher disappears. Unlike the
        // editor (whose builder openNoteOrEditorWindow IS the registry's unknown-
        // kind default), a persisted file-manager record is NOT coerced into a
        // note/editor on restore — openAppWindow's narrowed fallback (#84, core
        // 54) returns null for an unknown non-note/editor kind, leaving the stored
        // record intact so re-enabling the mod restores it faithfully. The builder
        // is still hoisted (reachable when the mod IS on), and fmFile() degrades
        // to the SAME core _modFileApi ctx.file wraps so I/O is identical mods on
        // or off.

        // ---- ctx.file accessor --------------------------------------------
        // The single choke point every file-manager /file/* call flows through.
        // init() stashes the per-mod ctx.file on fmFile.cap (a function property —
        // no TDZ, the window.__mods / editorFile.cap pattern), which the hoisted
        // builder closures read via fmFile(). With mods off (init never ran) it
        // degrades to a literal mirror of ctx.file's read/list/delete/upload over
        // the hoisted core _modFileApi (the SAME plumbing ctx.file wraps, identical
        // request bodies + fail-closed host-id routing), so the file manager's I/O
        // is identical mods on or off. opts.host is a host-id (win.fileHostId
        // semantics): '' / 'local' / omitted -> local broker, a known id -> that
        // broker, an UNKNOWN remote id -> {ok:false,error:'host_not_found'} (no
        // request) so a removed host never silently falls back to local.
        function fmFile() {
            return fmFile.cap || {
                read: function (path, opts) {
                    const body = { path: path };
                    if (opts && opts.b64 === true) body.b64 = true;
                    return _modFileApi('/file/read', body, opts);
                },
                list: function (path, opts) {
                    return _modFileApi('/file/list', { path: path || '' }, opts);
                },
                'delete': function (path, opts) {
                    return _modFileApi('/file/delete',
                        { path: path,
                          recursive: !!(opts && opts.recursive) }, opts);
                },
                upload: function (path, contentB64, opts) {
                    return _modFileApi('/file/upload',
                        { path: path, content_b64: contentB64,
                          overwrite: !!(opts && opts.overwrite) }, opts);
                },
                // #72 richer ops mirror (mods-off parity with ctx.file).
                mkdir: function (path, opts) {
                    return _modFileApi('/file/mkdir', { path: path }, opts);
                },
                copy: function (src, dst, opts) {
                    return _modFileApi('/file/copy',
                        { src: src, dst: dst,
                          overwrite: !!(opts && opts.overwrite) }, opts);
                },
                move: function (src, dst, opts) {
                    return _modFileApi('/file/move',
                        { src: src, dst: dst,
                          overwrite: !!(opts && opts.overwrite) }, opts);
                },
                zip: function (src, dest, opts) {
                    return _modFileApi('/file/zip',
                        { src: src, dest: dest,
                          overwrite: !!(opts && opts.overwrite) }, opts);
                },
                unzip: function (path, dest, opts) {
                    return _modFileApi('/file/unzip',
                        { path: path, dest: dest }, opts);
                },
                stat: function (path, opts) {
                    return _modFileApi('/file/stat', { path: path }, opts);
                },
            };
        }

        const FM_DRAG_MIME = 'application/x-webterm-file';
        function openFileManagerWindow(appData) {
            const id = String(appData.id);
            const title = appData.title || 'Files';
            const geom = clampGeom(appData.geom || appDefaultGeom('text-editor'));
            const color = normalizeHex(appData.color || defaultColor(id));
            const locked = appData.locked !== undefined ? !!appData.locked : true;

            // Shared chrome (#79): .term-window shell + title bar (_ / ×) + the
            // eight resize handles, built + wired by the window-runtime factory.
            const chrome = buildAppChrome({
                id, appClass: 'app-fm', badge: '#fm', geom, color, locked, title,
            });
            const { dom, titleText } = chrome;

            // Toolbar (Refresh / Open / ↑ Up) — same chrome class as the editor.
            const toolbar = document.createElement('div');
            toolbar.className = 'app-toolbar app-fm-toolbar';
            const mkBtn = (label, ttl) => {
                const b = document.createElement('button');
                b.type = 'button';
                b.textContent = label;
                if (ttl) b.title = ttl;
                return b;
            };
            const refreshBtn = mkBtn('Refresh', 'reload both panes');
            const openBtn = mkBtn('Open', 'open the selected file in the editor');
            const upBtn = mkBtn('↑ Up', 'go to the parent of the active pane');
            toolbar.appendChild(refreshBtn);
            toolbar.appendChild(openBtn);
            toolbar.appendChild(upBtn);

            // Body: a flex row of two equal panes (left/right), each a path
            // header above a scrollable list. Made focusable (tabIndex 0) so
            // the window can take focus for the Tab-toggles-pane shortcut.
            const fmBody = document.createElement('div');
            fmBody.className = 'app-fm-body';
            fmBody.tabIndex = 0;
            const mkPane = (side) => {
                const pane = document.createElement('div');
                pane.className = 'app-fm-pane';
                pane.dataset.side = side;
                // Header (#46): a per-pane host picker + the absolute cwd. Split
                // view can straddle two hosts, so the host control lives on each
                // PANE, not the window.
                const head = document.createElement('div');
                head.className = 'app-fm-head';
                const hostBtn = document.createElement('button');
                hostBtn.type = 'button';
                hostBtn.className = 'app-fm-hostbtn';
                // Re-home / choose-folder (#46 follow-up): jump THIS pane to a
                // folder picked from a dialog (in addition to click-navigation).
                const folderBtn = document.createElement('button');
                folderBtn.type = 'button';
                folderBtn.className = 'app-fm-hostbtn';
                folderBtn.textContent = '📁';
                folderBtn.title = 'choose this pane’s folder';
                const path = document.createElement('div');
                path.className = 'app-fm-path';
                path.textContent = '/';
                head.appendChild(hostBtn);
                head.appendChild(folderBtn);
                head.appendChild(path);
                const list = document.createElement('div');
                list.className = 'app-fm-list';
                pane.appendChild(head);
                pane.appendChild(list);
                return { pane, head, path, list, hostBtn, folderBtn };
            };
            const left = mkPane('left');
            const right = mkPane('right');
            fmBody.appendChild(left.pane);
            fmBody.appendChild(right.pane);

            dom.appendChild(toolbar);
            dom.appendChild(fmBody);
            addResizeHandles(dom);   // last children: edge/corner hit zones on top

            document.getElementById('desktop').appendChild(dom);
            document.getElementById('desktop').classList.remove('empty');

            const win = {
                id, sid: 'fm', hostId: 'app',
                type: 'app', appKind: 'file-manager',
                // body is the focusable FM root (vs a textarea for editors) so
                // bringToFront/focusWin can focus it for the Tab shortcut.
                dom, body: fmBody, titleText,
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
                // Legacy single-window host (#35). Kept ONLY so an old record
                // (written before per-pane host) restores faithfully: each pane
                // back-fills from it just below. Pane file ops use fmLeftHostId/
                // fmRightHostId now, and removeHost keys the FM on appKind — so
                // this is never again consulted for routing or cleanup.
                fileHostId: appData.fileHostId || 'local',
                // Per-pane host (#46): split view can straddle two brokers, so
                // the host is a property of each PANE. An old single-host record
                // (no fmLeftHostId/fmRightHostId) seeds both panes from the
                // legacy fileHostId; a brand-new FM seeds both from the launch
                // host (see launchFileManager).
                fmLeftHostId: appData.fmLeftHostId
                    || appData.fileHostId || 'local',
                fmRightHostId: appData.fmRightHostId
                    || appData.fileHostId || 'local',
                // The two panes' current ABSOLUTE dirs ('' = the broker's default
                // dir on first render, then adopted as the server's absolute cwd).
                // A legacy root-relative value still resolves under the default
                // dir, so old windows upgrade themselves to absolute on first list.
                fmLeft: appData.fmLeft != null ? String(appData.fmLeft) : '',
                fmRight: appData.fmRight != null ? String(appData.fmRight) : '',
            };
            windows.set(id, win);

            // stopProp is shared by the toolbar/pane/host/folder button handlers
            // below (the dom-mousedown raise + min/close are wired by wireAppChrome).
            const stopProp = (e) => e.stopPropagation();

            // ---- pane state + rendering ----
            // The active pane (left/right) gets the .active outline; clicking a
            // pane activates it. paneSeq guards against stale async renders: a
            // slower /file/list must not paint over a newer navigation/upload.
            let activeSide = 'left';
            const paneSeq = { left: 0, right: 0 };
            const sideOf = (s) => (s === 'left' ? left : right);
            const dirKey = (s) => (s === 'left' ? 'fmLeft' : 'fmRight');
            const setActive = (s) => {
                activeSide = s;
                left.pane.classList.toggle('active', s === 'left');
                right.pane.classList.toggle('active', s === 'right');
            };
            // Compose an ABSOLUTE child path under a pane's cwd, in the host's
            // own separator (#35). Empty cwd -> bare name (server default dir).
            const joinPath = (cwd, name) => joinNative(cwd, name);
            const hostKey = (s) => (s === 'left' ? 'fmLeftHostId'
                                                 : 'fmRightHostId');
            // Resolve a pane's broker (#46). MIRRORS the editor's fileHost():
            // a known-remote host that no longer resolves returns null (the op
            // aborts + the pane shows an error) rather than falling back to
            // local — a remote absolute path can also exist INSIDE the local
            // root, so a silent local fallback could read or clobber the wrong
            // file. Only an empty/'local' id ever resolves to localHost().
            const paneHost = (s) => {
                const id = win[hostKey(s)];
                const h = hostById(id);
                if (h) return h;
                if (!id || id === 'local') return localHost();
                return null;
            };
            // Reflect a pane's current host on its header button.
            const updatePaneHostBtn = (s) => {
                const ui = sideOf(s);
                if (!ui.hostBtn) return;
                const id = win[hostKey(s)];
                const h = hostById(id)
                    || ((!id || id === 'local') ? localHost() : null);
                const lbl = h ? hostPickerLabel(h) : (id + ' (removed)');
                ui.hostBtn.textContent = '🖥 ' + lbl + ' ▾';
                ui.hostBtn.title = s + ' pane on: ' + lbl
                    + ' — click to switch host';
            };
            // Switch a pane to another broker: reset its dir ('' = the new
            // host's default dir, since the old ABSOLUTE path belonged to the
            // old host) and re-list (which pops the new host's login if needed).
            const switchPaneHost = (s, host) => {
                if (!host || host.id === win[hostKey(s)]) return;
                win[hostKey(s)] = host.id;
                win[dirKey(s)] = '';
                saveAppWindow(win);
                updatePaneHostBtn(s);
                renderPane(s);
            };

            const renderPane = async (side, _retried) => {
                const ui = sideOf(side);
                const seq = ++paneSeq[side];
                const reqDir = win[dirKey(side)];
                const reqHostId = win[hostKey(side)];
                if (ui.hostBtn) updatePaneHostBtn(side);
                const host = paneHost(side);
                if (!host) {
                    // Known-remote host removed: NEVER let fileApiPost fall back
                    // to its `host || localHost()` default — that would silently
                    // list LOCAL files in a pane that was showing a remote.
                    ui.path.textContent = '(no host)';
                    ui.path.title = '';
                    ui.list.innerHTML = '';
                    const errRow = document.createElement('div');
                    errRow.className = 'app-fm-row';
                    errRow.textContent = '⚠ host unavailable — pick a host';
                    ui.list.appendChild(errRow);
                    return;
                }
                const res = await fmFile().list(reqDir, { host: host.id });
                // Drop a stale render: window closed, a newer render started, or
                // the pane navigated / switched HOST while we awaited — a stale
                // reply must not paint, nor pop the wrong host's login.
                if (win.disposed || seq !== paneSeq[side]
                    || win[dirKey(side)] !== reqDir
                    || win[hostKey(side)] !== reqHostId) return;
                if (!res || !res.ok) {
                    // Not authenticated on this host yet: pop its login (without
                    // stealing an in-progress different-host form) and show a
                    // neutral placeholder — not a scary ⚠ toast + row. _onHostAuth
                    // re-lists the pane once that host authenticates.
                    if (res && res.error === 'auth_required') {
                        promptFileHostAuth(host);
                        ui.path.textContent = hostPickerLabel(host);
                        ui.path.title = hostPickerLabel(host);
                        ui.list.innerHTML = '';
                        const signin = document.createElement('div');
                        signin.className = 'app-fm-row';
                        signin.textContent = '🔒 sign in to '
                            + hostPickerLabel(host) + '…';
                        ui.list.appendChild(signin);
                        return;
                    }
                    // A vanished dir (deleted/renamed) resets the pane to root and
                    // re-renders once — the guard above stops an infinite retry.
                    if (res && res.error === 'not_found' && !_retried && reqDir) {
                        win[dirKey(side)] = '';
                        saveAppWindow(win);
                        return renderPane(side, true);
                    }
                    showNotice('list failed: ' + ((res && res.error) || '?'));
                    ui.list.innerHTML = '';
                    const errRow = document.createElement('div');
                    errRow.className = 'app-fm-row';
                    errRow.textContent = '⚠ ' + ((res && res.error) || 'error');
                    ui.list.appendChild(errRow);
                    return;
                }
                // Adopt the server's canonical cwd (collapses '.'/trailing slash).
                const cwd = res.cwd || '';
                if (win[dirKey(side)] !== cwd) {
                    win[dirKey(side)] = cwd;
                    saveAppWindow(win);
                }
                // Absolute path now (#35) — show it as-is, not '/'-prefixed.
                ui.path.textContent = cwd;
                ui.path.title = cwd;
                ui.list.innerHTML = '';
                // Selecting a row (single-click) marks it for Open/Enter; only
                // file rows carry a path. Tracked per render via the .sel class.
                const selectRow = (row) => {
                    ui.list.querySelectorAll('.app-fm-row.sel')
                        .forEach(r => r.classList.remove('sel'));
                    row.classList.add('sel');
                };
                const openFile = async (path) => {
                    const h = paneHost(side);
                    if (!h) {
                        showNotice('open failed: host unavailable');
                        return;
                    }
                    const r = await fmFile().read(path, { host: h.id });
                    if (!r || !r.ok) {
                        if (r && r.error === 'auth_required') {
                            promptFileHostAuth(h);
                        }
                        showNotice('open failed: ' + ((r && r.error) || '?'));
                        return;
                    }
                    // Open the file on the SAME host THIS PANE is browsing (#46),
                    // at the dir it lives in, so its editor Save lands back there.
                    openAppWindow({
                        id: newAppId('editor'),
                        appKind: 'text-editor',
                        filePath: r.path || path,
                        content: r.content || '',
                        title: baseName(r.path || path),
                        fileHostId: win[hostKey(side)],
                    });
                };
                // Right-click row menu (#72). One builder shared by file and dir
                // rows; the item set grows in later commits. Today (pure
                // extraction): copy/move the row to the other pane. Captures the
                // descriptor (host id + child path + name) at build time so a
                // pane navigation mid-menu can't redirect the action.
                // Open a row = exactly what its dblclick does: a dir navigates,
                // a file opens in the editor.
                const activateRow = (ent, child) => {
                    if (ent.type === 'dir') {
                        win[dirKey(side)] = child;
                        saveAppWindow(win);
                        renderPane(side);
                    } else {
                        openFile(child);
                    }
                };
                // Make a row draggable to the other pane / another FM window
                // (#72): files AND dirs. effectAllowed='copyMove' so a plain drag
                // copies and a Shift-drag moves (the drop handler reads e.shiftKey
                // and the payload carries the type for the cross-host dir refusal).
                const makeDraggable = (row, ent, child) => {
                    row.draggable = true;
                    row.addEventListener('dragstart', (e) => {
                        setActive(side); selectRow(row);
                        if (!e.dataTransfer) return;
                        e.dataTransfer.effectAllowed = 'copyMove';
                        e.dataTransfer.setData(FM_DRAG_MIME, JSON.stringify({
                            winId: id, side, hostId: win[hostKey(side)],
                            path: child, name: ent.name, type: ent.type }));
                        row.classList.add('dragging');
                    });
                    row.addEventListener('dragend',
                        () => row.classList.remove('dragging'));
                };
                // Rename in place = a /file/move to a validated sibling name in
                // this same dir (cwd). Client-side name validation is UX; the
                // server re-checks.
                const renameRow = async (ent, child) => {
                    const host = paneHost(side);
                    if (!host) {
                        showNotice('rename failed: host unavailable');
                        return;
                    }
                    const name = await openTextPrompt({
                        title: 'Rename', label: 'New name', value: ent.name,
                        okLabel: 'Rename', validate: validateName });
                    if (name == null || win.disposed) return;
                    const trimmed = name.trim();
                    if (trimmed === ent.name) return;        // no change
                    const dst = joinNative(cwd, trimmed);
                    let res = await fmFile().move(child, dst, { host: host.id });
                    if (win.disposed) return;
                    if (res && res.error === 'exists') {
                        const ok = await openConfirmDialog({
                            title: 'Rename',
                            message: 'Overwrite ' + trimmed + '?',
                            okLabel: 'Overwrite', danger: true });
                        if (!ok || win.disposed) return;
                        res = await fmFile().move(child, dst,
                            { host: host.id, overwrite: true });
                        if (win.disposed) return;
                    }
                    if (!(res && res.ok)) {
                        if (res && res.error === 'auth_required') {
                            promptFileHostAuth(host);
                        }
                        showNotice('rename failed: ' + ((res && res.error) || '?'));
                        return;
                    }
                    showNotice('renamed to ' + trimmed);
                    renderPane(side);
                };
                // Delete with a styled confirm; a directory deletes recursively.
                const deleteRow = async (ent, child) => {
                    const host = paneHost(side);
                    if (!host) {
                        showNotice('delete failed: host unavailable');
                        return;
                    }
                    const isDir = ent.type === 'dir';
                    const ok = await openConfirmDialog({
                        title: 'Delete',
                        message: 'Delete ' + (isDir ? 'folder ' : '') + ent.name
                            + (isDir ? ' and everything inside it?' : '?'),
                        okLabel: 'Delete', danger: true });
                    if (!ok || win.disposed) return;
                    const res = await fmFile().delete(child,
                        { host: host.id, recursive: isDir });
                    if (win.disposed) return;
                    if (!(res && res.ok)) {
                        if (res && res.error === 'auth_required') {
                            promptFileHostAuth(host);
                        }
                        showNotice('delete failed: ' + ((res && res.error) || '?'));
                        return;
                    }
                    showNotice('deleted ' + ent.name);
                    renderPane(side);
                };
                // Download a file to the local machine: read base64, decode to
                // bytes, save via an anchor. Bounded by /file/read's 5 MiB cap —
                // a larger file surfaces a clear notice (a streaming download GET
                // is a future enhancement, #72 risks).
                const downloadRow = async (ent, child) => {
                    const host = paneHost(side);
                    if (!host) {
                        showNotice('download failed: host unavailable');
                        return;
                    }
                    const r = await fmFile().read(child,
                        { b64: true, host: host.id });
                    if (win.disposed) return;
                    if (!(r && r.ok) || typeof r.content_b64 !== 'string') {
                        const err = (r && r.error) || '?';
                        if (err === 'auth_required') promptFileHostAuth(host);
                        if (err === 'too_large') {
                            showNotice(ent.name
                                + ': too large to download (>5 MiB)');
                        } else {
                            showNotice('download failed: ' + err);
                        }
                        return;
                    }
                    try {
                        const bin = atob(r.content_b64);
                        const arr = new Uint8Array(bin.length);
                        for (let i = 0; i < bin.length; i++) {
                            arr[i] = bin.charCodeAt(i);
                        }
                        const url = URL.createObjectURL(
                            new Blob([arr], { type: 'application/octet-stream' }));
                        const a = document.createElement('a');
                        a.href = url;
                        a.download = ent.name;
                        document.body.appendChild(a);
                        a.click();
                        document.body.removeChild(a);
                        setTimeout(() => URL.revokeObjectURL(url), 10000);
                    } catch (_) {
                        showNotice('download failed: could not decode '
                            + ent.name);
                    }
                };
                // Zip a file/folder into a .zip in this dir (prompt for the name).
                const zipRow = async (ent, child) => {
                    const host = paneHost(side);
                    if (!host) {
                        showNotice('zip failed: host unavailable');
                        return;
                    }
                    const name = await openTextPrompt({
                        title: 'Zip', label: 'Archive name',
                        value: ent.name + '.zip', okLabel: 'Zip',
                        validate: validateName });
                    if (name == null || win.disposed) return;
                    const dest = joinNative(cwd, name.trim());
                    let res = await fmFile().zip(child, dest, { host: host.id });
                    if (win.disposed) return;
                    if (res && res.error === 'exists') {
                        const ok = await openConfirmDialog({
                            title: 'Zip',
                            message: 'Overwrite ' + name.trim() + '?',
                            okLabel: 'Overwrite', danger: true });
                        if (!ok || win.disposed) return;
                        res = await fmFile().zip(child, dest,
                            { host: host.id, overwrite: true });
                        if (win.disposed) return;
                    }
                    if (!(res && res.ok)) {
                        if (res && res.error === 'auth_required') {
                            promptFileHostAuth(host);
                        }
                        showNotice('zip failed: ' + ((res && res.error) || '?'));
                        return;
                    }
                    showNotice('created ' + name.trim());
                    renderPane(side);
                };
                // Unzip a .zip into a fresh archive-stem sibling dir.
                const unzipRow = async (ent, child) => {
                    const host = paneHost(side);
                    if (!host) {
                        showNotice('unzip failed: host unavailable');
                        return;
                    }
                    const stem = ent.name.replace(/\.zip$/i, '')
                        || (ent.name + '_extracted');
                    const dest = joinNative(cwd, stem);
                    const res = await fmFile().unzip(child, dest,
                        { host: host.id });
                    if (win.disposed) return;
                    if (!(res && res.ok)) {
                        if (res && res.error === 'auth_required') {
                            promptFileHostAuth(host);
                        }
                        const err = (res && res.error) || '?';
                        if (err === 'exists') {
                            showNotice('unzip failed: "' + stem
                                + '" already exists');
                        } else {
                            showNotice('unzip failed: ' + err);
                        }
                        return;
                    }
                    showNotice('extracted to ' + stem);
                    renderPane(side);
                };
                const buildRowMenu = (row, ent, child) => {
                    const other = side === 'left' ? 'right' : 'left';
                    const desc = { hostId: win[hostKey(side)], path: child,
                                   name: ent.name, type: ent.type };
                    // Paste lands in the dir itself for a folder row, else the
                    // current cwd (a file row's sibling dir).
                    const pasteDir = ent.type === 'dir' ? child : cwd;
                    const items = [];
                    items.push({ label: 'Open', enabled: true,
                                 action: () => activateRow(ent, child) });
                    items.push({ sep: true });
                    items.push({ label: 'Cut', enabled: true,
                                 action: () => setClipboard('cut', desc) });
                    items.push({ label: 'Copy', enabled: true,
                                 action: () => setClipboard('copy', desc) });
                    items.push({ label: 'Paste', enabled: !!win.fmClipboard,
                                 action: () => pasteInto(side, pasteDir) });
                    items.push({ label: 'Rename…', enabled: true,
                                 action: () => renameRow(ent, child) });
                    if (ent.type !== 'dir') {
                        items.push({ label: 'Download', enabled: true,
                                     action: () => downloadRow(ent, child) });
                    }
                    items.push({ label: 'Zip', enabled: true,
                                 action: () => zipRow(ent, child) });
                    if (ent.type !== 'dir' && /\.zip$/i.test(ent.name)) {
                        items.push({ label: 'Unzip', enabled: true,
                                     action: () => unzipRow(ent, child) });
                    }
                    items.push({ label: 'Delete', enabled: true,
                                 action: () => deleteRow(ent, child) });
                    items.push({ sep: true });
                    items.push({ label: 'Copy → ' + other + ' pane',
                                 enabled: true,
                                 action: () => doTransfer(other, desc, false) });
                    items.push({ label: 'Move → ' + other + ' pane',
                                 enabled: true,
                                 action: () => doTransfer(other, desc, true) });
                    items.push({ sep: true });
                    items.push({ label: 'Properties…', enabled: true,
                                 action: () => showProperties(side, child,
                                                              ent.name) });
                    return items;
                };
                const onRowMenu = (e, row, ent, child) => {
                    e.preventDefault();
                    e.stopPropagation();
                    setActive(side); selectRow(row);
                    renderMenu(buildRowMenu(row, ent, child),
                               e.clientX, e.clientY);
                };
                // '..' row (not at root): navigates to the parent dir.
                if (res.parent !== null && res.parent !== undefined) {
                    const up = document.createElement('div');
                    up.className = 'app-fm-row';
                    up.innerHTML = '<span class="fm-icon">📁</span>'
                        + '<span class="fm-name">..</span>'
                        + '<span class="fm-size"></span>';
                    const onUp = () => {
                        win[dirKey(side)] = res.parent;
                        saveAppWindow(win);
                        renderPane(side);
                    };
                    up.addEventListener('click', () => { setActive(side); selectRow(up); });
                    up.addEventListener('dblclick', onUp);
                    // '..' has no row menu (#72) — swallow the right-click so it
                    // doesn't fall through to the pane's empty-area menu.
                    up.addEventListener('contextmenu', (e) => {
                        e.preventDefault(); e.stopPropagation();
                    });
                    ui.list.appendChild(up);
                }
                for (const ent of (res.entries || [])) {
                    const row = document.createElement('div');
                    row.className = 'app-fm-row';
                    row.dataset.type = ent.type;
                    const icon = ent.type === 'dir' ? '📁' : '📄';
                    row.innerHTML = '<span class="fm-icon">' + icon
                        + '</span><span class="fm-name"></span>'
                        + '<span class="fm-size">'
                        + (ent.type === 'dir' ? '' : fmtSize(ent.size))
                        + '</span>';
                    row.querySelector('.fm-name').textContent = ent.name;
                    const child = joinPath(cwd, ent.name);
                    if (ent.type === 'dir') {
                        const enter = () => {
                            win[dirKey(side)] = child;
                            saveAppWindow(win);
                            renderPane(side);
                        };
                        row.addEventListener('click', () => {
                            setActive(side); selectRow(row);
                        });
                        row.addEventListener('dblclick', enter);
                    } else {
                        row.dataset.path = child;
                        row.addEventListener('click', () => {
                            setActive(side); selectRow(row);
                        });
                        row.addEventListener('dblclick', () => openFile(child));
                    }
                    // Draggable + shared row menu on BOTH file and dir rows (#72).
                    makeDraggable(row, ent, child);
                    row.addEventListener('contextmenu',
                        (e) => onRowMenu(e, row, ent, child));
                    ui.list.appendChild(row);
                }
            };
            // Activate the row currently selected in the active pane (toolbar
            // Open + Enter): a file row opens it in the editor, a dir/'..' row
            // navigates — exactly what each row's dblclick handler does.
            const openOrEnterActive = () => {
                const ui = sideOf(activeSide);
                const sel = ui.list.querySelector('.app-fm-row.sel');
                if (!sel) return;
                sel.dispatchEvent(new MouseEvent('dblclick'));
            };

            // ---- per-pane click-to-activate + drop-to-upload ----
            // Upload base64 bytes to a SPECIFIC host (#46) through the reviewed
            // ctx.file capability (#82). hostId is a host-id string (paneHost(s).id,
            // captured once so the whole upload lands on one broker); overwrite
            // (default false) lets a drop onto an existing file retry after a
            // confirm. Resolves to {ok,path,size} | {ok:false,error}; a clash
            // surfaces as error:'exists' (app.py pairs the 409 with that body, so
            // the old raw-status check is byte-equivalent). Shared by OS-file drops
            // (per-pane host) and the cross-pane transfer (dest pane's host).
            const uploadTo = (hostId, path, b64, overwrite) =>
                fmFile().upload(path, b64, { host: hostId, overwrite: overwrite });
            const readB64 = (file) => new Promise((resolve, reject) => {
                const fr = new FileReader();
                fr.onload = () => {
                    const s = String(fr.result || '');
                    const i = s.indexOf(',');         // strip data:...;base64,
                    resolve(i === -1 ? '' : s.slice(i + 1));
                };
                fr.onerror = () => reject(fr.error || new Error('read failed'));
                fr.readAsDataURL(file);
            });
            const dropFiles = async (side, fileList) => {
                const files = Array.from(fileList || []);
                if (!files.length) return;
                // Upload to THIS pane's host (#46), captured once so the whole
                // drop lands on one broker even if the pane switches mid-upload.
                const host = paneHost(side);
                if (!host) {
                    showNotice('upload failed: host unavailable');
                    return;
                }
                const cwd = win[dirKey(side)];
                let done = 0;
                for (const file of files) {
                    let b64;
                    try { b64 = await readB64(file); }
                    catch (_) { showNotice('could not read ' + file.name); continue; }
                    if (win.disposed) return;         // closed mid-upload
                    const path = joinPath(cwd, file.name);
                    let res = await uploadTo(host.id, path, b64, false);
                    if (win.disposed) return;         // closed before the prompt
                    // Existing file -> styled confirm + retry as an overwrite.
                    if (res && res.error === 'exists') {
                        const ok = await openConfirmDialog({
                            title: 'Overwrite',
                            message: 'Overwrite ' + file.name + '?',
                            okLabel: 'Overwrite', danger: true });
                        if (!ok || win.disposed) { if (win.disposed) return; continue; }
                        res = await uploadTo(host.id, path, b64, true);
                    }
                    if (win.disposed) return;
                    if (res && res.ok) { done++; continue; }
                    const err = (res && res.error) || 'error';
                    if (err === 'too_large') showNotice(file.name + ': too large (>5 MiB)');
                    else showNotice('upload failed (' + file.name + '): ' + err);
                }
                if (win.disposed) return;
                if (done) showNotice('uploaded ' + done + ' file(s)');
                renderPane(side);
            };

            // ---- cross-pane copy / move (#46) ----
            // Copy (or move) ONE file between panes — works ACROSS hosts: read
            // the source broker's bytes as base64 (binary-safe, server-side
            // encode), write them to the dest broker via /file/upload, and for
            // a move delete the source afterwards. Each side is gated by its
            // OWN per-host auth. The descriptor is captured at call time and
            // never recomputed after a prompt/await, so navigating a pane
            // mid-transfer can't redirect the write (codex review).
            // Cross-host SINGLE-FILE byte path (the only transfer a single
            // broker can't do server-side). Reached from doTransfer for a
            // cross-host file. Returns true on a complete success, false
            // otherwise (incl. the honest "copied but couldn't remove source"
            // partial-move), so a cut-paste only clears its clipboard when the
            // move actually completed.
            const transferTo = async (destSide, srcHostId, srcPath, srcName,
                                      move) => {
                const srcHost = hostById(srcHostId)
                    || ((!srcHostId || srcHostId === 'local')
                        ? localHost() : null);
                const destHost = paneHost(destSide);
                if (!srcHost) {
                    showNotice('transfer failed: source host unavailable');
                    return false;
                }
                if (!destHost) {
                    showNotice('transfer failed: destination host unavailable');
                    return false;
                }
                const destPath = joinNative(win[dirKey(destSide)], srcName);
                // Refuse a copy/move onto the SAME file (same host + same
                // absolute path): the copy would be a pointless self-overwrite,
                // and a self-MOVE would then delete the file it just wrote.
                if (srcHost.id === destHost.id && destPath === srcPath) {
                    showNotice('source and destination are the same file');
                    return false;
                }
                const r = await fmFile().read(srcPath,
                    { b64: true, host: srcHost.id });
                if (win.disposed) return false;
                if (!r || !r.ok) {
                    if (r && r.error === 'auth_required') {
                        promptFileHostAuth(srcHost);
                    }
                    const err = (r && r.error) || '?';
                    if (err === 'too_large') {
                        showNotice(srcName + ': too large (>5 MiB)');
                    } else {
                        showNotice('transfer failed (read ' + srcName + '): '
                            + err);
                    }
                    return false;
                }
                // The source broker accepted the read but returned no
                // content_b64: it predates the binary-safe read (#46 review).
                // Abort BEFORE any write/delete — an empty upload followed by a
                // move's delete-source would silently lose the file. '' is a
                // valid empty file (still a string), so check the TYPE, not
                // truthiness.
                if (typeof r.content_b64 !== 'string') {
                    showNotice('transfer failed: ' + srcName + ' — source broker '
                        + 'lacks binary read (upgrade it)');
                    return false;
                }
                const b64 = r.content_b64;
                let up = await uploadTo(destHost.id, destPath, b64, false);
                if (win.disposed) return false;
                // Existing dest file -> styled confirm + retry as an overwrite.
                if (up && up.error === 'exists') {
                    const ok = await openConfirmDialog({
                        title: 'Overwrite',
                        message: 'Overwrite ' + srcName + ' on "'
                            + hostPickerLabel(destHost) + '"?',
                        okLabel: 'Overwrite', danger: true });
                    if (!ok || win.disposed) return false;
                    up = await uploadTo(destHost.id, destPath, b64, true);
                    if (win.disposed) return false;
                }
                if (!(up && up.ok)) {
                    const err = (up && up.error) || 'error';
                    if (err === 'auth_required') promptFileHostAuth(destHost);
                    if (err === 'too_large') {
                        showNotice(srcName + ': too large (>5 MiB)');
                    } else {
                        showNotice('transfer failed (write ' + srcName + '): '
                            + err);
                    }
                    return false;
                }
                // Move = copy succeeded, now delete the source. Best-effort and
                // HONEST: the copy already landed, so a failed delete leaves the
                // file on BOTH hosts — say so rather than claim a move (codex).
                if (move) {
                    const d = await fmFile().delete(srcPath, { host: srcHost.id });
                    if (win.disposed) return false;
                    if (!(d && d.ok)) {
                        if (d && d.error === 'auth_required') {
                            promptFileHostAuth(srcHost);
                        }
                        showNotice('copied ' + srcName + ', but could not remove '
                            + 'the source: ' + ((d && d.error) || '?'));
                        renderPane('left');
                        renderPane('right');
                        return false;
                    }
                }
                showNotice((move ? 'moved ' : 'copied ') + srcName + ' to "'
                    + hostPickerLabel(destHost) + '"');
                // Re-list both panes: the dest gained a file, a move's source
                // lost one. Cheap, and side-agnostic (either pane may be source).
                renderPane('left');
                renderPane('right');
                return true;
            };
            // ---- unified transfer dispatcher (#72) ----
            // ONE entry point for every copy/move: the two pane actions, Paste,
            // and a drop all route through here. src is a descriptor captured at
            // call time {hostId, path, name, type}. Routing:
            //   same host        -> server-side /file/copy|/file/move (handles
            //                       DIRECTORIES, no 5 MiB cap; exists -> styled
            //                       confirm -> retry overwrite)
            //   cross host + file-> the existing binary byte path (transferTo)
            //   cross host + dir -> a clear "not supported yet" notice
            // Returns true on success so a caller (Paste) can clear a cut
            // clipboard only when the move actually landed. destDir overrides the
            // target directory (Paste into a folder row); default = destSide's
            // current cwd. The destination HOST is always destSide's pane host.
            const doTransfer = async (destSide, src, move, destDir) => {
                if (!src || !src.path) return false;
                const srcHost = hostById(src.hostId)
                    || ((!src.hostId || src.hostId === 'local')
                        ? localHost() : null);
                const destHost = paneHost(destSide);
                if (!srcHost) {
                    showNotice('transfer failed: source host unavailable');
                    return false;
                }
                if (!destHost) {
                    showNotice('transfer failed: destination host unavailable');
                    return false;
                }
                const srcName = src.name || baseName(src.path);
                const targetDir = (destDir != null)
                    ? destDir : win[dirKey(destSide)];
                const destPath = joinNative(targetDir, srcName);
                // Self-overwrite guard: same host + same absolute path (a copy
                // would be a pointless self-overwrite; a self-move would delete
                // what it just wrote). The server re-checks ('same'), but this
                // is a cleaner message and saves a round trip.
                if (srcHost.id === destHost.id && destPath === src.path) {
                    showNotice('source and destination are the same');
                    return false;
                }
                if (srcHost.id === destHost.id) {
                    // Same host: server-side op (files AND dirs, no size cap).
                    const op = (overwrite) => move
                        ? fmFile().move(src.path, destPath,
                                        { host: srcHost.id, overwrite: overwrite })
                        : fmFile().copy(src.path, destPath,
                                        { host: srcHost.id, overwrite: overwrite });
                    let res = await op(false);
                    if (win.disposed) return false;
                    if (res && res.error === 'exists') {
                        const ok = await openConfirmDialog({
                            title: move ? 'Move' : 'Copy',
                            message: 'Overwrite ' + srcName + '?',
                            okLabel: 'Overwrite', danger: true });
                        if (!ok || win.disposed) return false;
                        res = await op(true);
                        if (win.disposed) return false;
                    }
                    if (!(res && res.ok)) {
                        const err = (res && res.error) || 'error';
                        if (err === 'auth_required') promptFileHostAuth(srcHost);
                        showNotice((move ? 'move' : 'copy') + ' failed ('
                            + srcName + '): ' + err);
                        return false;
                    }
                    showNotice((move ? 'moved ' : 'copied ') + srcName);
                    renderPane('left');
                    renderPane('right');
                    return true;
                }
                // Cross host. The single-broker server-side ops can't straddle
                // two hosts, and the byte path only carries a single file.
                if (src.type === 'dir') {
                    showNotice('cross-host folder copy isn’t supported yet');
                    return false;
                }
                return await transferTo(destSide, src.hostId, src.path,
                                        srcName, move);
            };
            const transferFromPayload = (payload, destSide, move) => {
                if (!payload || !payload.path) return;
                doTransfer(destSide, {
                    hostId: payload.hostId, path: payload.path,
                    name: payload.name || baseName(payload.path),
                    type: payload.type }, move);
            };

            // ---- single-item clipboard (#72) ----
            // win.fmClipboard = {mode:'cut'|'copy', hostId, path, name, type}.
            // Cut/Copy stash a descriptor; Paste routes it through doTransfer
            // into a chosen directory; a successful cut-paste clears the
            // clipboard (one-shot move), a copy keeps it (paste again).
            const setClipboard = (mode, desc) => {
                win.fmClipboard = { mode: mode, hostId: desc.hostId,
                                    path: desc.path, name: desc.name,
                                    type: desc.type };
                showNotice((mode === 'cut' ? 'cut ' : 'copied ') + desc.name);
            };
            const pasteInto = async (destSide, destDir) => {
                const clip = win.fmClipboard;
                if (!clip) return;
                const ok = await doTransfer(destSide, {
                    hostId: clip.hostId, path: clip.path,
                    name: clip.name, type: clip.type },
                    clip.mode === 'cut', destDir);
                if (ok && clip.mode === 'cut') win.fmClipboard = null;
            };

            // Client-side name check (UX only — the server re-validates). Rejects
            // both separators (/ and \), the ADS colon, '.'/'..', and empty.
            // Returns '' when OK (openTextPrompt treats a truthy return as the
            // error to show and keeps the dialog open).
            const validateName = (name) => {
                const n = (name || '').trim();
                if (!n) return 'enter a name';
                if (n === '.' || n === '..') return 'invalid name';
                if (/[\/\\]/.test(n)) return 'name can’t contain / or \\';
                if (n.indexOf(':') !== -1) return 'name can’t contain :';
                return '';
            };
            // New folder in a pane's cwd via a styled prompt -> /file/mkdir.
            const newFolder = async (side) => {
                const host = paneHost(side);
                if (!host) {
                    showNotice('new folder failed: host unavailable');
                    return;
                }
                const name = await openTextPrompt({
                    title: 'New folder', label: 'Folder name',
                    okLabel: 'Create', validate: validateName });
                if (name == null || win.disposed) return;
                const dst = joinNative(win[dirKey(side)], name.trim());
                const res = await fmFile().mkdir(dst, { host: host.id });
                if (win.disposed) return;
                if (!(res && res.ok)) {
                    if (res && res.error === 'auth_required') {
                        promptFileHostAuth(host);
                    }
                    const err = (res && res.error) || '?';
                    showNotice('new folder failed: '
                        + (err === 'exists' ? 'already exists' : err));
                    return;
                }
                showNotice('created ' + name.trim());
                renderPane(side);
            };
            // Properties: /file/stat -> a read-only styled info modal. Shared by
            // the row menu (a file/dir) and the empty menu (the cwd).
            const showProperties = async (side, path, displayName) => {
                const host = paneHost(side);
                if (!host) {
                    showNotice('properties failed: host unavailable');
                    return;
                }
                const r = await fmFile().stat(path, { host: host.id });
                if (win.disposed) return;
                if (!(r && r.ok)) {
                    if (r && r.error === 'auth_required') {
                        promptFileHostAuth(host);
                    }
                    showNotice('properties failed: ' + ((r && r.error) || '?'));
                    return;
                }
                const rows = [];
                rows.push({ k: 'Name',
                            v: displayName || baseName(r.path || path) });
                rows.push({ k: 'Path', v: r.path || path });
                rows.push({ k: 'Type', v: r.type === 'dir' ? 'Folder'
                            : (r.type === 'file' ? 'File' : r.type) });
                if (r.type === 'dir') {
                    if (typeof r.children === 'number') {
                        rows.push({ k: 'Items', v: String(r.children) });
                    }
                } else {
                    rows.push({ k: 'Size',
                                v: fmtSize(r.size) + ' (' + r.size + ' bytes)' });
                }
                if (r.mtime != null) {
                    rows.push({ k: 'Modified',
                                v: new Date(r.mtime * 1000).toLocaleString() });
                }
                if (typeof r.mode === 'number') {
                    rows.push({ k: 'Mode',
                                v: '0' + (r.mode & 0o7777).toString(8) });
                }
                await openInfoModal({ title: 'Properties', rows: rows });
            };

            // Right-click on a pane's empty background: New folder / Paste /
            // Refresh / Properties of the cwd. Lives in the outer scope (reads
            // win[dirKey(side)] live) so the one pane-level contextmenu handler
            // can call it.
            const buildEmptyMenu = (side) => {
                const cwd = win[dirKey(side)];
                const items = [];
                items.push({ label: 'New folder…', enabled: true,
                             action: () => newFolder(side) });
                if (win.fmClipboard) {
                    items.push({ label: 'Paste', enabled: true,
                                 action: () => pasteInto(side, cwd) });
                }
                items.push({ sep: true });
                items.push({ label: 'Refresh', enabled: true,
                             action: () => { renderPane('left');
                                             renderPane('right'); } });
                items.push({ label: 'Properties…', enabled: true,
                             action: () => showProperties(side, cwd,
                                                          baseName(cwd)) });
                return items;
            };

            for (const side of ['left', 'right']) {
                const ui = sideOf(side);
                // Activate + focus the FM root so the Tab/Enter shortcuts fire
                // (they're bound on fmBody, which needs focus to receive keys).
                const onClick = () => { setActive(side); fmBody.focus(); };
                const onOver = (e) => {
                    e.preventDefault();
                    // Shift = move, else copy — mirror it in the cursor (#72).
                    if (e.dataTransfer) {
                        e.dataTransfer.dropEffect = e.shiftKey ? 'move' : 'copy';
                    }
                    ui.pane.classList.add('drop-hover');
                };
                const onLeave = () => ui.pane.classList.remove('drop-hover');
                const onDrop = (e) => {
                    e.preventDefault();
                    ui.pane.classList.remove('drop-hover');
                    setActive(side);
                    const dt = e.dataTransfer;
                    // Precedence (#46 / codex): an OS-file drop ALWAYS uploads,
                    // no matter what other types ride along. Only an internal
                    // row drag (our MIME, which JSON-parses) is a cross-pane
                    // copy. A drop back onto the SOURCE pane is a no-op below.
                    if (dt && dt.files && dt.files.length) {
                        dropFiles(side, dt.files);
                        return;
                    }
                    let payload = null;
                    try {
                        payload = JSON.parse(
                            (dt && dt.getData(FM_DRAG_MIME)) || 'null');
                    } catch (_) {}
                    // A true self-drop is the SAME window AND the SAME pane;
                    // dropping into the other pane — or another FM window's pane
                    // of the same side name — is a real transfer (#46 review).
                    if (payload && payload.path
                        && !(payload.winId === id && payload.side === side)) {
                        // Shift-drop = move, plain drop = copy (#72). doTransfer
                        // routes by type: a cross-host dir gets a clear refusal.
                        transferFromPayload(payload, side, e.shiftKey);
                    }
                };
                // Right-click the pane's empty background -> the empty-area menu
                // (#72). Rows stopPropagation in onRowMenu, so this fires only on
                // the background; stopPropagation here keeps the desktop's own
                // context menu from overwriting it.
                const onPaneMenu = (e) => {
                    e.preventDefault();
                    e.stopPropagation();
                    setActive(side);
                    renderMenu(buildEmptyMenu(side), e.clientX, e.clientY);
                };
                ui.pane.addEventListener('mousedown', onClick);
                ui.pane.addEventListener('dragover', onOver);
                ui.pane.addEventListener('dragleave', onLeave);
                ui.pane.addEventListener('drop', onDrop);
                ui.pane.addEventListener('contextmenu', onPaneMenu);
                win.cleanups.push(() => {
                    ui.pane.removeEventListener('mousedown', onClick);
                    ui.pane.removeEventListener('dragover', onOver);
                    ui.pane.removeEventListener('dragleave', onLeave);
                    ui.pane.removeEventListener('drop', onDrop);
                    ui.pane.removeEventListener('contextmenu', onPaneMenu);
                });
                // Per-pane host picker (#46): the header button opens the host
                // menu under itself; choosing another host re-roots this pane.
                const hb = ui.hostBtn;
                if (hb) {
                    const onHostDown = (e) => e.stopPropagation();
                    const onHostClick = (e) => {
                        e.stopPropagation();
                        setActive(side);
                        const r = hb.getBoundingClientRect();
                        showHostPicker(win[hostKey(side)], r.left, r.bottom,
                                       (host) => switchPaneHost(side, host));
                    };
                    hb.addEventListener('mousedown', onHostDown);
                    hb.addEventListener('click', onHostClick);
                    win.cleanups.push(() => {
                        hb.removeEventListener('mousedown', onHostDown);
                        hb.removeEventListener('click', onHostClick);
                    });
                    updatePaneHostBtn(side);
                }
                // Per-pane folder picker / re-home (#46 follow-up): jump this
                // pane to a chosen folder on its own host.
                const fb = ui.folderBtn;
                if (fb) {
                    const onFolderDown = (e) => e.stopPropagation();
                    const onFolderClick = async (e) => {
                        e.stopPropagation();
                        setActive(side);
                        const host = paneHost(side);
                        if (!host) {
                            showNotice('this pane’s host was removed — '
                                + 'pick a host first');
                            return;
                        }
                        const picked = await openFileDialog({ mode: 'dir',
                            host, startDir: win[dirKey(side)] || '' });
                        if (!picked || win.disposed) return;
                        if (win[hostKey(side)] === host.id) {
                            win[dirKey(side)] = picked;
                            saveAppWindow(win);
                            renderPane(side);
                        }
                    };
                    fb.addEventListener('mousedown', onFolderDown);
                    fb.addEventListener('click', onFolderClick);
                    win.cleanups.push(() => {
                        fb.removeEventListener('mousedown', onFolderDown);
                        fb.removeEventListener('click', onFolderClick);
                    });
                }
            }

            // Tab toggles the active pane, scoped to this window: only when the
            // FM has focus and the event isn't headed for an input/button. Bound
            // on the FM body (focusable) so it never hijacks global Tab.
            const onBodyKey = (e) => {
                if (e.key !== 'Tab') return;
                const t = e.target;
                if (t && /^(INPUT|TEXTAREA|SELECT|BUTTON)$/.test(t.tagName)) return;
                if (t && t.isContentEditable) return;
                e.preventDefault();
                e.stopPropagation();
                setActive(activeSide === 'left' ? 'right' : 'left');
                fmBody.focus();
            };
            const onBodyEnter = (e) => {
                if (e.key !== 'Enter') return;
                const t = e.target;
                if (t && /^(INPUT|TEXTAREA|SELECT|BUTTON)$/.test(t.tagName)) return;
                e.preventDefault();
                openOrEnterActive();
            };
            fmBody.addEventListener('keydown', onBodyKey);
            fmBody.addEventListener('keydown', onBodyEnter);
            win.cleanups.push(() => {
                fmBody.removeEventListener('keydown', onBodyKey);
                fmBody.removeEventListener('keydown', onBodyEnter);
            });

            // ---- toolbar wiring ----
            const wireBtn = (btn, fn) => {
                const onClick = (e) => { e.stopPropagation(); fn(); };
                btn.addEventListener('mousedown', stopProp);
                btn.addEventListener('click', onClick);
                win.cleanups.push(() => {
                    btn.removeEventListener('mousedown', stopProp);
                    btn.removeEventListener('click', onClick);
                });
            };
            wireBtn(refreshBtn, () => { renderPane('left'); renderPane('right'); });
            wireBtn(openBtn, openOrEnterActive);
            wireBtn(upBtn, () => {
                // Parent of the active pane: drop the last path segment ('' = root).
                const cur = win[dirKey(activeSide)];
                if (!cur) return;
                const i = cur.lastIndexOf('/');
                win[dirKey(activeSide)] = i === -1 ? '' : cur.slice(0, i);
                saveAppWindow(win);
                renderPane(activeSide);
            });

            // Raise / minimize / close / drag / 8-way resize / WM context menu.
            // Issue #11: × discards the file manager (the Closed list keeps only
            // non-empty sticky notes); its nav state is not retained.
            wireAppChrome(win, chrome);

            // Manual taskbar item (app windows are never poll-managed), same as
            // openAppWindow: synthetic session keeps formatTitle happy +
            // updateTaskbarColor fixes the accent.
            const appSess = { key: id, sid: 'fm', id, title, stale: false,
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

            // ---- host lifecycle hooks (#46) ----
            // A removed host resets only the affected pane(s) to local (dir
            // cleared, since the old ABSOLUTE path belonged to the old host)
            // and re-lists — the FM is NEVER force-closed by host removal (it
            // can still browse its other, surviving pane). Returns true so
            // removeHost stops there for this window.
            win._hostRemoved = (rid) => {
                let touched = false;
                for (const s of ['left', 'right']) {
                    if (win[hostKey(s)] === rid) {
                        win[hostKey(s)] = 'local';
                        win[dirKey(s)] = '';
                        touched = true;
                    }
                }
                if (touched) {
                    saveAppWindow(win);
                    showNotice('host removed — pane reset to this broker');
                    renderPane('left');
                    renderPane('right');
                }
                return true;
            };
            // Re-list any pane on the host that just authenticated (the auth
            // form keys terminal-healing on win.hostId, which is 'app' here).
            win._onHostAuth = (hid) => {
                for (const s of ['left', 'right']) {
                    if (win[hostKey(s)] === hid) renderPane(s);
                }
            };

            saveAppWindow(win);
            setActive('left');
            renderPane('left');
            renderPane('right');
            if (findKeyInLayout(id)) placeWindowTiled(win);
            else bringToFront(id);
            return win;
        }

        // The (+) launcher — moved verbatim from core 76_js_launch_fullscreen.js.
        // Both panes start at the Control Panel Default start path when set (#73),
        // else the active terminal's cwd, on its host (#35) — and on its host PER
        // PANE (#46), so each pane can be re-homed later.
        async function launchFileManager() {
            const s = activeTerminalStart();
            let startDir = s.cwd;                 // fallback = today's behavior
            try {
                // Mirror fileHost()'s host resolution: hostById, then explicit
                // 'local' fallback; a removed remote stays null so we don't resolve
                // a LOCAL startPath for a remote-targeted pane (Codex review).
                let h = hostById(s.host);
                if (!h && (!s.host || s.host === 'local')) h = localHost();
                // #73: Control Panel Default start path wins when set; else the
                // active terminal cwd / broker default, exactly as before.
                if (h) startDir = (await resolveStartPath(h)) || s.cwd;
            } catch (_) { startDir = s.cwd; }
            openAppWindow({ id: newAppId('fm'), appKind: 'file-manager',
                            fmLeft: startDir, fmRight: startDir, fileHostId: s.host,
                            fmLeftHostId: s.host, fmRightHostId: s.host });
        }

        // ---- mod registration: the file-manager window kind ----------------
        registerMod({
            id: 'file-manager',
            version: '1.0.0',
            ctxVersion: 1,
            tiers: ['file', 'window'],   // #86: host filesystem incl. destructive ops (ctx.file) + a window kind
            init: function (ctx) {
                // Route every file-manager /file/* op (incl. the DESTRUCTIVE
                // delete + upload) through the reviewed ctx.file capability (#82);
                // cleared on teardown so a disabled file-manager mod falls back to
                // the hoisted _modFileApi (see fmFile()).
                fmFile.cap = ctx.file;
                ctx.onUnload(function () { fmFile.cap = null; });
                // Register the file-manager kind (the #80 built-in spec, moved
                // here). serialize stays the shared core serializeAppWindow so
                // webterm:appwindows:v1 persistence is byte-identical; a duplicate
                // appKind throws -> initMod rolls the mod back; teardown removes
                // exactly THIS registration.
                ctx.registerWindowKind({
                    appKind: 'file-manager',
                    factory: function (d) { return openFileManagerWindow(d); },
                    serialize: serializeAppWindow,
                    menu: {
                        label: '🗂 File manager',
                        launch: function () { return launchFileManager(); },
                    },
                });
            },
        });
