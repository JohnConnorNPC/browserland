        // ---- client-only app windows (sticky note / text editor) ----------
        // A non-terminal window: reuses the .term-window chrome (drag, resize,
        // minimize, close, taskbar) but has a <textarea> body and NO terminal /
        // WebSocket. hostId:'app' keeps it out of every per-host loop
        // (visibility masking, auth-heal reattach, poll reaping). It can float
        // (pinned by default) OR tile like a terminal; content/geom/color/lock
        // persist in `appStore`, tiling membership in prefs._layout.
        //
        // A fresh sticky note opens in the classic yellow (its default --accent).
        const NOTE_DEFAULT_ACCENT = '#f4d35e';
        // Legacy hand-tuned paper/fg for the five original note accents (#29).
        // Kept ONLY so existing saved notes restore with their exact original
        // look; every other accent (the unified 11-color palette + custom +
        // recent colors) derives paper/fg via deriveNoteColors.
        const LEGACY_NOTE_SWATCHES = [
            { accent: '#f4d35e', paper: '#fff7ae', fg: '#3a3000' },  // yellow
            { accent: '#f48fb1', paper: '#ffd9e6', fg: '#4a0d24' },  // pink
            { accent: '#9ccc9c', paper: '#d7f3d8', fg: '#173d1c' },  // green
            { accent: '#90caf9', paper: '#d6ebff', fg: '#0d2a4a' },  // blue
            { accent: '#ffb74d', paper: '#ffe6c2', fg: '#4a2c00' },  // orange
        ];
        // Recover {accent, paper, fg} from a saved/picked accent: the exact
        // legacy triple for one of the five originals, else derived — so a
        // restore (or a custom/palette pick) never lands on a blank or yellow
        // paper for an unknown color.
        function noteSwatchFor(accent) {
            const a = normalizeHex(accent);
            for (const s of LEGACY_NOTE_SWATCHES) {
                if (normalizeHex(s.accent) === a) return s;
            }
            return deriveNoteColors(a);
        }
        function appDefaultGeom(kind) {
            const desktop = document.getElementById('desktop');
            const dw = desktop.clientWidth || 1024;
            const dh = desktop.clientHeight || 700;
            const w = Math.min(kind === 'sticky-note' ? 300 : 600, Math.max(MIN_W, dw - 40));
            const h = Math.min(kind === 'sticky-note' ? 240 : 440, Math.max(MIN_H, dh - 40));
            const slackX = Math.max(1, dw - w - 60);
            const slackY = Math.max(1, dh - h - 60);
            const left = 40 + (cascadeIndex * CASCADE_DX) % slackX;
            const top = 30 + (cascadeIndex * CASCADE_DY) % slackY;
            cascadeIndex++;
            return { left, top, width: w, height: h };
        }

        // ---- server file Open/Save (editor) ------------------------------
        // POST a JSON body to a /file/* route on a broker; the token (if any)
        // rides an Authorization header via hostFetch (#144). `host`
        // defaults to the LOCAL broker so every existing caller is unchanged;
        // AGENTS.md editors on a remote terminal pass that terminal's host so
        // the read/write hits the broker the file actually lives on.
        // Always resolves to a parsed object — {ok:false,error} on any
        // transport/HTTP failure so callers never see a rejected promise.
        function fileApiPost(path, body, host) {
            return hostFetch(host || localHost(), path, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(body || {}),
            }).then(r => r.json().catch(
                () => ({ ok: false, error: 'HTTP ' + r.status })
            )).catch(e => ({ ok: false, error: String(e) }));
        }
        // Host-wide file tools (#35) work in ABSOLUTE native paths: the server
        // returns the host's own separators (C:\a\b on Windows, /a/b on POSIX).
        // The file API only ever deals in ABSOLUTE paths, so a leading
        // drive-letter (C:\) or UNC (\\srv) prefix unambiguously marks Windows —
        // we must NOT sniff for any stray backslash, because '\' is a legal
        // filename char on POSIX (a dir named `we\b` would otherwise corrupt
        // joins + basenames; #35 review).
        function pathSepOf(p) {
            return /^([A-Za-z]:[\\/]|\\\\)/.test(p || '') ? '\\' : '/';
        }
        function joinNative(dir, name) {
            if (!dir) return name;                 // empty -> server default dir
            const sep = pathSepOf(dir);
            return dir.charAt(dir.length - 1) === sep
                ? dir + name : dir + sep + name;   // don't double a trailing sep
        }
        function baseName(p) {
            if (!p) return '';
            const i = p.lastIndexOf(pathSepOf(p));   // sep-aware (not both seps)
            return i === -1 ? p : p.slice(i + 1);
        }
        // The cwd + host ID of the most-recently-active terminal, so the file
        // tools open "where I am" (#35). `host` is a host ID (not an object) to
        // match win.fileHostId. Empty -> broker default dir + local host.
        function activeTerminalStart() {
            const t = lastTermId && sessions.get(lastTermId);
            if (t && t.cwd) {
                return { cwd: String(t.cwd), host: t.hostId || homeHostId() };
            }
            return { cwd: '', host: homeHostId() };
        }
        function fmtSize(n) {
            if (!(n > 0)) return '';
            if (n < 1024) return n + ' B';
            if (n < 1024 * 1024) return (n / 1024).toFixed(1) + ' K';
            return (n / (1024 * 1024)).toFixed(1) + ' M';
        }
        // Reusable host-wide file-browser dialog (#35). Resolves to a chosen
        // ABSOLUTE path (the host's own separators), or null on cancel. mode:
        // 'open' selects an existing file; 'save' navigates dirs and types a
        // filename; 'dir' picks a folder. Reuses the static #file-overlay panel.
        // The overlay is a singleton, so a second open() while one is live
        // would orphan the first promise + its keydown listener — _fileDlgFinish
        // lets a new call cleanly cancel (resolve null) the previous one first.
        let _fileDlgFinish = null;
        function openFileDialog(opts) {
            if (_fileDlgFinish) _fileDlgFinish(null);   // cancel any live dialog
            opts = opts || {};
            // Which broker this dialog browses. Defaults to local so the
            // generic editor + file manager are unchanged; an AGENTS.md editor
            // on a remote terminal passes opts.host so Open/Save-As browse the
            // same broker the file lives on.
            const dlgHost = opts.host || localHost();
            const mode = opts.mode === 'save' ? 'save'
                : (opts.mode === 'dir' ? 'dir' : 'open');
            const overlay = document.getElementById('file-overlay');
            const titleEl = document.getElementById('file-title');
            const pathEl = document.getElementById('file-path');
            const listEl = document.getElementById('file-list');
            const nameRow = document.getElementById('file-name-row');
            const nameEl = document.getElementById('file-name');
            const errEl = document.getElementById('file-err');
            const okBtn = document.getElementById('file-ok');
            const cancelBtn = document.getElementById('file-cancel');
            titleEl.textContent = mode === 'save' ? 'Save file as'
                : (mode === 'dir' ? 'Choose a folder' : 'Open file');
            okBtn.textContent = mode === 'save' ? 'Save'
                : (mode === 'dir' ? 'Use this folder' : 'Open');
            // 'dir' mode: no filename row (you commit the shown folder itself).
            nameRow.style.display = mode === 'save' ? 'flex' : 'none';
            nameEl.value = opts.suggestName || '';
            errEl.classList.remove('show');
            errEl.textContent = '';
            return new Promise((resolve) => {
                const showErr = (m) => {
                    errEl.textContent = m; errEl.classList.add('show');
                };
                const finish = (val) => {
                    if (_fileDlgFinish === finish) _fileDlgFinish = null;
                    overlay.classList.remove('open');
                    okBtn.onclick = null; cancelBtn.onclick = null;
                    document.removeEventListener('keydown', onKey, true);
                    // Flip the component's disposed flag synchronously so a
                    // superseded in-flight list can't paint the shared
                    // #file-list after we've closed (codex #9).
                    pane.destroy();
                    resolve(val);
                };
                _fileDlgFinish = finish;
                const commit = () => {
                    if (mode === 'save') {
                        const name = (nameEl.value || '').trim();
                        if (!name) { showErr('enter a filename'); return; }
                        // cwd is ABSOLUTE now (#35); join the typed name onto it.
                        const cwd = pane.getCwd();
                        finish(cwd ? joinNative(cwd, name) : name);
                    } else if (mode === 'dir') {
                        // The shown folder's cwd IS the absolute path to return
                        // (host-wide #35 — no root+relative reconstruction).
                        const cwd = pane.getCwd();
                        if (!cwd) { showErr('folder not loaded'); return; }
                        finish(cwd);
                    } else {
                        const sel = pane.getSelected();
                        if (!sel) { showErr('select a file'); return; }
                        finish(sel);
                    }
                };
                // The shared browse kernel (#93). The dialog is host-aware via a
                // closure over dlgHost, so snapshot/isCurrent are trivial — the
                // component's own seq + disposed flag suffice (one #file-overlay
                // singleton, no per-pane host/dir drift to validate).
                const pane = createBrowsePane({
                    listEl: listEl,
                    classes: { row: 'file-entry', icon: 'fe-icon',
                               name: 'fe-name', size: 'fe-size' },
                    filesInteractive: mode !== 'dir',
                    dirActivateOn: 'single',
                    listDir: (p) => {
                        errEl.classList.remove('show');
                        return fileApiPost('/file/list',
                            { path: p || '' }, dlgHost);
                    },
                    snapshot: () => ({}),
                    isCurrent: () => true,
                    onSelect: (entry) => {
                        if (mode === 'save' && entry) nameEl.value = entry.name;
                    },
                    onActivateFile: (child, entry) => {
                        if (mode === 'save') {
                            nameEl.value = entry.name; commit();
                        } else {
                            finish(child);   // 'open' (dir mode never reaches)
                        }
                    },
                    onDirChanged: (cwd, res) => {
                        pathEl.textContent = cwd || res.root || '';
                    },
                    onListError: (res) => {
                        showErr('list failed: ' + ((res && res.error) || '?'));
                    },
                });
                const onKey = (e) => {
                    if (e.key === 'Escape') {
                        e.preventDefault(); e.stopPropagation(); finish(null);
                    } else if (e.key === 'Enter' && mode === 'save') {
                        e.preventDefault(); commit();
                    }
                };
                okBtn.onclick = commit;
                cancelBtn.onclick = () => finish(null);
                document.addEventListener('keydown', onKey, true);
                overlay.classList.add('open');
                pane.navigate(opts.startDir || '');
            });
        }

