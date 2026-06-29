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
        // rides in the query via hostHttpUrl, mirroring /launch. `host`
        // defaults to the LOCAL broker so every existing caller is unchanged;
        // AGENTS.md editors on a remote terminal pass that terminal's host so
        // the read/write hits the broker the file actually lives on.
        // Always resolves to a parsed object — {ok:false,error} on any
        // transport/HTTP failure so callers never see a rejected promise.
        function fileApiPost(path, body, host) {
            return fetch(hostHttpUrl(host || localHost(), path), {
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
                // 'dir' mode tracks the absolute root so commit can join it with
                // the current relative cwd into an absolute path /launch accepts.
                const state = { cwd: '', selected: null, root: '' };
                const showErr = (m) => {
                    errEl.textContent = m; errEl.classList.add('show');
                };
                const finish = (val) => {
                    if (_fileDlgFinish === finish) _fileDlgFinish = null;
                    overlay.classList.remove('open');
                    okBtn.onclick = null; cancelBtn.onclick = null;
                    document.removeEventListener('keydown', onKey, true);
                    resolve(val);
                };
                _fileDlgFinish = finish;
                const commit = () => {
                    if (mode === 'save') {
                        const name = (nameEl.value || '').trim();
                        if (!name) { showErr('enter a filename'); return; }
                        // cwd is ABSOLUTE now (#35); join the typed name onto it.
                        finish(state.cwd ? joinNative(state.cwd, name) : name);
                    } else if (mode === 'dir') {
                        // The shown folder's cwd IS the absolute path to return
                        // (host-wide #35 — no root+relative reconstruction).
                        if (!state.cwd) { showErr('folder not loaded'); return; }
                        finish(state.cwd);
                    } else {
                        if (!state.selected) { showErr('select a file'); return; }
                        finish(state.selected);
                    }
                };
                const render = (data) => {
                    state.cwd = data.cwd || '';        // absolute path (#35)
                    state.root = data.root || '';      // FS anchor (informational)
                    state.selected = null;
                    pathEl.textContent = state.cwd || state.root || '';
                    listEl.innerHTML = '';
                    if (data.parent !== null && data.parent !== undefined) {
                        const up = document.createElement('div');
                        up.className = 'file-entry';
                        up.innerHTML = '<span class="fe-icon">📁</span>'
                            + '<span class="fe-name">..</span>'
                            + '<span class="fe-size"></span>';
                        up.addEventListener('click', () => navigate(data.parent));
                        listEl.appendChild(up);
                    }
                    for (const ent of (data.entries || [])) {
                        const row = document.createElement('div');
                        row.className = 'file-entry';
                        const icon = ent.type === 'dir' ? '📁' : '📄';
                        row.innerHTML = '<span class="fe-icon">' + icon
                            + '</span><span class="fe-name"></span>'
                            + '<span class="fe-size">'
                            + (ent.type === 'dir' ? '' : fmtSize(ent.size))
                            + '</span>';
                        row.querySelector('.fe-name').textContent = ent.name;
                        const child = joinNative(state.cwd, ent.name);
                        if (ent.type === 'dir') {
                            row.addEventListener('click', () => navigate(child));
                        } else if (mode === 'dir') {
                            // Folder picker: files are greyed + inert (you can
                            // only navigate folders and commit the shown one).
                            row.classList.add('disabled');
                            row.style.opacity = '0.4';
                            row.style.cursor = 'default';
                        } else {
                            row.addEventListener('click', () => {
                                listEl.querySelectorAll('.file-entry.sel')
                                    .forEach(e => e.classList.remove('sel'));
                                row.classList.add('sel');
                                state.selected = child;
                                if (mode === 'save') nameEl.value = ent.name;
                            });
                            row.addEventListener('dblclick', () => {
                                state.selected = child; commit();
                            });
                        }
                        listEl.appendChild(row);
                    }
                };
                const navigate = async (path) => {
                    errEl.classList.remove('show');
                    const data = await fileApiPost('/file/list',
                        { path: path || '' }, dlgHost);
                    if (!data || !data.ok) {
                        showErr('list failed: ' + ((data && data.error) || '?'));
                        return;
                    }
                    render(data);
                };
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
                navigate(opts.startDir || '');
            });
        }

